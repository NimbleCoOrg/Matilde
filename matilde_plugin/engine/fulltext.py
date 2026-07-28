"""Open-access full-text locator.

Given a DOI, resolve the best *legal* open-access location for the work — a
direct PDF where one exists, otherwise an OA landing page. Sources, in priority
order, are all free and unauthenticated (or email-gated, never key-gated):

  1. **OpenAlex** — ``open_access`` + ``best_oa_location`` (no email needed).
  2. **Unpaywall** — queried only when a contact email is supplied (its API
     requires ``?email=``). Often finds a publisher/repository OA copy OpenAlex
     hasn't indexed yet.
  3. **arXiv synthesis** — an arXiv-registered DOI (``10.48550/arXiv.<id>``)
     always has a legal PDF at ``arxiv.org/pdf/<id>``, even if the providers miss.

This locator only ever returns open-access sources. A paywalled work resolves to
``is_oa=False`` with no URL — it deliberately does **not** route around a
paywall. The point is to give a citation-verifier the *content* it can legally
read to ground a claim, not to bypass access controls.

Design mirrors ``citations``: all network I/O is injected via a ``fetch`` callable
``(url) -> parsed JSON dict`` (raising ``LookupError`` on 404), so every path is
unit-testable without a network. ``default_fetch`` is the stdlib production default.
"""
from __future__ import annotations

import dataclasses
import re
import urllib.parse
from dataclasses import dataclass, field
from typing import Callable, Optional

from .citations import OPENALEX_BASE, _fetch_json, _normalize_doi, default_fetch

FetchFn = Callable[..., dict]

UNPAYWALL_BASE = "https://api.unpaywall.org/v2"
# arXiv-registered DOIs look like 10.48550/arXiv.1706.03762 (case-insensitive).
_ARXIV_DOI = re.compile(r"^10\.48550/arxiv\.(.+)$", re.I)


# ---------------------------------------------------------------------------
# Data model
# ---------------------------------------------------------------------------

@dataclass
class FullTextResult:
    """The resolved best open-access location for a DOI (or a closed verdict)."""

    doi: str
    is_oa: bool = False
    oa_status: str = "unknown"   # gold | green | hybrid | bronze | closed | unknown
    best_url: str = ""           # best PDF if available, else best landing page
    pdf_url: str = ""
    landing_url: str = ""
    source: str = ""             # which provider produced the chosen location
    license: str = ""
    candidates: list = field(default_factory=list)
    # Providers that answered, and providers we could not reach. A lookup that
    # failed is not evidence that no open-access copy exists, so the two are
    # reported separately instead of both collapsing into is_oa=False.
    sources_consulted: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


# ---------------------------------------------------------------------------
# Provider parsers (each returns a normalized loc dict, or None on miss)
# ---------------------------------------------------------------------------

def _try_openalex(doi: str, fetch: FetchFn) -> tuple:
    """Return ``(loc_or_None, error_or_None)``.

    ``(None, None)`` means OpenAlex has no record for the DOI; ``(None, "…")``
    means OpenAlex could not be consulted at all. Reporting both as ``None`` is
    how "we could not check" became "there is no open-access copy".
    """
    work, err = _fetch_json(f"{OPENALEX_BASE}/works/doi:{doi}", fetch)
    if err is not None:
        return (None, f"openalex: {err}")
    if not isinstance(work, dict) or not work:
        return (None, None)
    oa = work.get("open_access") or {}
    best = work.get("best_oa_location") or {}
    # Only ``pdf_url`` is a guaranteed direct PDF. ``oa_url`` is the "best OA URL"
    # but is often a landing page — treat it as a landing fallback, never a PDF.
    return ({
        "is_oa": bool(oa.get("is_oa")),
        "oa_status": oa.get("oa_status") or "",
        "pdf_url": best.get("pdf_url") or "",
        "landing_url": best.get("landing_page_url") or oa.get("oa_url") or "",
        "license": best.get("license") or "",
        "version": best.get("version") or "",
        "host_type": (best.get("source") or {}).get("type") or "",
    }, None)


def _try_unpaywall(doi: str, fetch: FetchFn, email: str) -> tuple:
    """Return ``(loc_or_None, error_or_None)`` — see :func:`_try_openalex`."""
    email_q = urllib.parse.quote(email)
    data, err = _fetch_json(f"{UNPAYWALL_BASE}/{doi}?email={email_q}", fetch)
    if err is not None:
        return (None, f"unpaywall: {err}")
    if not isinstance(data, dict) or not data:
        return (None, None)
    best = data.get("best_oa_location") or {}
    return ({
        "is_oa": bool(data.get("is_oa")),
        "oa_status": data.get("oa_status") or "",
        "pdf_url": best.get("url_for_pdf") or "",
        "landing_url": best.get("url") or "",
        "license": best.get("license") or "",
        "version": best.get("version") or "",
        "host_type": best.get("host_type") or "",
    }, None)


def _arxiv_pdf(doi: str) -> str:
    m = _ARXIV_DOI.match(doi)
    return f"https://arxiv.org/pdf/{m.group(1)}" if m else ""


def _try_external_resolver(doi: str, fetch: FetchFn, resolver_url: str) -> tuple:
    """Ask a configured external full-text resolver for a PDF URL.

    This is a provider-neutral extension point: ``resolver_url`` points at a
    self-hosted or third-party service implementing the contract
    ``GET {resolver_url}/resolve?doi={doi} -> {"pdf_url": "...", ...}``. This
    package ships no such service and names no provider — the operator supplies
    one out of band (an institutional proxy, a paid API, anything). Consulted
    only after every legal open-access lookup has missed.

    Returns ``(pdf_url, error_or_None)``.
    """
    base = resolver_url.rstrip("/")
    doi_q = urllib.parse.quote(doi, safe="")
    data, err = _fetch_json(f"{base}/resolve?doi={doi_q}", fetch)
    if err is not None:
        return ("", f"external-resolver: {err}")
    return ((data or {}).get("pdf_url") or "", None)


# ---------------------------------------------------------------------------
# Assembly
# ---------------------------------------------------------------------------

def _from_loc(doi: str, loc: dict, source: str) -> FullTextResult:
    pdf = loc.get("pdf_url") or ""
    landing = loc.get("landing_url") or ""
    best = pdf or landing
    candidate = {
        "url": best, "pdf_url": pdf, "landing_url": landing,
        "license": loc.get("license") or "", "version": loc.get("version") or "",
        "host_type": loc.get("host_type") or "", "source": source,
    }
    return FullTextResult(
        doi=doi, is_oa=True, oa_status=loc.get("oa_status") or "unknown",
        best_url=best, pdf_url=pdf, landing_url=landing, source=source,
        license=loc.get("license") or "", candidates=[candidate],
    )


def find_open_access(doi: str, fetch: Optional[FetchFn] = None, *,
                     email: Optional[str] = None,
                     resolver_url: Optional[str] = None) -> FullTextResult:
    """Resolve the best full-text location for *doi*, preferring legal open access.

    Returns a :class:`FullTextResult`. Legal open-access sources are tried first
    (OpenAlex, then Unpaywall if *email* is given, then an arXiv-DOI synthesis).
    ``is_oa=False`` means no open-access copy was found.

    If — and only if — every OA lookup misses and *resolver_url* is configured,
    a provider-neutral external resolver is consulted as a last resort. A hit
    there populates the full-text URL but keeps ``is_oa=False`` and
    ``source="external-resolver"``: it is full text, not open access, and the
    result says so. A legal OA copy always wins, so a configured resolver is
    never consulted when an open-access copy exists.
    """
    fetch = fetch or default_fetch
    doi = _normalize_doi(doi)
    result = FullTextResult(doi=doi)
    if not doi:
        return result

    consulted: list = []
    errors: list = []

    def _finish(res: FullTextResult) -> FullTextResult:
        res.sources_consulted = consulted
        res.errors = errors
        return res

    # 1. OpenAlex (primary, no email needed)
    oa, err = _try_openalex(doi, fetch)
    if err:
        errors.append(err)
    else:
        consulted.append("openalex")
    if oa and oa["is_oa"] and (oa["pdf_url"] or oa["landing_url"]):
        return _finish(_from_loc(doi, oa, "openalex"))
    if oa is not None:
        result.is_oa = oa["is_oa"]
        result.oa_status = oa["oa_status"] or "closed"

    # 2. Unpaywall (only with a contact email)
    if email:
        up, err = _try_unpaywall(doi, fetch, email)
        if err:
            errors.append(err)
        else:
            consulted.append("unpaywall")
        if up and up["is_oa"] and (up["pdf_url"] or up["landing_url"]):
            return _finish(_from_loc(doi, up, "unpaywall"))

    # 3. arXiv synthesis — a registered arXiv DOI always has a legal PDF
    arx = _arxiv_pdf(doi)
    if arx:
        consulted.append("arxiv")
        return _finish(_from_loc(doi, {"pdf_url": arx, "oa_status": "green",
                                       "host_type": "repository"}, "arxiv"))

    # A provider we could not reach cannot support the conclusion "closed": we did
    # not establish that no open-access copy exists, only that we failed to look.
    if errors and not result.is_oa:
        result.oa_status = "unknown"

    # 4. External resolver (opt-in, last resort) — full text, NOT open access.
    if resolver_url:
        pdf, err = _try_external_resolver(doi, fetch, resolver_url)
        if err:
            errors.append(err)
        if pdf:
            result.pdf_url = pdf
            result.best_url = pdf
            result.source = "external-resolver"
            result.candidates = [{"url": pdf, "pdf_url": pdf, "landing_url": "",
                                  "license": "", "version": "", "host_type": "",
                                  "source": "external-resolver"}]

    return _finish(result)
