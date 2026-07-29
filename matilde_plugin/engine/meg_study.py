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

# How far outside the expected window a measured peak must fall before the finding
# is called ``refuted`` (rather than merely off-window). Inside the window ->
# supported; outside the plausibility band -> refuted; between the two ->
# inconclusive (not clear-cut either way).
#
# ASYMMETRIC on purpose. The M100 latency distribution is right-skewed: a genuine
# auditory peak arriving late (attenuated stimulus, older subject, degraded SNR)
# is ordinary, while one arriving materially EARLY is not physiological -- there
# is no cortical auditory generator before ~50 ms. So allow more room late than
# early before declaring the prediction refuted.
_PLAUSIBLE_EARLY_MS = 20.0
_PLAUSIBLE_LATE_MS = 60.0

# The earliest latency that can be a cortical auditory response at all. Floors
# the peak search so it can never reach back to the ~15 ms stimulus artifact --
# the original mis-measurement this module was fixed for.
_EARLIEST_CORTICAL_MS = 30.0

# How far the peak search extends BEYOND the plausibility band.
#
# This constant is what keeps the study falsifiable, and it is the whole reason
# the block above is shaped this way. The peak is defined as the argmax INSIDE the
# search range, so a range that stops at (or inside) the refutation threshold can
# never yield a latency the classifier calls ``refuted``: the hypothesis becomes
# unfalsifiable no matter what the data contains. That was the state of this file
# before 2026-07-30 -- search was window +/-10 ms while refutation required
# window +/-50 ms, so `refuted` was unreachable for EVERY configured window and
# the only outcomes were {supported, inconclusive}.
#
# INVARIANT, enforced by tests/test_meg_study.py:
#   the search range must strictly contain the refutation thresholds on both sides.
# If you narrow the search to chase an artifact, widen it here or loosen the
# plausibility band -- do not let them cross.
_SEARCH_BEYOND_BAND_MS = 20.0


def _plausible_band_ms(window_ms: Tuple[float, float]) -> Tuple[float, float]:
    """The band outside which a measured peak counts as refuting the prediction.

    Wider than the expected window (a peak just off the window is inconclusive,
    not a refutation) and asymmetric (see ``_PLAUSIBLE_LATE_MS``). Pure, so the
    falsifiability invariant is unit-testable without the scientific stack.
    """
    lo, hi = window_ms
    return lo - _PLAUSIBLE_EARLY_MS, hi + _PLAUSIBLE_LATE_MS

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
    """Compute the (tmin, tmax) seconds to search for the evoked peak.

    Pure and mne-free so it is unit-testable without the scientific stack. The
    search spans the plausibility band (``_plausible_band_ms``) plus
    ``_SEARCH_BEYOND_BAND_MS`` on each side, is floored at
    ``_EARLIEST_CORTICAL_MS``, then clamped to the available sample times
    ``[times_lo, times_hi]`` (seconds).

    Two constraints are in tension here and both matter:

    * The search must NOT reach the ~15 ms stimulus artifact. The original M100
      bug used a +/-100 ms search over an 80-120 ms window, grabbed that artifact
      and reported it as the peak. ``_EARLIEST_CORTICAL_MS`` is the floor that
      prevents it -- a floor on physiology, not an arbitrary margin.
    * The search MUST extend past the refutation thresholds, or ``refuted`` is
      unreachable and the prediction cannot fail. Clamping the search tighter than
      the plausibility band (the pre-2026-07-30 behaviour) silently converted a
      falsifiable study into an unfalsifiable one.

    Note the clamp to ``[times_lo, times_hi]`` can still break the invariant when
    the recording is genuinely too short to contain the band -- that is a real
    limit of the sample, not a classifier bug, and it correctly yields
    ``inconclusive`` rather than a refutation the data cannot support.
    """
    band_lo_ms, band_hi_ms = _plausible_band_ms(window_ms)
    lo_s = max(band_lo_ms - _SEARCH_BEYOND_BAND_MS,
               _EARLIEST_CORTICAL_MS) / 1000.0
    hi_s = (band_hi_ms + _SEARCH_BEYOND_BAND_MS) / 1000.0
    lo = max(times_lo, lo_s)
    hi = min(times_hi, hi_s)
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
        # Search WITHIN the expected window (+/- a small margin), clamped to the
        # available sample times. A wide search reaches the large early stimulus
        # artifact (~15 ms) and misreports it as the auditory peak; the pure
        # helper below keeps the search in-window (see _peak_search_bounds).
        lo, hi = _peak_search_bounds(window_ms, float(ev.times[0]),
                                     float(ev.times[-1]))
        ch, latency_s, amp = ev.get_peak(tmin=lo, tmax=hi, mode="abs",
                                         return_amplitude=True)
        return {"latency_ms": float(latency_s) * 1000.0,
                "amplitude": float(amp), "n_epochs": n_epochs, "channel": ch,
                # The actual in-window search bounds (ms) used for this peak — a
                # diagnostic so the verdict's provenance is inspectable (#14).
                "search_ms": [lo * 1000.0, hi * 1000.0]}


# ---------------------------------------------------------------------------
# Steps. Each reads the prior step's artifact handle from ctx.results, does ONE
# io transform, and persists its own. Built closing over the injected ``io``.
# ---------------------------------------------------------------------------

def _classify(latency_ms: Optional[float],
              window_ms: Tuple[float, float]) -> str:
    """Verdict for a measured peak latency against the expected window."""
    lo, hi = window_ms
    if latency_ms is None:
        return "inconclusive"           # degenerate sample / no measurable peak
    if lo <= latency_ms <= hi:
        return "supported"              # within the expected window
    band_lo, band_hi = _plausible_band_ms(window_ms)
    if latency_ms < band_lo or latency_ms > band_hi:
        return "refuted"                # outside the plausibility band
    return "inconclusive"              # just off the window — not clear-cut


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
        lo, hi = _peak_search_bounds(window_ms, -0.1, 0.3)
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
