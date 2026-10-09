# typed: true

"""Deterministic fake Microsoft edge-tts-compatible websocket server.

Each connection speaks exactly like the real endpoint the app expects:
  1. receive speech.config TEXT frame (we ignore body)
  2. receive SSML TEXT frame
  3. send the chunk's pre-encoded audio BINARY packets, with optional timing
     delays to simulate real network behaviour
  4. send a turn.end TEXT frame and close

The audio bytes come from tools/fake_server_codec.py (one loud 1.0s 48kbps
MP3 per chunk), so the app decodes them with the same miniaudio path it uses
for the real service.  Recordings under TapDevice are therefore honest.

Scenarios model the conditions we measured in the real service and that the
user is hitting.  Each scenario is run end-to-end by
tools/ctl_scenario.py and measured for audible pauses.

Usage:  python tools/fake_server.py --scenario <name> [--port PORT]
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import List, Optional

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)
os.chdir(BASE)

logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s fake-server %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("fake-server")

from aiohttp import WSMsgType
from aiohttp.web import Request
from aiohttp.web_runner import AppRunner, TCPSite
from aiohttp import web

from tools import fake_server_codec as codec

# scenario text chunks; each one becomes one audio chunk the app fetches
CHUNKS = [
    "Read Aloud turns whatever you have selected into speech using Microsoft Edge voices.",
    "Select any text in any app and the floating button reads it back to you.",
    "The quick brown fox jumps over the lazy dog.",
    "Sphinx of black quartz judge my vow.",
    "How vexingly quick daft zebras jump.",
    "The five boxing wizards jump quickly.",
]


class Scenario:
    """How a single chunk's packets are delivered to the client.

    gap_s       - seconds to wait before sending any bytes of this chunk
    stall_at_ms - if set, after this many ms of audio-equivalent progress,
                  wait stall_len_ms before continuing (a mid-chunk stall)
    stall_len_ms- length of the mid-chunk stall
    drop_after  - if set, stop sending after this many packets (dead connection)
    echo_until  - if set, pretend to be a warm spare that hears the SSML but
                  emits nothing until this many seconds after handshake (then
                  sends the real audio, reproducing a cold spare racing a warm
                  empty one)
    """

    def __init__(self, name: str, pkts: List[bytes],
                 gap_s: float = 0.0,
                 stall_at_ms: Optional[int] = None,
                 stall_len_ms: int = 0,
                 drop_after: Optional[int] = None,
                 echo_until: float = 0.0,
                 label: str = ""):
        self.name = name
        self.pkts = pkts
        self.gap_s = gap_s
        self.stall_at_ms = stall_at_ms
        self.stall_len_ms = stall_len_ms
        self.drop_after = drop_after
        self.echo_until = echo_until
        self.label = label or name

    async def send(self, ws, chunk_idx: int, t0: float, started_evt: Optional[asyncio.Event] = None):
        log.info("conn chunk %d %s: %d pkts gap=%.2fs stall=%s drop=%s echo=%.2fs",
                 chunk_idx, self.label, len(self.pkts),
                 self.gap_s,
                 f"{self.stall_at_ms}-{self.stall_at_ms + self.stall_len_ms}ms"
                 if self.stall_at_ms is not None else "n/a",
                 self.drop_after, self.echo_until)
        await asyncio.sleep(self.gap_s)
        # Stall applies once per chunk, mid-stream (edge-tts stalls are network
        # pauses, not per-packet); drop_after cuts the chunk short.
        half = max(1, len(self.pkts) // 2)
        for i, pkt in enumerate(self.pkts):
            if started_evt is not None and not started_evt.is_set():
                if time.perf_counter() - t0 < self.echo_until:
                    continue
                else:
                    started_evt.set()
            if self.stall_at_ms is not None and i == half:
                await asyncio.sleep(self.stall_len_ms / 1000)
            if self.drop_after is not None and i >= self.drop_after:
                log.info("chunk %d dropping after pkt %d", chunk_idx, i)
                break
            await ws.send_bytes(pkt)


class FakeServer:
    """One endpoint. The app opens one websocket per chunk request:
    config TEXT, then SSML TEXT; we reply with that chunk's audio + turn.end.
    Each connection advances the scenario sequence by one chunk.
    """

    def __init__(self, scenario_names: List[str], chunks: int = 6):
        self.scenarios: List[Scenario] = []
        self.chunks = min(chunks, len(CHUNKS))
        for name in scenario_names:
            for i in range(self.chunks):
                pkts = [codec.chunk_mp3(CHUNKS[i])]   # one packet = whole chunk
                self.scenarios.append(_scenario_for(name, pkts, i))
        self.next = 0  # global sequence across connections

    async def handle(self, request: Request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        t0 = time.perf_counter()
        idx = self.next % len(self.scenarios)
        self.next += 1
        sc = self.scenarios[idx]
        got_ssml = False
        try:
            async for msg in ws:
                if msg.type == WSMsgType.TEXT:
                    if "Path:speech.config" in msg.data:
                        continue          # handshake, no reply
                    got_ssml = True       # SSML frame -> speak one chunk
                    break
                elif msg.type not in (WSMsgType.BINARY, WSMsgType.PING,
                                      WSMsgType.PONG):
                    break
            if got_ssml:
                await sc.send(ws, idx, t0)
                await ws.send_str("X-Timestamp:1\r\nContent-Type:application/json; "
                                  "charset=utf-8\r\nPath:turn.end\r\n\r\n{}")
        except asyncio.CancelledError:
            raise
        except Exception as e:
            log.warning("connection error: %s", e)
        finally:
            try:
                await ws.close()
            except Exception:
                pass
        return ws


def _scenario_for(name: str, pkts: List[bytes], chunk_idx: int) -> Scenario:
    base = dict(pkts=pkts)
    if name == "fast":
        return Scenario("fast", **base)
    if name == "first_chunk_slow":
        return Scenario("first_chunk_slow", gap_s=(2.0 if chunk_idx == 0 else 0.0),
                        **base)
    if name == "inter_chunk_gap":
        return Scenario("inter_chunk_gap", gap_s=(1.4 if chunk_idx > 0 else 0.0),
                        **base)
    if name == "mid_chunk_stall":
        return Scenario("mid_chunk_stall",
                        stall_at_ms=(500 if chunk_idx == 1 else None),
                        stall_len_ms=(900 if chunk_idx == 1 else 0),
                        **base)
    if name == "idle_echo_stall":
        return Scenario("idle_echo_stall",
                        echo_until=(2.0 if chunk_idx == 0 else 0.0),
                        **base)
    if name == "spare_drops":
        return Scenario("spare_drops",
                        drop_after=(max(1, len(pkts)//2) if chunk_idx == 1 else None),
                        **base)
    return Scenario(name, **base)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--scenario", action="append", default=[],
                   help="scenario name (repeatable); default fast")
    p.add_argument("--port", type=int, default=9876)
    p.add_argument("--chunks", type=int, default=6)
    args = p.parse_args()
    names = args.scenario or ["fast"]
    srv = FakeServer(names, args.chunks)
    app = web.Application()
    app.router.add_get("/", srv.handle)
    runner = AppRunner(app)
    loop = asyncio.get_event_loop()
    loop.run_until_complete(runner.setup())
    site = TCPSite(runner, "127.0.0.1", args.port)
    loop.run_until_complete(site.start())
    log.info("listening on ws://127.0.0.1:%d", args.port)
    try:
        loop.run_forever()
    finally:
        loop.run_until_complete(runner.cleanup())


if __name__ == "__main__":
    main()
