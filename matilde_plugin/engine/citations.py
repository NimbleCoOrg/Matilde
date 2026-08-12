"""Matilde verifiable-citations engine — axes 1-3.

A citation is checked along four independent axes:

  1. existence       — does the cited work actually exist? (Crossref by DOI, else
                       title search)
  2. metadata-match  — do title / authors / year agree with the authoritative record?
  3. retraction      — has the work been retracted? (Crossref ``update-to`` /
                       ``relation.is-retracted-by``; Crossref owns the Retraction
                       Watch dataset)
  3b. url-liveness   — if a URL is given, does it resolve? (HTTP HEAD, with an
                       Internet Archive Wayback fallback)

The fourth axis — *claim-support grounding* (does the cited passage actually
substantiate the sentence that cites it?) — is intentionally **not** in v1. It is
the probabilistic frontier (GROBID + SemanticCite/SciFact) and lands in v2.

Design: all network I/O is injected. ``verify_*`` functions take a ``fetch``
callable ``(url) -> parsed JSON dict`` (raising ``LookupError`` on 404) and an
``http_head`` callable ``(url) -> (status_int_or_None, final_url)``. Production
defaults using the stdlib (``default_fetch`` / ``default_head``) are provided, but
every code path is unit-testable without a network by passing fakes.

Honest naming note: this is *verifiable*, not *provably correct*. Axes 1-3 are
near-deterministic; the composite is a confidence score, not a proof.
"""
from __future__ import annotations

import dataclasses
import json
import os
import re
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from difflib import SequenceMatcher
from typing import Any, Callable, Optional

# Title-match threshold (RefChecker / mcp-refchecker use ~0.85 fuzzy on titles).
TITLE_MATCH_THRESHOLD = 0.85
# Below this, a candidate title is considered a different paper entirely.
TITLE_DISTINCT_THRESHOLD = 0.60

CROSSREF_BASE = "https://api.crossref.org"
OPENALEX_BASE = "https://api.openalex.org"
DATACITE_BASE = "https://api.datacite.org"
WAYBACK_API = "https://archive.org/wayback/available"

FetchFn = Callable[..., dict]
HeadFn = Callable[..., tuple]

# Sources whose records carry retraction information. DataCite (arXiv, Zenodo,
# figshare, Dryad) publishes none, so a DataCite record can never support a
# "not retracted" conclusion — see :func:`check_retraction`.
_RETRACTION_CAPABLE_SOURCES = ("crossref", "openalex")

_SOURCE_LABELS = {"crossref": "Crossref", "openalex": "OpenAlex",
                  "datacite": "DataCite"}


def _source_label(source: str) -> str:
    """Human name for a provenance key. Never invents a name we didn't record."""
    return _SOURCE_LABELS.get(source, source or "an unidentified source")


def _fetch_json(url: str, fetch: FetchFn) -> tuple:
    """Fetch *url*, distinguishing "absent" from "could not be consulted".

    Returns ``(data, error)``:

      * ``(dict, None)``  — the source answered.
      * ``(None, None)``  — a clean 404: the record genuinely is not there.
      * ``(None, "TypeError: …")`` — DNS/TLS/timeout/5xx. The source was *not*
        consulted, and callers must not report its silence as evidence.

    Conflating the last two is how a network outage turns into a scientific claim.
    """
    try:
        data = fetch(url)
    except LookupError:
        return (None, None)
    except Exception as exc:  # transport/parse failure — NOT an absence of data
        return (None, f"{type(exc).__name__}: {exc}")
    return (data, None)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class Reference:
    """A citation to verify. Populate whatever fields you have; more is better."""

    raw: str = ""                       # original citation string (optional)
    title: str = ""
    authors: list = field(default_factory=list)  # surnames or "First Last" strings
    year: Optional[int] = None
    doi: str = ""
    venue: str = ""
    url: str = ""

    def __post_init__(self) -> None:
        # Normalize a DOI URL / "doi:" prefix down to the bare DOI.
        self.doi = _normalize_doi(self.doi)


@dataclass
class AxisResult:
    """The outcome of one verification axis.

    status: one of "pass" | "warn" | "fail" | "unknown".
      - For most axes, "pass" is good. For *retraction*, "pass" means NOT retracted
        and "fail" means IS retracted (a failure of the citation, not the check).
    confidence: 0..1 — how sure we are of this status.
    detail: human-readable one-liner.
    evidence: structured supporting data (the source record, matched fields, …).
    """

    status: str
    confidence: float = 0.0
    detail: str = ""
    evidence: dict = field(default_factory=dict)


@dataclass
class VerificationResult:
    reference: Reference
    existence: AxisResult
    metadata_match: AxisResult
    retraction: AxisResult
    url_liveness: AxisResult
    score: float = 0.0          # composite verifiability [0..1]
    verdict: str = "unverifiable"  # verified | warnings | retracted | not_found | unverifiable
    # Which axes actually ran — i.e. the denominator the score is averaged over.
    # Without it, a score cannot be read: 1.0 over two axes is not 1.0 over four.
    axes_evaluated: list = field(default_factory=list)

    def to_dict(self) -> dict:
        """A JSON-safe dict (for tool envelopes / persistence)."""
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# String primitives
# ---------------------------------------------------------------------------

def _normalize_doi(doi: str) -> str:
    if not doi:
        return ""
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.I)
    doi = re.sub(r"^doi:\s*", "", doi, flags=re.I)
    return doi.strip()


# Letters that carry no Unicode decomposition, so NFKD leaves them intact and a
# naive [^a-z0-9] scrub *deletes* them mid-word ("Łukasiewicz" -> "ukasiewicz").
# Folding them explicitly is what keeps author matching working for names outside
# the English alphabet. Mapping is one-way and for comparison only — the stored
# citation always keeps the author's own spelling.
_FOLD_SPECIALS = {
    "ł": "l", "Ł": "L", "ø": "o", "Ø": "O", "đ": "d", "Đ": "D",
    "ð": "d", "Ð": "D", "þ": "th", "Þ": "Th", "ħ": "h", "Ħ": "H",
    "ŧ": "t", "Ŧ": "T", "ı": "i", "İ": "I", "ŀ": "l", "Ŀ": "L",
    "ß": "ss", "ẞ": "SS", "æ": "ae", "Æ": "AE", "œ": "oe", "Œ": "OE",
    "ŋ": "ng", "Ŋ": "NG", "ə": "e", "Ə": "E", "ʼ": "'", "’": "'",
}


def ascii_fold(s: str) -> str:
    """Fold *s* toward ASCII for comparison: strip combining marks, map specials.

    ``unicodedata.normalize("NFKD", …)`` splits a precomposed letter into base +
    combining mark, which we then drop — so "Müller" folds to "Muller" and
    "Zhāng" to "Zhang". Letters that have *no* decomposition (ł, ø, ß, æ, đ, ı)
    are mapped explicitly by :data:`_FOLD_SPECIALS`.

    This is a *comparison* helper only. It is deliberately never applied to the
    reference or record text we display or persist: a citation must keep the
    author's own orthography. Folding here, and only here, is what lets
    "Łukasiewicz" match "Lukasiewicz" without ever rewriting either one.
    """
    if not s:
        return ""
    out = []
    for ch in unicodedata.normalize("NFKD", s):
        if unicodedata.combining(ch):
            continue  # a stripped accent, not a letter
        out.append(_FOLD_SPECIALS.get(ch, ch))
    return "".join(out)


def _norm_title(s: str) -> str:
    """Casefold + ASCII-fold *s* and reduce every non-alphanumeric run to a space.

    ``str.isalnum()`` (rather than an ``[a-z0-9]`` class) keeps letters and digits
    from *any* script, so a Cyrillic, Greek, or CJK title is normalized rather
    than erased.
    """
    s = ascii_fold(s).lower()
    s = "".join(ch if ch.isalnum() else " " for ch in s)
    return " ".join(s.split())


def title_similarity(a: str, b: str) -> float:
    """Return a 0..1 similarity for two titles (case/punctuation-insensitive)."""
    na, nb = _norm_title(a), _norm_title(b)
    if not na or not nb:
        return 0.0
    return SequenceMatcher(None, na, nb).ratio()


def _surname(name: str) -> str:
    """Best-effort surname extraction from 'First Last' or 'Last'."""
    name = name.strip()
    if not name:
        return ""
    # Handle "Last, First"
    if "," in name:
        return _norm_title(name.split(",")[0]).strip()
    parts = _norm_title(name).split()
    return parts[-1] if parts else ""


def author_overlap(ref_authors: list, record_authors: list) -> float:
    """Fraction of *ref_authors* whose surname appears among *record_authors*.

    ``record_authors`` may be plain strings or Crossref author dicts
    (``{"family": ..., "given": ...}``). Returns 0.0 if ``ref_authors`` is empty
    (nothing to corroborate).
    """
    ref_surnames = [s for s in (_surname(a) for a in ref_authors) if s]
    if not ref_surnames:
        return 0.0

    rec_surnames = set()
    for a in record_authors:
        if isinstance(a, dict):
            fam = a.get("family") or a.get("name") or ""
            rec_surnames.add(_surname(fam))
        else:
            rec_surnames.add(_surname(str(a)))
    rec_surnames.discard("")

    matched = sum(1 for s in ref_surnames if s in rec_surnames)
    return matched / len(ref_surnames)


def _record_title(message: dict) -> str:
    t = message.get("title") or []
    return t[0] if isinstance(t, list) and t else (t if isinstance(t, str) else "")


def _crossref_record(message: dict) -> dict:
    """Tag a Crossref ``message`` with its provenance, without mutating the input.

    Every record the engine passes around carries ``_source``, so downstream axes
    can tell *where* a field came from rather than assuming Crossref.
    """
    if not isinstance(message, dict):
        return {"_source": "crossref"}
    return dict(message, _source="crossref")


def _openalex_to_record(work: dict) -> dict:
    """Normalize an OpenAlex ``work`` into the Crossref-ish dict shape the rest
    of the engine consumes (so ``_record_title`` / ``_record_year`` /
    ``author_overlap`` / ``_retraction_signal`` all work unchanged).

    OpenAlex aggregates Crossref, DataCite (arXiv, Zenodo, …), PubMed and more,
    so it's the right fallback when a DOI isn't in Crossref. It also carries an
    authoritative ``is_retracted`` flag.
    """
    authors = []
    for a in work.get("authorships", []) or []:
        name = ((a or {}).get("author") or {}).get("display_name") or ""
        if name:
            authors.append({"family": name})
    year = work.get("publication_year")
    doi = _normalize_doi(work.get("doi") or "")
    return {
        "_source": "openalex",
        "DOI": doi,
        "title": [work.get("display_name") or ""],
        "author": authors,
        "published": {"date-parts": [[year]]} if year else {},
        "is_retracted": bool(work.get("is_retracted")),
        "openalex_id": work.get("id"),
    }


def _datacite_to_record(data: dict) -> dict:
    """Normalize a DataCite ``/dois/{doi}`` response into the common record shape.

    DataCite is the registration agency for arXiv (10.48550/*), Zenodo, figshare,
    Dryad and most data/preprint DOIs — the authoritative existence check when a
    DOI isn't in Crossref or OpenAlex.
    """
    attrs = (data.get("data") or {}).get("attributes") or data.get("attributes") or {}
    titles = attrs.get("titles") or []
    title = titles[0].get("title") if titles else ""
    authors = []
    for cr in attrs.get("creators", []) or []:
        fam = cr.get("familyName") or cr.get("name") or ""
        if fam:
            authors.append({"family": fam})
    year = attrs.get("publicationYear")
    return {
        "_source": "datacite",
        "DOI": _normalize_doi(attrs.get("doi") or ""),
        "title": [title or ""],
        "author": authors,
        "published": {"date-parts": [[year]]} if year else {},
    }


def _record_year(message: dict) -> Optional[int]:
    for key in ("published", "published-print", "published-online", "issued"):
        node = message.get(key) or {}
        parts = node.get("date-parts") or []
        if parts and parts[0]:
            try:
                return int(parts[0][0])
            except (TypeError, ValueError):
                continue
    return None


# ---------------------------------------------------------------------------
# Axis 1: existence
# ---------------------------------------------------------------------------

def verify_existence(ref: Reference, fetch: FetchFn) -> AxisResult:
    """Does the cited work exist? Prefer DOI lookup; fall back to title search."""
    if ref.doi:
        url = f"{CROSSREF_BASE}/works/{urllib.parse.quote(ref.doi)}"
        try:
            data = fetch(url)
            message = _crossref_record(data.get("message", data))
            return AxisResult(
                status="pass", confidence=0.95,
                detail=f"DOI {ref.doi} resolves to a Crossref record.",
                evidence={"record": message, "matched_by": "doi", "source": "crossref"},
            )
        except LookupError:
            pass  # not in Crossref — fall back to OpenAlex before crying fabrication

        oa = _openalex_by_doi(ref.doi, fetch)
        if oa is not None:
            return AxisResult(
                status="pass", confidence=0.9,
                detail=f"DOI {ref.doi} resolves via OpenAlex (not in Crossref).",
                evidence={"record": oa, "matched_by": "doi", "source": "openalex"},
            )

        dc = _datacite_by_doi(ref.doi, fetch)
        if dc is not None:
            return AxisResult(
                status="pass", confidence=0.9,
                detail=f"DOI {ref.doi} resolves via DataCite "
                       f"(e.g. an arXiv/Zenodo/figshare DOI).",
                evidence={"record": dc, "matched_by": "doi", "source": "datacite"},
            )
        return AxisResult(
            status="fail", confidence=0.9,
            detail=f"DOI {ref.doi} does not resolve in Crossref, OpenAlex, or "
                   f"DataCite — likely fabricated.",
            evidence={"doi": ref.doi},
        )

    if ref.title:
        url = f"{CROSSREF_BASE}/works?query.bibliographic={urllib.parse.quote(ref.title)}&rows=5"
        try:
            data = fetch(url)
        except LookupError:
            return AxisResult(status="unknown", confidence=0.3,
                              detail="Crossref title search returned nothing.")
        items = (data.get("message", {}) or {}).get("items", []) or []
        best, best_sim = None, 0.0
        for item in items:
            sim = title_similarity(ref.title, _record_title(item))
            if sim > best_sim:
                best, best_sim = item, sim
        if best is not None and best_sim >= TITLE_MATCH_THRESHOLD:
            return AxisResult(
                status="pass", confidence=best_sim,
                detail=f"Title matched a Crossref record (similarity {best_sim:.2f}).",
                evidence={"record": _crossref_record(best), "matched_by": "title",
                          "source": "crossref", "similarity": best_sim},
            )
        return AxisResult(
            status="unknown", confidence=0.4,
            detail=f"No Crossref title match above {TITLE_MATCH_THRESHOLD:.2f} "
                   f"(best {best_sim:.2f}).",
            evidence={"best_similarity": best_sim},
        )

    return AxisResult(status="unknown", confidence=0.0,
                      detail="No DOI or title supplied — cannot check existence.")


def _openalex_by_doi(doi: str, fetch: FetchFn) -> Optional[dict]:
    """Return a normalized record for *doi* from OpenAlex, or None if absent."""
    doi = _normalize_doi(doi)
    if not doi:
        return None
    url = f"{OPENALEX_BASE}/works/doi:{urllib.parse.quote(doi)}"
    try:
        work = fetch(url)
    except LookupError:
        return None
    if not work or work.get("id") is None and not work.get("display_name"):
        return None
    return _openalex_to_record(work)


def _datacite_by_doi(doi: str, fetch: FetchFn) -> Optional[dict]:
    """Return a normalized record for *doi* from DataCite, or None if absent."""
    doi = _normalize_doi(doi)
    if not doi:
        return None
    url = f"{DATACITE_BASE}/dois/{urllib.parse.quote(doi)}"
    try:
        data = fetch(url)
    except LookupError:
        return None
    if not data or not (data.get("data") or data.get("attributes")):
        return None
    return _datacite_to_record(data)


# ---------------------------------------------------------------------------
# Axis 2: metadata match
# ---------------------------------------------------------------------------

def check_metadata_match(ref: Reference, record: dict) -> AxisResult:
    """Do the citation's title/authors/year agree with the authoritative record?

    Only the sub-axes that *could* be compared are evaluated, and the detail names
    them. An absent field is never scored as agreement: a reference carrying a DOI
    and nothing else has nothing to corroborate the DOI with, so this returns
    ``unknown`` rather than ``pass``. ``evidence["axes_evaluated"]`` lists exactly
    which sub-axes ran, so a reader can see what the status is based on.

    Title outcomes are banded, because a title that is *close but not equal* is the
    signature of a fabricated reference (a hallucinated citation characteristically
    gets the DOI right and the title subtly wrong):

      * ``sim >= TITLE_MATCH_THRESHOLD``      — titles agree
      * ``TITLE_DISTINCT_THRESHOLD <= sim <`` — **warn**, and both titles are named
        ``TITLE_MATCH_THRESHOLD``               so the caller can eyeball the drift
      * ``sim < TITLE_DISTINCT_THRESHOLD``    — **fail**, a different paper
    """
    if not record:
        return AxisResult(status="unknown", detail="No record to compare against.")

    rec_title = _record_title(record)
    rec_year = _record_year(record)
    rec_authors = record.get("author", []) or []

    axes_evaluated: list = []
    evidence: dict = {"record_title": rec_title, "axes_evaluated": axes_evaluated}
    warnings: list = []
    not_compared: list = []

    # --- title ---
    sim = None
    if ref.title and rec_title:
        sim = title_similarity(ref.title, rec_title)
        axes_evaluated.append("title")
        evidence["title_similarity"] = sim
        if sim < TITLE_DISTINCT_THRESHOLD:
            return AxisResult(
                status="fail", confidence=1.0 - sim,
                detail=f"Title disagrees with the record (similarity {sim:.2f}): "
                       f"cited '{ref.title}' vs record '{rec_title}'.",
                evidence=evidence,
            )
        if sim < TITLE_MATCH_THRESHOLD:
            # The dangerous middle band. Previously this returned "pass" with the
            # detail "Title, authors, and year agree with the record." — the exact
            # shape of a hallucinated reference sailing through.
            warnings.append(
                f"title only partially matches (similarity {sim:.2f}, below the "
                f"{TITLE_MATCH_THRESHOLD:.2f} match threshold): cited "
                f"'{ref.title}' vs record '{rec_title}'"
            )
    elif ref.title and not rec_title:
        not_compared.append("title (the record carries no title)")
    else:
        not_compared.append("title (not supplied in the citation)")

    # --- year ---
    if ref.year and rec_year:
        axes_evaluated.append("year")
        evidence["record_year"] = rec_year
        if ref.year != rec_year:
            warnings.append(f"year mismatch (cited {ref.year}, record {rec_year})")
    elif ref.year and not rec_year:
        not_compared.append("year (the record carries no year)")
    else:
        not_compared.append("year (not supplied in the citation)")

    # --- authors ---
    if ref.authors and rec_authors:
        overlap = author_overlap(ref.authors, rec_authors)
        axes_evaluated.append("authors")
        evidence["author_overlap"] = overlap
        if overlap < 0.5:
            warnings.append(f"author overlap low ({overlap:.0%})")
    elif ref.authors and not rec_authors:
        not_compared.append("authors (the record lists none)")
    else:
        not_compared.append("authors (not supplied in the citation)")

    evidence["not_compared"] = not_compared

    # Neither of the two identifying axes ran: there is nothing corroborating the
    # citation, and saying "metadata agrees" would be a claim about a check that
    # never happened.
    if "title" not in axes_evaluated and "authors" not in axes_evaluated:
        return AxisResult(
            status="unknown", confidence=0.0,
            detail="Nothing to compare: neither title nor authors were available "
                   "on both sides (" + "; ".join(not_compared) + "). "
                   "Existence is corroborated, metadata is not.",
            evidence=evidence,
        )

    compared = ", ".join(axes_evaluated)
    if warnings:
        return AxisResult(status="warn", confidence=0.6,
                          detail="; ".join(warnings) + f". (Compared: {compared}.)",
                          evidence=evidence)
    return AxisResult(
        status="pass", confidence=max(sim if sim is not None else 0.8, 0.8),
        detail=f"Agrees with the record on {compared}."
               + (f" Not compared: {'; '.join(not_compared)}." if not_compared else ""),
        evidence=evidence)


# ---------------------------------------------------------------------------
# Axis 3: retraction
# ---------------------------------------------------------------------------

def _retraction_signal(message: dict) -> Optional[str]:
    """Return a human label if *message* shows the work is retracted, else None.

    Crossref exposes retraction on the affected work in a few shapes. Validated
    against the real API (e.g. the retracted Wakefield 1998 Lancet paper), the
    *primary* signal is the ``updated-by`` array — entries whose ``type`` is
    "retraction" (alongside any "correction" entries), typically carrying
    ``source: retraction-watch``. Crossref ingests the Retraction Watch dataset
    daily. Older/other records may instead use ``update-to`` or a
    ``relation.is-retracted-by`` link, so we check all three.
    """
    # OpenAlex carries an authoritative boolean flag (normalized into our record).
    if message.get("is_retracted"):
        return "retraction (OpenAlex is_retracted)"
    for upd in message.get("updated-by", []) or []:
        if isinstance(upd, dict) and "retract" in str(upd.get("type", "")).lower():
            return upd.get("label") or "retraction"
    for upd in message.get("update-to", []) or []:
        if isinstance(upd, dict) and "retract" in str(upd.get("type", "")).lower():
            return upd.get("label") or "retraction"
    relation = message.get("relation", {}) or {}
    for key in relation:
        if "retract" in key.lower() and relation[key]:
            return key
    return None


def check_retraction(doi: str, fetch: FetchFn,
                     record: Optional[dict] = None) -> AxisResult:
    """Has the work with *doi* been retracted? ('fail' == retracted.)

    Consults **every** source that publishes retraction data and can be reached:
    Crossref (which ingests the Retraction Watch dataset) and OpenAlex (whose
    ``is_retracted`` flag is independent, and is sometimes the only place a
    retraction shows up). A false *negative* is the worst error this tool can make,
    so both are checked even when the first one comes back clean.

    Two honesty rules, both of which this function used to break:

    1. A ``pass`` is only ever returned for a source we actually parsed, and the
       detail names those sources. A record from DataCite (arXiv/Zenodo/figshare)
       carries no retraction-capable fields at all, so it can only ever yield
       ``unknown`` — never "No retraction recorded in Crossref", which would be
       inventing provenance for a request that was never made.
    2. A source that could not be *reached* (DNS, TLS, timeout, 5xx) is reported as
       unreachable, not silently folded into "no retraction found".

    *record*, when given, is an already-fetched existence record; it is reused
    instead of re-requesting, but only if its ``_source`` publishes retractions.
    """
    doi = _normalize_doi(doi)
    consulted: list = []
    unreachable: list = []

    def _retracted(label: str, src: str) -> AxisResult:
        return AxisResult(
            status="fail", confidence=0.95,
            detail=f"Work is RETRACTED ({label}), per {_source_label(src)}.",
            evidence={"signal": label, "sources_consulted": [src]})

    # 1. Reuse a record we already hold — only if that source records retractions.
    record_source = (record or {}).get("_source") or ""
    if record and record_source in _RETRACTION_CAPABLE_SOURCES:
        label = _retraction_signal(record)
        if label:
            return _retracted(label, record_source)
        consulted.append(record_source)

    if doi:
        # 2. Crossref — the Retraction Watch dataset.
        if "crossref" not in consulted:
            data, err = _fetch_json(
                f"{CROSSREF_BASE}/works/{urllib.parse.quote(doi)}", fetch)
            if err:
                unreachable.append(f"Crossref ({err})")
            elif isinstance(data, dict) and data:
                message = data.get("message", data)
                label = _retraction_signal(message)
                if label:
                    return _retracted(label, "crossref")
                consulted.append("crossref")

        # 3. OpenAlex `is_retracted` — an independent signal, and the one a
        #    Crossref-only check misses entirely.
        if "openalex" not in consulted:
            work, err = _fetch_json(
                f"{OPENALEX_BASE}/works/doi:{urllib.parse.quote(doi)}", fetch)
            if err:
                unreachable.append(f"OpenAlex ({err})")
            elif isinstance(work, dict) and work:
                label = _retraction_signal(_openalex_to_record(work))
                if label:
                    return _retracted(label, "openalex")
                consulted.append("openalex")

    evidence = {"sources_consulted": list(consulted),
                "sources_unreachable": list(unreachable)}

    if not consulted:
        why: list = []
        if not doi:
            why.append("no DOI was supplied")
        if record_source and record_source not in _RETRACTION_CAPABLE_SOURCES:
            why.append(f"the existence record came from "
                       f"{_source_label(record_source)}, which publishes no "
                       f"retraction fields")
        if unreachable:
            why.append("could not reach " + "; ".join(unreachable))
        elif doi:
            why.append(f"{doi} is in neither Crossref nor OpenAlex")
        return AxisResult(
            status="unknown", confidence=0.3,
            detail="Retraction status UNKNOWN — " + "; ".join(why) + ".",
            evidence=evidence)

    names = " and ".join(_source_label(s) for s in consulted)
    detail = f"No retraction recorded in {names}."
    if unreachable:
        detail += " Not consulted (unreachable): " + "; ".join(unreachable) + "."
    return AxisResult(status="pass",
                      confidence=0.95 if len(consulted) > 1 else 0.9,
                      detail=detail, evidence=evidence)


# ---------------------------------------------------------------------------
# Axis 3b: URL liveness
# ---------------------------------------------------------------------------

def _unpack_head(result: Any) -> tuple:
    """Normalize a head callable's return to ``(status, final_url, transport_error)``.

    Accepts both the two-tuple ``(status, final_url)`` shape and the three-tuple
    ``(status, final_url, transport_error)`` shape, so injected fakes written
    against the older contract keep working.
    """
    if not isinstance(result, (tuple, list)):
        return (None, "", None)
    status = result[0] if len(result) > 0 else None
    final = result[1] if len(result) > 1 else ""
    error = result[2] if len(result) > 2 else None
    return (status, final, error)


def _wayback_snapshot(url: str, fetch: Optional[FetchFn]) -> tuple:
    """Return ``(archived_url_or_None, error_or_None)`` for *url*."""
    if fetch is None:
        return (None, None)
    data, err = _fetch_json(f"{WAYBACK_API}?url={urllib.parse.quote(url)}", fetch)
    if err or not isinstance(data, dict):
        return (None, err)
    snap = (data.get("archived_snapshots", {}) or {}).get("closest", {}) or {}
    return (snap.get("url") if snap.get("available") else None, None)


def check_url_liveness(url: str, http_head: HeadFn,
                       fetch: Optional[FetchFn] = None) -> AxisResult:
    """Does *url* resolve? Dead URLs fall back to an Internet Archive check.

    A **transport failure** (DNS failure, TLS error, connection reset, timeout) is
    reported as ``unknown`` and names the exception — it is not evidence that the
    link is dead. Only an actual HTTP response can support ``fail``. The old code
    collapsed both into ``fail`` with the detail "URL does not resolve (HTTP None)",
    which asserted a dead link on the strength of our own network trouble.
    """
    if not url:
        return AxisResult(status="unknown", detail="No URL supplied.")
    status, final, transport_error = _unpack_head(http_head(url))

    if status is not None and 200 <= status < 400:
        return AxisResult(status="pass", confidence=0.9,
                          detail=f"URL is live (HTTP {status}).",
                          evidence={"http_status": status, "final_url": final})

    archived, _archive_err = _wayback_snapshot(url, fetch)

    # No HTTP status at all: we never reached the host, so we know nothing about
    # the link itself.
    if status is None:
        reason = transport_error or "no HTTP response was obtained"
        evidence: dict = {"http_status": None, "transport_error": transport_error}
        detail = (f"URL liveness UNKNOWN — the request itself failed ({reason}). "
                  f"This is a transport failure on our side, not evidence that the "
                  f"link is dead.")
        if archived:
            evidence["wayback_url"] = archived
            detail += " A Wayback Machine snapshot does exist."
        return AxisResult(status="unknown", confidence=0.2, detail=detail,
                          evidence=evidence)

    if archived:
        return AxisResult(status="warn", confidence=0.6,
                          detail=f"URL is dead (HTTP {status}) but archived in the "
                                 f"Wayback Machine.",
                          evidence={"http_status": status, "wayback_url": archived})
    return AxisResult(status="fail", confidence=0.8,
                      detail=f"URL does not resolve (HTTP {status}) and is not archived.",
                      evidence={"http_status": status})


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

# Relative weight of each axis in the composite score.
_AXIS_WEIGHTS = {"existence": 0.5, "metadata_match": 0.3,
                 "retraction": 0.1, "url_liveness": 0.1}
# How much of an axis's weight a given status earns. "unknown" is absent on
# purpose: an axis that did not run is dropped from the average entirely rather
# than being handed partial credit.
_STATUS_QUALITY = {"pass": 1.0, "warn": 0.6, "fail": 0.0}

# A composite at or above this is called "verified" — but only if no axis warned
# or failed (see :func:`verify_reference`).
VERIFIED_SCORE_THRESHOLD = 0.8

# The score is the number downstream filters compare against a threshold, so it
# must never contradict the verdict. Each non-"verified" verdict caps the
# composite, which preserves the ordering
#     retracted / not_found  <  unverifiable  <  warnings  <  verified
# and enforces the invariant
#     score >= VERIFIED_SCORE_THRESHOLD  <=>  verdict == "verified".
# Without the cap, a subtly-wrong title scored 0.867 and a bare DOI with nothing
# to corroborate it scored 1.00 — both "warnings"/"unverifiable" on paper while
# reading as high confidence to anything that looked at the number.
_VERDICT_SCORE_CAP = {
    "retracted": 0.1,
    "not_found": 0.1,
    "unverifiable": 0.4,
    "warnings": round(VERIFIED_SCORE_THRESHOLD - 0.01, 2),
}

# Ceiling for a reference where an axis was *attempted and came back
# indeterminate* — as opposed to an axis that never applied. Dropping "unknown"
# axes from the average (see :func:`_composite_score`) is right, but on its own it
# lets "we tried to reach the URL and could not" score a flawless 1.0, identical to
# "we fetched the URL and it was live". A check we attempted and could not complete
# is not the same as a check that was never needed, and the number must show it.
_INDETERMINATE_SCORE_CAP = 0.95


def _composite_score(axes: dict) -> tuple:
    """Weighted mean over the axes that actually ran → ``(score, axes_evaluated)``.

    An axis whose status is ``unknown`` contributes to *neither* the numerator nor
    the denominator, so the score reads as "of what could be checked, how much
    checked out" — and ``axes_evaluated`` states what that denominator was. The old
    scheme gave ``unknown`` axes a positive weight, which let a reference with no
    title, no authors and no URL reach 0.90 on the strength of checks that never
    ran.
    """
    numerator = denominator = 0.0
    evaluated: list = []
    for name, result in axes.items():
        quality = _STATUS_QUALITY.get(result.status)
        if quality is None:
            continue  # "unknown" — not evidence in either direction
        weight = _AXIS_WEIGHTS[name]
        numerator += weight * quality
        denominator += weight
        evaluated.append(name)
    if denominator == 0.0:
        return (0.0, evaluated)
    return (round(numerator / denominator, 3), evaluated)


def verify_reference(ref: Reference, fetch: Optional[FetchFn] = None,
                     http_head: Optional[HeadFn] = None) -> VerificationResult:
    """Run all v1 axes and produce a composite verdict + score.

    Verdict precedence:

      * ``retracted``     — a retraction dominates everything else.
      * ``not_found``     — the DOI/title resolves nowhere.
      * ``unverifiable``  — existence could not be established, **or** nothing
        corroborates it (a bare DOI with no title and no authors).
      * ``warnings``      — it exists, but an axis warned or failed: a partial
        title match, a year or author mismatch, a dead URL, or a retraction status
        we could not establish. Any of these blocks ``verified`` outright,
        regardless of score — "something is off" is not a matter of degree.
      * ``verified``      — every axis that ran passed, and the composite clears
        :data:`VERIFIED_SCORE_THRESHOLD`.

    ``score`` is a weighted mean over the axes that ran (``result.axes_evaluated``
    lists them), then capped by the verdict per :data:`_VERDICT_SCORE_CAP` so the
    two can never disagree: anything short of ``verified`` also scores short of
    :data:`VERIFIED_SCORE_THRESHOLD`.
    """
    fetch = fetch or default_fetch
    http_head = http_head or default_head

    existence = verify_existence(ref, fetch=fetch)
    record = existence.evidence.get("record") if existence.status == "pass" else None

    if record:
        metadata = check_metadata_match(ref, record)
    else:
        metadata = AxisResult(status="unknown", detail="No record to compare against.")

    # Reuses the already-fetched record when that record's source publishes
    # retraction data, and always cross-checks the source it has not seen.
    retraction = check_retraction(ref.doi or "", fetch=fetch, record=record)

    url_liveness = check_url_liveness(ref.url, http_head=http_head, fetch=fetch) \
        if ref.url else AxisResult(status="unknown", detail="No URL supplied.")

    score, axes_evaluated = _composite_score({
        "existence": existence,
        "metadata_match": metadata,
        "retraction": retraction,
        "url_liveness": url_liveness,
    })

    # --- Verdict ---
    if retraction.status == "fail":
        verdict = "retracted"
    elif existence.status == "fail":
        verdict = "not_found"
    elif existence.status == "unknown":
        verdict = "unverifiable"
    elif metadata.status == "unknown":
        # Existence is corroborated but nothing corroborates the citation itself.
        verdict = "unverifiable"
    elif any(axis.status in ("warn", "fail")
             for axis in (metadata, retraction, url_liveness)):
        verdict = "warnings"
    elif retraction.status == "unknown":
        # We could not establish that it has NOT been retracted; "verified" would
        # be claiming a check we did not manage to make.
        verdict = "warnings"
    elif score >= VERIFIED_SCORE_THRESHOLD:
        verdict = "verified"
    else:
        verdict = "warnings"

    score = min(score, _VERDICT_SCORE_CAP.get(verdict, 1.0))

    # A URL was cited but we never reached the host: the axis is "unknown" and so
    # was dropped from the average, which would otherwise let this score a perfect
    # 1.0 — indistinguishable from a reference whose URL we fetched and confirmed
    # live. Cap it so the number cannot claim a completeness it does not have.
    if ref.url and url_liveness.status == "unknown":
        score = min(score, _INDETERMINATE_SCORE_CAP)

    return VerificationResult(
        reference=ref,
        existence=existence,
        metadata_match=metadata,
        retraction=retraction,
        url_liveness=url_liveness,
        score=score,
        verdict=verdict,
        axes_evaluated=axes_evaluated,
    )


# ---------------------------------------------------------------------------
# Production I/O defaults (stdlib only)
# ---------------------------------------------------------------------------

def _user_agent() -> str:
    contact = os.environ.get("MATILDE_CONTACT_EMAIL", "").strip()
    base = "Matilde-citation-verifier/0.1 (https://github.com/cyborg-garden/Matilde)"
    return f"{base} mailto:{contact}" if contact else base


# HTTP statuses that mean "ask again shortly", not "no". Crossref and OpenAlex
# both rate-limit with 429, and both return 503 under load.
RETRYABLE_STATUSES = (429, 500, 502, 503, 504)
DEFAULT_RETRIES = 3
RETRY_BASE_DELAY = 1.0


def _retry_after_seconds(exc: Any, attempt: int) -> float:
    """Seconds to wait before retry *attempt*, honouring a ``Retry-After`` header.

    Providers tell us how long to back off; ignoring that header is how a polite
    client becomes an impolite one. Falls back to exponential backoff.
    """
    header = ""
    try:
        header = (exc.headers or {}).get("Retry-After", "") or ""
    except Exception:
        header = ""
    try:
        wait = float(str(header).strip())
        if wait >= 0:
            return min(wait, 60.0)
    except (TypeError, ValueError):
        pass
    return RETRY_BASE_DELAY * (2 ** attempt)


def default_fetch(url: str, timeout: float = 20.0, *,
                  retries: int = DEFAULT_RETRIES,
                  sleep: Callable[[float], Any] = time.sleep) -> dict:
    """GET *url* and parse JSON. Raises ``LookupError`` on HTTP 404.

    Retries with backoff on the transient statuses in
    :data:`RETRYABLE_STATUSES` (a 429 from Crossref is a request to slow down, not
    an answer). A 404 is a real answer and is never retried. *sleep* is injected so
    tests can exercise the backoff without waiting.
    """
    req = urllib.request.Request(url, headers={"User-Agent": _user_agent(),
                                               "Accept": "application/json"})
    attempts = max(1, int(retries))
    last_exc: Optional[BaseException] = None
    for attempt in range(attempts):
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise LookupError(url) from exc
            last_exc = exc
            if exc.code in RETRYABLE_STATUSES and attempt < attempts - 1:
                sleep(_retry_after_seconds(exc, attempt))
                continue
            raise
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            # Transport-level: also worth one more try, but never swallowed.
            last_exc = exc
            if attempt < attempts - 1:
                sleep(RETRY_BASE_DELAY * (2 ** attempt))
                continue
            raise
    if last_exc is not None:  # pragma: no cover - loop always returns or raises
        raise last_exc
    raise RuntimeError(f"default_fetch exhausted retries for {url}")


def default_head(url: str, timeout: float = 15.0) -> tuple:
    """HEAD *url*; return ``(status, final_url, transport_error)``.

    Falls back to GET if HEAD is rejected. The third element is ``None`` on any
    real HTTP exchange and a ``"ExcType: message"`` string when the request never
    got that far (DNS failure, TLS error, reset, timeout) — the caller must be able
    to tell "the server said 404" from "we never reached a server". Returning a
    bare ``(None, url)`` for both is what produced the output string
    "URL does not resolve (HTTP None)".
    """
    error: Optional[str] = None
    for method in ("HEAD", "GET"):
        req = urllib.request.Request(url, method=method,
                                     headers={"User-Agent": _user_agent()})
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return (resp.status, resp.geturl(), None)
        except urllib.error.HTTPError as exc:
            if method == "HEAD" and exc.code in (403, 405, 501):
                continue  # some servers reject HEAD; retry with GET
            return (exc.code, url, None)
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            return (None, url, error)
    return (None, url, error)
