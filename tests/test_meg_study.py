"""Tests for the MEG data-sample validation study (the M100 worked claim).

The whole study is exercised with FAKES — no mne, no numpy, no network, no data
files. Each step takes injected loader/analysis callables, so the unit tests are
deterministic and stdlib-only. Covered here:

  - each step transforms the study state correctly (fetch -> preprocess -> epoch
    -> evoked -> validate_finding), reading prior-step data and persisting an
    artifact per heavy step;
  - the ``validate_finding`` verdict boundaries — peak at 100 ms -> supported,
    peak at 300 ms -> refuted, empty/degenerate sample -> inconclusive;
  - resumability — force a failure at the ``evoked`` step, assert the study is
    blocked with the earlier steps done, then resume with a working analyzer and
    assert fetch/preprocess/epoch are NOT re-run (call counters).

An opt-in live test (``MATILDE_LIVE=1`` and mne importable) runs the real bounded
pipeline on ``bst_auditory`` — it SKIPS without the flag / without mne.
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from matilde_plugin.engine.meg_study import (  # noqa: E402
    DEFAULT_BOUNDS,
    MIN_RELIABLE_EPOCHS,
    MegIO,
    _ARTIFACT_EXCLUSION_FLOOR_MS,
    DEFAULT_WINDOW_MS,
    _P50_UPPER_MS,
    _P200_UPPER_MS,
    _classify,
    _latency_zone,
    _peak_search_bounds,
    _refute_threshold_ms,
    build_steps,
)
from matilde_plugin.engine.pipeline import resume, run  # noqa: E402
from matilde_plugin.engine.store import StudyStore  # noqa: E402


# ---------------------------------------------------------------------------
# A fake MEG I/O backend: pure dicts, deterministic, counts each call so the
# resumability test can prove done steps are not re-run. No mne / numpy / fs.
# ---------------------------------------------------------------------------

class FakeMegIO:
    """Stand-in for the real (lazy mne) I/O. Each method bumps a counter and
    returns a small dict 'handle' threaded through the steps."""

    def __init__(self, *, peak_latency_ms=100.0, peak_amp=5.0, n_epochs=40,
                 fail_evoked=False):
        self.peak_latency_ms = peak_latency_ms
        self.peak_amp = peak_amp
        self.n_epochs = n_epochs
        self.fail_evoked = fail_evoked
        self.calls = {"fetch": 0, "preprocess": 0, "epoch": 0, "evoked": 0,
                      "measure_peak": 0}
        # Records the event_id the study asked epoch to filter to (None=all).
        self.epoch_event_id = "__unset__"
        # Records the epoch bounds actually used, so measure_peak can report the
        # real search range. Hardcoding DEFAULT_BOUNDS here would make the fake
        # ignore a caller's tmax override and silently skip the falsifiability
        # check that depends on it.
        self.epoch_bounds = (float(DEFAULT_BOUNDS["tmin"]), float(DEFAULT_BOUNDS["tmax"]))

    def fetch_sample(self, *, dataset_id, bounds):
        self.calls["fetch"] += 1
        return {"dataset_id": dataset_id, "bounds": dict(bounds),
                "path": "/tmp/fake_raw.fif"}

    def preprocess(self, raw, *, l_freq, h_freq):
        self.calls["preprocess"] += 1
        return {**raw, "filtered": [l_freq, h_freq], "path": "/tmp/fake_filt.fif"}

    def epoch(self, filtered, *, tmin, tmax, event_id=None):
        self.calls["epoch"] += 1
        self.epoch_event_id = event_id
        self.epoch_bounds = (float(tmin), float(tmax))
        return {**filtered, "n_epochs": self.n_epochs,
                "window": [tmin, tmax], "path": "/tmp/fake_epo.fif"}

    def evoked(self, epochs):
        self.calls["evoked"] += 1
        if self.fail_evoked:
            raise RuntimeError("OOM while averaging evoked response")
        return {**epochs, "averaged": True, "path": "/tmp/fake_ave.fif"}

    def measure_peak(self, evoked, *, window_ms):
        self.calls["measure_peak"] += 1
        if self.n_epochs <= 0:  # degenerate sample -> no measurable peak
            return {"latency_ms": None, "amplitude": None, "n_epochs": 0}
        # Report the REAL search range. Omitting it was how the fake hid the
        # boundary-hit and falsifiability logic from every end-to-end test: the
        # guards key off `search_ms`, so a fake without it silently skipped them.
        lo, hi = _peak_search_bounds(window_ms, self.epoch_bounds[0],
                                     self.epoch_bounds[1])
        return {"latency_ms": self.peak_latency_ms, "amplitude": self.peak_amp,
                "n_epochs": self.n_epochs,
                "search_ms": [lo * 1000.0, hi * 1000.0]}


@pytest.fixture()
def store(tmp_path):
    return StudyStore(str(tmp_path / "studies.db"))


def _plan():
    return ["fetch_sample", "preprocess", "epoch", "evoked", "validate_finding"]


# ---------------------------------------------------------------------------
# MegIO is a Protocol-ish contract; the fake should satisfy it (sanity).
# ---------------------------------------------------------------------------

def test_fakeio_satisfies_contract():
    io = FakeMegIO()
    assert isinstance(io, MegIO)


# ---------------------------------------------------------------------------
# Regression: the peak search must exclude the early stimulus artifact.
#
# The original bug widened the search by +/-100 ms, so an 80-120 ms M100 window
# became a ~-20..220 ms search that grabbed the large ~15 ms stimulus artifact
# instead of the real ~100 ms auditory peak (reported 15 ms -> "refuted").
#
# The first fix over-corrected, clamping the search to window +/-10 ms. That
# excluded the artifact but also made the search NARROWER than the refutation
# thresholds, so `refuted` became unreachable at every window (see the
# falsifiability section below). The bound that matters is the physiological
# floor, not a narrow band around the window.
# ---------------------------------------------------------------------------

def test_peak_search_bounds_exclude_the_artifact_without_pinning_the_window():
    """Guards the original bug by its CAUSE, not by a narrow numeric range.

    This test used to assert the search stayed inside ~0.06..0.14 s for an
    80-120 ms window. That over-fitted the fix: it made the search narrower than
    the refutation thresholds, which is what rendered `refuted` unreachable (see
    the falsifiability tests below). The defect the original bug actually had was
    reaching the ~15 ms stimulus artifact -- so assert *that*, and leave the upper
    bound free for the falsifiability invariant to constrain.
    """
    lo, hi = _peak_search_bounds((80.0, 120.0), times_lo=-0.1, times_hi=0.3)
    # The thing that actually went wrong: the artifact must be out of range.
    assert lo > 0.015, f"search reaches the ~15 ms artifact at {lo * 1000:.1f} ms"
    # Still anchored in physiology, not arbitrarily wide on the early side.
    assert lo >= 0.03, f"search floor {lo * 1000:.1f} ms is sub-cortical"
    # And the window itself is still inside the search.
    assert lo <= 0.080 and hi >= 0.120


def test_peak_search_bounds_clamped_to_available_times():
    # If the recording is shorter than the window, clamp to available samples.
    lo, hi = _peak_search_bounds((80.0, 120.0), times_lo=0.09, times_hi=0.11)
    assert lo == 0.09
    assert hi == 0.11


# ---------------------------------------------------------------------------
# Falsifiability, and the harder question of IDENTIFIABILITY.
#
# `refuted` must be reachable from the real search range, or the prediction
# cannot fail. But reachable is not enough: the peak is a polarity-blind
# (mode="abs") argmax, so any latency sitting where another auditory component
# lives is more likely that component than a displaced M100. A `refuted` drawn
# from P50 or P200 territory is a misidentification wearing a verdict.
#
# So the contract is: refutation fires only beyond the last canonical component,
# and everything ambiguous degrades to `inconclusive` with a named caveat.
# ---------------------------------------------------------------------------

def test_late_refutation_is_reachable_from_the_real_search_range():
    """The realistic failure direction must be achievable."""
    lo_s, hi_s = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=-0.1, times_hi=0.3)
    assert _classify(hi_s * 1000.0, DEFAULT_WINDOW_MS) == "refuted", (
        f"latest searchable peak {hi_s * 1000:.1f} ms is not refutable — the "
        f"prediction cannot fail"
    )


def test_search_range_contains_the_late_refutation_threshold():
    """The invariant. Search must extend PAST the threshold, not up to it."""
    _, hi_s = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=-1.0, times_hi=1.0)
    thresh = _refute_threshold_ms(DEFAULT_WINDOW_MS)
    assert hi_s * 1000.0 > thresh, (
        f"search ceiling {hi_s * 1000:.1f} ms does not exceed the refute "
        f"threshold {thresh:.1f} ms"
    )


def test_peak_at_the_search_floor_is_not_a_confident_refutation():
    """THE REGRESSION GUARD.

    An argmax pinned to the search floor means the true extremum is probably
    outside the range — classically the ~15 ms stimulus artifact smeared upward
    by the band-pass. Widening the search to gain falsifiability must not convert
    that into a confident `refuted`. An earlier revision of this module did
    exactly that: floor 40 ms with an early refute threshold of 60 ms, so the
    artifact returned `refuted` where the previous code said `inconclusive`.
    """
    lo_s, _ = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=-0.1, times_hi=0.3)
    assert _classify(lo_s * 1000.0, DEFAULT_WINDOW_MS) != "refuted", (
        f"a peak at the search floor ({lo_s * 1000:.1f} ms) is reported as "
        f"refuted — the artifact incident, relocated"
    )


@pytest.mark.parametrize("latency,zone", [
    (45.0, "p50_ambiguous"),      # Pa/P50 territory — this dataset has one ~50 ms
    (65.0, "p50_ambiguous"),
    (100.0, "supported"),
    (140.0, "off_window_late"),
    (190.0, "p200_ambiguous"),    # P200 territory
    (260.0, "refutable"),         # beyond every canonical component
])
def test_latency_zones_are_component_aware(latency, zone):
    assert _latency_zone(latency, DEFAULT_WINDOW_MS) == zone


@pytest.mark.parametrize("latency", [45.0, 65.0, 190.0])
def test_known_component_territory_is_never_refuted(latency):
    """P50 and P200 latencies degrade to inconclusive, not refuted."""
    assert _classify(latency, DEFAULT_WINDOW_MS) == "inconclusive"


def test_artifact_floor_is_an_absolute_bound_not_a_derived_one():
    """Kills the mutant `_ARTIFACT_EXCLUSION_FLOOR_MS = 75.0`.

    The previous version of this assertion compared the floor to the same
    constant that produced it, so it held for any value. Pin it absolutely:
    above the artifact, below the earliest plausible M100.
    """
    assert 20.0 <= _ARTIFACT_EXCLUSION_FLOOR_MS <= 50.0
    lo, _ = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=-0.1, times_hi=0.3)
    assert lo > 0.015, "search reaches the ~15 ms stimulus artifact"
    assert lo <= DEFAULT_WINDOW_MS[0] / 1000.0, "floor excludes the window itself"


def test_search_ceiling_is_bounded():
    """Kills the mutant `_SEARCH_CEILING_MS = 900.0`.

    Previously the only assertion on `hi` was a LOWER bound, so the search could
    be widened without limit and the suite stayed green.
    """
    _, hi = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=-1.0, times_hi=1.0)
    assert hi <= 0.30, f"search ceiling {hi * 1000:.0f} ms is unbounded"


def test_p50_boundary_is_pinned():
    """Kills mutants on the component boundaries themselves."""
    assert _latency_zone(_P50_UPPER_MS, DEFAULT_WINDOW_MS) == "p50_ambiguous"
    assert _latency_zone(_P50_UPPER_MS + 0.1, DEFAULT_WINDOW_MS) == "off_window_early"
    assert _latency_zone(_P200_UPPER_MS, DEFAULT_WINDOW_MS) == "p200_ambiguous"
    assert _latency_zone(_P200_UPPER_MS + 0.1, DEFAULT_WINDOW_MS) == "refutable"


def test_peak_on_the_search_boundary_is_downgraded_end_to_end(store):
    """THE REGRESSION GUARD, through the whole pipeline.

    A peak sitting on the search floor is what the ~15 ms stimulus artifact looks
    like once the band-pass smears it into the searchable range. It must come back
    `inconclusive` with a `boundary_hit` caveat and a next step — never a
    confident verdict. An earlier revision returned `refuted` here.
    """
    lo, _ = _peak_search_bounds(DEFAULT_WINDOW_MS, float(DEFAULT_BOUNDS["tmin"]),
                                float(DEFAULT_BOUNDS["tmax"]))
    sid = store.create_study(slug="meg-edge-lo", title="edge", plan=_plan())
    io = FakeMegIO(peak_latency_ms=lo * 1000.0, n_epochs=40)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive", f["verdict"]
    caveats = f["evidence"].get("caveats") or []
    assert any("boundary_hit" in c for c in caveats), caveats
    assert f["evidence"].get("next_step")


def test_p200_latency_is_not_reported_as_refuted_end_to_end(store):
    """190 ms is P200 territory. A polarity-blind argmax cannot call that a
    displaced M100, so it degrades with a named component caveat."""
    sid = store.create_study(slug="meg-p200", title="p200", plan=_plan())
    io = FakeMegIO(peak_latency_ms=190.0, n_epochs=40)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive", f["verdict"]
    caveats = f["evidence"].get("caveats") or []
    assert any("component_ambiguous" in c for c in caveats), caveats


def test_short_epoch_flags_the_claim_as_untestable(store):
    """If the epoch cannot reach the refutation threshold, `supported` was the
    only attainable answer and must be labelled untested rather than confirmed."""
    sid = store.create_study(slug="meg-short", title="short", plan=_plan())
    io = FakeMegIO(peak_latency_ms=100.0, n_epochs=40)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io,
                                bounds={**DEFAULT_BOUNDS, "tmax": 0.15}))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "supported"
    caveats = f["evidence"].get("caveats") or []
    assert any("unfalsifiable_late" in c for c in caveats), caveats


def test_falsifiability_holds_under_the_shipped_epoch_bounds():
    """Bind the invariant to DEFAULT_BOUNDS, not hardcoded times.

    Mutating `tmax` must not silently close refutation with a green suite.
    """
    tmax = float(DEFAULT_BOUNDS["tmax"])
    _, hi_s = _peak_search_bounds(DEFAULT_WINDOW_MS, times_lo=float(DEFAULT_BOUNDS["tmin"]),
                                  times_hi=tmax)
    assert _classify(hi_s * 1000.0, DEFAULT_WINDOW_MS) == "refuted", (
        f"under the shipped epoch (tmax={tmax}s) refutation is unreachable"
    )



# ---------------------------------------------------------------------------
# Regression: epoch must filter to the standard-tone event by default, not
# average ALL triggers (standards + deviants + button presses), which muddied
# the evoked average in the live run.
# ---------------------------------------------------------------------------

def test_epoch_applies_standards_event_filter_by_default(store):
    sid = store.create_study(slug="evfilter", title="EvFilter", plan=_plan())
    io = FakeMegIO()
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    # The study must NOT ask epoch to average every trigger. The default is the
    # standards-only auto filter (event_id=None -> most-frequent code, resolved
    # inside the real backend), never the "all events" sentinel that produced
    # the muddied average behind the original mis-measurement.
    assert io.epoch_event_id != "__unset__", "epoch was never called"
    assert io.epoch_event_id != "all", (
        "epoch was told to average ALL triggers -> muddied evoked average")
    assert io.epoch_event_id is None, (
        "default should be the auto standards filter (None), resolved in backend")


def test_epoch_event_id_overridable_via_bounds(store):
    sid = store.create_study(slug="evfilter2", title="EvFilter2", plan=_plan())
    io = FakeMegIO()
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io,
                                bounds={"event_id": 7}))
    assert io.epoch_event_id == 7


# ---------------------------------------------------------------------------
# Happy path — peak at 100 ms is within the default 80-120 window -> supported.
# ---------------------------------------------------------------------------

def test_happy_path_peak_within_window_is_supported(store):
    sid = store.create_study(slug="m100", title="M100", plan=_plan())
    io = FakeMegIO(peak_latency_ms=100.0)
    steps = build_steps(dataset_id="bst_auditory", io=io)
    summary = run(store, sid, steps)

    assert summary["status"] == "done"
    assert store.get_study(sid)["status"] == "done"

    findings = store.get_findings(sid)
    assert len(findings) == 1
    f = findings[0]
    assert f["verdict"] == "supported"
    assert f["evidence"]["latency_ms"] == 100.0
    assert f["evidence"]["amplitude"] == 5.0

    # each heavy step checkpointed an artifact (resume-after-OOM target)
    arts = {a["step_name"] for a in store.get_artifacts(sid)}
    assert {"fetch_sample", "preprocess", "epoch", "evoked"} <= arts


# ---------------------------------------------------------------------------
# Verdict boundaries.
# ---------------------------------------------------------------------------

def test_peak_far_outside_window_is_refuted(store):
    sid = store.create_study(slug="r", title="R", plan=_plan())
    io = FakeMegIO(peak_latency_ms=225.0)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "refuted"
    assert f["evidence"]["latency_ms"] == 225.0


def test_degenerate_sample_is_inconclusive(store):
    sid = store.create_study(slug="i", title="I", plan=_plan())
    io = FakeMegIO(n_epochs=0)  # no epochs -> no measurable peak
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive"
    assert f["evidence"]["latency_ms"] is None


def test_custom_window_changes_verdict(store):
    """A peak at 150 ms is refuted under default (80-120) but supported under a
    wider custom window — proves the expected-window param is honored."""
    sid = store.create_study(slug="w", title="W", plan=_plan())
    io = FakeMegIO(peak_latency_ms=150.0)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io,
                                expected_window_ms=(130.0, 170.0)))
    assert store.get_findings(sid)[0]["verdict"] == "supported"


# ---------------------------------------------------------------------------
# Step state transitions — each step's data threads into the next.
# ---------------------------------------------------------------------------

def test_steps_thread_state_forward(store):
    sid = store.create_study(slug="t", title="T", plan=_plan())
    io = FakeMegIO()
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io,
                                bounds={"crop_tmax": 9.0}))

    # bounds flowed into the fetch handle and the artifact meta.
    fetch_res = store.get_step(sid, "fetch_sample")["result"]
    assert fetch_res["dataset_id"] == "bst_auditory"
    assert fetch_res["bounds"]["crop_tmax"] == 9.0

    epoch_res = store.get_step(sid, "epoch")["result"]
    assert epoch_res["n_epochs"] == 40

    evoked_res = store.get_step(sid, "evoked")["result"]
    assert evoked_res["n_epochs"] == 40


# ---------------------------------------------------------------------------
# Resumability — fail at evoked, resume, earlier steps not re-run.
# ---------------------------------------------------------------------------

def test_fail_at_evoked_then_resume_does_not_rerun_earlier_steps(store):
    sid = store.create_study(slug="resume", title="Resume", plan=_plan())

    # First pass: evoked raises (simulated OOM mid-average).
    failing_io = FakeMegIO(fail_evoked=True)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=failing_io))

    assert store.get_step(sid, "fetch_sample")["status"] == "done"
    assert store.get_step(sid, "preprocess")["status"] == "done"
    assert store.get_step(sid, "epoch")["status"] == "done"
    assert store.get_step(sid, "evoked")["status"] == "failed"
    assert store.get_step(sid, "validate_finding")["status"] == "pending"
    assert store.get_study(sid)["status"] in ("blocked", "failed")
    assert store.get_findings(sid) == []  # no finding emitted yet

    assert failing_io.calls["fetch"] == 1
    assert failing_io.calls["preprocess"] == 1
    assert failing_io.calls["epoch"] == 1
    assert failing_io.calls["evoked"] == 1  # attempted, raised

    # Second pass: working io, resume.
    good_io = FakeMegIO(fail_evoked=False, peak_latency_ms=100.0)
    resume(store, sid, build_steps(dataset_id="bst_auditory", io=good_io))

    assert store.get_study(sid)["status"] == "done"
    # Earlier (done) steps were NOT re-run on the working io.
    assert good_io.calls["fetch"] == 0
    assert good_io.calls["preprocess"] == 0
    assert good_io.calls["epoch"] == 0
    # Only the previously-failed step + the remaining step ran.
    assert good_io.calls["evoked"] == 1
    assert good_io.calls["measure_peak"] == 1

    f = store.get_findings(sid)[0]
    assert f["verdict"] == "supported"


# ---------------------------------------------------------------------------
# Opt-in LIVE test — real bounded mne pipeline. SKIPS without flag / mne.
# ---------------------------------------------------------------------------

@pytest.mark.skipif(os.environ.get("MATILDE_LIVE") != "1",
                    reason="live MEG test is opt-in (set MATILDE_LIVE=1)")
def test_live_bst_auditory_m100_bounded():
    pytest.importorskip("mne")
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        store = StudyStore(os.path.join(tmp, "studies.db"))
        sid = store.create_study(slug="live-m100", title="Live M100",
                                 plan=_plan())
        # Default io=None -> the real, memory-bounded mne backend.
        steps = build_steps(dataset_id="bst_auditory")
        summary = run(store, sid, steps)

        assert summary["status"] == "done", summary
        f = store.get_findings(sid)[0]
        # A plausible auditory peak should be found and measured.
        assert f["evidence"]["latency_ms"] is not None
        assert 50.0 <= f["evidence"]["latency_ms"] <= 200.0
        assert f["verdict"] in ("supported", "refuted")


# ---------------------------------------------------------------------------
# Skepticism (#14): a `refuted` measured from too few epochs is not trustworthy
# — the evoked average is noise-dominated, so a missing/displaced M100 more
# likely reflects a weak sample than a real absence. Withhold the confident
# refutation: return `inconclusive` with a stated next step instead.
# ---------------------------------------------------------------------------

def test_out_of_window_peak_from_few_epochs_is_inconclusive_not_refuted(store):
    sid = store.create_study(slug="meg-weak", title="weak", plan=_plan())
    # Peak far outside the 80-120 window, but only a handful of epochs.
    io = FakeMegIO(peak_latency_ms=225.0, n_epochs=MIN_RELIABLE_EPOCHS - 1)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive"
    assert f["evidence"]["caveats"], "low-evidence caveat expected"
    assert f["evidence"].get("next_step"), "a stated next step expected"
    # The raw measurement is still reported for transparency.
    assert f["evidence"]["latency_ms"] == 225.0


def test_out_of_window_peak_from_enough_epochs_still_refuted(store):
    # With a reliable epoch count, an out-of-window peak is a real refutation.
    sid = store.create_study(slug="meg-strong", title="strong", plan=_plan())
    io = FakeMegIO(peak_latency_ms=225.0, n_epochs=MIN_RELIABLE_EPOCHS + 5)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "refuted"
    assert not f["evidence"].get("next_step")


def test_min_reliable_epochs_boundary_is_inclusive(store):
    # Exactly MIN_RELIABLE_EPOCHS is "reliable" (the gate is `< threshold`), so an
    # out-of-window peak at the boundary is a real refutation, not a downgrade.
    sid = store.create_study(slug="meg-edge", title="edge", plan=_plan())
    io = FakeMegIO(peak_latency_ms=225.0, n_epochs=MIN_RELIABLE_EPOCHS)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "refuted"
    assert f["evidence"]["caveats"] == []


def test_finding_evidence_carries_diagnostics(store):
    # A correct (supported) finding still surfaces the material an agent needs to
    # sanity-check it: the search window, the channel, epoch count, caveats list.
    sid = store.create_study(slug="meg-diag", title="diag", plan=_plan())
    io = FakeMegIO(peak_latency_ms=100.0)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    ev = store.get_findings(sid)[0]["evidence"]
    for key in ("search_window_ms", "channel", "n_epochs", "caveats"):
        assert key in ev, f"missing diagnostic key: {key}"
    assert isinstance(ev["caveats"], list)
