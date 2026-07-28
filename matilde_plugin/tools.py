"""Matilde tools for Hermes — verifiable-citation checking.

These tools expose the bundled ``.engine.citations`` verifier to the agent. A citation is
checked along four axes (existence, metadata-match, retraction, URL-liveness) and
scored. This is *verifiable*, not *provably correct* — the score is a confidence,
not a proof. Claim-support grounding (does the cited passage back the claim?) is a
v2 axis and is not wired here.

No API key is required: Crossref, OpenAlex, and DataCite are all free and
unauthenticated. Set ``MATILDE_CONTACT_EMAIL`` to join the providers' polite
pools (recommended, not required).

Handlers are plain functions ``(args: dict, **kwargs) -> str`` returning a JSON
string built by ``_tool_result`` / ``_tool_error``. They never raise.
"""

from __future__ import annotations

import json
import math
from typing import Any

# The engine lives INSIDE this plugin package (matilde_plugin/engine/), so it is
# imported via relative imports below — no sys.path manipulation needed. Imports
# are done lazily at call time so the plugin still registers (the _check_available
# gate guards execution) even if an import would fail.


# ---------------------------------------------------------------------------
# Shared envelope helpers
# ---------------------------------------------------------------------------

def _json_safe(obj: Any) -> Any:
    """Recursively make *obj* strict-JSON-serializable.

    Non-finite floats (``NaN``/``Infinity``) are the trap: ``json.dumps`` emits
    them as bare ``NaN``/``Infinity`` literals, which are INVALID JSON, so a
    strict downstream parser rejects the whole envelope and treats the tool call
    as an error (this is what made ``matilde_study_status`` fail on a real run
    whose finding evidence held a non-finite measured value). We convert those
    to ``None`` so the envelope is always valid JSON. Numpy scalars are handled
    by ``default=str`` at dump time.
    """
    if isinstance(obj, float):
        return obj if math.isfinite(obj) else None
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def _dumps(payload: dict) -> str:
    """Serialize an envelope to strictly-valid JSON (no bare NaN/Infinity)."""
    # allow_nan=False makes any non-finite that slipped past _json_safe (e.g.
    # inside a numpy type stringified by default=str — harmless) a hard failure
    # rather than silent invalid output; _json_safe already neutralizes plain
    # Python floats so this should not trigger.
    return json.dumps(_json_safe(payload), default=str, allow_nan=False)


def _tool_result(data: Any = None, **kwargs: Any) -> str:
    if data is not None:
        payload = data if isinstance(data, dict) else {"result": data}
    else:
        payload = kwargs
    payload.setdefault("success", True)
    return _dumps(payload)


def _tool_error(message: str, **extra: Any) -> str:
    return _dumps({"error": message, "success": False, **extra})


# ---------------------------------------------------------------------------
# Availability gate
# ---------------------------------------------------------------------------

def _check_available() -> bool:
    """Matilde's citation tools need no credentials — only that the engine imports.

    The provider APIs (Crossref/OpenAlex/DataCite) are free and unauthenticated,
    so the only real prerequisite is that the engine module loads.
    """
    try:
        from .engine import citations  # noqa: F401
        return True
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Argument coercion
# ---------------------------------------------------------------------------

def _reference_from_args(d: dict) -> Any:
    from .engine.citations import Reference
    authors = d.get("authors") or []
    if isinstance(authors, str):
        # accept "Vaswani; Shazeer" or "Vaswani, Shazeer"
        sep = ";" if ";" in authors else ","
        authors = [a.strip() for a in authors.split(sep) if a.strip()]
    year = d.get("year")
    try:
        year = int(year) if year not in (None, "") else None
    except (TypeError, ValueError):
        year = None
    return Reference(
        raw=str(d.get("raw", "")),
        title=str(d.get("title", "")).strip(),
        authors=list(authors),
        year=year,
        doi=str(d.get("doi", "")).strip(),
        venue=str(d.get("venue", "")).strip(),
        url=str(d.get("url", "")).strip(),
    )


# ---------------------------------------------------------------------------
# Tool: verify a single citation
# ---------------------------------------------------------------------------

VERIFY_CITATION_SCHEMA = {
    "name": "matilde_verify_citation",
    "description": (
        "Verify a single academic citation along four axes — existence (does the "
        "work exist, via Crossref/OpenAlex/DataCite), metadata-match (do title/"
        "authors/year agree), retraction (Retraction Watch via Crossref/OpenAlex), "
        "and URL-liveness — and return a composite verifiability score (0-1) plus a "
        "verdict: verified | warnings | retracted | not_found | unverifiable. "
        "Provide as many fields as you have; a DOI gives the most reliable result. "
        "Use this before trusting any reference an LLM produced — hallucinated "
        "references are common."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "description": "DOI (bare, or as a doi.org URL). Most reliable signal."},
            "title": {"type": "string", "description": "Paper title. Used for existence search when no DOI, and always for metadata-match."},
            "authors": {"type": "array", "items": {"type": "string"}, "description": "Author names or surnames (e.g. ['Vaswani', 'Shazeer'])."},
            "year": {"type": "integer", "description": "Publication year."},
            "venue": {"type": "string", "description": "Journal/conference (optional)."},
            "url": {"type": "string", "description": "A URL for the work, if cited. Checked for liveness with a Wayback fallback."},
        },
        "required": [],
    },
}


def _handle_verify_citation(args: dict, **kwargs: Any) -> str:
    if not (args.get("doi") or args.get("title")):
        return _tool_error("Provide at least a 'doi' or a 'title' to verify.")
    try:
        from .engine.citations import verify_reference
        ref = _reference_from_args(args)
        result = verify_reference(ref)
        out = result.to_dict()
        return _tool_result(
            verdict=result.verdict,
            score=result.score,
            axes={
                "existence": result.existence.status,
                "metadata_match": result.metadata_match.status,
                "retraction": result.retraction.status,
                "url_liveness": result.url_liveness.status,
            },
            detail=out,
            message=f"Citation verdict: {result.verdict} (score {result.score}).",
        )
    except Exception as exc:  # never raise out of a handler
        return _tool_error(f"verify_citation failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool: verify a whole bibliography
# ---------------------------------------------------------------------------

VERIFY_BIBLIOGRAPHY_SCHEMA = {
    "name": "matilde_verify_bibliography",
    "description": (
        "Verify a list of citations at once and return per-reference verdicts plus "
        "a summary (counts by verdict, and the indices of any not_found or retracted "
        "references — the ones that most need attention). Use this to audit a "
        "reference list or bibliography in bulk."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "references": {
                "type": "array",
                "description": "List of citation objects, each with any of: doi, title, authors, year, venue, url.",
                "items": {
                    "type": "object",
                    "properties": {
                        "doi": {"type": "string"},
                        "title": {"type": "string"},
                        "authors": {"type": "array", "items": {"type": "string"}},
                        "year": {"type": "integer"},
                        "venue": {"type": "string"},
                        "url": {"type": "string"},
                    },
                },
            },
        },
        "required": ["references"],
    },
}


def _handle_verify_bibliography(args: dict, **kwargs: Any) -> str:
    refs = args.get("references")
    if not isinstance(refs, list) or not refs:
        return _tool_error("'references' must be a non-empty list of citation objects.")
    try:
        from .engine.citations import verify_reference
    except Exception as exc:
        return _tool_error(f"verify_bibliography failed: {type(exc).__name__}: {exc}")

    results, summary = [], {}
    flagged = []
    errors = 0
    for i, item in enumerate(refs):
        if not isinstance(item, dict):
            errors += 1
            results.append({"index": i, "error": "not an object"})
            continue
        ref = _reference_from_args(item)
        label = ref.title or ref.doi or ref.raw
        # Per-reference isolation: one transient network error (a 429 on
        # reference 40 of 50) used to abort the whole run and discard every
        # verdict already computed. A bibliography audit must be resumable and
        # must never silently lose work.
        try:
            r = verify_reference(ref)
        except Exception as exc:
            errors += 1
            results.append({"index": i, "title": label, "verdict": "error",
                            "error": f"{type(exc).__name__}: {exc}"})
            flagged.append({"index": i, "verdict": "error", "title": label,
                            "reason": f"verification failed: "
                                      f"{type(exc).__name__}: {exc}"})
            continue
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
        if r.verdict in ("not_found", "retracted"):
            flagged.append({"index": i, "verdict": r.verdict, "title": label,
                            "reason": r.verdict})
        elif r.metadata_match.status == "fail":
            # The DOI resolves, but to a *different paper*. Materially worse than
            # a year typo, which also lands on the "warnings" verdict.
            flagged.append({"index": i, "verdict": r.verdict, "title": label,
                            "reason": "metadata_mismatch",
                            "detail": r.metadata_match.detail})
        results.append({
            "index": i, "verdict": r.verdict, "score": r.score,
            "axes_evaluated": r.axes_evaluated,
            "metadata_match": r.metadata_match.status,
            "title": ref.title or ref.doi, "detail": r.to_dict(),
        })

    verified_count = len(refs) - errors
    message = (f"Verified {verified_count} of {len(refs)} references: " +
               ", ".join(f"{k}={v}" for k, v in sorted(summary.items())))
    if errors:
        message += (f". {errors} reference(s) could not be checked "
                    f"(see results[].error) — their status is unknown, not clean")
    message += f". {len(flagged)} need attention." if flagged else "."
    return _tool_result(
        count=len(refs),
        verified=verified_count,
        errors=errors,
        summary=summary,
        needs_attention=flagged,
        results=results,
        message=message,
    )


# ---------------------------------------------------------------------------
# Tool: quick retraction check
# ---------------------------------------------------------------------------

CHECK_RETRACTION_SCHEMA = {
    "name": "matilde_check_retraction",
    "description": (
        "Quickly check whether a work (by DOI) has been retracted, using Crossref's "
        "Retraction Watch data. Returns status 'pass' (not retracted), 'fail' "
        "(RETRACTED), or 'unknown' (DOI not found / no DOI). Faster than a full "
        "verification when you only care about retraction status."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "description": "DOI to check (bare or doi.org URL)."},
        },
        "required": ["doi"],
    },
}


def _handle_check_retraction(args: dict, **kwargs: Any) -> str:
    doi = str(args.get("doi", "")).strip()
    if not doi:
        return _tool_error("'doi' is required.")
    try:
        from .engine.citations import check_retraction, default_fetch
        res = check_retraction(doi, fetch=default_fetch)
        return _tool_result(
            doi=doi,
            status=res.status,
            retracted=(res.status == "fail"),
            detail=res.detail,
            evidence=res.evidence,
            message=res.detail,
        )
    except Exception as exc:
        return _tool_error(f"check_retraction failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool: OpenNeuro dataset metadata
# ---------------------------------------------------------------------------

OPENNEURO_INFO_SCHEMA = {
    "name": "matilde_openneuro_dataset_info",
    "description": (
        "Get metadata for an OpenNeuro neuroimaging dataset by accession ID "
        "(e.g. 'ds000246'): human title, authors, imaging modalities (meg/mri/"
        "eeg/ieeg/ecog), subjects, tasks, total size, and latest snapshot tag. "
        "OpenNeuro hosts public BIDS-formatted brain-imaging datasets."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "OpenNeuro accession ID, e.g. 'ds000246'."},
        },
        "required": ["dataset_id"],
    },
}


def _handle_openneuro_dataset_info(args: dict, **kwargs: Any) -> str:
    dsid = str(args.get("dataset_id", "")).strip()
    if not dsid:
        return _tool_error("'dataset_id' is required (e.g. 'ds000246').")
    try:
        from .engine.openneuro import get_dataset, OpenNeuroError
        try:
            ds = get_dataset(dsid)
        except OpenNeuroError as exc:
            return _tool_error(str(exc), dataset_id=dsid)
        payload = ds.to_dict()
        payload["message"] = (f"{ds.id}: '{ds.name}' — modalities {ds.modalities}, "
                              f"{len(ds.subjects)} subject(s), {ds.size} bytes "
                              f"(snapshot {ds.latest_tag}).")
        return _tool_result(payload)
    except Exception as exc:
        return _tool_error(f"openneuro_dataset_info failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool: OpenNeuro dataset listing
# ---------------------------------------------------------------------------

OPENNEURO_SEARCH_SCHEMA = {
    "name": "matilde_openneuro_search",
    "description": (
        "List OpenNeuro dataset accession IDs (most recent first) to discover "
        "datasets. This lists rather than full-text-searches; call "
        "matilde_openneuro_dataset_info on candidates to inspect modalities/tasks "
        "and filter. Use to find brain-imaging datasets to analyze or replicate."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many dataset IDs to return (default 20)."},
        },
        "required": [],
    },
}


def _handle_openneuro_search(args: dict, **kwargs: Any) -> str:
    try:
        from .engine.openneuro import list_datasets
        limit = args.get("limit") or 20
        try:
            limit = max(1, min(int(limit), 100))
        except (TypeError, ValueError):
            limit = 20
        ids = list_datasets(limit=limit)
        return _tool_result(count=len(ids), dataset_ids=ids,
                            message=f"Listed {len(ids)} OpenNeuro dataset(s).")
    except Exception as exc:
        return _tool_error(f"openneuro_search failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool: OpenNeuro file listing
# ---------------------------------------------------------------------------

OPENNEURO_FILES_SCHEMA = {
    "name": "matilde_openneuro_list_files",
    "description": (
        "List the files in an OpenNeuro dataset's latest snapshot — filename, size, "
        "and a direct download URL for each. Use to inspect a dataset's structure "
        "(BIDS layout) or to get a URL for a specific file before downloading."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "dataset_id": {"type": "string", "description": "OpenNeuro accession ID, e.g. 'ds000246'."},
            "tag": {"type": "string", "description": "Snapshot tag (optional; defaults to the latest)."},
        },
        "required": ["dataset_id"],
    },
}


def _handle_openneuro_list_files(args: dict, **kwargs: Any) -> str:
    dsid = str(args.get("dataset_id", "")).strip()
    if not dsid:
        return _tool_error("'dataset_id' is required (e.g. 'ds000246').")
    try:
        from .engine.openneuro import list_files, OpenNeuroError
        tag = str(args.get("tag", "")).strip() or None
        try:
            files = list_files(dsid, tag=tag)
        except OpenNeuroError as exc:
            return _tool_error(str(exc), dataset_id=dsid)
        return _tool_result(
            dataset_id=dsid, count=len(files), files=files,
            message=f"{dsid} has {len(files)} file(s) in its snapshot.",
        )
    except Exception as exc:
        return _tool_error(f"openneuro_list_files failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Tool: open-access full-text locator
# ---------------------------------------------------------------------------

FETCH_FULLTEXT_SCHEMA = {
    "name": "matilde_fetch_fulltext",
    "description": (
        "Find the best *legal open-access* full-text location for a paper by DOI "
        "— a direct PDF where one exists, else an OA landing page — using OpenAlex, "
        "Unpaywall, and arXiv. Use this to retrieve the actual paper content so a "
        "claim can be grounded against the source, not just its metadata. Returns "
        "is_oa, oa_status (gold/green/hybrid/bronze/closed), pdf_url, landing_url, "
        "license, and the provider. If no open-access copy exists, is_oa is false. "
        "Set MATILDE_CONTACT_EMAIL (or pass 'email') to enable the Unpaywall lookup, "
        "which widens coverage. If an external full-text resolver is configured "
        "(MATILDE_FULLTEXT_RESOLVER_URL or 'resolver_url'), it is consulted only as "
        "a last resort after every open-access lookup misses; such a result is "
        "flagged is_oa=false with source 'external-resolver' — it is full text, not "
        "open access, so confirm you have the right to access it."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "doi": {"type": "string", "description": "DOI of the work (bare, or as a doi.org URL)."},
            "email": {"type": "string", "description": "Contact email to enable the Unpaywall lookup (optional; falls back to MATILDE_CONTACT_EMAIL)."},
            "resolver_url": {"type": "string", "description": "Base URL of an external full-text resolver to use as a last resort (optional; falls back to MATILDE_FULLTEXT_RESOLVER_URL). Off unless configured."},
        },
        "required": ["doi"],
    },
}


def _handle_fetch_fulltext(args: dict, **kwargs: Any) -> str:
    doi = str(args.get("doi", "")).strip()
    if not doi:
        return _tool_error("'doi' is required (bare or doi.org URL).")
    try:
        import os
        from .engine.fulltext import find_open_access
        email = (str(args.get("email", "")).strip()
                 or os.environ.get("MATILDE_CONTACT_EMAIL", "").strip()
                 or None)
        resolver_url = (str(args.get("resolver_url", "")).strip()
                        or os.environ.get("MATILDE_FULLTEXT_RESOLVER_URL", "").strip()
                        or None)
        res = find_open_access(doi, email=email, resolver_url=resolver_url)
        payload = res.to_dict()
        if res.is_oa:
            payload["message"] = (f"Open access ({res.oa_status}) via {res.source}: "
                                  f"{res.best_url}")
        elif res.best_url:
            # Full text via a configured external resolver — NOT open access.
            payload["message"] = (f"Full text via {res.source} (NOT open access — "
                                  f"verify you have the right to access it): "
                                  f"{res.best_url}")
        elif res.errors:
            # Not the same statement as "no open-access copy exists".
            payload["message"] = (
                f"Open-access lookup for {res.doi} is INCOMPLETE — "
                f"{len(res.errors)} provider(s) could not be consulted "
                f"({'; '.join(res.errors)}). "
                f"Consulted successfully: {', '.join(res.sources_consulted) or 'none'}. "
                f"No copy was found, but absence was not established.")
        else:
            payload["message"] = (f"No open-access copy found for {res.doi} "
                                  f"(status: {res.oa_status}; consulted: "
                                  f"{', '.join(res.sources_consulted) or 'none'}).")
        return _tool_result(payload)
    except Exception as exc:
        return _tool_error(f"fetch_fulltext failed: {type(exc).__name__}: {exc}")


# ---------------------------------------------------------------------------
# Study pipeline — durable, resumable, agent-drivable analyses
#
# These tools let the agent kick off / advance / inspect a long analysis without
# holding it in a single turn. State lives in a SQLite ``StudyStore`` on the
# mounted data volume, so an OOM or container rebuild mid-run is recovered by
# simply calling matilde_study_run again. Logic lives in engine/store.py +
# engine/pipeline.py; these handlers stay thin.
# ---------------------------------------------------------------------------

def _open_store() -> Any:
    """Open the StudyStore at the injected/default on-volume db path."""
    from .engine.store import StudyStore
    return StudyStore()  # default_db_path() honors MATILDE_STUDY_DB / HERMES_HOME


def _coerce_plan(plan: Any) -> list:
    """Accept a list of step names or a comma/newline-separated string."""
    if isinstance(plan, list):
        return [str(p).strip() for p in plan if str(p).strip()]
    if isinstance(plan, str):
        sep = "," if "," in plan else "\n"
        return [p.strip() for p in plan.split(sep) if p.strip()]
    return []


STUDY_CREATE_SCHEMA = {
    "name": "matilde_study_create",
    "description": (
        "Create a durable, resumable study — a long analysis decomposed into "
        "ordered steps whose state is checkpointed to disk so it survives an OOM "
        "or container rebuild. Provide a unique 'slug', a human 'title', and a "
        "'plan' (ordered step names). Returns the study_id. For the built-in "
        "bibliography-validation study, set kind='bibliography' and pass 'bibtex'; "
        "then call matilde_study_run to verify each reference. Creating with an "
        "existing slug returns that study (idempotent)."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "slug": {"type": "string", "description": "Unique short id for the study (e.g. 'paper42-bib')."},
            "title": {"type": "string", "description": "Human-readable title."},
            "plan": {"type": "array", "items": {"type": "string"},
                     "description": "Ordered step names (e.g. ['parse_refs','verify_each','summarize'])."},
            "kind": {"type": "string", "description": "Optional study kind. 'bibliography' wires the built-in reference-validation steps; 'meg_validation' wires the memory-bounded MEG evoked-peak (M100) validation steps; 'golden_meg_validation' runs the offline, dependency-free worked example (a planted in-window M100 -> 'supported') — the self-demonstrating reference recipe, no mne or download needed."},
            "bibtex": {"type": "string", "description": "For kind='bibliography': the BibTeX to validate (stored on the study)."},
            "dataset_id": {"type": "string", "description": "For kind='meg_validation': the open MEG dataset to validate against (e.g. 'bst_auditory')."},
            "expected_window_ms": {"type": "array", "items": {"type": "number"}, "description": "For kind='meg_validation': the [low, high] expected peak-latency window in ms (default [80, 120] for the auditory M100)."},
            "bounds": {"type": "object", "description": "For kind='meg_validation': sample-bounding overrides (run, max_channels, crop_tmax, resample_hz, l_freq, h_freq, tmin, tmax) keeping the loaded slice small."},
        },
        "required": ["slug"],
    },
}


def _handle_study_create(args: dict, **kwargs: Any) -> str:
    slug = str(args.get("slug", "")).strip()
    if not slug:
        return _tool_error("'slug' is required (a unique short id for the study).")
    try:
        title = str(args.get("title", "")).strip() or slug
        plan = _coerce_plan(args.get("plan"))
        kind = str(args.get("kind", "")).strip()
        bibtex = args.get("bibtex")
        if kind == "bibliography" and not plan:
            plan = ["parse_refs", "verify_each", "summarize"]
        if kind in ("meg_validation", "golden_meg_validation") and not plan:
            plan = ["fetch_sample", "preprocess", "epoch", "evoked",
                    "validate_finding"]
        meta: dict = {}
        if kind:
            meta["kind"] = kind
        if isinstance(bibtex, str) and bibtex.strip():
            meta["bibtex"] = bibtex
        if kind == "meg_validation":
            dsid = str(args.get("dataset_id", "")).strip() or "bst_auditory"
            meta["dataset_id"] = dsid
            window = args.get("expected_window_ms")
            if isinstance(window, (list, tuple)) and len(window) == 2:
                try:
                    meta["expected_window_ms"] = [float(window[0]),
                                                  float(window[1])]
                except (TypeError, ValueError):
                    pass
            bounds = args.get("bounds")
            if isinstance(bounds, dict):
                meta["bounds"] = bounds
        store = _open_store()
        # create_study is idempotent on slug: an existing study is *reused*, and
        # the submitted plan/meta are NOT applied. Reporting the submitted plan
        # back described a study that does not exist.
        existing = store.get_study_by_slug(slug)
        sid = store.create_study(slug=slug, title=title, plan=plan, meta=meta)
        stored = store.get_study(sid) or {}
        stored_plan = stored.get("plan") or []
        store.add_steps(sid, stored_plan)
        if existing is not None:
            reused_note = ""
            if stored_plan != plan:
                reused_note = (f" The submitted plan ({len(plan)} step(s)) was NOT "
                               f"applied; the stored plan is unchanged.")
            return _tool_result(
                study_id=sid, slug=slug, plan=stored_plan, reused=True,
                submitted_plan=plan,
                message=(f"Study {sid} ('{slug}') already existed — reusing it "
                         f"with its stored plan of {len(stored_plan)} step(s)."
                         + reused_note))
        return _tool_result(
            study_id=sid, slug=slug, plan=stored_plan, reused=False,
            message=f"Created study {sid} ('{slug}') with {len(stored_plan)} step(s).")
    except Exception as exc:
        return _tool_error(f"study_create failed: {type(exc).__name__}: {exc}")


STUDY_RUN_SCHEMA = {
    "name": "matilde_study_run",
    "description": (
        "Advance a study: run its pending steps in order, checkpointing after "
        "each. Already-completed steps are skipped, so if a previous run was "
        "OOM-killed or the container was rebuilt mid-run, just call this again to "
        "resume from the last completed step. Returns the study status and a "
        "per-step summary; on a step failure the study is left 'blocked' and "
        "resumable. For bibliography studies the BibTeX stored at create time is "
        "used automatically."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "study_id": {"type": "integer", "description": "The study to advance (from matilde_study_create)."},
        },
        "required": ["study_id"],
    },
}


def _handle_study_run(args: dict, **kwargs: Any) -> str:
    sid_raw = args.get("study_id")
    try:
        sid = int(sid_raw)
    except (TypeError, ValueError):
        return _tool_error("'study_id' is required and must be an integer.")
    try:
        from .engine.pipeline import run
        store = _open_store()
        study = store.get_study(sid)
        if study is None:
            return _tool_error(f"No study with id {sid}.", study_id=sid)
        meta = study.get("meta") or {}
        kind = meta.get("kind")
        if kind == "bibliography":
            from .engine.bibliography_study import build_steps
            steps = build_steps(bibtex=meta.get("bibtex", ""))
        elif kind == "golden_meg_validation":
            # Offline, dependency-free worked example — plants a known in-window
            # M100 and yields `supported`. The self-demonstrating reference
            # recipe; needs no mne / download (docs/golden-validation-recipe.md).
            from .engine import meg_study
            steps = meg_study.build_golden_steps()
        elif kind == "meg_validation":
            from .engine import meg_study
            window = meta.get("expected_window_ms") or list(
                meg_study.DEFAULT_WINDOW_MS)
            # _TEST_IO is a stdlib test hook (None in production -> real lazy-mne).
            steps = meg_study.build_steps(
                dataset_id=meta.get("dataset_id", "bst_auditory"),
                io=getattr(meg_study, "_TEST_IO", None),
                expected_window_ms=(window[0], window[1]),
                bounds=meta.get("bounds"))
        else:
            return _tool_error(
                f"Study {sid} has no runnable step implementations "
                f"(kind={kind!r}). Built-in runner currently supports "
                f"kind='bibliography', 'meg_validation', and "
                f"'golden_meg_validation'.", study_id=sid)
        summary = run(store, sid, steps)
        return _tool_result(
            study_id=sid, status=summary["status"], steps=summary["steps"],
            failed_step=summary.get("failed_step"),
            message=f"Study {sid} is now '{summary['status']}'.")
    except Exception as exc:
        return _tool_error(f"study_run failed: {type(exc).__name__}: {exc}")


STUDY_STATUS_SCHEMA = {
    "name": "matilde_study_status",
    "description": (
        "Get the full status of a study: its steps (with per-step status), "
        "recorded findings (claim + verdict + evidence), artifacts, and a "
        "step-status tally. Use this to inspect progress or read out the "
        "scientific findings after a run."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "study_id": {"type": "integer", "description": "The study to inspect."},
        },
        "required": ["study_id"],
    },
}


def _handle_study_status(args: dict, **kwargs: Any) -> str:
    sid_raw = args.get("study_id")
    try:
        sid = int(sid_raw)
    except (TypeError, ValueError):
        return _tool_error("'study_id' is required and must be an integer.")
    try:
        store = _open_store()
        summary = store.study_summary(sid)
        if summary is None:
            return _tool_error(f"No study with id {sid}.", study_id=sid)
        study = summary["study"]
        return _tool_result(
            study_id=sid,
            slug=study["slug"],
            status=study["status"],
            steps=summary["steps"],
            findings=summary["findings"],
            finding_count=summary["finding_count"],
            artifacts=summary["artifacts"],
            step_status_counts=summary["step_status_counts"],
            message=(f"Study {sid} ('{study['slug']}') is '{study['status']}' — "
                     f"{summary['finding_count']} finding(s)."))
    except Exception as exc:
        return _tool_error(f"study_status failed: {type(exc).__name__}: {exc}")


STUDY_LIST_SCHEMA = {
    "name": "matilde_study_list",
    "description": (
        "List recent studies (most recent first) with their slug, title, and "
        "status. Use to discover existing studies — e.g. to find one to resume "
        "after a restart."
    ),
    "parameters": {
        "type": "object",
        "properties": {
            "limit": {"type": "integer", "description": "How many studies to return (default 20)."},
        },
        "required": [],
    },
}


def _handle_study_list(args: dict, **kwargs: Any) -> str:
    try:
        limit = args.get("limit") or 20
        try:
            limit = max(1, min(int(limit), 200))
        except (TypeError, ValueError):
            limit = 20
        store = _open_store()
        studies = [
            {"id": s["id"], "slug": s["slug"], "title": s["title"],
             "status": s["status"], "updated_at": s["updated_at"]}
            for s in store.list_studies(limit=limit)
        ]
        return _tool_result(
            count=len(studies), studies=studies,
            message=f"Listed {len(studies)} study(ies).")
    except Exception as exc:
        return _tool_error(f"study_list failed: {type(exc).__name__}: {exc}")
