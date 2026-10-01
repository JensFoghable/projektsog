"""Text normalisation shared by the indexer, search and Resolve mapping.

``fold()`` turns a file/folder name or a query into a lower-case, accent-free,
separator-normalised form so that e.g. "Forår 2026 RØD", "forar 2026 rod" and
"FORAAR_2026-RØD" all compare equal.  The index stores ``fold(name)`` and every
query token is folded the same way, so matching is a plain substring test.

``fold_with_map()`` is the slow reference implementation that also returns, for every
folded character, the index of the original character it came from.  It is only used
to compute highlight ranges for the (few) results that are displayed.  Both functions
MUST produce identical folded strings (covered by tests/test_textutil.py).

``alt()`` is a second, looser normalisation applied on top of ``fold()``: it turns the
ASCII spelling "oe" of "ø" into "o" ("Infomoede" ~ "infomøde", "boegely" ~ "Bøgely").
It is NOT part of ``fold()`` because dropping the "e" would break substring matches in
Danish compounds ("Videoeksport" must still match "eksport").  Matching rule used by
the search (SPEC §7): a token ``t`` matches a name ``n`` when
``t in fold(n)`` **or** ``alt(t) in alt(fold(n))``.
"""

from __future__ import annotations

import re
import unicodedata

# Characters that NFKD does not decompose into base letter + accent.
_SPECIAL = {
    "æ": "ae",
    "ø": "o",
    "å": "a",  # (NFKD would also do this; kept explicit for speed/clarity)
    "ß": "ss",
    "œ": "oe",
    "ð": "d",
    "þ": "th",
    "ł": "l",
    "đ": "d",
    "ı": "i",
}
_TRANS = str.maketrans(_SPECIAL)

# Punctuation that separates words in file names.  Runs of these (and whitespace)
# collapse to a single space.
SEPARATOR_CHARS = "_-.,;:()[]{}&+'\"´`!?#@/\\|~^=<>%$€*’‘“”–—·•"
_SEP_SET = frozenset(SEPARATOR_CHARS)
_SEP_RE = re.compile("[\\s" + re.escape(SEPARATOR_CHARS) + "]+")


def _strip_marks(s: str) -> str:
    s = unicodedata.normalize("NFKD", s)
    return "".join(ch for ch in s if not unicodedata.combining(ch))


def fold(s: str) -> str:
    """Fast fold used for indexing and queries."""
    if not s:
        return ""
    s = s.casefold()
    if not s.isascii():
        s = s.translate(_TRANS)
        if not s.isascii():
            s = _strip_marks(s)
    if "aa" in s:  # Danish: "aa" == "å" -> both fold to "a"
        s = s.replace("aa", "a")
    return _SEP_RE.sub(" ", s).strip()


def fold_with_map(s: str) -> tuple[str, list[int]]:
    """Reference fold that also returns ``index_map[i]`` = index in ``s`` of folded char i."""
    chars: list[str] = []
    origin: list[int] = []
    for i, ch in enumerate(s):
        t = ch.casefold().translate(_TRANS)
        if not t.isascii():
            t = _strip_marks(t)
        for c in t:
            chars.append(c)
            origin.append(i)

    # "aa" -> "a" (non-overlapping, left to right: same semantics as str.replace)
    chars2: list[str] = []
    origin2: list[int] = []
    j = 0
    n = len(chars)
    while j < n:
        if chars[j] == "a" and j + 1 < n and chars[j + 1] == "a":
            chars2.append("a")
            origin2.append(origin[j])
            j += 2
        else:
            chars2.append(chars[j])
            origin2.append(origin[j])
            j += 1

    # collapse separator runs to one space, strip both ends
    out: list[str] = []
    out_map: list[int] = []
    pending_space_at = -1
    for c, o in zip(chars2, origin2):
        if c.isspace() or c in _SEP_SET:
            if pending_space_at < 0:
                pending_space_at = o
            continue
        if pending_space_at >= 0 and out:
            out.append(" ")
            out_map.append(pending_space_at)
        pending_space_at = -1
        out.append(c)
        out_map.append(o)
    return "".join(out), out_map


def alt(folded: str) -> str:
    """Looser variant of an already folded string: ASCII "oe" (for ø) → "o"."""
    return folded.replace("oe", "o") if "oe" in folded else folded


def fold_alt(s: str) -> str:
    """``alt(fold(s))`` – stored as ``entries.name_alt`` when it differs from ``fold(s)``."""
    return alt(fold(s))


def _alt_with_map(folded: str, index_map: list[int]) -> tuple[str, list[int]]:
    """Apply ``alt()`` to a folded string while keeping the origin map aligned."""
    if "oe" not in folded:
        return folded, index_map
    out: list[str] = []
    out_map: list[int] = []
    j = 0
    n = len(folded)
    while j < n:
        if folded[j] == "o" and j + 1 < n and folded[j + 1] == "e":
            out.append("o")
            out_map.append(index_map[j])
            j += 2
        else:
            out.append(folded[j])
            out_map.append(index_map[j])
            j += 1
    return "".join(out), out_map


def token_matches(token: str, name_fold: str, name_alt: str | None = None) -> bool:
    """SPEC §7 matching rule for one folded token against one folded name."""
    if token in name_fold:
        return True
    t_alt = alt(token)
    n_alt = name_alt if name_alt is not None else alt(name_fold)
    return t_alt in n_alt


def tokenize(query: str) -> list[str]:
    """Fold a user query and split it into unique tokens (order preserved)."""
    seen: set[str] = set()
    tokens: list[str] = []
    for t in fold(query).split(" "):
        if t and t not in seen:
            seen.add(t)
            tokens.append(t)
    return tokens


def highlight_ranges(name: str, tokens: list[str]) -> list[list[int]]:
    """Return merged ``[start, end)`` ranges in the ORIGINAL ``name`` matched by tokens."""
    if not tokens or not name:
        return []
    folded, index_map = fold_with_map(name)
    folded_alt: str | None = None
    alt_map: list[int] = []
    spans: list[tuple[int, int]] = []
    for tok in tokens:
        if not tok:
            continue
        text, mapping, needle = folded, index_map, tok
        if tok not in folded:
            # Fall back to the looser "oe" ~ "ø" comparison (see module docstring).
            if folded_alt is None:
                folded_alt, alt_map = _alt_with_map(folded, index_map)
            text, mapping, needle = folded_alt, alt_map, alt(tok)
            if not needle:
                continue
        start = 0
        while True:
            k = text.find(needle, start)
            if k < 0:
                break
            a = mapping[k]
            b = mapping[k + len(needle) - 1] + 1
            spans.append((a, b))
            start = k + 1
    if not spans:
        return []
    spans.sort()
    merged: list[list[int]] = [list(spans[0])]
    for a, b in spans[1:]:
        if a <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], b)
        else:
            merged.append([a, b])
    return merged
