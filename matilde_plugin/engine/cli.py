"""Matilde command-line interface — verify a bibliography from the terminal.

Usage::

    python3 -m matilde_plugin.engine.cli refs.bib                 # verify a BibTeX file
    python3 -m matilde_plugin.engine.cli dois.txt                 # verify a list of DOIs
    python3 -m matilde_plugin.engine.cli --doi 10.1038/171737a0   # verify one DOI
    python3 -m matilde_plugin.engine.cli refs.bib --json          # machine-readable output
    python3 -m matilde_plugin.engine.cli refs.bib --email you@example.org   # polite-pool contact

Exit codes: 0 = all references verified / only warnings; 1 = at least one
reference that needs attention — ``not_found``, ``retracted``, or a metadata-axis
``fail`` (the DOI resolves to a different paper) — useful as a pre-commit / CI
gate on a manuscript's .bib; 2 = usage error.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from typing import Callable, Optional

from .citations import Reference, verify_reference
from .parsing import parse_bibtex, parse_dois

_VERDICT_MARK = {
    "verified": "OK ",
    "warnings": "!  ",
    "unverifiable": "?  ",
    "not_found": "XX ",
    "retracted": "RET",
}


def load_references(text: str, hint: str = "") -> list:
    """Load references from *text*, choosing BibTeX vs DOI-list by content/hint."""
    if "@" in text and "=" in text or hint.endswith(".bib"):
        refs = parse_bibtex(text)
        if refs:
            return refs
    return parse_dois(text)


def _format_text(results: list) -> str:
    lines, summary = [], {}
    flagged = []
    for i, r in enumerate(results):
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
        label = r.reference.title or r.reference.doi or r.reference.raw[:50] or "(no id)"
        mark = _VERDICT_MARK.get(r.verdict, "   ")
        lines.append(f"  [{mark}] {r.verdict:<12} {r.score:>4}  {label}")
        if r.verdict in ("not_found", "retracted"):
            flagged.append((i, r.verdict, label, ""))
        elif getattr(r, "metadata_match", None) is not None \
                and r.metadata_match.status == "fail":
            # A DOI that resolves to a *different paper* is not the same class of
            # problem as a year typo, though both are verdict 'warnings'.
            flagged.append((i, r.verdict, label,
                            f"metadata mismatch — {r.metadata_match.detail}"))
    header = f"Verified {len(results)} reference(s):"
    tally = "  ".join(f"{k}={v}" for k, v in sorted(summary.items()))
    out = [header, *lines, "", tally]
    if flagged:
        out.append("")
        out.append("Needs attention:")
        for i, verdict, label, note in flagged:
            out.append(f"  - #{i} [{verdict}] {label}"
                       + (f"\n      {note}" if note else ""))
    return "\n".join(out)


def _format_json(results: list) -> str:
    summary: dict = {}
    for r in results:
        summary[r.verdict] = summary.get(r.verdict, 0) + 1
    return json.dumps({
        "count": len(results),
        "summary": summary,
        "results": [r.to_dict() for r in results],
    }, default=str, indent=2)


def _needs_attention(r) -> bool:
    """Is *r* a finding a manuscript gate must not let through?

    ``not_found`` and ``retracted``, plus a metadata-axis ``fail`` — a DOI that
    resolves to an entirely different paper. That last one lands on the verdict
    ``warnings``, the same bucket as a one-digit year typo, so gating on the
    verdict alone let it pass.
    """
    if r.verdict in ("not_found", "retracted"):
        return True
    metadata = getattr(r, "metadata_match", None)
    return metadata is not None and metadata.status == "fail"


def _exit_code(results: list) -> int:
    return 1 if any(_needs_attention(r) for r in results) else 0


def main(argv: Optional[list] = None,
         verify_fn: Callable = verify_reference) -> int:
    parser = argparse.ArgumentParser(prog="matilde", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("path", nargs="?", help="Path to a .bib file or a DOI list (one per line).")
    parser.add_argument("--doi", help="Verify a single DOI instead of a file.")
    parser.add_argument("--json", action="store_true", help="Emit JSON instead of text.")
    parser.add_argument("--email", help="Contact email for provider polite pools (sets MATILDE_CONTACT_EMAIL).")
    args = parser.parse_args(argv)

    if args.email:
        os.environ["MATILDE_CONTACT_EMAIL"] = args.email

    if args.doi:
        refs = [Reference(doi=args.doi)]
    elif args.path:
        try:
            with open(args.path, encoding="utf-8") as fh:
                text = fh.read()
        except OSError as exc:
            parser.error(f"cannot read {args.path}: {exc}")
            return 2
        refs = load_references(text, hint=args.path)
    else:
        parser.error("provide a file path or --doi")
        return 2

    if not refs:
        print("No references found to verify.", file=sys.stderr)
        return 2

    results = [verify_fn(ref) for ref in refs]
    print(_format_json(results) if args.json else _format_text(results))
    return _exit_code(results)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
