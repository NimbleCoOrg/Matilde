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
    MIN_RELIABLE_EPOCHS,
    MegIO,
    _EARLIEST_CORTICAL_MS,
    _classify,
    _peak_search_bounds,
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
        return {"latency_ms": self.peak_latency_ms, "amplitude": self.peak_amp,
                "n_epochs": self.n_epochs}


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
# Falsifiability: `refuted` must be REACHABLE from the real search range.
#
# The refuted tests elsewhere in this file all drive FakeMegIO, which returns a
# latency directly and never consults _peak_search_bounds. That hid a defect: the
# real backend takes its peak as the argmax INSIDE the search range, so if the
# search range cannot produce a latency that _classify calls "refuted", the
# hypothesis cannot fail no matter what the data says.
#
# These tests compose the two pure helpers -- the composition the fake skips.
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("window", [(80.0, 120.0), (70.0, 130.0),
                                    (50.0, 150.0), (100.0, 100.0)])
def test_refuted_is_reachable_from_the_real_search_range(window):
    """Some latency the search can actually return must classify as refuted.

    Sweep the achievable range at 0.1 ms and require all three verdicts to be
    possible. If only {supported, inconclusive} appear, the study is
    unfalsifiable by construction.
    """
    lo_s, hi_s = _peak_search_bounds(window, times_lo=-0.1, times_hi=0.3)
    lo_ms, hi_ms = lo_s * 1000.0, hi_s * 1000.0

    verdicts = set()
    n = int(round((hi_ms - lo_ms) / 0.1))
    for i in range(n + 1):
        verdicts.add(_classify(lo_ms + i * 0.1, window))

    assert "refuted" in verdicts, (
        f"window {window}: search range [{lo_ms:.1f}, {hi_ms:.1f}] ms can only "
        f"produce {sorted(verdicts)} -- 'refuted' is unreachable, so the "
        f"prediction cannot fail"
    )


@pytest.mark.parametrize("window", [(80.0, 120.0), (70.0, 130.0), (50.0, 150.0)])
def test_late_refutation_is_always_reachable(window):
    """The late side of the search must always extend past the refute threshold.

    A genuine auditory peak arriving too late is the realistic way this
    prediction fails, so this side must never be clamped shut.
    """
    _, hi_s = _peak_search_bounds(window, times_lo=-1.0, times_hi=1.0)
    hi_ms = hi_s * 1000.0
    assert _classify(hi_ms, window) == "refuted", (
        f"window {window}: latest searchable peak {hi_ms:.1f} ms is not refuted"
    )


@pytest.mark.parametrize("window", [(80.0, 120.0), (70.0, 130.0), (50.0, 150.0)])
def test_early_refutation_is_reachable_unless_physiology_floors_it(window):
    """Early-side refutation may be legitimately unreachable -- but only for one
    reason, and the reason must be the physiological floor.

    For a window starting at 50 ms the plausibility band's early edge (30 ms)
    coincides with ``_EARLIEST_CORTICAL_MS``, so the search cannot go below the
    refute threshold. That is correct: "too early to be plausible" and "too early
    to be a cortical response at all" have converged, and we decline to search
    sub-cortical latencies just to manufacture a refutation. What must never
    happen is the early side being closed by an arbitrary search margin -- which
    is what the pre-2026-07-30 code did at every window.
    """
    lo_s, _ = _peak_search_bounds(window, times_lo=-1.0, times_hi=1.0)
    lo_ms = lo_s * 1000.0
    if _classify(lo_ms, window) != "refuted":
        assert lo_ms == pytest.approx(_EARLIEST_CORTICAL_MS), (
            f"window {window}: early refutation unreachable and the search floor "
            f"({lo_ms:.1f} ms) is NOT the physiological floor "
            f"({_EARLIEST_CORTICAL_MS} ms) -- an arbitrary margin has closed it"
        )


def test_stimulus_artifact_stays_outside_the_search():
    """The original bug's regression guard, preserved while widening the search.

    Widening the range to make `refuted` reachable must NOT reach back to the
    ~15 ms stimulus artifact that the +/-100 ms search mistook for the M100.
    """
    lo_s, _ = _peak_search_bounds((80.0, 120.0), times_lo=-0.1, times_hi=0.3)
    assert lo_s > 0.015, f"search starts at {lo_s * 1000:.1f} ms, at/below the artifact"
    # And no window may drag the floor below the earliest plausible cortical response.
    for window in [(80.0, 120.0), (50.0, 150.0), (100.0, 100.0)]:
        lo_s, _ = _peak_search_bounds(window, times_lo=-1.0, times_hi=1.0)
        assert lo_s >= 0.03, f"{window}: floor {lo_s * 1000:.1f} ms is sub-cortical"


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
    io = FakeMegIO(peak_latency_ms=300.0)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "refuted"
    assert f["evidence"]["latency_ms"] == 300.0


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
    io = FakeMegIO(peak_latency_ms=300.0, n_epochs=MIN_RELIABLE_EPOCHS - 1)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "inconclusive"
    assert f["evidence"]["caveats"], "low-evidence caveat expected"
    assert f["evidence"].get("next_step"), "a stated next step expected"
    # The raw measurement is still reported for transparency.
    assert f["evidence"]["latency_ms"] == 300.0


def test_out_of_window_peak_from_enough_epochs_still_refuted(store):
    # With a reliable epoch count, an out-of-window peak is a real refutation.
    sid = store.create_study(slug="meg-strong", title="strong", plan=_plan())
    io = FakeMegIO(peak_latency_ms=300.0, n_epochs=MIN_RELIABLE_EPOCHS + 5)
    run(store, sid, build_steps(dataset_id="bst_auditory", io=io))
    f = store.get_findings(sid)[0]
    assert f["verdict"] == "refuted"
    assert not f["evidence"].get("next_step")


def test_min_reliable_epochs_boundary_is_inclusive(store):
    # Exactly MIN_RELIABLE_EPOCHS is "reliable" (the gate is `< threshold`), so an
    # out-of-window peak at the boundary is a real refutation, not a downgrade.
    sid = store.create_study(slug="meg-edge", title="edge", plan=_plan())
    io = FakeMegIO(peak_latency_ms=300.0, n_epochs=MIN_RELIABLE_EPOCHS)
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
