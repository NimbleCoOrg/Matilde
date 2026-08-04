"""Structural checks on the package's own documentation.

These exist because the promotion process this package documents
(``docs/lesson-promotion-filter.md``) moves text from a private instance into a
public repository, and because a link nobody follows is exactly the "available,
not enforced" control that ``hermes-skill/references/enforcement-ladder.md``
warns about. Prose cannot enforce itself; these tests are the rung-3 half.

The sanitization gate (``scripts/check_sanitization.py``) covers credentials and
PII deterministically, and study particulars semantically via an LLM. It does not
check *shapes* that are specific to an operator's runtime — absolute host paths,
raw data filenames, chat-platform snowflake IDs. Those are cheap to check
deterministically and are the residue most likely to survive a hand edit.

Deliberately NOT implemented as a denylist of names: in a public repository, a
list of collaborator names to exclude is itself a published list of collaborator
names. Shape checks only.
"""

from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent

# Only these prefixes are installed into a deployed agent's data directory (see
# the use-case template's artifact sources). docs/ is contributor-facing.
SHIPPED_PREFIXES = ("hermes-skill/", "matilde_plugin/", "docker/SOUL")


def _tracked_markdown() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "*.md"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.split()
    return [ROOT / p for p in out]


def _rel(p: Path) -> str:
    return p.relative_to(ROOT).as_posix()


# --- the corpus itself -----------------------------------------------------


def test_markdown_corpus_is_non_empty():
    """Positive control.

    Every other test in this module iterates the tracked-markdown list and
    asserts that nothing in it is bad. If that list were ever empty -- a broken
    ``git ls-files``, a changed working directory -- all of them would pass
    while checking nothing. A negative result is only meaningful once the check
    has been shown capable of returning a positive.
    """
    files = _tracked_markdown()
    assert len(files) > 5, f"expected a markdown corpus, got {files}"
    assert any(_rel(f) == "README.md" for f in files)
    assert any(_rel(f).startswith("hermes-skill/") for f in files)


# --- links -----------------------------------------------------------------

_LINK = re.compile(r"\[[^\]]*\]\(([^)]+)\)")


def _internal_links(text: str):
    for target in _LINK.findall(text):
        if target.startswith(("http://", "https://", "mailto:", "#")):
            continue
        yield target


def test_internal_markdown_links_resolve():
    """A relative link in any tracked markdown file must point at a real file."""
    broken = []
    for path in _tracked_markdown():
        for target in _internal_links(path.read_text(encoding="utf-8")):
            resolved = (path.parent / target.split("#")[0]).resolve()
            if not resolved.exists():
                broken.append(f"{_rel(path)} -> {target}")
    assert not broken, "broken internal links:\n  " + "\n  ".join(broken)


def test_link_checker_detects_a_broken_link(tmp_path):
    """Positive control for the link checker itself."""
    doc = tmp_path / "x.md"
    doc.write_text("see [nope](./does-not-exist.md)\n", encoding="utf-8")
    targets = list(_internal_links(doc.read_text(encoding="utf-8")))
    assert targets == ["./does-not-exist.md"]
    assert not (doc.parent / targets[0]).resolve().exists()


def test_shipped_skill_does_not_link_outside_itself():
    """The skill directory is copied to the agent standalone.

    A markdown link from ``hermes-skill/`` to ``docs/`` resolves in the
    repository and is dead on the deployed agent, which is the worst kind of
    broken -- invisible to a contributor reading it in place. Refer to
    contributor docs as path code-spans instead, the convention SKILL.md already
    uses.
    """
    skill_dir = ROOT / "hermes-skill"
    escaping = []
    for path in sorted(skill_dir.rglob("*.md")):
        for target in _internal_links(path.read_text(encoding="utf-8")):
            resolved = (path.parent / target.split("#")[0]).resolve()
            if skill_dir.resolve() not in resolved.parents and resolved != skill_dir.resolve():
                escaping.append(f"{_rel(path)} -> {target}")
    assert not escaping, (
        "shipped skill links outside its own directory (dead on a deployed "
        "agent):\n  " + "\n  ".join(escaping)
    )


def test_skill_points_at_every_reference_it_ships():
    """A reference nothing opens is documentation, not a control.

    ``enforcement-ladder.md`` argues that an available-but-uninvoked guard is
    the failure mode; a reference file with no pointer from SKILL.md is the
    documentation equivalent, so require the pointer.
    """
    skill = (ROOT / "hermes-skill" / "SKILL.md").read_text(encoding="utf-8")
    refs = sorted((ROOT / "hermes-skill" / "references").glob("*.md"))
    assert refs, "expected the skill to ship a references/ directory"
    unreferenced = [r.name for r in refs if f"references/{r.name}" not in skill]
    assert not unreferenced, (
        "shipped reference files that SKILL.md never points at: " + ", ".join(unreferenced)
    )


# --- instance-particular shapes -------------------------------------------

# Shapes, never names. Each has cost a real leak or near-leak when text moved
# from an operator's runtime into a repository.
_PARTICULAR_SHAPES = [
    (
        "operator-host-path",
        # A host path whose next segment is a username or volume name. NOT
        # /opt/data -- that is this package's documented container mount point
        # (see docs/onboarding.md), identical on every instance and therefore
        # not a particular. The distinction is whether the path identifies a
        # machine or a person.
        re.compile(r"(?<![\w`/])/(?:home|Users|Volumes)/[\w.\-]+"),
        "a host path identifying an operator's machine or account",
    ),
    (
        "raw-data-filename",
        # Raw capture filenames routinely encode site + subject + date, which is
        # the identifying material even when the derived metric is publishable.
        re.compile(r"\b[\w\-]*\d{4}[_\-]\d{2}[_\-]\d{2}[\w\-]*\.(?:wav|mp3|flac|edf|fif|nii|csv)\b"),
        "a raw data filename encoding a date",
    ),
    (
        "chat-platform-snowflake",
        # 17-19 digit IDs identify a guild/channel/bot/account and usually sit
        # next to a credential location.
        re.compile(r"(?<!\d)\d{17,19}(?!\d)"),
        "a chat-platform identifier",
    ),
]


@pytest.mark.parametrize("label,pattern,why", _PARTICULAR_SHAPES, ids=[s[0] for s in _PARTICULAR_SHAPES])
def test_no_instance_particular_shapes(label, pattern, why):
    """No tracked markdown may carry instance-runtime shapes."""
    hits = []
    for path in _tracked_markdown():
        for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line):
                hits.append(f"{_rel(path)}:{n} ({why})")
    assert not hits, f"{label} found in tracked markdown:\n  " + "\n  ".join(hits)


def test_particular_shape_patterns_actually_match():
    """Positive control for the shape patterns.

    Without this, a regex that silently stopped matching -- a typo, an escaping
    change -- would leave every test above passing while checking nothing. The
    strings here are synthetic.
    """
    samples = {
        "operator-host-path": "the file at /home/analyst/data lives there",
        "raw-data-filename": "recorded as SITE_1999_01_02_SUBJ01.wav today",
        "chat-platform-snowflake": "channel 1531073024047059106 was used",
    }
    for label, pattern, _why in _PARTICULAR_SHAPES:
        assert pattern.search(samples[label]), f"{label} pattern matched nothing"


def test_docs_are_not_shipped_to_agents():
    """Guard the split the promotion filter depends on.

    If someone adds an agent-facing rule to docs/ believing it reaches a running
    agent, it silently will not. Keep the shipped set explicit and small.
    """
    tracked = subprocess.run(
        ["git", "ls-files"], cwd=ROOT, capture_output=True, text=True, check=True
    ).stdout.split()
    shipped = [p for p in tracked if p.startswith(SHIPPED_PREFIXES)]
    assert shipped, "expected some shipped artifact paths"
    assert not any(p.startswith("docs/") for p in shipped)
