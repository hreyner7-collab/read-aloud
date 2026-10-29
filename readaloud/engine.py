"""Speech engine: Microsoft's neural voices streamed as they arrive.

One read at a time.  A warm websocket is kept ready, each sentence atom is
streamed and played as its first packet arrives, and failures walk a fixed
ladder (fast connection -> edge-tts -> offline voice) with strict deadlines.
The session/ledger in `session.py` decides what was actually heard.
"""
import asyncio
import os
import threading
import time

from .atoms import clean
from .cache import AudioCache
from .paths import LOG, data_dir, log
from .player import SAMPLE_RATE, AudioPlayer
from .session import (CIRCUIT_AFTER, CIRCUIT_SECONDS, FIRST_PACKET, PACKET_GAP,
                      Session)

VOICE = "en-GB-SoniaNeural"
RATE = "+0%"
KEEP_WARM = True
SPARES = 1
SPARE_MAX_AGE = 1800
PING_EVERY = 10
HEDGE_AFTER = 0.6
OFFLINE_ONLY = False
MAX_SOCKETS = 4
REFILL_MIN, REFILL_MAX = 20.0, 300.0
POOL_PAUSE = 300.0
READY_TIMEOUT = 2.0
START_TIMEOUT = 10.0

WSS_OVERRIDE = os.environ.get("READ_ALOUD_WSS_URL")   # testing only


class SynthError(Exception):
    pass


class NoAudio(SynthError):
    pass


class Stalled(SynthError):
    pass


class Truncated(SynthError):
    pass


# ---------------------------------------------------------------- connection

class Connection:
    """One websocket, handshake and config already sent.

    A reader notices immediately when Microsoft closes it and a ping keeps it
    alive while idle.  Closing releases the socket slot exactly once.
    """

    def __init__(self, ws, msg_types, on_dead, on_release):
        self.ws = ws
        self.opened = time.time()
        self.q = asyncio.Queue()
        self.dead = False
        self._T = msg_types
        self._on_dead = on_dead
        self._on_release = on_release
        self._released = False
        self._pinger = asyncio.ensure_future(self._ping())
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            while True:
                msg = await self.ws.receive()
                self.q.put_nowait(msg)
                if msg.type not in (self._T.TEXT, self._T.BINARY):
                    break
        except Exception:
            pass
        finally:
            self.dead = True
            self.q.put_nowait(None)
            self._pinger.cancel()
            self._release()
            self._on_dead(self)

    async def _ping(self):
        try:
            while True:
                await asyncio.sleep(PING_EVERY)
                await self.ws.ping()
        except Exception:
            pass

    def _release(self):
        if not self._released:
            self._released = True
            self._on_release()

    async def close(self):
        self.dead = True
        self._pinger.cancel()
        if not self._reader.done():
            self._reader.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass
        self._release()


# ---------------------------------------------------------------- voice

class Voice:
    """Microsoft Read Aloud, with a small pool of warm connections."""

    def __init__(self, keep_warm=True):
        import aiohttp
        from edge_tts import communicate as c, constants as k, drm
        self.aiohttp, self.c, self.k, self.drm = aiohttp, c, k, drm.DRM
        self.session = None
        self.keep_warm = keep_warm
        self.spares = []
        self.last_use = 0.0
        self.last_open_ok = 0.0
        self.pings_work = True
        self.idle_deaths = []
        self._slots = asyncio.Semaphore(MAX_SOCKETS)
        self._refill_task = None
        self._fail_streak = 0
        self._backoff = REFILL_MIN
        self._pause_until = 0.0
        self._last_log = None
        self._health = "ok"

    # ---- pool
    def set_keep_warm(self, on):
        self.keep_warm = bool(on)
        if not on:
            while self.spares:
                asyncio.ensure_future(self.spares.pop().close())

    def _note(self, health, message):
        self._health = health
        if message and message != self._last_log:
            self._last_log = message
            log(message)

    def on_read(self):
        """A read is starting: clear pool backoff and try to have a spare ready."""
        self._fail_streak = 0
        self._pause_until = 0.0
        self._backoff = REFILL_MIN
        self.refill()

    async def _session(self):
        if self.session is None or self.session.closed:
            self.session = self.aiohttp.ClientSession(
                trust_env=True,
                timeout=self.aiohttp.ClientTimeout(total=None, sock_connect=10,
                                                   sock_read=None))
        return self.session

    def _url(self):
        base = WSS_OVERRIDE or self.k.WSS_URL
        sep = "&" if "?" in base else "?"
        return (f"{base}{sep}ConnectionId={self.c.connect_id()}"
                f"&Sec-MS-GEC={self.drm.generate_sec_ms_gec()}"
                f"&Sec-MS-GEC-Version={self.k.SEC_MS_GEC_VERSION}")

    async def open(self):
        """Open one socket; waits when four are already open."""
        await self._slots.acquire()
        conn = None
        try:
            session = await self._session()
            kwargs = dict(compress=15, headers=self.drm.headers_with_muid(self.k.WSS_HEADERS))
            if not WSS_OVERRIDE:
                kwargs["ssl"] = self.c._SSL_CTX
            try:
                ws = await session.ws_connect(self._url(), **kwargs)
            except self.aiohttp.ClientResponseError as e:
                if e.status != 403:
                    raise
                self.drm.handle_client_response_error(e)   # clock skew
                ws = await session.ws_connect(self._url(), **kwargs)
            await ws.send_str(
                f"X-Timestamp:{self.c.date_to_string()}\r\n"
                "Content-Type:application/json; charset=utf-8\r\n"
                "Path:speech.config\r\n\r\n"
                '{"context":{"synthesis":{"audio":{"metadataoptions":{'
                '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
                '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n')
            self.last_open_ok = time.time()
            conn = Connection(ws, self.aiohttp.WSMsgType, self._conn_died,
                              self._slots.release)
            return conn
        except BaseException:
            self._slots.release()
            raise

    def _conn_died(self, conn):
        if conn in self.spares:
            self.spares.remove(conn)
            lived = time.time() - conn.opened
            self.idle_deaths = (self.idle_deaths + [lived])[-3:]
            if self.pings_work and len(self.idle_deaths) >= 2 and max(self.idle_deaths) < 180:
                self.pings_work = False
                self._note("ok", "Microsoft drops idle connections; "
                                 "keeping one ready only for 15 min after each use")
            self.refill()

    def _usable(self, conn):
        return not conn.dead and time.time() - conn.opened < SPARE_MAX_AGE

    async def take(self):
        while self.spares:
            conn = self.spares.pop(0)
            if self._usable(conn):
                return conn, "warm"
            asyncio.ensure_future(conn.close())
        return await self.open(), "cold"

    def give_back(self, conn):
        if self.keep_warm and self._usable(conn) and len(self.spares) < SPARES:
            self.spares.append(conn)
        else:
            asyncio.ensure_future(conn.close())

    def _want_spares(self):
        if not self.keep_warm:
            return False
        return self.pings_work or time.time() - self.last_use < 900

    def refill(self):
        if self._refill_task is None or self._refill_task.done():
            self._refill_task = asyncio.ensure_future(self._refill())

    async def _refill(self):
        try:
            for conn in [c for c in self.spares if not self._usable(c)]:
                self.spares.remove(conn)
                asyncio.ensure_future(conn.close())
            if not self._want_spares():
                while self.spares:
                    asyncio.ensure_future(self.spares.pop().close())
                return
            if time.time() < self._pause_until:
                return
            while len(self.spares) < SPARES:
                try:
                    conn = await self.open()
                except Exception as e:
                    self._fail_streak += 1
                    why = type(e).__name__
                    if self._fail_streak >= 3:
                        self._pause_until = time.time() + POOL_PAUSE
                        self._note("down", f"warm pool paused for 5 min ({why})")
                    else:
                        self._backoff = min(REFILL_MAX, self._backoff * 2)
                        jitter = 0.9 + 0.2 * (time.time() % 1)
                        self._pause_until = time.time() + self._backoff * jitter
                        self._note("down", f"warm pool open failed ({why})")
                    return
                if not self._want_spares():
                    await conn.close()
                    return
                self._fail_streak = 0
                self._backoff = REFILL_MIN
                self._pause_until = 0.0
                self.spares.append(conn)
                if self._health != "ok":
                    self._note("ok", "warm pool recovered")
        except asyncio.CancelledError:
            raise
        except Exception as e:                       # never kill the loop
            log(f"warm pool: {type(e).__name__}: {e}")

    async def keep_warm_loop(self):
        while True:
            self.refill()
            await asyncio.sleep(20)

    # ---- synthesis
    async def _synth_on(self, conn, voice, text, rate, emit):
        cfg = self.c.TTSConfig(voice, rate, "+0%", "+0Hz", "SentenceBoundary")
        escaped = self.c.escape(self.c.remove_incompatible_characters(text))
        try:
            await asyncio.wait_for(conn.ws.send_str(
                self.c.ssml_headers_plus_data(
                    self.c.connect_id(), self.c.date_to_string(),
                    self.c.mkssml(cfg, escaped))), 5.0)
        except asyncio.TimeoutError:
            raise Stalled("send")
        T = self.aiohttp.WSMsgType
        loop = asyncio.get_running_loop()
        got = False
        turn_deadline = None
        while True:
            now = loop.time()
            timeout = FIRST_PACKET if not got else PACKET_GAP
            if turn_deadline is not None:
                timeout = min(timeout, turn_deadline - now)
            if timeout <= 0:
                raise Stalled("first packet" if not got else "packet gap")
            try:
                msg = await asyncio.wait_for(conn.q.get(), timeout)
            except asyncio.TimeoutError:
                if not got:
                    raise Stalled("first packet")
                if turn_deadline is not None and loop.time() >= turn_deadline - 0.01:
                    raise Truncated("no turn.end")
                raise Stalled("packet gap")
            if msg is None or msg.type not in (T.TEXT, T.BINARY):
                if got:
                    raise Truncated("connection closed before turn.end")
                raise NoAudio("connection closed before audio")
            if msg.type == T.TEXT:
                if "Path:turn.end" in msg.data:
                    break
                continue
            data = msg.data
            if len(data) < 2:
                continue
            hl = int.from_bytes(data[:2], "big")
            headers, audio = self.c.get_headers_and_data(data, hl)
            if headers.get(b"Path") == b"audio" and audio:
                got = True
                emit(audio)
                now = loop.time()
                if turn_deadline is None:
                    expected = max(1.0, len(text) / 13.0)
                    turn_deadline = now + max(15.0, min(30.0, expected * 3.0 + 6.0))
        if not got:
            raise NoAudio("no audio")

    async def synth(self, text, rate, emit, stats=None, voice=None):
        """Stream one atom.  First emitter wins; a slow spare is hedged."""
        self.last_use = time.time()
        voice = voice or VOICE
        conn, kind = await self.take()
        self.refill()
        if stats is not None:
            stats["conn"] = kind
        winner = []
        first = asyncio.Event()

        def gate(tag):
            def _emit(data):
                if not data:
                    return
                if not winner:
                    winner.append(tag)
                    first.set()
                if winner[0] == tag:
                    emit(data)
            return _emit

        async def run(c, tag):
            ok = False
            try:
                await self._synth_on(c, voice, text, rate, gate(tag))
                ok = True
            finally:
                if ok and winner and winner[0] == tag:
                    self.give_back(c)
                else:
                    asyncio.ensure_future(c.close())

        tag1 = "warm" if kind == "warm" else "fresh"
        t1 = asyncio.ensure_future(run(conn, tag1))
        if kind == "cold":
            await t1
            return
        waiter = asyncio.ensure_future(first.wait())
        try:
            await asyncio.wait({t1, waiter}, timeout=HEDGE_AFTER,
                               return_when=asyncio.FIRST_COMPLETED)
        finally:
            waiter.cancel()
        if first.is_set():
            await t1
            return
        if t1.done():
            await t1                               # warm attempt already failed
            return
        if stats is not None:
            stats["conn"] = "warm->hedged"
        try:
            conn2 = await self.open()
        except Exception:
            await asyncio.wait_for(t1, FIRST_PACKET + PACKET_GAP)
            return
        t2 = asyncio.ensure_future(run(conn2, "fresh"))
        pending, errors = {t1, t2}, []
        while pending and not winner:
            done, pending = await asyncio.wait(pending,
                                               return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.cancelled():
                    continue
                exc = t.exception()
                if exc is not None:
                    errors.append(exc)
        if not winner:
            for t in pending:
                t.cancel()
            if errors:
                raise errors[0]
            raise NoAudio("no audio")
        winner_task = t1 if winner[0] == tag1 else t2
        for t in pending:
            if t is not winner_task:     # the winner may still be streaming
                t.cancel()
        await winner_task

    async def synth_fallback(self, text, rate, emit, voice=None):
        """Plain edge-tts, used when the fast path fails."""
        import edge_tts
        voice = voice or VOICE
        agen = edge_tts.Communicate(text, voice, rate=rate).stream()
        loop = asyncio.get_running_loop()
        got = False
        deadline = loop.time() + FIRST_PACKET
        try:
            while True:
                timeout = deadline - loop.time()
                if timeout <= 0:
                    raise Stalled("edge first packet" if not got else "edge packet gap")
                try:
                    chunk = await asyncio.wait_for(agen.__anext__(), timeout)
                except StopAsyncIteration:
                    break
                except asyncio.TimeoutError:
                    raise Stalled("edge first packet" if not got else "edge packet gap")
                if chunk["type"] == "audio" and chunk["data"]:
                    got = True
                    emit(chunk["data"])
                    deadline = loop.time() + PACKET_GAP
        finally:
            try:
                await agen.aclose()
            except Exception:
                pass
        if not got:
            raise NoAudio("edge: no audio")


# ---------------------------------------------------------------- engine

class Engine:
    """Owns the background loop and the single live read.

    on_state(state, detail, generation) is called from background threads;
    generations make late events from a stopped read harmless.
    """

    def __init__(self, on_state, settings=None):
        self.on_state = on_state
        self.settings = dict(settings or {})
        self.loop = asyncio.new_event_loop()
        self.voice = None
        self.local = None
        self.local_pool = None
        self.cache = None
        self.session = None
        self.task = None
        self.gen = 0
        self.last_ledger = None
        self._lock = threading.Lock()
        self._start_error = None
        self._fast_failures = 0
        self._circuit_until = 0.0
        self._edge_failures = 0
        self._edge_until = 0.0
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="engine").start()
        if not self.ready.wait(READY_TIMEOUT):       # C2: never block longer
            log("engine still starting after 2s")

    def _run(self):
        import concurrent.futures
        from .local_voice import LocalVoice
        asyncio.set_event_loop(self.loop)
        try:
            self.voice = Voice(KEEP_WARM)
            self.local = LocalVoice()
            self.local_pool = concurrent.futures.ThreadPoolExecutor(
                1, thread_name_prefix="offline")
            self.cache = AudioCache(
                disk_dir=os.path.join(data_dir(), "audio-cache"),
                disk_enabled=bool(self.settings.get("disk_cache")))
            if KEEP_WARM:
                self.loop.create_task(self.voice.keep_warm_loop())
            self.local.download_in_background()
            if OFFLINE_ONLY:
                self.voice.set_keep_warm(False)
                self.preload_offline()
            log("engine started")
        except Exception as e:                       # e.g. portaudio missing
            self._start_error = f"{type(e).__name__}: {e}"
            log(f"engine start failed: {self._start_error}")
        finally:
            self.ready.set()
        self.loop.run_forever()

    # ---- input
    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def speak(self, text):
        voice, rate = VOICE, RATE
        offline_only = bool(OFFLINE_ONLY)
        with self._lock:
            self.gen += 1
            gen = self.gen
        self._stop_read()
        if not self.ready.is_set():
            self._emit("loading", "", gen)
        if self.ready.is_set():
            self.loop.call_soon_threadsafe(self._start, text, voice, rate,
                                           offline_only, gen)
        else:
            threading.Thread(target=self._start_when_ready,
                             args=(text, voice, rate, offline_only, gen, time.time()),
                             daemon=True, name="speak-wait").start()

    def _start_when_ready(self, text, voice, rate, offline_only, gen, asked):
        remaining = START_TIMEOUT - (time.time() - asked)
        if not self.ready.wait(max(0.0, remaining)):
            self._emit_if_current("error", "still starting", gen)
            return
        self.loop.call_soon_threadsafe(self._start, text, voice, rate,
                                       offline_only, gen)

    def _start(self, text, voice, rate, offline_only, gen):
        if gen != self.gen:
            return
        if self._start_error or self.voice is None or self.local is None:
            self._emit_if_current("error", "still starting", gen)
            return
        if offline_only:
            self.voice.set_keep_warm(False)
        self.session = Session(self, text, gen, voice, rate, offline_only,
                               self._session_state,
                               player_factory=self._make_player)
        self.task = self.loop.create_task(self.session.run())
        self.task.add_done_callback(self._task_done)

    def _task_done(self, task):
        if task.cancelled():
            return
        exc = task.exception()
        if exc is not None:                          # Session catches its own
            log(f"read crashed: {type(exc).__name__}: {exc}"[:200])

    def _make_player(self, on_first_sound, on_finished, fast_start=False):
        return AudioPlayer(on_first_sound, on_finished, fast_start=fast_start)

    def _session_state(self, state, detail, gen):
        self._emit_if_current(state, detail, gen)

    def _emit(self, state, detail="", gen=None):
        try:
            self.on_state(state, detail or "", self.gen if gen is None else gen)
        except Exception as e:
            log(f"state callback: {type(e).__name__}")

    def _emit_if_current(self, state, detail, gen):
        if gen == self.gen:
            self._emit(state, detail, gen)

    def _stop_read(self):
        old = self.session
        if old is not None and old.player is not None:
            old.player.stop()                        # device closed before new one
        task = self.task
        if task is not None and not task.done():
            self.loop.call_soon_threadsafe(task.cancel)

    def stop(self):
        with self._lock:
            self.gen += 1
            gen = self.gen
        self._stop_read()
        self._emit("idle", "", gen)

    def set_voice(self, voice=None, rate=None):
        global VOICE, RATE
        if voice:
            VOICE = voice
        if rate:
            RATE = rate

    def set_offline_only(self, on):
        global OFFLINE_ONLY
        OFFLINE_ONLY = bool(on)
        if on and self.voice is not None:
            self.voice.set_keep_warm(False)
        elif self.voice is not None:
            self.voice.refill()

    # ---- offline voice
    def begin_read(self):
        if self.voice is not None:
            self.voice.on_read()
        self.preload_offline()

    def preload_offline(self):
        if self.local is None:
            return
        try:
            self.local.preload(self.local_pool)
        except Exception as e:
            log(f"offline preload: {type(e).__name__}: {e}")

    def offline_future(self):
        return getattr(self.local, "preload_future", None)

    def offline_voice_id(self, voice):
        from .local_voice import VOICE_MAP
        return "kokoro:" + VOICE_MAP.get(voice, ("bf_emma", "en-gb"))[0]

    # ---- fast-path circuit (C7)
    def note_fast_failure(self):
        self._fast_failures += 1
        if self._fast_failures >= CIRCUIT_AFTER and not self.fast_circuit_open():
            self._circuit_until = time.monotonic() + CIRCUIT_SECONDS
            log("fast path paused for 60s after 3 failures")
        # The fallback has to be usable before the user needs it.
        if self.local is not None:
            self.local.request_repair("Microsoft voice unavailable")

    def note_fast_success(self):
        self._fast_failures = 0

    def fast_circuit_open(self):
        return time.monotonic() < self._circuit_until

    def note_edge_failure(self):
        self._edge_failures += 1
        if self._edge_failures >= 2 and not self.edge_circuit_open():
            self._edge_until = time.monotonic() + CIRCUIT_SECONDS
            log("edge-tts fallback paused for 60s after 2 failures")
        if self.local is not None:
            self.local.request_repair("Microsoft voice unavailable")

    def note_edge_success(self):
        self._edge_failures = 0

    def edge_circuit_open(self):
        return time.monotonic() < self._edge_until
