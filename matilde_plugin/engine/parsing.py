"""Bibliography parsing — turn BibTeX and loose DOI lists into ``Reference``s.

Stdlib-only (no ``bibtexparser`` dependency) so the engine stays import-light. The
BibTeX parser is brace-balanced and handles the common field shapes
(``field = {value}`` / ``"value"`` / bareword) and the ``A and B and C`` author
convention. It is not a full BibTeX grammar — it targets real-world reference
lists, not every edge of the format.
"""
from __future__ import annotations

import re
import unicodedata

from .citations import Reference, _normalize_doi

# LaTeX accent commands -> the Unicode combining mark they apply to the next letter.
# BibTeX has no other way to write a non-ASCII name, so real .bib files are full of
# these: M\"uller, Nu\~nez, Erd\H{o}s, Luk\'{a}cs, \v{S}koda, Fran\c{c}ois.
_LATEX_COMBINING = {
    '"': "\u0308",   # diaeresis      \"u -> ü
    "'": "\u0301",   # acute          \'e -> é
    "`": "\u0300",   # grave          \`a -> à
    "^": "\u0302",   # circumflex     \^o -> ô
    "~": "\u0303",   # tilde          \~n -> ñ
    "=": "\u0304",   # macron         \=a -> ā
    ".": "\u0307",   # dot above      \.z -> ż
    "u": "\u0306",   # breve          \u{a} -> ă
    "v": "\u030C",   # caron          \v{s} -> š
    "H": "\u030B",   # double acute   \H{o} -> ő
    "r": "\u030A",   # ring above     \r{a} -> å
    "c": "\u0327",   # cedilla        \c{c} -> ç
    "k": "\u0328",   # ogonek         \k{a} -> ą
    "d": "\u0323",   # dot below      \d{h} -> ḥ
    "b": "\u0331",   # macron below   \b{h} -> ẖ
}

# LaTeX commands for letters that are not "base + accent" at all. These must be
# substituted *before* the accent pass, longest first, or \dh (eth) would be read
# as \d applied to "h".
_LATEX_LETTERS = {
    "ss": "ß", "ae": "æ", "AE": "Æ", "oe": "œ", "OE": "Œ",
    "aa": "å", "AA": "Å", "dh": "ð", "DH": "Ð", "th": "þ", "TH": "Þ",
    "dj": "đ", "DJ": "Đ", "ng": "ŋ", "NG": "Ŋ",
    "o": "ø", "O": "Ø", "l": "ł", "L": "Ł",
    "i": "i", "j": "j",  # dotless forms; the dot is irrelevant once accented
}

# \ss, \o, ... : a control word swallows the whitespace that terminates it, which
# is why "S\o rensen" is one word ("Sørensen") and not two.
_LATEX_LETTER_RE = re.compile(
    r"\\(" + "|".join(sorted(_LATEX_LETTERS, key=len, reverse=True))
    + r")(?![A-Za-z])\s*(?:\{\})?")

# \"u  \"{u}  \c c  \v{s}  \'{\i}  — optional braces, optional separating space.
_LATEX_ACCENT_RE = re.compile(
    r"\\(?P<acc>[\"'`^~=.]|[uvHrckdb](?=[\s{]))"
    r"\s*(?:\{\s*(?P<braced>\\?[A-Za-z])\s*\}|(?P<bare>\\?[A-Za-z]))")


def _decode_latex_accents(s: str) -> str:
    r"""Decode the common LaTeX accent/letter escapes into real Unicode.

    ``author={M\"uller, Hans and S\o rensen, Ida}`` used to survive brace-stripping
    as ``M uller`` / ``S o rensen``, whose "surnames" were ``uller`` and
    ``rensen`` — so a perfectly correct citation by German or Danish authors came
    back with a false "author overlap low (0%)" warning.

    The decoded text keeps the diacritic (``Müller``, not ``Muller``): the parsed
    reference must preserve the author's own spelling, because it is what we show
    the user and what they may paste back into a manuscript. ASCII-folding happens
    later and only inside the comparison path — see
    :func:`citations.ascii_fold`. Round-tripping through Unicode is what makes the
    fold work at all.
    """
    if not s or "\\" not in s:
        return s
    s = _LATEX_LETTER_RE.sub(lambda m: _LATEX_LETTERS[m.group(1)], s)

    def _accent(m: "re.Match") -> str:
        letter = (m.group("braced") or m.group("bare") or "").lstrip("\\")
        mark = _LATEX_COMBINING.get(m.group("acc"), "")
        return letter + mark

    s = _LATEX_ACCENT_RE.sub(_accent, s)
    # Compose base+mark into the single precomposed codepoint where one exists.
    return unicodedata.normalize("NFC", s)


def _strip_value(raw: str) -> str:
    """Strip surrounding {}/"" delimiters, decode LaTeX escapes, collapse space.

    LaTeX escapes are decoded *before* braces are stripped, so both ``{\\"u}`` and
    ``\\"{u}`` resolve — stripping first would leave a bare accent command behind.
    """
    s = raw.strip().rstrip(",").strip()
    if s and s[0] == '"' and s[-1] == '"':
        s = s[1:-1]
    s = _decode_latex_accents(s)
    s = s.replace("{", "").replace("}", "")
    return " ".join(s.split())


def _split_authors(value: str) -> list:
    """Split a BibTeX author value on the ' and ' separator."""
    parts = re.split(r"\s+and\s+", value.strip())
    return [p.strip() for p in parts if p.strip()]


def _iter_entry_bodies(text: str):
    """Yield (entry_type, body) for each @type{...} block, brace-balanced."""
    i, n = 0, len(text)
    while i < n:
        at = text.find("@", i)
        if at == -1:
            return
        brace = text.find("{", at)
        if brace == -1:
            return
        entry_type = text[at + 1:brace].strip().lower()
        # balance braces from `brace`
        depth, j = 0, brace
        while j < n:
            if text[j] == "{":
                depth += 1
            elif text[j] == "}":
                depth -= 1
                if depth == 0:
                    break
            j += 1
        body = text[brace + 1:j]
        yield entry_type, body
        i = j + 1


def _parse_fields(body: str) -> dict:
    """Parse ``name = value`` fields from an entry body (skips the citekey)."""
    # Drop the citekey (everything up to the first comma).
    comma = body.find(",")
    fields_blob = body[comma + 1:] if comma != -1 else body

    fields: dict = {}
    # Match: key = {balanced} | "quoted" | bareword , at top level.
    pos, n = 0, len(fields_blob)
    key_re = re.compile(r"\s*([A-Za-z][A-Za-z0-9_-]*)\s*=\s*")
    while pos < n:
        m = key_re.match(fields_blob, pos)
        if not m:
            pos += 1
            continue
        key = m.group(1).lower()
        vstart = m.end()
        if vstart >= n:
            break
        ch = fields_blob[vstart]
        if ch == "{":
            depth, j = 0, vstart
            while j < n:
                if fields_blob[j] == "{":
                    depth += 1
                elif fields_blob[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            value = fields_blob[vstart:j + 1]
            pos = j + 1
        elif ch == '"':
            j = vstart + 1
            while j < n and fields_blob[j] != '"':
                j += 1
            value = fields_blob[vstart:j + 1]
            pos = j + 1
        else:
            j = vstart
            while j < n and fields_blob[j] != ",":
                j += 1
            value = fields_blob[vstart:j]
            pos = j + 1
        fields[key] = _strip_value(value)
    return fields


def parse_bibtex(text: str) -> list:
    """Parse a BibTeX string into a list of :class:`Reference`."""
    refs = []
    for entry_type, body in _iter_entry_bodies(text or ""):
        if entry_type in ("comment", "string", "preamble"):
            continue
        f = _parse_fields(body)
        if not f:
            continue
        year = None
        if f.get("year"):
            m = re.search(r"\d{4}", f["year"])
            if m:
                year = int(m.group(0))
        refs.append(Reference(
            raw=body.strip(),
            title=f.get("title", ""),
            authors=_split_authors(f["author"]) if f.get("author") else [],
            year=year,
            doi=f.get("doi", ""),
            venue=f.get("journal") or f.get("booktitle") or "",
            url=f.get("url", ""),
        ))
    return refs


def parse_dois(text: str) -> list:
    """Parse a newline-separated list of DOIs (bare, ``doi:`` or doi.org URLs).

    Lines that are blank or start with ``#`` are ignored, as are lines that don't
    contain a DOI-shaped token.
    """
    refs = []
    for line in (text or "").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        doi = _normalize_doi(line)
        if re.search(r"10\.\d{4,9}/", doi):
            refs.append(Reference(doi=doi))
    return refs
