"""CEO gates G2 and G3: every character is accounted for, faults never hang.

G2: 500 seeded fault reads -> 100% of cleaned chars in exactly one ledger row,
    0 silent drops/duplicates, no run over its deadline + 1 s, none stuck for
    2 s, never more than one live player.
G3: 100 no-fault reads -> 100% SPOKEN, 0 PARTIAL/SKIPPED/error.
"""
import asyncio
import random
import time

import pytest

from readaloud import session as S
from tests.fakes import FakeEngine, FakeLocal, fast_session

WORDS = ("the quick brown fox jumps over a lazy dog while sunlight falls on "
         "quiet gardens and small birds gather near the old stone wall").split()

FAULT_KINDS = ("fail", "edge_fail", "trunc", "stall", "bad_decode")


def make_text(rng, sentences):
    parts = []
    for _ in range(sentences):
        n = rng.randint(4, 13)
        words = [rng.choice(WORDS) for _ in range(n)]
        words[0] = words[0].capitalize()
        parts.append(" ".join(words) + rng.choice((".", "!", "?", ",")))
    return " ".join(parts)


def check_ledger(session, engine, factory, states, elapsed, label):
    summary = engine.last_ledger
    text = session.text
    rows = session.ledger.rows

    # exact partition of clean(text): every char in exactly one row
    assert rows[0]["start"] == 0, label
    assert rows[-1]["end"] == len(text), label
    for prev, row in zip(rows, rows[1:]):
        assert row["start"] == prev["end"], f"{label}: gap/overlap at {row['start']}"
    for row in rows:
        assert row["status"] is not None, f"{label}: unaccounted row {row['i']}"
        atom_text = session.atoms[row["i"]].text
        assert atom_text == text[row["start"]:row["end"]].strip(), label
    assert (summary["spoken"] + summary["partial"] + summary["skipped"]
            + summary["cancelled"]) == summary["atoms"], label
    assert (summary["spoken_chars"] + summary["partial_chars"]
            + summary["skipped_chars"] + summary["cancelled_chars"]
            ) == summary["total_chars"], label

    # every loss is recorded with its offsets and a reason, never silent
    for row in summary["offsets"]:
        assert row["reason"], f"{label}: loss without reason {row}"
    for row in rows:
        if row["status"] in (S.PARTIAL, S.SKIPPED):
            assert any(o["start"] == row["start"] and o["status"] == row["status"]
                       for o in summary["offsets"]), label
            assert row["reason"], label
    assert summary["complete"] == (summary["spoken"] == summary["atoms"])

    # C3/C6 state rules
    assert summary["end"] in ("idle", "error"), label
    if summary["end"] == "error":
        assert summary["warning"] == "", label
    else:
        assert (summary["warning"] != "") == bool(summary["partial"]
                                                  or summary["skipped"]), label
    if summary["partial"] or summary["skipped"]:
        if factory.total_played == 0:
            assert summary["end"] == "error", f"{label}: loss with no audio"
    assert factory.live == 0, f"{label}: player left open"
    assert factory.max_live <= 1, f"{label}: {factory.max_live} live players"

    assert elapsed <= session._hard_deadline() + 1.0, (
        f"{label}: {elapsed:.2f}s over deadline {session._hard_deadline():.2f}s")
    assert elapsed < 2.0, f"{label}: stuck {elapsed:.2f}s"
    assert states and states[-1][0] in ("idle", "error"), label


async def _fault_read(rng, monkeypatch, index):
    offline_ready = rng.random() > 0.2
    consume = rng.random() < 0.5
    text = make_text(rng, rng.randint(3, 6))
    engine = FakeEngine(local=FakeLocal(ready=offline_ready))
    session, factory, states = fast_session(
        monkeypatch, engine, text, consume=consume)

    if index % 50 == 0:                       # seeded total-outage reads
        engine.local.ready = False
        for atom in session.atoms:
            engine.voice.behaviour[atom.text] = "edge_fail"
        start = time.monotonic()
        await asyncio.wait_for(session.run(), 10)
        return session, factory, states, time.monotonic() - start, engine

    victims = rng.sample(range(len(session.atoms)),
                         k=min(len(session.atoms), rng.choice((0, 0, 1, 1, 2, 3))))
    for i in victims:
        engine.voice.behaviour[session.atoms[i].text] = rng.choice(FAULT_KINDS)
    if consume and rng.random() < 0.4:
        # a mid-atom failure that has already produced sound: PARTIAL, no replay
        engine.voice.behaviour[
            session.atoms[rng.choice(range(len(session.atoms)))].text] = "trunc"

    start = time.monotonic()
    await asyncio.wait_for(session.run(), 10)
    elapsed = time.monotonic() - start
    return session, factory, states, elapsed, engine


def test_g2_500_seeded_fault_reads(monkeypatch):
    rng = random.Random(20261026)
    totals = {"reads": 0, "spoken": 0, "partial": 0, "skipped": 0,
              "idle": 0, "error": 0}
    slowest = 0.0
    for n in range(500):
        session, factory, states, elapsed, engine = asyncio.run(
            _fault_read(rng, monkeypatch, n))
        check_ledger(session, engine, factory, states, elapsed, f"g2#{n}")
        summary = engine.last_ledger
        totals["reads"] += 1
        totals["spoken"] += summary["spoken"]
        totals["partial"] += summary["partial"]
        totals["skipped"] += summary["skipped"]
        totals["idle"] += summary["end"] == "idle"
        totals["error"] += summary["end"] == "error"
        slowest = max(slowest, elapsed)
    # the gate has teeth only if faults really happened
    assert totals["partial"] + totals["skipped"] > 0
    assert totals["error"] > 0
    assert totals["idle"] > 0
    print(f"G2 {totals} slowest={slowest:.2f}s")


async def _clean_read(rng, monkeypatch):
    text = make_text(rng, rng.randint(3, 8))
    engine = FakeEngine(local=FakeLocal(ready=True))
    session, factory, states = fast_session(monkeypatch, engine, text)
    start = time.monotonic()
    await asyncio.wait_for(session.run(), 10)
    return session, factory, states, time.monotonic() - start


def test_g3_100_clean_reads(monkeypatch):
    rng = random.Random(777)
    for n in range(100):
        session, factory, states, elapsed = asyncio.run(
            _clean_read(rng, monkeypatch))
        summary = session.engine.last_ledger
        label = f"g3#{n}"
        assert summary["complete"], f"{label}: {summary}"
        assert summary["spoken"] == summary["atoms"] > 0, label
        assert summary["partial"] == summary["skipped"] == 0, label
        assert summary["cancelled"] == 0, label
        assert summary["end"] == "idle" and summary["warning"] == "", label
        assert factory.total_played > 0, label
        assert [s[0] for s in states] == ["loading", "speaking", "idle"], (
            f"{label}: {states}")
        assert elapsed <= session._hard_deadline() + 1.0, label
        assert elapsed < 2.0, label
        assert factory.max_live == 1 and factory.live == 0, label


def test_every_atom_gets_audio_once(monkeypatch):
    """No duplicate audio: each atom's token is played at most once."""
    async def run():
        engine = FakeEngine(local=FakeLocal(ready=True))
        text = make_text(random.Random(5), 6)
        session, factory, states = fast_session(monkeypatch, engine, text)
        await asyncio.wait_for(session.run(), 10)
        return session, factory

    session, factory = asyncio.run(run())
    played = factory.players[-1]._played
    assert sorted(played) == list(range(len(session.atoms)))
    assert all(session.player.played(i) > 0 for i in range(len(session.atoms)))
