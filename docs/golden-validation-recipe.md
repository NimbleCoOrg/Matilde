# Golden validation recipe (offline, self-demonstrating)

Builds on the [MEG data-sample validation study](meg-validation-study.md) and the
[stateful study pipeline](stateful-study-pipeline.md). The **golden recipe** is a
fully worked, dependency-free validation that runs the *same* five-step pipeline
and the *same* verdict logic as the real `bst_auditory` study — but against a
synthetic backend that plants a known in-window M100. No `mne`, no `numpy`, no
network, no multi-GB download.

It exists for two reasons:

1. **A reference the agent imitates.** A fresh Matilde can run it to see what a
   correct, well-evidenced validation looks like before judging a real result —
   the shape of a `supported` finding, with clean diagnostics.
2. **An offline regression smoke test.** It exercises the pipeline + skepticism
   logic deterministically in CI, with nothing heavy installed
   (`tests/test_golden_meg.py`, `tests/test_study_tools.py`).

## Why a golden recipe

The original M100 study *ran without error* yet measured the wrong number — a
15 ms stimulus artifact reported as the peak, calling a textbook finding
`refuted`. "The pipeline ran" is not evidence the science is right. A golden run
with a **known** answer gives both the agent and CI a fixed point: if the planted
in-window peak does not come back `supported` with clean diagnostics, something
regressed in the pipeline or the verdict logic.

## Run it live

```
matilde_study_create  slug="golden-demo"  kind="golden_meg_validation"
matilde_study_run     study_id=<id>
matilde_study_status  study_id=<id>
```

Expected: status `done`, one finding, `verdict: "supported"`, `latency_ms: 100.0`
inside the 80–120 ms window, a healthy `n_epochs`, and `caveats: []`.

## What it demonstrates

A correct finding carries the material to **sanity-check** it, not just a verdict:

| field | golden value | why it matters |
|---|---|---|
| `verdict` | `supported` | in-window peak from a reliable sample |
| `latency_ms` | `100.0` | the planted peak |
| `expected_window_ms` | `[80.0, 120.0]` | the auditory M100 window |
| `search_window_ms` | `[30.0, 235.0]` | provenance of the measurement. Floor excludes the ~15 ms artifact; ceiling clears the 215 ms refutation threshold so the claim could actually have failed |
| `n_epochs` | `40` | above `MIN_RELIABLE_EPOCHS`, so the average is trustworthy |
| `channel` | the peak channel | which sensor carried it |
| `caveats` | `[]` | nothing to flag |

## The skepticism path

The same recipe demonstrates the *defensive* behavior too. Built in code with a
thin, out-of-window sample:

```python
from matilde_plugin.engine.meg_study import build_golden_steps, MIN_RELIABLE_EPOCHS
steps = build_golden_steps(peak_latency_ms=225.0, n_epochs=MIN_RELIABLE_EPOCHS - 1)
```

This does **not** return a confident `refuted`. Because the epoch count is below
`MIN_RELIABLE_EPOCHS`, the evoked average is noise-dominated, so the study
downgrades to `inconclusive` and attaches a `next_step` ("increase the sample /
larger crop, or inspect the evoked intermediate, before concluding refuted").
That is the rule a real validation must follow: a result contradicting a
well-established finding is a red flag to inspect, never a confident refutation
built on weak evidence.

## Where it lives

- Backend + builder: `matilde_plugin/engine/meg_study.py`
  (`SyntheticMegIO`, `build_golden_steps`, `GOLDEN_PEAK_MS`, `MIN_RELIABLE_EPOCHS`).
- Tool kind: `golden_meg_validation` (`matilde_plugin/tools.py`).
- Tests: `tests/test_golden_meg.py`, `tests/test_study_tools.py`.
