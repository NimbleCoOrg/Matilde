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
5. `validate_finding` — measure peak latency in the M100 window; emit a finding:
   `supported` if within the expected window (default 80–120 ms), `refuted` if
   outside the **plausibility band** (window −20 ms / +60 ms, asymmetric because
   the M100 latency distribution is right-skewed), `inconclusive` between the two
   or if SNR is too low / the sample too small. Record the measured latency +
   amplitude as evidence.

   The peak search deliberately spans **wider than the plausibility band**
   (±20 ms beyond it), floored at 30 ms — the earliest a cortical auditory
   response can occur. That floor is what keeps the ~15 ms stimulus artifact out
   of range; the extra width is what keeps `refuted` reachable.

> **Falsifiability invariant — do not break this.** The search range must strictly
> contain the refutation thresholds. The peak is the argmax *inside* the search
> range, so a range narrower than those thresholds makes `refuted` unreachable and
> the prediction unfalsifiable. Between the `e079993` artifact fix and 2026-07-30
> this was exactly the state: search was window ±10 ms while refutation required
> window ±50 ms, so **no measurement at any configured window could return
> `refuted`** — only `supported` or `inconclusive`. The existing `refuted` tests
> did not catch it because they drive `FakeMegIO`, which returns a latency
> directly and never consults the search bounds.
>
> Consequences for the record: (a) the single historical `refuted` predates the
> artifact fix and came from measuring the 15 ms artifact — **it is retracted**,
> it was never evidence against the M100; (b) every `supported` recorded between
> `e079993` and 2026-07-30, including the golden recipe's `latency_ms: 100.0`,
> carried **no falsification risk** — re-run under the corrected range before
> making any "validated the M100 against real data" claim. Enforced by
> `tests/test_meg_study.py::test_refuted_is_reachable_from_the_real_search_range`
> and `::test_late_refutation_is_always_reachable`.

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
