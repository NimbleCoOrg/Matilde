"""MEG data-sample validation study — the first heavy / real-data study type.

Validates a reported neuroscience finding against a **bounded sample** of an open
MEG dataset, as resumable checkpointed steps. The worked claim is the auditory
**M100 / N100m** evoked response: a magnetic field peak ~80-120 ms after an
auditory stimulus.

Five resumable steps, each reading the **artifact the previous step checkpointed**
and writing its own — so a memory kill or container rebuild resumes from the last
completed step by reloading that on-disk intermediate, never re-running the whole
chain:

  1. ``fetch_sample``     — locate the dataset and load a **memory-bounded** slice,
     persist a small raw-sample artifact.
  2. ``preprocess``       — load that, band-pass filter, persist a filtered artifact.
  3. ``epoch``            — load that, epoch around stimulus events (read from the
     data), persist an epochs artifact.
  4. ``evoked``           — load that, average to the evoked response, persist it.
  5. ``validate_finding`` — load the evoked artifact, measure the peak latency,
     emit a finding.

Two hard design constraints (mirroring the rest of ``matilde_plugin/engine``):

* **Lazy heavy deps.** ``mne`` / ``numpy`` are imported ONLY inside the real I/O
  backend's methods (``_MneIO``), never at module import. This module — and the
  whole plugin — imports fine with neither installed. The agent container has mne
  available at runtime.

* **Injected I/O.** Every step calls through an injected :class:`MegIO` handle
  (``io=...``) that does exactly one load-from-prior-artifact + one transform +
  one persist. The whole study is unit-testable with FAKES — no mne, no numpy, no
  network, no data files, deterministic. Production passes no ``io`` and gets the
  real, memory-bounded :class:`_MneIO` backend.

Because each step's only input is the prior step's *artifact path* (threaded
through the pipeline's ``ctx.results``, which is plain JSON), a resumed step
reloads from disk — there is no live object held across the run and no re-fetch
of earlier stages. This is exactly the durability the bounded-sample design needs.

**Memory-bounded by design.** The real backend never full-preloads a recording:
it reads with ``preload=False``, picks a single run, restricts to a subset of
gradiometer channels, crops to a stimulus-locked window, and downsamples — then
persists small intermediates as artifacts. This is what keeps a multi-GB MEG
recording from exhausting the container's memory budget; combined with per-step
checkpointing, an OOM resumes from the last completed step instead of restarting.
"""
from __future__ import annotations

import abc
from typing import Any, Dict, List, Optional, Tuple

from .pipeline import Step, StepContext, StepResult

# Default M100 window (ms) and the bounded-sample defaults. These keep the real
# backend's footprint small; tests override them via build_steps args.
DEFAULT_WINDOW_MS: Tuple[float, float] = (80.0, 120.0)
DEFAULT_BOUNDS: Dict[str, Any] = {
    "run": 1,            # one run only
    "max_channels": 60,  # subset of gradiometers, not the full array
    # seconds — a stimulus-locked slice, never the whole recording. Raised from
    # 30 to 90 s so the standards-only average has enough trials for a clean
    # peak (a 30 s crop yielded only ~16 standard tones). Still bounded: after
    # the channel-subset + resample the loaded array stays well within budget
    # (~60 grad ch x 90 s x 200 Hz ~ a few MB), and per-step checkpointing means
    # an OOM still resumes from the last completed step.
    "crop_tmax": 90.0,
    "resample_hz": 200,  # downsample to shrink the in-memory array
    "l_freq": 1.0,       # band-pass low edge (Hz)
    "h_freq": 40.0,      # band-pass high edge (Hz)
    "tmin": -0.1,        # epoch window start (s) relative to event
    "tmax": 0.3,         # epoch window end (s) relative to event
    # Which trigger code to epoch. None -> auto: the most-frequent event code,
    # which for an oddball auditory paradigm is the STANDARD tone. Epoching ALL
    # triggers (standards + deviants + button presses) muddies the average and
    # was part of the original mis-measurement.
    "event_id": None,
}

# Verdict geometry. Read this before touching any number here.
#
# The peak is a polarity-blind argmax (``mode="abs"``) over a search range. That
# makes two failure modes possible, and they pull in OPPOSITE directions:
#
#   * Too NARROW a search and ``refuted`` becomes unreachable — the argmax is
#     confined to latencies the hypothesis already accepts, so the prediction
#     cannot fail. This module shipped that state between e079993 and 2026-07-30:
#     search was window +/-10 ms while refutation needed window +/-50 ms, so no
#     measurement at any configured window could return ``refuted``.
#   * Too WIDE a search and the argmax starts catching things that are not the
#     M100 — the ~15 ms stimulus artifact (the original bug), the P50/Pa around
#     30-70 ms, the P200 around 170-215 ms. A ``refuted`` drawn from any of those
#     is a misidentified component wearing a verdict.
#
# So width alone cannot fix this. The resolution is to search wide enough to
# contain the refutation threshold, and then refuse to refute anywhere a known
# component could plausibly be the winner:
#
#     30 ms          70 ms      window      170 ms      215 ms        280 ms
#   ----|--------------|---------[==]---------|-----------|-------------|----
#       floor      P50/Pa      supported    off-window   P200        ceiling
#       (artifact  ambiguous                 late      ambiguous   refutable -->
#        excluded)  -> inconclusive                  -> inconclusive
#
# Consequence stated plainly: **early refutation is not achievable this way.**
# Everything early enough to contradict the M100 is also P50/Pa territory. A
# genuinely absent M100 has to be established from amplitude/SNR or topography,
# not latency — see "Still open" in docs/meg-validation-study.md. Pretending
# otherwise by putting a refute threshold inside the P50 band is what the
# 2026-07-30 revision did, and it reproduced the artifact incident at 40 ms.

# Floor for the peak search. NOT "the earliest cortical response" — cortical
# auditory activity starts far earlier than this (Na ~19 ms, Pa ~28 ms in medial
# Heschl's gyrus). It is an ARTIFACT EXCLUSION bound: high enough to keep the
# ~15 ms stimulus artifact and its band-pass smearing out of the argmax, low
# enough to sit below any plausible M100.
_ARTIFACT_EXCLUSION_FLOOR_MS = 30.0

# Upper edge of P50/Pa territory. At or below this a peak is not attributable to
# a displaced M100 by latency alone.
_P50_UPPER_MS = 70.0

# P200 territory. A peak in here is more likely the P200 than a very late M100.
_P200_LOWER_MS = 170.0
_P200_UPPER_MS = 215.0

# Hard ceiling on the search, inside DEFAULT_BOUNDS["tmax"] (0.3 s). Beyond the
# P200 there is no canonical component to confuse the argmax, so this span is
# where refutation is both reachable and identifiable.
_SEARCH_CEILING_MS = 280.0

# Margin by which the search must exceed the refutation threshold, so the
# threshold is strictly contained rather than merely touched.
_SEARCH_BEYOND_THRESHOLD_MS = 20.0

# How far past the expected window a peak must fall before it is refutable, when
# the window itself sits late enough that the P200 bound is not the binding one.
_PLAUSIBLE_LATE_MS = 60.0


def _refute_threshold_ms(window_ms: Tuple[float, float]) -> float:
    """Latency above which a peak refutes the prediction.

    Binding constraint is whichever is later: the end of P200 territory, or a
    generous margin past the expected window. Pure, so the falsifiability
    invariant is unit-testable without the scientific stack.
    """
    return max(_P200_UPPER_MS, window_ms[1] + _PLAUSIBLE_LATE_MS)


def _latency_zone(latency_ms: Optional[float],
                  window_ms: Tuple[float, float]) -> str:
    """Which interpretive region a measured latency falls in.

    Separated from ``_classify`` so the zones can be asserted directly — the
    verdict alone cannot distinguish "off-window" from "another component", and
    that distinction is the whole point.
    """
    if latency_ms is None:
        return "no_peak"
    lo, hi = window_ms
    if lo <= latency_ms <= hi:
        return "supported"
    if latency_ms <= _P50_UPPER_MS:
        return "p50_ambiguous"
    if _P200_LOWER_MS <= latency_ms <= _P200_UPPER_MS:
        return "p200_ambiguous"
    if latency_ms > _refute_threshold_ms(window_ms):
        return "refutable"
    return "off_window_early" if latency_ms < lo else "off_window_late"


# Below this many epochs the evoked average is noise-dominated, so an out-of-
# window (or absent) peak is more likely a weak-sample artifact than a real
# refutation. We therefore WITHHOLD a confident ``refuted`` under this count and
# return ``inconclusive`` with a stated next step instead (#14 skepticism: a
# result that contradicts a well-established finding — e.g. a missing M100 in a
# dataset famous for it — is a red flag to inspect, not to accept). A genuine
# bst_auditory standards average has dozens of epochs; a handful means the crop
# was too small. Tune conservatively — this only gates the *refuted* downgrade.
MIN_RELIABLE_EPOCHS = 8


def _peak_search_bounds(window_ms: Tuple[float, float],
                        times_lo: float, times_hi: float) -> Tuple[float, float]:
    """The (tmin, tmax) seconds to search for the evoked peak.

    Spans ``_ARTIFACT_EXCLUSION_FLOOR_MS`` to ``_SEARCH_BEYOND_THRESHOLD_MS``
    past the refutation threshold, then clamps to the available sample times.
    Pure and mne-free.

    The clamp can still leave the threshold outside the range when the recording
    is too short (or the window sits very late). That is a real limit of the
    sample, not a classifier bug — callers detect it via
    ``_refute_threshold_ms`` and emit an ``unfalsifiable_late`` caveat rather
    than reporting a verdict that could not have been contradicted.
    """
    lo_ms = min(_ARTIFACT_EXCLUSION_FLOOR_MS, window_ms[0])
    hi_ms = min(_SEARCH_CEILING_MS,
                _refute_threshold_ms(window_ms) + _SEARCH_BEYOND_THRESHOLD_MS)
    lo = max(times_lo, lo_ms / 1000.0)
    hi = min(times_hi, hi_ms / 1000.0)
    return lo, hi


class MegIO(abc.ABC):
    """Structural contract for the MEG I/O backend the steps call through.

    Any object with these five methods satisfies it (duck-typed via
    ``__subclasshook__``, like ``collections.abc``) — the real :class:`_MneIO` or
    a test fake. Each method takes the prior step's artifact handle (or dataset
    id) and returns a new handle; the heavy work (and heavy imports) live behind
    this boundary.
    """

    _REQUIRED = ("fetch_sample", "preprocess", "epoch", "evoked", "measure_peak")

    @classmethod
    def __subclasshook__(cls, other: type):
        if cls is MegIO:
            return all(any(m in B.__dict__ for B in other.__mro__)
                       for m in cls._REQUIRED)
        return NotImplemented


# A module-level test hook: when set, the tools-layer meg_validation dispatch uses
# it instead of the real lazy-mne backend. Lets the offline tool test stay
# stdlib-only. Production never sets this.
_TEST_IO: Optional[Any] = None


# ---------------------------------------------------------------------------
# Real, memory-bounded mne backend. ALL heavy imports are inside the methods.
# Each method persists its output to disk and returns a small JSON-able handle
# (path + scalar metadata) so the pipeline can checkpoint it and a later/ resumed
# step can reload from that path alone.
# ---------------------------------------------------------------------------

class _MneIO:
    """The real I/O backend. Memory-bounded: never full-preloads a recording.

    Imports ``mne`` / ``numpy`` lazily inside each method, so importing this
    module costs nothing and works without the scientific stack installed.
    """

    def __init__(self, work_dir: Optional[str] = None):
        import tempfile
        self.work_dir = work_dir or tempfile.mkdtemp(prefix="meg_study_")

    def _path(self, name: str) -> str:
        import os
        return os.path.join(self.work_dir, name)

    def fetch_sample(self, *, dataset_id: str, bounds: dict) -> dict:
        import mne  # lazy

        if dataset_id != "bst_auditory":
            raise ValueError(
                f"meg_validation currently supports dataset 'bst_auditory', "
                f"not {dataset_id!r}.")
        data_path = mne.datasets.brainstorm.bst_auditory.data_path()
        run = bounds.get("run", DEFAULT_BOUNDS["run"])
        raw_fname = (f"{data_path}/MEG/bst_auditory/"
                     f"S01_AEF_20131218_0{run}.ds")
        # preload=False: header/metadata only — the samples are NOT pulled into
        # memory here. We crop + pick + resample BEFORE materializing.
        raw = mne.io.read_raw_ctf(raw_fname, preload=False, verbose="ERROR")
        crop_tmax = float(bounds.get("crop_tmax", DEFAULT_BOUNDS["crop_tmax"]))
        crop_tmax = min(crop_tmax, raw.times[-1])
        raw.crop(tmax=crop_tmax)
        # Keep a subset of gradiometer channels (+ stim) before loading samples.
        picks = mne.pick_types(raw.info, meg="grad", eeg=False, stim=True,
                               exclude="bads")
        max_ch = int(bounds.get("max_channels", DEFAULT_BOUNDS["max_channels"]))
        keep = [raw.info["ch_names"][p] for p in picks[:max_ch]]
        raw.pick(keep)
        raw.load_data(verbose="ERROR")  # now small: cropped + channel-subset
        resample_hz = bounds.get("resample_hz", DEFAULT_BOUNDS["resample_hz"])
        if resample_hz:
            raw.resample(float(resample_hz), verbose="ERROR")
        path = self._path("raw_sample.fif")
        raw.save(path, overwrite=True, verbose="ERROR")
        return {"path": path, "dataset_id": dataset_id, "bounds": dict(bounds)}

    def preprocess(self, raw_handle: dict, *, l_freq: float, h_freq: float) -> dict:
        import mne  # lazy

        raw = mne.io.read_raw_fif(raw_handle["path"], preload=True,
                                  verbose="ERROR")
        raw.filter(l_freq=l_freq, h_freq=h_freq, verbose="ERROR")
        path = self._path("filtered.fif")
        raw.save(path, overwrite=True, verbose="ERROR")
        return {"path": path, "filtered": [l_freq, h_freq]}

    def epoch(self, filtered_handle: dict, *, tmin: float, tmax: float,
              event_id: Optional[int] = None) -> dict:
        import mne  # lazy

        raw = mne.io.read_raw_fif(filtered_handle["path"], preload=True,
                                  verbose="ERROR")
        # Read events FROM the data rather than assuming counts.
        events = mne.find_events(raw, verbose="ERROR")
        # Filter to a SINGLE trigger code so we average one condition, not every
        # trigger (standards + deviants + button presses). event_id semantics:
        #   None   -> auto: the most-frequent code (the standard tone) [default]
        #   "all"  -> no filter: epoch every trigger (legacy/diagnostic only)
        #   int    -> that explicit trigger code
        if event_id == "all":
            mne_event_id = None  # mne: None means "use all event codes"
        elif event_id is None:
            # Most-frequent trigger code = the standard tone. Use stdlib Counter
            # on plain ints so this needs no extra numpy surface.
            from collections import Counter
            codes = [int(c) for c in events[:, 2]]
            mne_event_id = Counter(codes).most_common(1)[0][0] if codes else None
            event_id = mne_event_id
        else:
            mne_event_id = int(event_id)
            event_id = mne_event_id
        epochs = mne.Epochs(raw, events, event_id=mne_event_id, tmin=tmin,
                            tmax=tmax, baseline=(None, 0), preload=True,
                            verbose="ERROR")
        path = self._path("epochs-epo.fif")
        epochs.save(path, overwrite=True, verbose="ERROR")
        return {"path": path, "n_epochs": len(epochs), "window": [tmin, tmax],
                "event_id": event_id}

    def evoked(self, epochs_handle: dict) -> dict:
        import mne  # lazy

        epochs = mne.read_epochs(epochs_handle["path"], preload=True,
                                 verbose="ERROR")
        ev = epochs.average()
        path = self._path("evoked-ave.fif")
        ev.save(path, overwrite=True, verbose="ERROR")
        return {"path": path, "n_epochs": epochs_handle.get("n_epochs",
                                                            len(epochs))}

    def measure_peak(self, evoked_handle: dict, *,
                     window_ms: Tuple[float, float]) -> dict:
        import mne  # lazy

        n_epochs = int(evoked_handle.get("n_epochs", 0))
        ev = mne.read_evokeds(evoked_handle["path"], verbose="ERROR")[0]
        if n_epochs <= 0 or len(ev.times) == 0:
            return {"latency_ms": None, "amplitude": None, "n_epochs": n_epochs}
        # Search from the artifact-exclusion floor to past the refutation
        # threshold, clamped to the available sample times. Wide enough that the
        # prediction can fail, floored so the ~15 ms stimulus artifact cannot win
        # the argmax. See the verdict-geometry comment near the constants — the
        # two constraints pull opposite ways and the floor is what reconciles
        # them. `mode="abs"` is polarity-blind, which is why a peak in P50/P200
        # territory is caveated rather than refuted downstream.
        lo, hi = _peak_search_bounds(window_ms, float(ev.times[0]),
                                     float(ev.times[-1]))
        ch, latency_s, amp = ev.get_peak(tmin=lo, tmax=hi, mode="abs",
                                         return_amplitude=True)
        return {"latency_ms": float(latency_s) * 1000.0,
                "amplitude": float(amp), "n_epochs": n_epochs, "channel": ch,
                # The actual search bounds (ms) used for this peak. Load-bearing,
                # not just diagnostic: _validate_finding_step reads these to
                # detect a boundary-pinned argmax and an unfalsifiable range.
                "search_ms": [lo * 1000.0, hi * 1000.0]}


# ---------------------------------------------------------------------------
# Steps. Each reads the prior step's artifact handle from ctx.results, does ONE
# io transform, and persists its own. Built closing over the injected ``io``.
# ---------------------------------------------------------------------------

def _classify(latency_ms: Optional[float],
              window_ms: Tuple[float, float]) -> str:
    """Verdict for a measured peak latency against the expected window.

    Only the ``refutable`` zone yields ``refuted``. Everything a known component
    could account for degrades to ``inconclusive`` — see the verdict geometry
    comment above for why that is not timidity but identifiability.
    """
    zone = _latency_zone(latency_ms, window_ms)
    if zone == "supported":
        return "supported"
    if zone == "refutable":
        return "refuted"
    return "inconclusive"


def _fetch_sample_step(io: Any, dataset_id: str, bounds: dict) -> Step:
    def fn(ctx: StepContext) -> StepResult:
        handle = io.fetch_sample(dataset_id=dataset_id, bounds=bounds)
        return StepResult(
            data={"dataset_id": dataset_id, "bounds": dict(bounds),
                  "path": handle.get("path")},
            artifacts=[{"path": handle.get("path", ""), "kind": "raw_sample",
                        "meta": {"dataset_id": dataset_id, "bounds": bounds}}],
        )
    return Step(name="fetch_sample", fn=fn)


def _preprocess_step(io: Any, bounds: dict) -> Step:
    def fn(ctx: StepContext) -> StepResult:
        prior = ctx.results["fetch_sample"]
        out = io.preprocess(
            {"path": prior.get("path")},
            l_freq=bounds.get("l_freq", DEFAULT_BOUNDS["l_freq"]),
            h_freq=bounds.get("h_freq", DEFAULT_BOUNDS["h_freq"]))
        return StepResult(
            data={"filtered": out.get("filtered"), "path": out.get("path")},
            artifacts=[{"path": out.get("path", ""), "kind": "filtered_sample",
                        "meta": {"filtered": out.get("filtered")}}],
        )
    return Step(name="preprocess", fn=fn)


def _epoch_step(io: Any, bounds: dict) -> Step:
    def fn(ctx: StepContext) -> StepResult:
        prior = ctx.results["preprocess"]
        epochs = io.epoch(
            {"path": prior.get("path")},
            tmin=bounds.get("tmin", DEFAULT_BOUNDS["tmin"]),
            tmax=bounds.get("tmax", DEFAULT_BOUNDS["tmax"]),
            event_id=bounds.get("event_id", DEFAULT_BOUNDS["event_id"]))
        return StepResult(
            data={"n_epochs": epochs.get("n_epochs"),
                  "window": epochs.get("window"),
                  "event_id": epochs.get("event_id"), "path": epochs.get("path")},
            artifacts=[{"path": epochs.get("path", ""), "kind": "epochs",
                        "meta": {"n_epochs": epochs.get("n_epochs"),
                                 "event_id": epochs.get("event_id")}}],
        )
    return Step(name="epoch", fn=fn)


def _evoked_step(io: Any) -> Step:
    def fn(ctx: StepContext) -> StepResult:
        prior = ctx.results["epoch"]
        ev = io.evoked({"path": prior.get("path"),
                        "n_epochs": prior.get("n_epochs")})
        # may raise (e.g. OOM) -> step fails, study resumable
        return StepResult(
            data={"n_epochs": ev.get("n_epochs"), "path": ev.get("path")},
            artifacts=[{"path": ev.get("path", ""), "kind": "evoked",
                        "meta": {"n_epochs": ev.get("n_epochs")}}],
        )
    return Step(name="evoked", fn=fn)


def _validate_finding_step(io: Any, dataset_id: str,
                           window_ms: Tuple[float, float]) -> Step:
    def fn(ctx: StepContext) -> StepResult:
        prior = ctx.results["evoked"]
        peak = io.measure_peak(
            {"path": prior.get("path"), "n_epochs": prior.get("n_epochs")},
            window_ms=window_ms)
        latency = peak.get("latency_ms")
        amplitude = peak.get("amplitude")
        n_epochs = peak.get("n_epochs")
        verdict = _classify(latency, window_ms)

        # Skepticism layer (#14): surface the material an agent needs to sanity-
        # check the number, and refuse to report a *confident* refutation drawn
        # from too thin a sample. A `refuted` that rests on a noise-dominated
        # average (few epochs, or no measurable amplitude) is downgraded to
        # `inconclusive` with a next step — never silently accepted.
        caveats: List[str] = []
        next_step = None

        # An argmax pinned to the edge of the search range is evidence the true
        # extremum lies OUTSIDE the range, not that the M100 moved there. This is
        # how the original incident happened: a search reaching down to the ~15 ms
        # stimulus artifact returned the artifact as "the peak" and called a
        # textbook finding refuted. Widening the search to make refutation
        # reachable reintroduces the same hazard at the new boundary, so the
        # boundary itself must never produce a confident verdict.
        search = peak.get("search_ms") or []
        if latency is not None and len(search) == 2:
            s_lo, s_hi = float(search[0]), float(search[1])
            # One sample period at the sample rates in use here is well under a
            # millisecond; 1 ms is a deliberately generous "at the edge" band.
            at_edge = min(abs(latency - s_lo), abs(latency - s_hi)) <= 1.0
            if at_edge:
                caveats.append(
                    f"boundary_hit: the peak ({latency:.1f} ms) sits at the edge "
                    f"of the search range [{s_lo:.1f}, {s_hi:.1f}] ms, which "
                    f"usually means the real extremum is outside it — an "
                    f"artifact or a later component, not a displaced M100")
                if verdict != "supported":
                    verdict = "inconclusive"
                    next_step = (
                        "Peak landed on the search boundary. Widen the epoch "
                        "(DEFAULT_BOUNDS tmax) or inspect the evoked intermediate "
                        "before drawing any conclusion from this latency.")

        # Falsifiability check on the ACTUAL clamped range. If the recording (or a
        # very late window) leaves the refute threshold outside the searchable
        # span, then `supported` was the only attainable answer and must not be
        # reported as though it survived a test it could not have failed.
        thresh = _refute_threshold_ms(window_ms)
        if len(search) == 2 and float(search[1]) <= thresh:
            caveats.append(
                f"unfalsifiable_late: the search ceiling ({float(search[1]):.1f} "
                f"ms) does not reach the refutation threshold ({thresh:.1f} ms), "
                f"so no measurable latency could have refuted this claim. Treat "
                f"a `supported` here as untested, not confirmed")

        # Latency alone cannot separate a displaced M100 from a neighbouring
        # component, so name which one is in play rather than implying a verdict.
        zone = _latency_zone(latency, window_ms)
        if zone == "p50_ambiguous":
            caveats.append(
                f"component_ambiguous: {latency:.1f} ms is P50/Pa territory "
                f"(<= {_P50_UPPER_MS:.0f} ms). A polarity-blind argmax cannot "
                f"tell an early component from an absent M100")
        elif zone == "p200_ambiguous":
            caveats.append(
                f"component_ambiguous: {latency:.1f} ms is P200 territory "
                f"({_P200_LOWER_MS:.0f}-{_P200_UPPER_MS:.0f} ms). More likely the "
                f"P200 than a very late M100")

        low_evidence = isinstance(n_epochs, int) and n_epochs < MIN_RELIABLE_EPOCHS
        if low_evidence:
            caveats.append(
                f"low_evidence: {n_epochs} epochs is below the reliable "
                f"threshold ({MIN_RELIABLE_EPOCHS}); the evoked average is "
                f"noise-dominated and a single peak number is not yet trustworthy")
        if verdict == "refuted" and (low_evidence or amplitude is None):
            verdict = "inconclusive"
            next_step = (
                "Refutation withheld on weak evidence: a missing or displaced "
                "M100 in a dataset known for it is more likely too few epochs "
                "than a real absence. Increase the sample (more epochs / a "
                "larger crop) and re-run, or inspect the evoked intermediate, "
                "before concluding refuted.")

        claim = (f"auditory M100 peak for {dataset_id} falls within "
                 f"{window_ms[0]:.0f}-{window_ms[1]:.0f} ms")
        evidence = {
            "latency_ms": latency,
            "amplitude": amplitude,
            "expected_window_ms": list(window_ms),
            "search_window_ms": peak.get("search_ms"),
            "channel": peak.get("channel"),
            "n_epochs": n_epochs,
            "caveats": caveats,
        }
        if next_step:
            evidence["next_step"] = next_step
        finding = {
            "claim": claim,
            "verdict": verdict,
            "score": None,
            "evidence": evidence,
        }
        return StepResult(data={"verdict": verdict, "latency_ms": latency,
                                "amplitude": amplitude},
                          findings=[finding])
    return Step(name="validate_finding", fn=fn)


# ---------------------------------------------------------------------------
# Public builder — mirrors bibliography_study.build_steps.
# ---------------------------------------------------------------------------

def build_steps(*, dataset_id: str = "bst_auditory",
                io: Optional[Any] = None,
                expected_window_ms: Tuple[float, float] = DEFAULT_WINDOW_MS,
                bounds: Optional[dict] = None) -> List[Step]:
    """Build the five meg_validation steps for *dataset_id*.

    Pass *io* to inject the MEG I/O backend (tests pass a fake). With ``io=None``
    the real, memory-bounded :class:`_MneIO` backend is used (lazy mne). *bounds*
    overrides the memory-bounding defaults (run/channels/crop/resample/filter/
    epoch window); *expected_window_ms* is the M100 window the finding is judged
    against. The returned steps feed ``pipeline.run`` / ``resume``.
    """
    io = io if io is not None else _MneIO()
    merged_bounds = dict(DEFAULT_BOUNDS)
    if bounds:
        merged_bounds.update(bounds)
    window = (float(expected_window_ms[0]), float(expected_window_ms[1]))
    return [
        _fetch_sample_step(io, dataset_id, merged_bounds),
        _preprocess_step(io, merged_bounds),
        _epoch_step(io, merged_bounds),
        _evoked_step(io),
        _validate_finding_step(io, dataset_id, window),
    ]


# ---------------------------------------------------------------------------
# Golden recipe (#14): a shipped, offline, dependency-free worked example.
#
# The real study needs mne + a multi-GB download; that is the wrong thing to run
# in CI or to point a fresh agent at as "what correct looks like." The golden
# recipe runs the SAME five-step pipeline and the SAME verdict/skepticism logic
# against a synthetic backend that plants a known in-window M100 — deterministic,
# stdlib-only, instant. It doubles as a regression smoke test and a reference the
# agent can imitate. See docs/golden-validation-recipe.md.
# ---------------------------------------------------------------------------

GOLDEN_DATASET_ID = "synthetic-golden"
GOLDEN_PEAK_MS = 100.0          # planted peak latency — squarely inside 80-120
GOLDEN_N_EPOCHS = 40            # a healthy, reliable epoch count


class SyntheticMegIO:
    """A real, dependency-free MEG backend that plants a known M100 peak.

    Mirrors the :class:`MegIO` contract exactly, but fabricates small dict
    handles instead of touching mne / numpy / the filesystem / the network — so
    the full study runs deterministically and instantly. The planted peak
    (``peak_latency_ms``, default :data:`GOLDEN_PEAK_MS`) sits inside the default
    80-120 ms window with a healthy epoch count, so a correct pipeline yields
    ``supported``. The parameters let a test also plant an out-of-window or
    thin-sample peak to exercise the skepticism path.
    """

    def __init__(self, *, peak_latency_ms: float = GOLDEN_PEAK_MS,
                 peak_amp: float = 4.2e-13, n_epochs: int = GOLDEN_N_EPOCHS):
        self.peak_latency_ms = float(peak_latency_ms)
        self.peak_amp = float(peak_amp)
        self.n_epochs = int(n_epochs)

    def fetch_sample(self, *, dataset_id: str, bounds: dict) -> dict:
        return {"dataset_id": dataset_id, "bounds": dict(bounds),
                "path": f"synthetic://{dataset_id}/raw"}

    def preprocess(self, raw_handle: dict, *, l_freq: float, h_freq: float) -> dict:
        return {**raw_handle, "filtered": [l_freq, h_freq],
                "path": "synthetic://filtered"}

    def epoch(self, filtered_handle: dict, *, tmin: float, tmax: float,
              event_id: Any = None) -> dict:
        return {**filtered_handle, "n_epochs": self.n_epochs,
                "window": [tmin, tmax], "event_id": event_id,
                "path": "synthetic://epochs"}

    def evoked(self, epochs_handle: dict) -> dict:
        return {**epochs_handle, "averaged": True, "path": "synthetic://evoked"}

    def measure_peak(self, evoked_handle: dict, *,
                     window_ms: Tuple[float, float]) -> dict:
        if self.n_epochs <= 0:
            return {"latency_ms": None, "amplitude": None, "n_epochs": 0,
                    "channel": None, "search_ms": list(window_ms)}
        lo, hi = _peak_search_bounds(window_ms, float(DEFAULT_BOUNDS["tmin"]),
                                     float(DEFAULT_BOUNDS["tmax"]))
        # A planted peak outside the search range is evidence that could not have
        # been produced by the real pipeline. This is the reference artefact
        # agents are pointed at, so refuse to emit an impossible one rather than
        # teaching the shape of a self-contradicting finding.
        if not (lo * 1000.0 <= self.peak_latency_ms <= hi * 1000.0):
            raise ValueError(
                f"planted peak {self.peak_latency_ms} ms is outside the search "
                f"range [{lo * 1000.0:.1f}, {hi * 1000.0:.1f}] ms for window "
                f"{window_ms}; the real pipeline could never return it")
        return {"latency_ms": self.peak_latency_ms, "amplitude": self.peak_amp,
                "n_epochs": self.n_epochs, "channel": "MEG 1631",
                "search_ms": [lo * 1000.0, hi * 1000.0]}


def build_golden_steps(*, peak_latency_ms: float = GOLDEN_PEAK_MS,
                       n_epochs: int = GOLDEN_N_EPOCHS,
                       expected_window_ms: Tuple[float, float] = DEFAULT_WINDOW_MS
                       ) -> List[Step]:
    """Build the offline *golden* meg_validation pipeline (synthetic backend).

    No mne / numpy / network — a fully deterministic worked example that yields
    ``supported`` for a planted in-window M100. Doubles as the package's smoke
    test and the reference recipe (see docs/golden-validation-recipe.md). The
    defaults plant a correct peak; override ``peak_latency_ms`` / ``n_epochs`` to
    demonstrate the refuted / skeptical-inconclusive paths.
    """
    io = SyntheticMegIO(peak_latency_ms=peak_latency_ms, n_epochs=n_epochs)
    return build_steps(dataset_id=GOLDEN_DATASET_ID, io=io,
                       expected_window_ms=expected_window_ms)
