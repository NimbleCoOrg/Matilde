"""Golden MEG validation recipe (#14): an offline, dependency-free worked example.

Runs the SAME five-step meg_validation pipeline and the SAME verdict/skepticism
logic as the real ``bst_auditory`` study, but against the shipped synthetic
backend that plants a known in-window M100 — no mne, no numpy, no network, no
download. It is the package's regression smoke test AND the reference recipe a
fresh agent can imitate for "what a correct, well-evidenced validation looks
like" (docs/golden-validation-recipe.md).
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from matilde_plugin.engine.meg_study import (  # noqa: E402
    GOLDEN_PEAK_MS,
    MIN_RELIABLE_EPOCHS,
    build_golden_steps,
)
from matilde_plugin.engine.pipeline import run  # noqa: E402
from matilde_plugin.engine.store import StudyStore  # noqa: E402


def _store(tmp_path):
    return StudyStore(str(tmp_path / "studies.db"))


def test_golden_recipe_is_supported_offline(tmp_path):
    # The whole point: a deterministic, offline `supported` verdict at the
    # planted latency, with a healthy epoch count and no caveats.
    store = _store(tmp_path)
    sid = store.create_study(
        slug="golden", title="golden",
        plan=["fetch_sample", "preprocess", "epoch", "evoked", "validate_finding"])
    summary = run(store, sid, build_golden_steps())

    assert summary["status"] == "done"
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "supported"
    ev = f["evidence"]
    assert ev["latency_ms"] == GOLDEN_PEAK_MS
    assert ev["n_epochs"] >= MIN_RELIABLE_EPOCHS
    assert ev["caveats"] == []
    # The diagnostics an agent would sanity-check against are all present.
    assert ev["search_window_ms"] is not None
    assert ev["channel"]
    assert ev["expected_window_ms"] == [80.0, 120.0]


def test_golden_recipe_demonstrates_skeptical_inconclusive(tmp_path):
    # The same recipe, parameterised to a thin out-of-window sample, must follow
    # the skepticism path: inconclusive (not a confident refuted) + a next step.
    store = _store(tmp_path)
    sid = store.create_study(
        slug="golden-weak", title="golden-weak",
        plan=["fetch_sample", "preprocess", "epoch", "evoked", "validate_finding"])
    steps = build_golden_steps(peak_latency_ms=225.0,
                               n_epochs=MIN_RELIABLE_EPOCHS - 1)
    run(store, sid, steps)

    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive"
    assert f["evidence"]["next_step"]


def test_golden_recipe_needs_no_heavy_deps(tmp_path):
    # Guard the "offline" promise: running the golden recipe must not import mne
    # or numpy. If a future change pulls them in, this fails loudly.
    for mod in ("mne", "numpy"):
        sys.modules.pop(mod, None)
    store = _store(tmp_path)
    sid = store.create_study(
        slug="golden-light", title="golden-light",
        plan=["fetch_sample", "preprocess", "epoch", "evoked", "validate_finding"])
    run(store, sid, build_golden_steps())
    assert "mne" not in sys.modules
    assert "numpy" not in sys.modules
