"""One read: atoms + retry ladder + a ledger that accounts for every character.

The ledger is the source of truth for "did it read everything?".  Playback is
a dumb PCM consumer; this module decides retries, records PARTIAL/SKIPPED with
exact offsets, and chooses the end state.
"""
import array
import asyncio
import threading
import time

from .atoms import clean, split_atoms
from .cache import cache_key
from .paths import log
from .player import SAMPLE_RATE, AudioPlayer

SPOKEN, PARTIAL, SKIPPED, CANCELLED = "SPOKEN", "PARTIAL", "SKIPPED", "CANCELLED"
SENTENCE_CHARS_PER_SEC = 13.0
# Microsoft's first audio packet took 1.8-3.5 s in measurement; a 1.2 s
# window made every atom after the first fail over to the offline voice.
FIRST_PACKET = 4.0
# A slow server can pause between packets without being dead.  Cutting at
# 2.5 s was audible mid-sentence on a loaded connection; 4 s still recovers.
PACKET_GAP = 4.0
# Look one more atom ahead than the old design: synthesis is slower than
# playback per sentence, so a single lookahead left audible gaps.
LOOKAHEAD = 2
# A 48-character atom needs ~3.7 s of audio; 8 s cut sentences that were still
# arriving.  The first-packet and packet-gap limits catch real stalls sooner.
ATOM_MIN_SECONDS = 20.0
ATOM_TIMEOUT_SCALE = 1.0
DEADLINE_MIN_SECONDS = 20.0
DEADLINE_SCALE = 2.5
MAX_CONNECTIONS = 4
CIRCUIT_SECONDS = 60.0
CIRCUIT_AFTER = 3


class DeviceLost(Exception):
    pass


class ByteSource:
    """Blocking byte stream for miniaudio's MP3 decoder."""

    def __init__(self):
        self.q = []
        self.cv = threading.Condition()
        self.buf = b""
        self.closed = False

    def feed(self, data):
        with self.cv:
            self.q.append(data)
            self.cv.notify_all()

    def end(self):
        with self.cv:
            self.closed = True
            self.cv.notify_all()

    def read(self, n):
        if not self.buf:
            with self.cv:
                while not self.q and not self.closed:
                    self.cv.wait()
                if not self.q:
                    return b""
                self.buf = self.q.pop(0)
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class Decoder:
    """MP3 bytes -> PCM arrays on a background thread; cancellable."""

    def __init__(self, token, emit):
        self.token = token
        self.emit = emit
        self.src = ByteSource()
        self.cancelled = False
        self.error = None
        self.done = threading.Event()
        threading.Thread(target=self._run, name="decoder", daemon=True).start()

    def feed(self, data):
        if not self.cancelled:
            self.src.feed(data)

    def end(self):
        self.src.end()

    def cancel(self):
        self.cancelled = True
        self.src.end()

    def _run(self):
        try:
            import miniaudio

            class Source(miniaudio.StreamableSource):
                def read(inner, n):
                    return self.src.read(n)

            for frames in miniaudio.stream_any(
                    Source(), miniaudio.FileFormat.MP3, nchannels=1,
                    sample_rate=SAMPLE_RATE, frames_to_read=240):
                if self.cancelled:
                    return
                self.emit(frames)
        except Exception as e:                       # corrupt stream
            if not self.cancelled:
                self.error = f"{type(e).__name__}: {e}"[:80]
        finally:
            self.done.set()


class AtomSink:
    """Where one attempt of one atom puts its audio.

    MP3 is decoded the moment it arrives, so a corrupt atom is known bad
    before it can be marked SPOKEN.  Frames for the atom being read go
    straight to the player (first sound as the first packet decodes); frames
    for the one-atom lookahead wait here until that atom reaches the head, so
    a retried atom can never inject audio ahead of the atom being played.
    """

    def __init__(self, session, index, token):
        self.session = session
        self.index = index
        self.token = token
        self.chunks = []          # ("mp3", bytes) | ("pcm", array) for the cache
        self.pcm = []             # decoded frames held until this atom is head
        self.frames = []          # every frame decoded by this attempt (RAM cache)
        self.direct = False
        self.decoder = None
        self.discarded = False
        self._lock = threading.Lock()
        self._ended = False
        self._finish_error = None

    def write_mp3(self, data):
        if not data or self.discarded:
            return
        self.chunks.append(("mp3", data))
        if self.decoder is None:
            self.decoder = Decoder(self.token, self._frames)
        self.decoder.feed(data)

    def write_pcm(self, pcm):
        if not pcm or self.discarded:
            return
        self.chunks.append(("pcm", pcm))
        self._frames(pcm)

    def _frames(self, frames):
        if not frames:
            return
        with self._lock:
            if self.discarded:
                return
            self.frames.append(frames)
            if self.direct or self.session.head == self.index:
                self.session.player.add(self.token, frames)
            else:
                self.pcm.append(frames)

    def flush(self):
        """This atom is now at the head: release anything it was holding."""
        with self._lock:
            if self.discarded:
                return
            self.direct = True
            pending, self.pcm = self.pcm, []
            for frames in pending:
                self.session.player.add(self.token, frames)

    async def finish_stream(self):
        """End decoding and wait for the remaining frames; returns an error."""
        if self.discarded:
            return self._finish_error
        if self.decoder is not None and not self._ended:
            self._ended = True
            self.decoder.end()
            if not self.decoder.done.is_set():
                await asyncio.to_thread(self.decoder.done.wait, 3.0)
            if self.decoder.error:
                self._finish_error = self.decoder.error
            elif not self.decoder.done.is_set():
                self._finish_error = "decoder stalled"
        return self._finish_error

    def discard(self):
        with self._lock:
            self.discarded = True
            if self.decoder is not None:
                self.decoder.cancel()
            self.chunks.clear()
            self.pcm.clear()

    def payload(self):
        if any(kind == "pcm" for kind, _ in self.chunks):
            return "pcm", [data for kind, data in self.chunks if kind == "pcm"]
        return "mp3", b"".join(data for _kind, data in self.chunks)


class Ledger:
    def __init__(self, text, atoms):
        self.text = text
        self.rows = [{"i": i, "start": a.start, "end": a.end,
                      "status": None, "reason": ""} for i, a in enumerate(atoms)]

    def mark(self, i, status, reason=""):
        row = self.rows[i]
        # A decode failure discovered after the atom was provisionally marked
        # SPOKEN must still be able to downgrade it; silence is never hidden.
        if row["status"] is None or (row["status"] == SPOKEN and status != SPOKEN):
            row["status"] = status
        if reason:
            row["reason"] = reason

    def summary(self):
        def count(status):
            return sum(1 for r in self.rows if r["status"] == status)

        def chars(status):
            return sum(r["end"] - r["start"] for r in self.rows if r["status"] == status)

        return {
            "atoms": len(self.rows),
            "spoken": count(SPOKEN),
            "partial": count(PARTIAL),
            "skipped": count(SKIPPED),
            "cancelled": count(CANCELLED),
            "total_chars": len(self.text),
            "spoken_chars": chars(SPOKEN),
            "partial_chars": chars(PARTIAL),
            "skipped_chars": chars(SKIPPED),
            "cancelled_chars": chars(CANCELLED),
            "complete": count(SPOKEN) == len(self.rows) and bool(self.rows),
            "offsets": [{"status": r["status"], "start": r["start"], "end": r["end"],
                         "reason": r["reason"]}
                        for r in self.rows if r["status"] in (PARTIAL, SKIPPED)],
        }


class Session:
    """Runs one read to a terminal ledger and one visible end state."""

    def __init__(self, engine, text, gen, voice, rate, offline_only, on_state,
                 player_factory=AudioPlayer):
        self.engine = engine
        self.gen = gen
        self.voice = voice
        self.rate = rate
        self.offline_only = offline_only
        self.on_state = on_state
        self.player_factory = player_factory
        self.text = clean(text)
        self.atoms = split_atoms(self.text)
        self.ledger = Ledger(self.text, self.atoms)
        self.player = None
        self.head = 0
        self.next_i = 0
        self.sinks = {}
        self.tasks = {}
        self.attempt = 0
        self.requests = 0
        self.failure_logged = set()
        self.first_ms = None
        self.t0 = time.perf_counter()
        self.finished = asyncio.Event()
        self.finished_reason = None
        self.end_state = None
        self.timed_out = False
        self.device_lost = False
        self.offline_missing = False
        self.cancel_why = ""
        self.trace = []          # small in-memory trail for the failure log

    def _trace(self, what):
        self.trace.append(what)
        del self.trace[:-40]

    # ------------------------------------------------------------ events
    def _emit(self, state, detail=""):
        try:
            self.on_state(state, detail, self.gen)
        except Exception as e:
            log(f"state callback: {type(e).__name__}")

    def _first_sound(self):
        self.engine.loop.call_soon_threadsafe(self._first_sound_on_loop)

    def _first_sound_on_loop(self):
        if self.first_ms is None:
            self.first_ms = (time.perf_counter() - self.t0) * 1000
            self._emit("speaking")

    def _player_finished(self, reason):
        self.engine.loop.call_soon_threadsafe(self._player_finished_on_loop, reason)

    def _player_finished_on_loop(self, reason):
        if not self.finished.is_set():
            self.finished_reason = reason
            self.finished.set()

    def _stop_emitting(self):
        return self.engine.gen != self.gen

    def _cancelled(self):
        if self._stop_emitting():
            self.cancel_why = self.cancel_why or "generation"
            return True
        if self.player and self.player.stopped:
            self.cancel_why = self.cancel_why or "player stopped"
            return True
        return False

    def _final(self, state, detail=""):
        self.end_state = state
        self._emit(state, detail)

    # ------------------------------------------------------------ run
    async def run(self):
        try:
            if not self.atoms:
                self._final("idle")
                return
            if self.offline_only and not (self.engine.local and self.engine.local.ready):
                self.offline_missing = True
                self._final("offline_missing", "download")
                return
            self._emit("loading")
            self.player = self.player_factory(self._first_sound, self._player_finished,
                                              fast_start=self._all_cached())
            opened = await asyncio.to_thread(self.player.open, 3.0)
            if not opened:
                self._final("error", "audio device unavailable")
                return
            if not self.offline_only:
                self.engine.begin_read()
            try:
                await asyncio.wait_for(self._run_atoms(), timeout=self._hard_deadline())
            except asyncio.TimeoutError:
                self.timed_out = True
            self._trace(f"atoms returned timed_out={int(self.timed_out)}")
            if self.timed_out:
                await asyncio.to_thread(self.player.stop, 3.0)
            elif self._cancelled():
                await asyncio.to_thread(self.player.stop, 3.0)
            else:
                await self._flush_all()
                self._trace(f"flushed queue={self.player.queued_seconds():.1f}")
                self.player.end()
                await self._wait_for_playback()
                self._trace(f"playback reason={self.finished_reason}")
            self._finish_state()
        except asyncio.CancelledError:
            raise
        except DeviceLost:
            self.finished_reason = self.finished_reason or "device lost"
            self.device_lost = True
            self._finish_state()
        except Exception as e:                        # never strand a read
            log(f"read failed: {type(e).__name__}: {e}"[:200])
            self._final("error", type(e).__name__)
        finally:
            for task in list(self.tasks.values()):
                task.cancel()                  # stop never waits for synthesis
            for sink in list(self.sinks.values()):
                sink.discard()
            if self.player is not None:
                await asyncio.to_thread(self.player.stop, 3.0)
            for row in self.ledger.rows:
                if row["status"] is None:
                    row["status"] = CANCELLED
            summary = self.ledger.summary()
            summary["end"] = self.end_state or ("idle" if self.ledger.summary()["complete"]
                                                else "cancelled")
            summary["warning"] = self._warning() if self.end_state == "idle" else ""
            summary["first_ms"] = round(self.first_ms, 1) if self.first_ms is not None else None
            summary["requests"] = self.requests
            summary["gen"] = self.gen
            summary["frames"] = self.player.frames_played if self.player is not None else 0
            self.engine.last_ledger = summary
            self._log_report(summary)

    async def _wait_for_playback(self):
        remaining = max(1.0, self._hard_deadline() - (time.perf_counter() - self.t0))
        if self.ledger.summary()["complete"]:
            # A slow first packet can use most of the hard deadline before any
            # sound starts.  Once every atom is decoded, playback gets its own
            # window: otherwise a complete read is reported as a timeout at the
            # exact moment the last sentence finishes.
            remaining = max(remaining, self.player.queued_seconds() + 10.0)
        try:
            await asyncio.wait_for(self.finished.wait(), remaining)
        except asyncio.TimeoutError:
            self.timed_out = True
            await asyncio.to_thread(self.player.stop, 3.0)

    def _hard_deadline(self):
        chars = sum(len(a.text) for a in self.atoms)
        speed = SENTENCE_CHARS_PER_SEC * (1 + _rate_percent(self.rate) / 100.0)
        return max(DEADLINE_MIN_SECONDS,
                   DEADLINE_SCALE * (chars / max(4.0, speed)) + DEADLINE_MIN_SECONDS)

    # ------------------------------------------------------------ atoms
    async def _run_atoms(self):
        n = len(self.atoms)
        self._trace(f"run n={n}")
        for i in range(n):
            if self._cancelled():
                return
            await self._ensure_tasks(i)
            task = self.tasks[i]
            await self._await_atom(i, task)
            self.head = i + 1
            self._trace(f"head{i + 1}")
            sink = self.sinks.get(i + 1)
            if sink is not None:
                sink.flush()
        for i in range(n):
            if self._stop_emitting():
                return
            sink = self.sinks.get(i)
            if sink is not None and not sink.discarded:
                sink.flush()
        self._trace("atoms done")

    async def _ensure_tasks(self, i):
        while self.next_i < len(self.atoms) and self.next_i <= i + LOOKAHEAD:
            if self.next_i > i:
                while self.player.queued_seconds() > 60:
                    await asyncio.sleep(0.2)
            index = self.next_i
            self.tasks[index] = asyncio.create_task(self._synth_atom(index))
            self.next_i += 1

    async def _await_atom(self, i, task):
        waiter = asyncio.create_task(self.finished.wait())
        self._trace(f"wait{i} in done={int(task.done())} fin={int(self.finished.is_set())}")
        try:
            done, _pending = await asyncio.wait({task, waiter},
                                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        self._trace(f"wait{i} out task={int(task in done)} "
                    f"waiter={int(waiter in done)} reason={self.finished_reason}")
        if self.finished_reason == "device lost":
            task.cancel()
            raise DeviceLost()
        if task not in done:
            self._trace(f"wait{i} await task")
            await task
        elif task.cancelled():
            self._trace(f"wait{i} task cancelled")
            if self._stop_emitting() or (self.player is not None
                                         and self.player.stopped):
                raise asyncio.CancelledError()
            self._atom_stopped(i)
        elif task.exception() is not None:
            await task                      # surface the real failure
        self._trace(f"wait{i} exit")

    def _atom_stopped(self, i):
        """A synthesis task died without the read being stopped: say so."""
        sink = self.sinks.get(i)
        if self.player.played(i) > 0:
            self.ledger.mark(i, PARTIAL, "synthesis stopped")
            return
        if sink is not None:
            sink.discard()
            self.player.drop(sink.token)
        self.ledger.mark(i, SKIPPED, "synthesis stopped")
        log(f"read: atom {i} synthesis stopped without a result")

    async def _flush_all(self):
        for i in range(len(self.atoms)):
            sink = self.sinks.get(i)
            if sink is None or sink.discarded:
                continue
            sink.flush()
            err = await sink.finish_stream()
            if err:
                self._late_decode_error(i, sink, err)

    def _late_decode_error(self, i, sink, err):
        if self.player.played(i) > 0:
            self.ledger.mark(i, PARTIAL, "decode error")
        else:
            self.player.drop(sink.token)
            self.ledger.mark(i, SKIPPED, "decode error")
        log(f"read: decode error atom {i} ({err})")

    async def _try_cached(self, i, cached, key):
        """Play an atom from the cache; returns (spoken, error)."""
        sink = self._new_sink(i)
        fmt, payload = cached
        if fmt == "pcm":
            for a in payload:
                sink.write_pcm(a)
        else:
            sink.write_mp3(payload)
        err = await sink.finish_stream()
        if err is None and self._cancelled():
            sink.discard()
            self.player.drop(sink.token)
            return False, "cancelled"
        if err is None:
            if fmt != "pcm" and sink.frames:
                # A disk hit that had to decode is now a RAM hit: the next
                # re-read of this sentence skips MP3 decoding entirely.
                self.engine.cache.put_ram(key, "pcm", list(sink.frames))
            self.ledger.mark(i, SPOKEN)
            return True, ""
        sink.discard()
        self.player.drop(sink.token)
        return False, err

    def _all_cached(self):
        """True when every atom can start from PCM already in RAM."""
        if not self.atoms or self.engine.cache is None:
            return False
        cache = self.engine.cache
        return all(cache.pcm_cached(self._key(atom.text, self.offline_only))
                   for atom in self.atoms)

    async def _synth_atom(self, i):
        cancelled = False
        self._trace(f"synth{i} start")
        try:
            return await self._synth_atom_body(i)
        except asyncio.CancelledError:
            cancelled = True
            raise
        finally:
            self._trace(f"synth{i} end cancelled={int(cancelled)} "
                        f"status={self.ledger.rows[i]['status']}")
            # If this ever fires, the ledger would otherwise show a clean end
            # while a sentence was never played: exactly the silent stop this
            # ledger exists to make impossible.
            if (not cancelled and self.ledger.rows[i]["status"] is None
                    and not self._cancelled()):
                log(f"read: synth atom {i} ended unmarked "
                    f"(head={self.head} next={self.next_i} "
                    f"gen={self.engine.gen}/{self.gen})")

    async def _synth_atom_body(self, i):
        atom = self.atoms[i]
        last_reason = "no voice available"
        # A repeat must not pay for a network ladder just because a circuit is
        # cooling down: cached audio for this voice always wins first.
        key = self._key(atom.text, self.offline_only)
        cached = self.engine.cache.get(key)
        if cached is not None:
            spoken, reason = await self._try_cached(i, cached, key)
            if spoken:
                return True
            if self._cancelled():
                return False
            last_reason = reason
        for rung in self._rungs():
            if self._cancelled():
                return False
            if rung == "offline":
                offline_key = self._key(atom.text, True)
                cached = self.engine.cache.get(offline_key)
                if cached is not None:
                    spoken, reason = await self._try_cached(i, cached, offline_key)
                    if spoken:
                        return True
                    if self._cancelled():
                        return False
                    last_reason = reason
            attempts = 1
            key = self._key(atom.text, rung == "offline")
            for _ in range(attempts):
                if self._cancelled():
                    return False
                sink = self._new_sink(i)
                try:
                    await asyncio.wait_for(
                        self._perform(rung, atom, sink),
                        timeout=max(ATOM_MIN_SECONDS,
                                    min(45.0, (3.0 * len(atom.text) / 12.0 + 8.0)
                                        * ATOM_TIMEOUT_SCALE)))
                except asyncio.CancelledError:
                    sink.discard()
                    raise
                except Exception as e:
                    last_reason = _short(e)
                    if self._cancelled():
                        sink.discard()
                        return False
                    self._note_failure(rung, last_reason)
                    if self.player.played(i) > 0:
                        sink.flush()
                        await sink.finish_stream()
                        self.ledger.mark(i, PARTIAL, last_reason)
                        return False
                    sink.discard()
                    self.player.drop(sink.token)
                    continue
                if self._cancelled():
                    sink.discard()
                    self.player.drop(sink.token)
                    return False
                if rung == "fast":
                    self.engine.note_fast_success()
                elif rung == "edge":
                    self.engine.note_edge_success()
                err = await sink.finish_stream()
                if err:
                    last_reason = err
                    if self._cancelled():
                        sink.discard()
                        return False
                    self._note_failure(rung, err)
                    if self.player.played(i) > 0:
                        self.ledger.mark(i, PARTIAL, err)
                        return False
                    sink.discard()
                    self.player.drop(sink.token)
                    continue
                fmt, payload = sink.payload()
                if payload:
                    self.engine.cache.put(
                        key, fmt, payload,
                        pcm=list(sink.frames) if fmt == "mp3" else None)
                self.ledger.mark(i, SPOKEN)
                return True
        if self._cancelled():
            return False
        self.ledger.mark(i, SKIPPED, last_reason)
        return False

    def _new_sink(self, i):
        self.attempt += 1
        sink = AtomSink(self, i, (i, self.attempt))
        self.sinks[i] = sink
        return sink

    def _rungs(self):
        if self.offline_only:
            return ["offline"]
        local_ready = bool(self.engine.local and self.engine.local.ready)
        out = [] if self.engine.fast_circuit_open() else ["fast"]
        if not (local_ready and self.engine.edge_circuit_open()):
            out.append("edge")
        if local_ready:
            out.append("offline")
        return out or ["edge"]

    def _note_failure(self, rung, reason):
        """One line per run and rung: never a traceback flood, never text."""
        if rung == "fast":
            self.engine.note_fast_failure()
        elif rung == "edge":
            self.engine.note_edge_failure()
        if rung not in self.failure_logged:
            self.failure_logged.add(rung)
            log(f"read: {rung} failed ({reason})")

    async def _perform(self, rung, atom, sink):
        self.requests += 1
        if rung == "fast":
            await self.engine.voice.synth(atom.text, self.rate, sink.write_mp3,
                                          voice=self.voice)
        elif rung == "edge":
            await self.engine.voice.synth_fallback(atom.text, self.rate,
                                                   sink.write_mp3, voice=self.voice)
        else:
            await self._perform_offline(atom, sink)

    async def _perform_offline(self, atom, sink):
        local = self.engine.local
        if not (local and local.ready):
            raise RuntimeError("offline voice not downloaded")
        future = self.engine.offline_future()
        if future is not None and not future.done():
            try:
                await asyncio.wait_for(asyncio.wrap_future(future), 10)
            except Exception:
                pass                                  # synth will re-raise properly
        pcm = await self.engine.loop.run_in_executor(
            self.engine.local_pool, local.synth, atom.text, self.voice, self.rate)
        sink.write_pcm(pcm)

    def _key(self, text, offline):
        if offline:
            return cache_key(self.engine.offline_voice_id(self.voice), self.rate, text)
        return cache_key(self.voice, self.rate, text)

    # ------------------------------------------------------------ end
    def _warning(self):
        s = self.ledger.summary()
        if s["partial"] or s["skipped"]:
            return "Part of the last read was skipped."
        return ""

    def _end_state(self):
        summary = self.ledger.summary()
        if self.offline_missing:
            return "offline_missing", "download"
        if self.timed_out:
            return "error", "timed out"
        if self.device_lost or self.finished_reason == "device lost":
            return "error", "audio device lost"
        if summary["partial"] or summary["skipped"]:
            if self.player is not None and self.player.frames_played > 0:
                return "idle", self._warning()
            reason = next((r["reason"] for r in summary["offsets"] if r["reason"]),
                          "couldn't read aloud")
            return "error", reason
        if summary["complete"]:
            return "idle", ""
        return "idle", ""

    def _finish_state(self):
        if self._stop_emitting():
            return
        if (not self.device_lost and self.finished_reason != "device lost"
                and not self.timed_out
                and any(row["status"] is None for row in self.ledger.rows)):
            # An atom that was never decoded must never look like a clean idle
            # read: that is the silent-stop failure this ledger exists to stop.
            log("read: early end with unmarked atoms "
                + self._end_diagnosis())
            log("read: trace " + " | ".join(self.trace[-14:]))
            self._final("error", "read stopped early")
            return
        state, detail = self._end_state()
        self._final(state, detail)

    def _end_diagnosis(self):
        tasks = ",".join(f"{i}:{'c' if t.cancelled() else ('d' if t.done() else 'p')}"
                         for i, t in sorted(self.tasks.items()))
        p = self.player
        player = "none" if p is None else (
            f"stop={int(p.stopped)} fin={int(p.finished)} "
            f"frames={p.frames_played} queue={p.queued_seconds():.1f}")
        rows = "".join("." if row["status"] is None else row["status"][0]
                       for row in self.ledger.rows)
        return (f"(rows={rows} head={self.head} next={self.next_i} "
                f"tasks={tasks} player={player} reason={self.finished_reason} "
                f"why={self.cancel_why} gen={self.engine.gen}/{self.gen})")

    def _log_report(self, summary):
        first = f"{self.first_ms:.0f}ms" if self.first_ms is not None else "-"
        total = (time.perf_counter() - self.t0) * 1000
        state = summary["end"]
        log(f"read: atoms={summary['atoms']} chars={summary['total_chars']} "
            f"requests={self.requests} first={first} total={total:.0f}ms "
            f"end={state} spoken={summary['spoken']} partial={summary['partial']} "
            f"skipped={summary['skipped']} cancelled={summary['cancelled']}"
            + (f" why={self.cancel_why}" if self.cancel_why else ""))
        for row in summary["offsets"]:
            log(f"read: {row['status']} {row['start']}-{row['end']} ({row['reason'][:60]})")


def _rate_percent(rate):
    try:
        return int(str(rate).strip().rstrip("%"))
    except ValueError:
        return 0


def _short(e):
    text = f"{type(e).__name__}: {e}".replace("\n", " ")
    return text[:80]
