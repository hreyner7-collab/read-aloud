"""Turn text into sentence-sized atoms that tile it exactly.

An atom is one sentence (a terminator plus any closing quote/bracket).  The
first atom is shorter so sound starts sooner.  Every atom carries offsets into
`clean(text)`; the slices concatenate back to the whole cleaned text, so the
read ledger can account for every character (no silent drops).
"""
import re
from dataclasses import dataclass

TERMINATORS = ".!?\u2026"
CJK_TERMINATORS = "\u3002\uff01\uff1f"       # 。！？
CLOSERS = "\"'\u201d\u2019\u00bb)]}\u300d\u300f\u3011\uff09"
ZWJ = "\u200d"
COMBINING = "\u0300-\u036f\ufe0e\ufe0f\u20e3"

# A '.' after one of these words is not the end of a sentence.
ABBREVIATIONS = {
    "mr", "mrs", "ms", "dr", "prof", "sr", "jr", "st", "vs", "etc", "fig",
    "figs", "no", "nos", "inc", "ltd", "co", "corp", "dept", "est", "approx",
    "cf", "al", "eq", "ref", "vol", "pp", "p", "ch", "sec", "min", "max",
    "jan", "feb", "mar", "apr", "jun", "jul", "aug", "sep", "sept", "oct",
    "nov", "dec", "mon", "tue", "wed", "thu", "fri", "sat", "sun",
    "e.g", "i.e", "u.s", "u.k", "a.m", "p.m",
}
_MULTI_INITIAL = re.compile(r"^(?:[A-Za-z]\.)+[A-Za-z]?$")
_LONG_WORD = re.compile(r"\S+$")


@dataclass(frozen=True)
class Atom:
    start: int
    end: int
    text: str

    def __len__(self):
        return len(self.text)


def clean(text):
    """Same normalisation as before: no URLs, single spaces, no blank runs."""
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"https?://\S+", "link", text)
    text = re.sub(r"[ \t\u00a0]+", " ", text)
    text = re.sub(r"\n\s*\n+", "\n\n", text)
    return text.strip()


def _is_abbreviation(text, dot_start, dot_end):
    """True if the full stop(s) at [dot_start, dot_end) do not end a sentence."""
    if text[dot_start] != "." or dot_end - dot_start != 1:
        return False                      # an ellipsis run may end a sentence
    a = dot_start
    while a > 0 and (text[a - 1].isalnum() or text[a - 1] in ".&"):
        a -= 1
    token = text[a:dot_start]
    low = token.lower().strip(".")
    if low in ABBREVIATIONS:
        return True
    if _MULTI_INITIAL.match(token):
        return True                       # U.S / e.g / J.K
    # A single capital letter before the dot is an initial when a capitalised
    # word follows ("J. Smith"), which is never a sentence break.
    if len(token.strip(".")) == 1 and token[:1].isupper():
        k = dot_end
        while k < len(text) and text[k].isspace():
            k += 1
        if k < len(text) and text[k].isupper():
            return True
    return False


def _safe_cut(text, cut, start):
    """Move a hard cut back so it never splits a grapheme-ish sequence."""
    while cut > start + 1:
        ch = text[cut] if cut < len(text) else ""
        if ch and (ch == ZWJ or re.match(f"[{COMBINING}]", ch)):
            cut -= 1
            continue
        if text[cut - 1] == ZWJ:
            cut -= 1
            continue
        break
    return cut


def _sentence_spans(text):
    spans, start, i, n = [], 0, 0, len(text)
    while i < n:
        ch = text[i]
        if ch in TERMINATORS:
            j = i + 1
            while j < n and text[j] in ".\u2026":
                j += 1
            k = j
            while k < n and text[k] in CLOSERS:
                k += 1
            if k >= n or text[k].isspace() or text[k] in CJK_TERMINATORS:
                # A lower-case word after the full stop is a continuation
                # ("Wait..." and left.), not a new sentence.
                m = k
                while m < n and text[m] not in CJK_TERMINATORS and text[m].isspace():
                    m += 1
                lower = m < n and text[m].islower()
                if not lower and not _is_abbreviation(text, i, j):
                    spans.append((start, k))
                    start = i = k
                    continue
            i = j
            continue
        if ch in CJK_TERMINATORS:
            k = i + 1
            while k < n and text[k] in CLOSERS:
                k += 1
            spans.append((start, k))
            start = i = k
            continue
        i += 1
    if start < n:
        spans.append((start, n))
    return spans


def _is_and(text, cut):
    k = cut
    while k < len(text) and text[k].isspace():
        k += 1
    return text[k:k + 4].lower() == "and " or text[k:k + 3].lower() == "and"


def _first_cut(text, start, end, limit):
    """Largest natural cut <= start+limit at , ; : — then a space; never 'and'."""
    hard = min(start + limit, end)
    for p in range(hard, start, -1):
        if text[p - 1] in ",;:\u2014" and (p >= end or text[p].isspace()):
            return p
    for p in range(hard, start, -1):
        if text[p - 1].isspace() and not _is_and(text, p) and p - start > 1:
            return p
    return _safe_cut(text, hard, start)


def _split_long(text, start, end, max_len):
    """Split [start, end) at ; : — , then spaces so every piece is <= max_len."""
    pieces, pos = [], start
    while len(text[pos:end].strip()) > max_len:
        hard = min(pos + max_len, end)
        cut = 0
        for p in range(hard, pos, -1):
            if text[p - 1] in ",;:\u2014" and (p >= end or text[p].isspace()):
                cut = p
                break
        if not cut:
            for p in range(hard, pos, -1):
                if text[p - 1].isspace() and not _is_and(text, p) and p - pos > 1:
                    cut = p
                    break
        if not cut:
            cut = _safe_cut(text, hard, pos)
        cut = max(cut, pos + 1)
        pieces.append((pos, cut))
        pos = cut
    if pos < end:
        pieces.append((pos, end))
    return pieces


def split_atoms(text, first_max=100, first_cut=80, max_len=320):
    """Atoms that exactly partition `text` (already cleaned)."""
    spans = _sentence_spans(text)
    if not spans:
        return []
    atoms = []
    first_start, first_end = spans[0][0], (spans[1][0] if len(spans) > 1 else len(text))
    if len(text[first_start:first_end].strip()) > first_max:
        cut = _first_cut(text, first_start, first_end, first_cut)
        atoms.append((first_start, cut))
        rest_start = cut
    else:
        rest_start = first_start
    tiles = [(rest_start, first_end)] + [
        (spans[i][0], spans[i + 1][0] if i + 1 < len(spans) else len(text))
        for i in range(1, len(spans))]
    for a, b in tiles:
        if a >= b:
            continue
        atoms.extend(_split_long(text, a, b, max_len))
    out = []
    for a, b in atoms:
        spoken = text[a:b].strip()
        if spoken:
            out.append(Atom(a, b, spoken))
    return out


def atom_key(atom):
    """What the caches are keyed on; never the text itself on disk."""
    return atom.text
