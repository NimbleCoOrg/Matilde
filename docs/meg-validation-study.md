# MEG data-sample validation study (design)

Builds on the stateful study pipeline (`docs/stateful-study-pipeline.md`). First
**heavy / real-data** study type: validate a reported neuroscience finding against
a **bounded sample** of an open MEG dataset, as resumable checkpointed steps.

## Why "data sample", not full-dataset

Full-preloading a multi-GB MEG recording into the agent process exhausts container
memory. This study deliberately validates against a **bounded sample** —
enough signal to test the claimed effect, never the whole recording. Combined
with the pipeline's per-step checkpointing, a memory kill or container restart
**resumes from the last completed step** instead of restarting the analysis.

## Worked claim (first target)

The auditory **M100 / N100m** evoked response: a magnetic field peak ~80–120 ms
after an auditory stimulus. Open data: the Brainstorm auditory sample
(`mne.datasets.brainstorm.bst_auditory`, the ds000246 tutorial data) — standard
tones, a well-established M100. The study measures the peak latency of the evoked
response to standard tones and validates it falls within the expected window.

## Steps (resumable; each checkpoints an artifact + may emit a finding)

1. `fetch_sample` — obtain a bounded slice: download/locate the dataset, but load
   **memory-bounded** — `preload=False`, pick one run, a subset of gradiometer
   channels, crop to a stimulus-locked window, and downsample. Persist a small
   intermediate (e.g. epochs/evoked `.npy` or `.fif`) as an artifact; never hold
   the full recording.
2. `preprocess` — band-pass filter (e.g. 1–40 Hz) the bounded sample; checkpoint.
3. `epoch` — epoch around standard-tone events (the analysis must read the events
   from the data, not assume counts); checkpoint epoch metadata.
4. `evoked` — average to the evoked response; checkpoint the evoked array artifact.
5. `validate_finding` — measure peak latency in the M100 window; emit a finding.
   `supported` if the peak is inside the expected window (default 80–120 ms).
   `refuted` only if it is later than the **refutation threshold** — the later of
   215 ms (the end of P200 territory) or `window_hi + 60 ms`. Everything else is
   `inconclusive`, with a caveat naming *why*. Record latency + amplitude as
   evidence.

   The peak search runs from **30 ms** (artifact-exclusion floor) to **20 ms past
   the refutation threshold**, capped at 280 ms and clamped to the epoch.

### Why the verdict geometry looks like this

The peak is a polarity-blind argmax (`mode="abs"`). Two failure modes pull in
opposite directions, and both have actually happened here:

- **Search too narrow → `refuted` unreachable.** Between `e079993` and
  2026-07-30 the search was window ±10 ms while refutation required window
  ±50 ms, so **no measurement at any configured window could return `refuted`**.
  The prediction could not fail.
- **Search too wide → the argmax catches something that is not the M100.** The
  original bug was exactly this: a ±100 ms search grabbed the ~15 ms stimulus
  artifact. The first attempt to fix the falsifiability problem (2026-07-30)
  widened the search to a 40 ms floor while leaving an early refute threshold at
  60 ms — reproducing the same incident at a new latency, with a *confident*
  `refuted` where the previous code had safely said `inconclusive`.

Width alone cannot resolve that. So the search is wide enough to contain the
refutation threshold, and the classifier refuses to refute anywhere a known
component could be the winner:

```
   30 ms         70 ms      window     170 ms    215 ms       280 ms
 ---|-------------|---------[====]-------|---------|------------|---
   floor       P50/Pa     supported   off-window  P200      refutable -->
 (artifact)   ambiguous               late      ambiguous
              -> inconclusive                -> inconclusive
```

**Early refutation is not achievable this way, and the code does not pretend
otherwise.** Everything early enough to contradict the M100 is also P50/Pa
territory (this dataset has a P50 around 50 ms). Establishing a genuinely absent
M100 needs amplitude/SNR or topography, not latency — see "Still open".

Three guards make the remaining verdicts honest:

- **`boundary_hit`** — an argmax pinned within 1 ms of a search edge means the
  true extremum is probably *outside* the range. Forced to `inconclusive`.
- **`component_ambiguous`** — a peak in P50 or P200 territory is named as such
  rather than scored.
- **`unfalsifiable_late`** — if the clamped epoch cannot reach the refutation
  threshold, `supported` was the only attainable answer, and it is labelled
  untested rather than confirmed.

> **Falsifiability invariant, test-enforced:** the search range must strictly
> contain the late refutation threshold. `_SEARCH_BEYOND_THRESHOLD_MS` is the
> margin that guarantees it. If you narrow the search to chase an artifact, raise
> the floor — do not let the floor cross a refute threshold. Enforced by
> `test_late_refutation_is_reachable_from_the_real_search_range`,
> `test_search_range_contains_the_late_refutation_threshold`,
> `test_peak_at_the_search_floor_is_not_a_confident_refutation` and
> `test_falsifiability_holds_under_the_shipped_epoch_bounds`; the constants are
> mutation-tested (7/7 mutants killed).

**Consequences for the record.** The single historical `refuted` predates the
artifact fix and came from measuring the 15 ms artifact — **retracted**; it was
never evidence against the M100. Every `supported` recorded between `e079993`
and 2026-07-30 carried **no falsification risk**. Re-running the *golden* recipe
does not re-test anything (its synthetic backend returns a planted constant); the
number that needs re-measuring is the real **120.0 ms** from `e079993`, which sat
exactly on the old search's upper bound and disagrees with this dataset's
published N100 (~95 ms) by 25 ms.

### Still open

- **Absence cannot be refuted.** `get_peak` always returns an argmax, so "no
  M100" is indistinguishable from "some peak somewhere". Needs an amplitude/SNR
  threshold.
- **Early refutation** needs polarity or topography, per above.
- **The band magnitudes (70 / 170 / 215 ms) are conventional, not fitted.** They
  bracket the canonical N1 (60–150 ms) and P2 (150–250 ms) and the M100's
  reported ~99–153 ms range across frequency and intensity (Roberts et al. 2000),
  but they are not derived from this dataset. Refutation therefore requires a
  large latency departure — it is a guard against gross misidentification, not a
  sensitive test of latency shifts.
- **`MIN_RELIABLE_EPOCHS = 8`** gates `refuted` but never `supported`. That
  asymmetric evidentiary bar deserves its own review; 8 is also well below CAEP
  practice.

## Implementation rules (match the repo)

- **Lazy heavy deps**: `mne`/`numpy` imported **inside** the step functions, never
  at module import — the plugin must load (and the rest of the engine stay
  stdlib-only) without mne installed. The agent container already has mne available
  at runtime.
- **Injected I/O for tests**: the step functions take injected loader/analysis
  handles so the whole study is testable with **fakes** (no mne, no network, no
  data) — deterministic, fast, stdlib-only. The real mne path is exercised only at
  runtime and by an **opt-in** live test (`MATILDE_LIVE=1`, skipped in CI).
- **Wire into the framework**: add a `meg_validation` study type the
  `matilde_study_create` plan can request (params: dataset id, expected-window ms,
  channel/sample bounds), runnable via `matilde_study_run` and resumable like any
  study.

## Tests

- Unit (stdlib, fakes): each step's logic; `validate_finding` verdict boundaries
  (within window → supported; far outside → refuted; degenerate sample →
  inconclusive); resumability (fail at `evoked`, resume, earlier steps not re-run).
- Opt-in live (`MATILDE_LIVE=1`): run the real bounded mne pipeline on
  `bst_auditory`, assert a peak is found in a plausible window and the study
  completes `done` within a bounded memory footprint. Skipped without the flag.

## Out of scope (later)

Multi-subject/meta-analysis, source localization, statcheck/GRIM re-checking —
separate study types once this single-finding validation is proven live.
