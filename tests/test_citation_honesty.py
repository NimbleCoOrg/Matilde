"""Regression tests for the 2026-07-27 citation-honesty audit.

Every test here corresponds to a defect where the engine reported a *check it had
not made*. They are grouped by the claim the old code made falsely, and each
docstring names the old (wrong) output so the test explains itself when it fails.

The shared theme, and the property this module defends: **the engine may only
assert what it actually verified.** A missing input, an unreachable host, or a
provider that does not publish a field are all "unknown" — never "pass".
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from matilde_plugin.engine.citations import (  # noqa: E402
    TITLE_DISTINCT_THRESHOLD,
    TITLE_MATCH_THRESHOLD,
    VERIFIED_SCORE_THRESHOLD,
    Reference,
    _surname,
    author_overlap,
    check_url_liveness,
    title_similarity,
    verify_reference,
)
from matilde_plugin.engine.parsing import parse_bibtex  # noqa: E402


def make_fetch(mapping):
    """fetch(url)->dict, looked up by substring; Exception values are raised."""
    def _fetch(url: str, **_):
        for key, value in mapping.items():
            if key in url:
                if isinstance(value, Exception):
                    raise value
                return value
        raise LookupError(f"no fake response for {url}")
    return _fetch


def make_head(mapping, default=None):
    """http_head(url)->(status, final_url, transport_error)."""
    def _head(url: str, **_):
        for key, value in mapping.items():
            if key in url:
                return value
        return (default, url, None)
    return _head


CROSSREF_CLEAN = {
    "message": {
        "DOI": "10.5555/attention",
        "title": ["Attention Is All You Need"],
        "author": [
            {"family": "Vaswani", "given": "Ashish"},
            {"family": "Shazeer", "given": "Noam"},
        ],
        "published": {"date-parts": [[2017]]},
    }
}

# Crossref knows nothing about it; OpenAlex has it and flags the retraction. The
# old code never asked OpenAlex once Crossref answered.
OPENALEX_RETRACTED = {
    "id": "https://openalex.org/W1",
    "doi": "https://doi.org/10.5555/attention",
    "title": "Attention Is All You Need",
    "publication_year": 2017,
    "is_retracted": True,
    "authorships": [{"author": {"display_name": "Ashish Vaswani"}}],
}

CLEAN_REF = dict(doi="10.5555/attention", title="Attention Is All You Need",
                 authors=["Vaswani, Ashish", "Shazeer, Noam"], year=2017)


def _crossref_only():
    return make_fetch({"crossref": CROSSREF_CLEAN,
                       "openalex": LookupError("404"),
                       "datacite": LookupError("404")})


# ---------------------------------------------------------------------------
# The metadata axis must not award credit for comparisons it did not make
# ---------------------------------------------------------------------------

def test_bare_doi_is_unverifiable_not_verified():
    """A DOI with no title/authors/year corroborating it.

    Old behaviour: verdict 'verified', score 0.90, detail "Title, authors, and
    year agree with the record" — asserting agreement between a record and
    nothing at all.
    """
    res = verify_reference(Reference(doi="10.5555/attention"), fetch=_crossref_only())

    assert res.verdict == "unverifiable"
    assert res.score < VERIFIED_SCORE_THRESHOLD
    assert res.metadata_match.status == "unknown"
    # The detail must not claim fields agreed when none were supplied.
    assert "agree with the record" not in (res.metadata_match.detail or "")
    # And it must say what the denominator actually was.
    assert "metadata_match" not in res.axes_evaluated
    assert "existence" in res.axes_evaluated


@pytest.mark.parametrize("cited_title", [
    "Attention is all you need for translation",   # ~0.76 — a different paper
    "Attention Is All You Need for Speech",        # ~0.79
])
def test_partial_title_match_cannot_reach_verified(cited_title):
    """The silent 0.60-0.85 band.

    Old behaviour: any similarity above TITLE_DISTINCT_THRESHOLD returned
    status='pass' with "Title, authors, and year agree with the record", so a
    subtly-wrong title — the signature of an LLM-hallucinated reference — scored
    'verified' 0.90.
    """
    sim = title_similarity(cited_title, "Attention Is All You Need")
    assert TITLE_DISTINCT_THRESHOLD <= sim < TITLE_MATCH_THRESHOLD, (
        f"fixture drifted out of the warn band: {sim}")

    res = verify_reference(
        Reference(doi="10.5555/attention", title=cited_title,
                  authors=["Vaswani, Ashish"], year=2017),
        fetch=_crossref_only())

    assert res.verdict != "verified"
    assert res.score < VERIFIED_SCORE_THRESHOLD
    assert res.metadata_match.status == "warn"
    assert "agree with the record" not in (res.metadata_match.detail or "")


@pytest.mark.parametrize("cited_title", [
    "Attention Is All You Need for Machine Translation Tasks",  # ~0.63, band floor
    "Attention Is All You Need for Speech Recognition",         # ~0.68
    "Is Attention All You Really Need?",                        # ~0.77, mid-band
    "Attention Is Really All That You Need",                    # ~0.81
    "Attention Is All You Need, Revisited",                     # ~0.83, band ceiling
])
def test_warn_band_is_never_reported_as_agreement(cited_title):
    """Sweep several titles across the warn band, not one fixture.

    The previous version of this test asserted `0.65 < TITLE_MATCH_THRESHOLD` —
    arithmetic on two constants, which passes against an empty module and proves
    nothing. It was pointed out in review, and it is exactly the failure the rest
    of this file exists to catch: a check that reports success without testing
    the behaviour it claims to test.

    Every title here must land strictly inside the band and must NOT be reported
    as agreement. If a fixture drifts out of the band the test says so rather
    than passing quietly.
    """
    sim = title_similarity(cited_title, "Attention Is All You Need")
    assert TITLE_DISTINCT_THRESHOLD <= sim < TITLE_MATCH_THRESHOLD, (
        f"fixture {cited_title!r} scored {sim:.3f}, outside the warn band "
        f"[{TITLE_DISTINCT_THRESHOLD}, {TITLE_MATCH_THRESHOLD}) — retune it")

    res = verify_reference(
        Reference(doi="10.5555/attention", title=cited_title,
                  authors=["Vaswani, Ashish"], year=2017),
        fetch=_crossref_only())

    assert res.metadata_match.status == "warn", (
        f"similarity {sim:.3f} reported as {res.metadata_match.status!r}")
    assert res.verdict != "verified"
    assert res.score < VERIFIED_SCORE_THRESHOLD
    detail = res.metadata_match.detail or ""
    assert "agree with the record" not in detail
    # The detail must name the mismatch it found, not just flag one.
    assert cited_title[:12].lower() in detail.lower() or "similarity" in detail.lower()


# ---------------------------------------------------------------------------
# The retraction axis must name the source it actually consulted
# ---------------------------------------------------------------------------

def test_retraction_unknown_when_neither_provider_has_the_doi():
    """Old behaviour: 'No retraction recorded in Crossref.' with confidence 0.9
    even when Crossref had never been consulted (DataCite-resolved DOIs carry no
    retraction-capable fields at all), so absence of data read as absence of
    retraction."""
    fetch = make_fetch({"crossref": LookupError("404"),
                        "openalex": LookupError("404"),
                        "datacite": LookupError("404")})
    res = verify_reference(
        Reference(doi="10.5281/zenodo.1", title="Some Dataset",
                  authors=["Smith, Jo"], year=2020),
        fetch=fetch)

    assert res.retraction.status == "unknown"
    detail = res.retraction.detail or ""
    # It may say it could not establish the status; it may not claim a clean
    # Crossref result it never obtained.
    assert "No retraction recorded in Crossref." != detail
    assert res.verdict != "verified"


def test_openalex_retraction_is_consulted_even_when_crossref_resolves():
    """README promises "Crossref's Retraction Watch data + OpenAlex is_retracted".

    Old behaviour: once Crossref resolved the DOI, OpenAlex was never asked, so a
    retraction recorded only in OpenAlex returned 'verified' 0.9.
    """
    fetch = make_fetch({"crossref": CROSSREF_CLEAN,
                        "openalex": OPENALEX_RETRACTED,
                        "datacite": LookupError("404")})
    res = verify_reference(Reference(**CLEAN_REF), fetch=fetch)

    assert res.retraction.status == "fail", (
        "an OpenAlex-only retraction must still be caught")
    assert res.verdict == "retracted"


# ---------------------------------------------------------------------------
# A transport failure is not a factual claim about the citation
# ---------------------------------------------------------------------------

def test_transport_failure_is_unknown_not_dead_link():
    """Old behaviour: DNS/TLS/reset/timeout all collapsed into status='fail' with
    "URL does not resolve (HTTP None) and is not archived." — reporting our own
    network trouble as a property of the cited work."""
    head = make_head({"example.invalid": (None, "https://example.invalid/x",
                                          "TimeoutError: read timed out")})
    fetch = make_fetch({"archive.org": LookupError("no snapshot")})

    res = check_url_liveness("https://example.invalid/x", http_head=head, fetch=fetch)

    assert res.status == "unknown"
    assert "HTTP None" not in (res.detail or "")
    assert "TimeoutError" in (res.detail or "")


def test_genuine_http_404_still_fails_the_url_axis():
    """The counterpart: a real HTTP response saying 404, with no Wayback copy, IS
    evidence the link is dead. The fix must not have made the axis toothless."""
    head = make_head({"gone": (404, "https://example.com/gone", None)})
    fetch = make_fetch({"archive.org": LookupError("no snapshot")})

    res = check_url_liveness("https://example.com/gone", http_head=head, fetch=fetch)

    assert res.status == "fail"


def test_dead_url_blocks_verified_verdict():
    """existence 0.5 + metadata 0.3 + url fail 0.0 landed on exactly 0.8, the
    'verified' threshold, so a reference with a dead unarchived URL was reported
    as safe to use."""
    fetch = make_fetch({"crossref": CROSSREF_CLEAN,
                        "openalex": LookupError("404"),
                        "datacite": LookupError("404"),
                        "archive.org": LookupError("no snapshot")})
    head = make_head({"gone": (404, "https://example.com/gone", None)})

    res = verify_reference(Reference(url="https://example.com/gone", **CLEAN_REF),
                           fetch=fetch, http_head=head)

    assert res.verdict == "warnings"
    assert res.score < VERIFIED_SCORE_THRESHOLD


def test_unreachable_url_scores_below_a_confirmed_one():
    """An axis we *attempted and could not complete* must not score identically to
    one we completed successfully.

    Dropping 'unknown' axes from the weighted mean is correct, but on its own it
    let "we never reached the host" score a flawless 1.0 — the same number as
    "we fetched it and it was live".
    """
    fetch = make_fetch({"crossref": CROSSREF_CLEAN,
                        "openalex": LookupError("404"),
                        "datacite": LookupError("404"),
                        "archive.org": LookupError("no snapshot")})

    live = verify_reference(
        Reference(url="https://ok.example/x", **CLEAN_REF), fetch=fetch,
        http_head=make_head({"ok.example": (200, "https://ok.example/x", None)}))
    unreachable = verify_reference(
        Reference(url="https://x.invalid/y", **CLEAN_REF), fetch=fetch,
        http_head=make_head({"x.invalid": (None, "https://x.invalid/y",
                                           "TimeoutError: read timed out")}))

    assert live.url_liveness.status == "pass"
    assert unreachable.url_liveness.status == "unknown"
    assert unreachable.score < live.score, (
        "an unverifiable URL must not score as high as a verified one")


# ---------------------------------------------------------------------------
# Names. A linguistics-facing tool that fails on diacritics is not credible.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("unicode_name,ascii_name", [
    ("Müller", "Muller"),
    ("Sørensen", "Sorensen"),
    ("Łukasiewicz", "Lukasiewicz"),
    ("Ó Séaghdha", "O Seaghdha"),
    ("Nuñez", "Nunez"),
    ("Zhāng", "Zhang"),
    ("Škoda", "Skoda"),
    ("Þórsdóttir", "Thorsdottir"),
])
def test_surname_folds_diacritics(unicode_name, ascii_name):
    """Old behaviour: the non-ASCII run was treated as a word break and the
    surname became the fragment AFTER the diacritic — 'Müller' -> 'ller',
    'Zhāng' -> 'ng', 'Ó Séaghdha' -> 'aghdha'. Correct citations by authors with
    non-English names drew false "author overlap low (0%)" warnings.
    """
    folded = _surname(unicode_name)
    assert folded == _surname(ascii_name), (
        f"{unicode_name!r} folded to {folded!r}, "
        f"{ascii_name!r} to {_surname(ascii_name)!r}")
    # And specifically: it must not be truncated to the diacritic's tail.
    assert len(folded) >= len(ascii_name.replace(" ", "")) - 2


@pytest.mark.parametrize("unicode_name,ascii_name", [
    ("Müller", "Muller"),
    ("Sørensen", "Sorensen"),
    ("Ó Séaghdha", "O Seaghdha"),
])
def test_author_overlap_survives_diacritics(unicode_name, ascii_name):
    assert author_overlap([ascii_name], [{"family": unicode_name}]) == 1.0
    assert author_overlap([unicode_name], [{"family": ascii_name}]) == 1.0


def test_bibtex_latex_accents_round_trip_to_full_overlap():
    r"""Old behaviour: parsing.py stripped braces but left the escape, so
    ``author={M\"uller, Hans and S\o rensen, Ida}`` yielded surnames
    ['m uller', 's o rensen'] and author_overlap == 0.0 against the true record —
    a false "author overlap low (0%)" on a perfectly correct citation.
    """
    refs = parse_bibtex(
        r'@article{k, title={A Paper}, '
        r'author={M\"uller, Hans and S\o rensen, Ida}, year={2020}}')
    assert len(refs) == 1

    overlap = author_overlap(refs[0].authors,
                             [{"family": "Müller"}, {"family": "Sørensen"}])
    assert overlap == 1.0, f"authors parsed as {refs[0].authors!r}"


# ---------------------------------------------------------------------------
# The score must never contradict the verdict
# ---------------------------------------------------------------------------

def test_score_and_verdict_never_disagree():
    """Downstream filters compare the number, humans read the word. The
    invariant: score >= VERIFIED_SCORE_THRESHOLD iff verdict == 'verified'.
    """
    fetch = make_fetch({"crossref": CROSSREF_CLEAN,
                        "openalex": LookupError("404"),
                        "datacite": LookupError("404"),
                        "archive.org": LookupError("no snapshot")})
    cases = [
        Reference(doi="10.5555/attention"),                       # unverifiable
        Reference(doi="10.5555/attention", title="A Survey of Llamas",
                  authors=["Nobody"], year=1999),                 # mismatch
        Reference(**CLEAN_REF),                                   # verified
        Reference(url="https://example.com/gone", **CLEAN_REF),   # dead url
    ]
    for ref in cases:
        res = verify_reference(ref, fetch=fetch,
                               http_head=make_head({"gone": (404, "u", None)}))
        if res.verdict == "verified":
            assert res.score >= VERIFIED_SCORE_THRESHOLD, res.verdict
        else:
            assert res.score < VERIFIED_SCORE_THRESHOLD, (
                f"{res.verdict!r} scored {res.score}, at or above the "
                f"'verified' threshold")


def test_axes_evaluated_is_reported_and_honest():
    """The score is a mean over the axes that ran, so the caller must be told
    which those were — otherwise 'of what could be checked' is unfalsifiable."""
    res = verify_reference(Reference(**CLEAN_REF), fetch=_crossref_only())

    assert res.axes_evaluated, "axes_evaluated must not be empty"
    # No URL was supplied, so that axis cannot be among those evaluated.
    assert "url_liveness" not in res.axes_evaluated
    assert res.url_liveness.status == "unknown"
