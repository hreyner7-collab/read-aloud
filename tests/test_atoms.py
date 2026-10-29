"""Atom splitter: exact partition, sentence-sized atoms, C1 limits."""
import random

import pytest

from readaloud.atoms import Atom, clean, split_atoms

VOCAB = ("alpha bravo charlie delta echo foxtrot golf hotel india juliet "
         "kilo lima mike november oscar papa quebec romeo sierra tango").split()
PUNCT = (".", ".", ".", "!", "?", ";", ",", ":", "\u2014")


def random_text(rng, pieces):
    out = []
    for _ in range(pieces):
        kind = rng.random()
        if kind < 0.08:
            out.append(rng.choice(["Dr.", "Mrs.", "e.g.", "i.e.", "U.S.", "etc."]))
            out.append(rng.choice(VOCAB) + ".")
        elif kind < 0.16:
            out.append(f"{rng.randint(0, 9)}.{rng.randint(0, 99)}")
            out.append(rng.choice(VOCAB) + rng.choice(PUNCT))
        elif kind < 0.22:
            out.append(rng.choice(['"Wait...', "He said \u201cno...\u201d",
                                   "(really?)", "[sic]"]))
            out.append(rng.choice(VOCAB) + ".")
        elif kind < 0.28:
            out.append(rng.choice(["\u4f60\u597d\u3002", "\u4eca\u5929\u5f88\u597d\uff01",
                                   "\u53bb\u516c\u56ed\u5427\uff1f", "\u8c22\u8c22\u3002"]))
        elif kind < 0.34:
            out.append(rng.choice(["\U0001F600", "\U0001F469\u200D\U0001F4BB",
                                   "\U0001F1EC\U0001F1E7", "1\uFE0F\u20E3"]))
            out.append(rng.choice(VOCAB) + rng.choice(PUNCT))
        elif kind < 0.40:
            out.append("See https://example.com/" + rng.choice(VOCAB) + "?a=1 now.")
        elif kind < 0.46:
            out.append("\n\n")
        else:
            words = [rng.choice(VOCAB) for _ in range(rng.randint(1, 20))]
            words[0] = words[0].capitalize()
            out.append(" ".join(words) + rng.choice(PUNCT))
    return " ".join(out)


def check(text):
    atoms = split_atoms(text)
    if not text:
        assert atoms == []
        return atoms
    assert atoms, f"no atoms for {text[:80]!r}"
    assert atoms[0].start == 0
    assert atoms[-1].end == len(text)
    for prev, atom in zip(atoms, atoms[1:]):
        assert atom.start == prev.end, "gap or overlap"
    for atom in atoms:
        assert isinstance(atom, Atom)
        assert atom.start < atom.end
        assert atom.text == text[atom.start:atom.end].strip()
        assert 0 < len(atom.text) <= 320, f"{len(atom.text)} chars: {atom.text[:60]!r}"
    assert len(atoms[0].text) <= 100, f"first atom {len(atoms[0].text)} chars"
    # no clause atoms: a sentence <= 320 chars is never cut mid-sentence.
    # The first, long sentence is the one allowed exception (fast start).
    from readaloud.atoms import _sentence_spans
    spans = _sentence_spans(text)
    first_span_end = spans[0][1]
    first_long = first_span_end - spans[0][0] > 100
    for atom in atoms:
        span_start, span_end = _span(text, atom.start, spans)
        if span_end - span_start > 320:
            continue
        if first_long and atom.start < first_span_end:
            continue
        assert atom.start == span_start, f"mid-sentence start in {atom.text[:40]!r}"
        assert atom.end == span_end, f"mid-sentence end in {atom.text[:40]!r}"
    return atoms


def _span(text, pos, spans=None):
    if spans is None:
        from readaloud.atoms import _sentence_spans
        spans = _sentence_spans(text)
    for start, end in spans:
        if start <= pos < end:
            return start, end
    return pos, len(text)


def test_500_seeded_texts_partition_exactly():
    rng = random.Random(424242)
    for n in range(500):
        raw = random_text(rng, rng.randint(1, 12))
        text = clean(raw)
        check(text)


@pytest.mark.parametrize("raw", [
    "",
    "   ",
    "\n\n\t ",
    "a",
    "Hello.",
    "Hello",
    "One sentence only, with a comma, and more words to fill it out.",
    "word " * 3000,
    "word " * 200 + "end.",
    "x" * 5000,
    "\u4f60\u597d\u3002" * 200,
    "\U0001F469\u200D\U0001F4BB" * 300,
    "Dr. Smith met Mrs. Jones at 3.14 p.m. on Jan. 5. They talked.",
    "She said \"Wait...\" and left. Then it rained.",
    "Wait... what happened? Then silence.",
    "Price is 1,234.56 dollars. That is all!",
    "A" * 100 + ".",
    "A" * 101 + ".",
    "A" * 320 + ".",
    "A" * 321 + ".",
    "One.\n\nTwo.\nThree.\n\n\nFour.",
    "\u201cQuoted sentence.\u201d Next one?",
    "See https://example.com/a?b=c now. Then stop.",
    "No terminator here at all " * 40,
])
def test_edge_texts(raw):
    check(clean(raw))


def test_long_first_sentence_is_cut_before_the_hard_limit():
    text = " ".join(["Long first sentence filler"] * 200)
    atoms = check(text)
    assert len(atoms) > 1
    assert len(atoms[0].text) <= 80
    assert not atoms[0].text.lower().endswith("and")


def test_first_atom_prefers_a_clause_boundary():
    text = ("When the quick brown fox jumps over the lazy dog in the quiet "
            "garden, it stops to rest for a while today. Then it runs away.")
    atoms = split_atoms(clean(text))
    assert atoms[0].text.endswith(",")
    assert len(atoms[0].text) <= 80


def test_abbreviations_and_decimals_do_not_end_sentences():
    atoms = split_atoms(clean("Dr. Smith paid 3.14 dollars. He left."))
    assert atoms[0].text.startswith("Dr. Smith paid 3.14 dollars.")
    atoms = split_atoms(clean("The U.S. team won. They cheered."))
    assert atoms[0].text == "The U.S. team won."
    atoms = split_atoms(clean("Use e.g. this one. Not that."))
    assert atoms[0].text == "Use e.g. this one."


def test_ellipsis_and_quotes():
    atoms = split_atoms(clean('She said "Wait..." and left. Then it rained.'))
    assert len(atoms) == 2
    assert atoms[0].text.endswith("and left.")
    atoms = split_atoms(clean("Wait... what happened? Then silence."))
    assert len(atoms) == 2


def test_cjk_terminators():
    atoms = split_atoms(clean("\u4f60\u597d\u3002\u4eca\u5929\u5f88\u597d\uff01"
                              "\u53bb\u516c\u56ed\u5427\uff1f"))
    assert [a.text for a in atoms] == ["\u4f60\u597d\u3002", "\u4eca\u5929\u5f88\u597d\uff01",
                                       "\u53bb\u516c\u56ed\u5427\uff1f"]


def test_no_split_inside_joined_emoji():
    family = "\U0001F469\u200D\U0001F469\u200D\U0001F467\u200D\U0001F466"
    text = (family + " ") * 120
    atoms = check(text)
    for atom in atoms:
        assert not atom.text.endswith("\u200d")


def test_clean_removes_urls_and_collapses_space():
    assert clean("See https://x.test/a?b=1 here") == "See link here"
    assert clean("a\t b\u00a0 c") == "a b c"
    assert clean("a\n\n\n\nb") == "a\n\nb"
