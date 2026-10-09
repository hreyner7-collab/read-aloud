"""Speech engine for Read Aloud: Microsoft's neural voices, started as fast as possible.

Same ideas Apple uses for its own voices:
  * a connection to Microsoft is kept open and warm, so nothing slow happens on click
  * audio plays as it streams in rather than after a whole file downloads
  * the first sentence is sent on its own so it comes back quickest; the rest
    downloads while that plays

Engine runs its own asyncio loop in a background thread. The UI calls
engine.speak(text) / engine.stop() and gets state callbacks:
"loading", "speaking", "idle", or "error".
"""
import array
import asyncio
import collections
import os
import re
import sys
import threading
import time

VOICE = "en-GB-SoniaNeural"
RATE = "+0%"
KEEP_WARM = True           # keep a connection open between reads
SPARES = 1                 # connections kept open and ready
SPARE_MAX_AGE = 1800       # replace a spare after this long regardless (seconds)
PING_EVERY = 10            # keep-alive ping on idle connections (seconds)
PARALLEL = 2               # chunks downloading at once
FIRST_CHUNK = 180          # max characters in the first chunk
HEDGE_AFTER = 0.4          # s without audio on a spare -> race a fresh connection
SAMPLE_RATE = 24000        # Microsoft sends 24 kHz mono MP3
MIN_PREFILL_FRAMES = 480 * 6   # ~60ms before the very first sound
CUSHION_FRAMES = 24000 * 3     # keep ~3s of decoded audio ready: Microsoft
                               # stalls up to ~2.5s mid-chunk (measured), so
                               # the cushion must ride those out gaplessly
MAX_PCM_QUEUE = 24000 * 8      # cap decoder queue (~8s) to bound latency
FEED_AHEAD = 3             # how many chunks to prefetch in parallel ahead of current

# For testing only
WSS_OVERRIDE = os.environ.get("READ_ALOUD_WSS_URL")
AUDIO_BACKEND = os.environ.get("READ_ALOUD_AUDIO_BACKEND")  # e.g. "null"


def data_dir():
    if sys.platform == "darwin":
        d = os.path.expanduser("~/Library/Application Support/Read Aloud")
    elif os.name == "nt":
        d = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "Read Aloud")
    else:
        d = os.path.join(os.path.expanduser("~"), ".config", "read-aloud")
    os.makedirs(d, exist_ok=True)
    return d


LOG = os.path.join(data_dir(), "read-aloud.log")


def log(msg):
    try:
        if os.path.exists(LOG) and os.path.getsize(LOG) > 1_000_000:
            os.replace(LOG, LOG + ".old")
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S ") + msg + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- text

def clean(text):
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[ \t ]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def split_chunks(text):
    """Tiny first chunk (fast first sound), then progressively bigger ones."""
    sentences = []
    for para in text.split("\n\n"):
        para = " ".join(para.split())
        if para:
            sentences += [s for s in re.split(r"(?<=[.!?:;])\s+", para) if s]
    if not sentences:
        return []

    first = sentences.pop(0)
    if len(first) > FIRST_CHUNK:  # cut a long opener at a comma or space
        cut = max(first.rfind(", ", 0, FIRST_CHUNK), first.rfind(" ", 0, FIRST_CHUNK))
        if cut > 20:
            sentences.insert(0, first[cut + 1:].strip())
            first = first[:cut + 1].strip()
    chunks, buf, limit = [first], "", 400
    for s in sentences:
        if buf and len(buf) + len(s) + 1 > limit:
            chunks.append(buf)
            buf, limit = s, 1500
        else:
            buf = f"{buf} {s}".strip()
    if buf:
        chunks.append(buf)
    return chunks


# ---------------------------------------------------------------- audio

class ByteSource:
    """Feeds MP3 bytes to the decoder as they arrive from the network."""

    def __init__(self):
        self.q = collections.deque()
        self.cv = threading.Condition()
        self.buf = b""
        self.closed = False

    def feed(self, data):
        with self.cv:
            self.q.append(data)
            self.cv.notify()

    def end(self):
        with self.cv:
            self.closed = True
            self.cv.notify()

    def read(self, n):
        if not self.buf:
            with self.cv:
                while not self.q and not self.closed:
                    self.cv.wait()
                if not self.q:
                    return b""
                self.buf = self.q.popleft()
        out, self.buf = self.buf[:n], self.buf[n:]
        return out


class StreamPlayer:
    """Decodes a growing MP3 byte stream and plays it gaplessly (miniaudio)."""

    def __init__(self, on_first_sound, on_finished):
        import miniaudio
        self.ma = miniaudio
        self.src = ByteSource()
        self.pcm = collections.deque()
        self.decoded_all = False
        self.stopped = False
        self.on_first_sound = on_first_sound
        self.on_finished = on_finished

        class _Src(miniaudio.StreamableSource):
            def read(inner, n):
                return self.src.read(n)

        self._src = _Src()
        kwargs = dict(nchannels=1, sample_rate=SAMPLE_RATE, buffersize_msec=120,
                      app_name="Read Aloud")
        if AUDIO_BACKEND:
            kwargs["backends"] = [getattr(miniaudio.Backend, AUDIO_BACKEND.upper())]
        self.device = miniaudio.PlaybackDevice(**kwargs)
        threading.Thread(target=self._decode, daemon=True).start()
        gen = self._generator()
        next(gen)
        self.device.start(gen)  # opens the output now, while the network works

    def feed(self, data):
        self.src.feed(data)

    def end(self):
        self.src.end()

    def _decode(self):
        try:
            for frames in self.ma.stream_any(
                self._src, self.ma.FileFormat.MP3, nchannels=1,
                sample_rate=SAMPLE_RATE, frames_to_read=480,
            ):
                if self.stopped:
                    return
                # Keep the decoder ahead but bounded: once the queue holds
                # MAX_PCM_QUEUE frames (~8s), pause feeding until it drains.
                while len(self.pcm) >= MAX_PCM_QUEUE and not self.stopped:
                    time.sleep(0.01)
                self.pcm.append(frames)
        except Exception as e:  # corrupt data etc.
            if not self.stopped:
                log(f"decoder error: {e!r}")
        finally:
            self.decoded_all = True

    def _generator(self):
        need = yield b""
        pending = array.array("h")
        started = finished = False
        # Small prefill so the device opens on real audio, then a deep cushion
        # absorbs network hiccups between chunks for the rest of the read.
        while len(pending) < MIN_PREFILL_FRAMES and not self.stopped:
            if self.pcm:
                pending.extend(self.pcm.popleft())
            elif self.decoded_all:
                break
            else:
                time.sleep(0.005)
        if pending and not started:
            started = True
            self.on_first_sound()
        while True:
            # Top the cushion up from decoded PCM whenever there is headroom.
            while len(pending) < CUSHION_FRAMES and self.pcm:
                pending.extend(self.pcm.popleft())
            if len(pending) < need:
                if self.decoded_all and not self.pcm and not finished:
                    finished = True
                    self.on_finished()
                # Underflow: the cushion has run dry. Pad with silence so the
                # device always gets a full callback; playback resumes the
                # instant the next chunk arrives.
                pending.extend([0] * (need - len(pending)))
            out, pending = pending[:need], pending[need:]
            need = yield out

    def stop(self):
        self.stopped = True
        self.src.end()
        try:
            self.device.stop()
            self.device.close()
        except Exception:
            pass


# ---------------------------------------------------------------- network

class Connection:
    """One websocket to Microsoft, handshake + config already sent.

    A background reader notices straight away if Microsoft closes it, and a
    ping every few seconds stops it being dropped while idle.
    """

    def __init__(self, ws, msg_types, on_dead):
        self.ws = ws
        self.opened = time.time()
        self.q = asyncio.Queue()
        self.dead = False
        self._T = msg_types
        self._on_dead = on_dead
        self._pinger = asyncio.ensure_future(self._ping())
        self._reader = asyncio.ensure_future(self._read())

    async def _read(self):
        try:
            while True:
                msg = await self.ws.receive()  # pings/pongs are handled inside aiohttp
                self.q.put_nowait(msg)
                if msg.type not in (self._T.TEXT, self._T.BINARY):
                    break
        except Exception:
            pass
        finally:
            self.dead = True
            self.q.put_nowait(None)
            self._pinger.cancel()
            self._on_dead(self)

    async def _ping(self):
        try:
            while True:
                await asyncio.sleep(PING_EVERY)
                await self.ws.ping()
        except Exception:
            pass

    async def close(self):
        self.dead = True
        self._pinger.cancel()
        try:
            await self.ws.close()
        except Exception:
            pass


class Voice:
    """Talks to Microsoft's Read Aloud service, keeping a connection warm."""

    def __init__(self, keep_warm):
        import aiohttp
        from edge_tts import communicate as c, constants as k, drm
        self.aiohttp, self.c, self.k, self.drm = aiohttp, c, k, drm.DRM
        self.session = None
        self.keep_warm = keep_warm
        self.spares = []
        self._refilling = False
        self.last_use = 0.0
        self.idle_deaths = []      # how long spares lasted when Microsoft dropped them
        self.pings_work = True     # assume pings keep spares alive until shown otherwise

    async def _session(self):
        if self.session is None or self.session.closed:
            self.session = self.aiohttp.ClientSession(
                trust_env=True,
                timeout=self.aiohttp.ClientTimeout(total=None, sock_connect=10, sock_read=None),
            )
        return self.session

    def _url(self):
        base = WSS_OVERRIDE or self.k.WSS_URL
        sep = "&" if "?" in base else "?"
        return (f"{base}{sep}ConnectionId={self.c.connect_id()}"
                f"&Sec-MS-GEC={self.drm.generate_sec_ms_gec()}"
                f"&Sec-MS-GEC-Version={self.k.SEC_MS_GEC_VERSION}")

    async def open(self):
        session = await self._session()
        kwargs = dict(compress=15, headers=self.drm.headers_with_muid(self.k.WSS_HEADERS))
        if not WSS_OVERRIDE:
            kwargs["ssl"] = self.c._SSL_CTX
        try:
            ws = await session.ws_connect(self._url(), **kwargs)
        except self.aiohttp.ClientResponseError as e:
            if e.status != 403:
                raise
            self.drm.handle_client_response_error(e)  # fixes clock skew
            ws = await session.ws_connect(self._url(), **kwargs)
        await ws.send_str(
            f"X-Timestamp:{self.c.date_to_string()}\r\n"
            "Content-Type:application/json; charset=utf-8\r\n"
            "Path:speech.config\r\n\r\n"
            '{"context":{"synthesis":{"audio":{"metadataoptions":{'
            '"sentenceBoundaryEnabled":"false","wordBoundaryEnabled":"false"},'
            '"outputFormat":"audio-24khz-48kbitrate-mono-mp3"}}}}\r\n'
        )
        return Connection(ws, self.aiohttp.WSMsgType, self._conn_died)

    def _conn_died(self, conn):
        if conn in self.spares:  # dropped while idle, not by us
            self.spares.remove(conn)
            lived = time.time() - conn.opened
            self.idle_deaths = (self.idle_deaths + [lived])[-3:]
            if self.pings_work and len(self.idle_deaths) >= 2 and max(self.idle_deaths) < 180:
                self.pings_work = False
                log(f"Microsoft drops idle connections after ~{lived:.0f}s even with pings; "
                    "keeping one ready only for 15 min after each use")
            self.refill()

    def _usable(self, conn):
        return not conn.dead and time.time() - conn.opened < SPARE_MAX_AGE

    async def take(self):
        """A ready connection: a warm spare if there is one, else a fresh one."""
        while self.spares:
            conn = self.spares.pop(0)
            if self._usable(conn):
                return conn, "warm"
            asyncio.ensure_future(conn.close())
        return await self.open(), "cold"

    def give_back(self, conn):
        """Reuse a connection after a request instead of opening a new one."""
        if self.keep_warm and self._usable(conn) and len(self.spares) < SPARES:
            self.spares.append(conn)
        else:
            asyncio.ensure_future(conn.close())

    def _want_spares(self):
        if not self.keep_warm:
            return False
        return self.pings_work or time.time() - self.last_use < 900

    def refill(self):
        if not self._refilling:
            asyncio.ensure_future(self._refill())

    async def _refill(self):
        self._refilling = True
        try:
            for conn in [c for c in self.spares if not self._usable(c)]:
                self.spares.remove(conn)
                asyncio.ensure_future(conn.close())
            if not self._want_spares():
                while self.spares:
                    asyncio.ensure_future(self.spares.pop().close())
                return
            while len(self.spares) < SPARES:
                self.spares.append(await self.open())
        except Exception as e:
            log(f"couldn't pre-open connection: {e!r}")
        finally:
            self._refilling = False

    async def keep_warm_loop(self):
        while True:
            self.refill()
            await asyncio.sleep(20)

    async def _synth_on(self, conn, text, rate, emit):
        """Send one SSML request on conn; emit(bytes) for each audio packet."""
        cfg = self.c.TTSConfig(VOICE, rate, "+0%", "+0Hz", "SentenceBoundary")
        escaped = self.c.escape(self.c.remove_incompatible_characters(text))
        await conn.ws.send_str(self.c.ssml_headers_plus_data(
            self.c.connect_id(), self.c.date_to_string(), self.c.mkssml(cfg, escaped)))
        got = False
        T = self.aiohttp.WSMsgType
        while True:
            msg = await conn.q.get()
            if msg is None or msg.type not in (T.TEXT, T.BINARY):
                break  # connection closed
            if msg.type == T.TEXT:
                if "Path:turn.end" in msg.data:
                    break
            else:
                data = msg.data
                if len(data) < 2:
                    continue
                hl = int.from_bytes(data[:2], "big")
                headers, audio = self.c.get_headers_and_data(data, hl)
                if headers.get(b"Path") == b"audio" and audio:
                    got = True
                    emit(audio)
        if not got:
            raise ConnectionError("no audio received")

    async def synth(self, text, rate, emit, stats=None):
        """Stream one chunk. Uses a warm spare; if it's slow or dead, races a fresh one."""
        self.last_use = time.time()
        conn, kind = await self.take()
        self.refill()
        if stats is not None:
            stats["conn"] = kind
        started = asyncio.Event()
        winner = []

        def gate(tag):
            def _emit(b):
                if not winner:
                    winner.append(tag)
                if winner[0] == tag:
                    started.set()
                    emit(b)
            return _emit

        async def run(c, tag):
            ok = False
            try:
                await self._synth_on(c, text, rate, gate(tag))
                ok = True
            finally:
                if ok:
                    self.give_back(c)
                else:
                    asyncio.ensure_future(c.close())

        async def fresh():
            await run(await self.open(), "fresh")

        first = asyncio.ensure_future(run(conn, "spare"))
        if kind == "cold":
            await first
            return
        waiter = asyncio.ensure_future(started.wait())
        await asyncio.wait({first, waiter}, timeout=HEDGE_AFTER,
                           return_when=asyncio.FIRST_COMPLETED)
        waiter.cancel()
        if started.is_set():
            await first
            return
        # The spare is slow or dead: race a fresh connection, keep whichever speaks first.
        if stats is not None:
            stats["conn"] = "warm->hedged"
        second = asyncio.ensure_future(fresh())
        pending = {first, second}
        errors = []
        while pending:
            done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
            for t in done:
                if t.cancelled():
                    continue
                if t.exception():
                    errors.append(t.exception())
                elif winner:  # the winner finished cleanly
                    for p in pending:
                        p.cancel()
                    return
            if winner and not pending:
                return
        if not winner:
            raise errors[0] if errors else ConnectionError("no audio")

    async def synth_fallback(self, text, rate, emit):
        """Plain edge-tts, in case Microsoft changes something our fast path relies on."""
        import edge_tts
        async for ch in edge_tts.Communicate(text, VOICE, rate=rate).stream():
            if ch["type"] == "audio":
                emit(ch["data"])


# ---------------------------------------------------------------- one read

class Reading:
    def __init__(self, voice, text, on_state):
        self.voice = voice
        self.loop = asyncio.get_running_loop()
        self.chunks = split_chunks(clean(text))
        self.on_state = on_state
        self.t_start = time.time()
        self.player = None
        self.done = asyncio.Event()
        self.stats = {}

    def _first_sound(self):  # audio thread
        self.stats["first_sound"] = time.time()
        self.on_state("speaking")

    def _finished(self):  # audio thread
        self.loop.call_soon_threadsafe(self.done.set)

    async def run(self):
        if not self.chunks:
            self.on_state("idle")
            return
        self.on_state("loading")
        ok = False
        try:
            self.player = StreamPlayer(self._first_sound, self._finished)
            await self._fetch_all()
            await self.done.wait()
            ok = True
        except asyncio.CancelledError:
            ok = True
            raise
        except Exception as e:
            log(f"error: {e!r}")
        finally:
            if self.player:
                self.player.stop()
            self.on_state("idle" if ok else "error")
            self._log()

    async def _fetch_all(self):
        n = len(self.chunks)
        queues = [asyncio.Queue() for _ in range(n)]
        sem = asyncio.Semaphore(PARALLEL)

        async def fetch(i):
            async with sem:
                sent = []

                def emit(b):
                    sent.append(1)
                    queues[i].put_nowait(b)
                st = self.stats if i == 0 else None
                try:
                    await self.voice.synth(self.chunks[i], RATE, emit, st)
                except Exception as e:
                    if sent:  # got partway; don't repeat audio
                        log(f"chunk {i} cut short: {e!r}")
                    else:
                        log(f"fast path failed on chunk {i} ({e!r}), using edge-tts")
                        if st is not None:
                            st["conn"] = "fallback"
                        try:
                            await self.voice.synth_fallback(self.chunks[i], RATE, emit)
                        except Exception as e2:
                            log(f"chunk {i} failed: {e2!r}")
                            if i == 0:
                                queues[i].put_nowait(e2)
                queues[i].put_nowait(None)

        # Limited lookahead: always FEED_AHEAD chunks fetching in the
        # background while we drain and play the current one, so the decoder
        # never runs dry waiting for the next chunk to be requested.
        tasks = [asyncio.ensure_future(fetch(i))
                 for i in range(min(n, FEED_AHEAD + 1))]
        try:
            for i in range(n):
                while True:
                    b = await queues[i].get()
                    if b is None:
                        break
                    if isinstance(b, Exception):
                        raise b
                    if i == 0 and "first_byte" not in self.stats:
                        self.stats["first_byte"] = time.time()
                    self.player.feed(b)
                nxt = i + FEED_AHEAD + 1
                if nxt < n:
                    tasks.append(asyncio.ensure_future(fetch(nxt)))
            self.player.end()
        finally:
            for t in tasks:
                t.cancel()

    def _log(self):
        def ms(key):
            t = self.stats.get(key)
            return f"{(t - self.t_start) * 1000:.0f}ms" if t else "-"
        log(f"first byte {ms('first_byte')} | first sound {ms('first_sound')} | "
            f"conn {self.stats.get('conn', '?')} | chunks {len(self.chunks)}")


# ---------------------------------------------------------------- engine

class Engine:
    """Owns the background asyncio loop. Thread-safe speak()/stop()."""

    def __init__(self, on_state):
        self.on_state = on_state      # called from background threads
        self.loop = asyncio.new_event_loop()
        self.voice = None
        self.task = None
        self.ready = threading.Event()
        threading.Thread(target=self._run, daemon=True, name="engine").start()
        self.ready.wait(10)

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.voice = Voice(KEEP_WARM)
        if KEEP_WARM:
            self.loop.create_task(self.voice.keep_warm_loop())
        self.ready.set()
        log("engine started")
        self.loop.run_forever()

    @property
    def busy(self):
        return self.task is not None and not self.task.done()

    def speak(self, text):
        def start():
            if self.busy:
                self.task.cancel()
            r = Reading(self.voice, text, self.on_state)
            self.task = self.loop.create_task(r.run())
        self.loop.call_soon_threadsafe(start)

    def stop(self):
        def _stop():
            if self.busy:
                self.task.cancel()
        self.loop.call_soon_threadsafe(_stop)

    def set_voice(self, voice=None, rate=None):
        global VOICE, RATE
        if voice:
            VOICE = voice
        if rate:
            RATE = rate
