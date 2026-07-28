# Results provenance checklist

**The test:** a results file is finished when a second script — or you, six weeks
later, with the conversation gone — can **regenerate its headline number from the
file alone.** Not "understand roughly what was done." Regenerate.

Everything below exists because its absence made a real number
non-reproducible. The failures are recorded in
[trustworthy-comparison.md](trustworthy-comparison.md).

---

## The checklist

A results JSON must carry:

- [ ] **Seed, and which RNGs it was applied to.** Not `"seed": 42` alone —
      `random.seed(42)` fixed only a file-level split while weight init, dropout
      and dataloader shuffling drew unseeded, and the same script on the same
      data returned F1 0.7839 then 0.7675. Record the list of libraries actually
      seeded, so an unseeded library cannot look like a seeded one.
- [ ] **Library versions** for anything whose behaviour affects the number
      (numpy, torch, scipy, the model library), plus the Python version.
- [ ] **Git SHA** of the code, and the script filename. A number produced by
      uncommitted code should say so.
- [ ] **The exact split member lists** — train, dev, test — or a hash of each.
      A *description* is not enough: two runs both described as "seed 42, 50/50"
      produced 30-file test splits that differed in 16 files.
- [ ] **The ground-truth unit count per split**, derived (e.g. `tp + fn`), as an
      independent fingerprint. It catches "same files, different label
      extraction" — which is how a 499-vs-376 mismatch surfaced before anyone
      noticed the file lists differed.
- [ ] **Every data-reduction decision**: duration caps, file caps, channel
      selection, decimation, resampling, frequency bounds, minimum-duration
      filters. One script read the first 180 s of each recording and scored
      against full-file annotations, making 39.8% of ground truth unreachable by
      construction — an engineering shortcut reported as an algorithmic result.
- [ ] **The preprocessing configuration used at evaluation**, stated explicitly
      and asserted identical to the training one. A sample rate that differed
      between training and inference changed true positives by 2.1×.
- [ ] **The matching / scoring criterion** as structured fields, with operators
      — `iou_threshold`, `overlap_threshold`, `iou_op`, tie-breaking order — not
      as a sentence. And know that the sentence can be wrong: one file's prose
      said `IoU > 0.3` where the code evaluated `>=`.
- [ ] **How the operating point was chosen.** Which split the threshold was
      selected on, the full sweep that was searched, and whether the best value
      landed on the boundary of that range. If it was chosen on the reported
      set, name the key `test_set_oracle_best` so nobody quotes it as held-out.
- [ ] **Known leakage between splits**, if any, stated as a count. "22 of 30 test
      files were in the other arm's training set" belongs beside the number, not
      in someone's memory.
- [ ] **The tuned parameters actually passed to each arm** — not the names of
      the functions, the values. Re-runs at library signature defaults are
      indistinguishable from tuned runs otherwise.
- [ ] **Every diagnostic that was computed**, including the unflattering ones. A
      pipeline that computes an agreement figure of 35.8% and reports a 91%
      purity number elsewhere has chosen; the choice is the problem.

## Two rules about the file itself

- [ ] **Never write a rerun into an existing output path.** Stamp it
      (`run_stamped()`). A rerun once overwrote the artifact a published figure
      was read from, and the published number then existed nowhere on disk. An
      artifact behind a published figure must be immutable.
- [ ] **Keep one results index** — one row per run: script, git SHA, date,
      dataset, `n_train`/`n_test`, ground-truth unit count, matching criterion,
      threshold-selection procedure, headline metrics. **A run not in the index
      does not exist.** Without one, two files claimed F1 = 0.784 and F1 = 0.361
      for the same model on the same data with nothing reconciling them.

## Assertions that cost nothing

Put these in the producing script and let them kill the run. Each one would have
caught a real bug:

- [ ] per-file counts sum to the reported aggregate
- [ ] `tp + fn` is constant across every point of a threshold sweep
- [ ] combined counts equal the sum of the per-class counts
- [ ] the evaluation preprocessing config equals the training config
- [ ] the number of distinct label values is far below the number of examples
      (a label column with one value per example is a continuous measurement)
- [ ] every configuration constant declared in the file is read somewhere in it

---

## In code

`matilde_plugin/engine/comparison.py` supplies the parts that can be collected
automatically:

```python
from matilde_plugin.engine.comparison import (
    set_all_seeds, provenance_block, run_stamped)

seed_info = set_all_seeds(42)          # returns what it actually seeded, + versions
...
out = run_stamped("results.json")      # results.20260728T113000Z.json
json.dump({
    "provenance": provenance_block(
        seed_info,                     # seed, seeded libs, library versions
        # ... and the parts only this script knows:
        test_items=test_files,
        n_gt_units=len(gt),
        matching_criterion=criterion.as_dict(),
        threshold_selected_on="dev",
        threshold_sweep=sweep,
        duration_cap_s=None,
        resample_hz=None,
        train_files=train_files,
    ),
    "aggregate": metrics,
}, open(out, "w"), indent=2)
```

`provenance_block()` fills in the timestamp, script name, git SHA and Python
version, and merges the seed info. Everything else you must pass, because only
the calling script knows it — and the checklist above is the list of what to
pass.
