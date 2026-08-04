# Evaluation validity — questions to answer before the number means anything

> Promoted from an instance, 2026-08. Companion to
> `docs/trustworthy-comparison.md`, which covers whether two
> numbers can be compared. This document covers whether **one** number means what
> you think it means, and what a validation pass misses when it only ever looks at
> one artifact at a time.

Answer these in writing **before** the next modelling run. They are cheap relative
to a training run and they determine whether anything downstream is interpretable.

---

## E1 — What is the ceiling? How well do humans agree with each other?

Every score you report against human labels is agreement with **one person's**
judgment, treated as truth. For many tasks that judgment is genuinely ambiguous —
where an event starts, whether a borderline case is in class, which of two
overlapping categories applies.

This decides how to read your own results:

- If two independent annotators agree at F1 ≈ 0.85, then a model at 0.858 **is at
  the ceiling**, and further tuning is fitting one annotator's habits.
- If they agree at 0.97, there is real headroom and the tuning was measuring
  something.

Those are opposite conclusions from the same model score. An instance spent weeks
tuning without being able to say which world it was in — and that single unmeasured
number decided whether the tuning had measured signal or noise.

Four ways to get at it, cheapest first:

- **A. A tolerance curve — needs nobody, do this first.** See E2.
- **B. Test–retest.** Ask your existing annotator to re-label a small sample of
  their own old items, blind to their originals. Self-consistency runs higher than
  two-person agreement, so it is a generous upper bound — but a model beating it is
  definitely overfitting.
- **C. Existing redundancy.** Query first: was anything already labelled twice?
  Free if it exists, and five minutes is cheaper than asking anyone for labour.
- **D. A second annotator.** Someone else labels a small sample blind. The real
  answer. Independence matters; seniority does not.

**A model score without a ceiling estimate is uninterpretable in the direction
that matters** — you cannot tell improvement from overfitting.

## E2 — Report the curve, not the point

Where a match depends on a tolerance — a time window, an overlap threshold, an
edit distance, a similarity cutoff — plot the metric **against** that tolerance
instead of reporting one value at one setting.

The curve is not a nicety. Its shape tells you which problem you have, and the two
have completely different fixes:

- Score rises steeply as tolerance loosens → your errors are **localisation**. The
  model finds the thing and puts the boundary in the wrong place.
- Score stays flat as tolerance loosens → your errors are **detection**. The model
  is missing the thing entirely; boundary precision is not your problem.

Two points of such a curve, from one instance: tightening the matching rule cost
the learned method 0.024 (0.858 → 0.834) and the classical baseline 0.091 (0.721 →
0.630). The baseline lost **3.8× more** — its hits were far more often
overlapping-but-poorly-localised. That is a real, reportable finding about the two
methods that a single-threshold comparison hides entirely, and it is more
informative than the headline delta the project had been arguing about.

## E3 — Leakage has a unit. Which unit did you check?

The package already requires that known leakage be stated as a count and that
splits be identified by their member lists rather than by a description
(`docs/results-provenance-checklist.md`). This is the
next question, and it is the one that gets skipped:

> **Disjoint at the file level is not disjoint at the subject level, the session
> level, or the site level.**

An instance's split was verified file-disjoint. That check passed and was honest.
But **4 of 12 test subjects also appeared in training**, and every file came from a
single site over a single week. The reported score was therefore
*within-individual, within-site, within-session* performance. That is a real
quantity — it is simply not the one a reader assumes, and it does not support "this
generalises."

The clearest case in that dataset: two recordings the annotator's **own file
naming** marked as the same individual landed on opposite sides of the split.
Nothing in the pipeline looked at the annotator's naming, so nothing objected.

Note that two pipelines in the same project split at different units — one by
subject, one by file. Their numbers were never comparable **even before** any
scoring-criterion mismatch, and nobody noticed because both reported an
"F1 on the test set."

**State the split unit beside the number, and say whether you verified
disjointness at that unit.** If your data has any grouping structure — repeated
measures, multiple items per subject, sessions, sites, authors, devices — the file
is almost never the right unit.

## E4 — Is the effect larger than your configuration noise?

An instance's own code carried a comment recording that a preprocessing mismatch
"dropped F1 from 0.826 to 0.595 on identical data." That is a swing of **0.23**.
The entire effect the project was arguing about was **0.137**.

> **If a configuration knob moves the metric more than the effect you are
> claiming, you are measuring configuration, not method.**

This inverts the usual priority. Preprocessing is not hygiene to be pinned and
forgotten; until it is pinned *and swept* under one fixed split and one scoring
criterion, the headline comparison is smaller than its own error bars from
configuration drift.

Choices in that project that were never justified and never swept:

- Two variants of the "same" pipeline read input at different sampling rates — one
  at the source rate, one resampled — giving different time–frequency resolution
  from the same raw data. A declared rate constant in one of them was never read.
- A linear-power representation where the field standard is log-magnitude. On a
  linear scale the loudest component dominates and the quiet transitions — exactly
  what was being detected — compress toward zero.
- Per-item normalisation computed over the whole item, including the scored region.

Each is a defensible choice. None had been recorded as a choice.

Note the second-order consequence: when the research question is *"how does each
method behave across conditions?"* rather than *"which method wins?"*,
preprocessing stops being a confound to control and becomes **the object of
study**. E4 is then not hygiene at all — it is the experiment.

## E5 — Was the data selection recorded?

Any filter that reduces the label space or drops examples — a minimum-count
threshold on classes, a duration floor, a confidence cutoff, a quality filter — is
a modelling decision.

An instance filtered a taxonomy from 41 families to 14 classes by a minimum-count
threshold that appeared in no provenance block anywhere.

- **Record the threshold**, in the results file, with the other provenance.
- **Report what fraction of the data the dropped categories represent.** "27 of 41
  families" and "1.2% of examples" are very different situations.
- **Show the result at one other cut.** If the conclusion only holds at your
  chosen threshold, that is the finding.

---

## Before you report any evaluation number

State, in the same message as the number:

1. **n** — items, and labelled units. `n=15` is not a footnote.
2. **The split unit** — file, subject, session, site — and whether you verified
   disjointness *at that unit* (E3).
3. **The scoring criterion**, exactly, and that every arm used it.
4. **Where the operating point was chosen** (never on the reported set) and whether
   the optimum was interior to the swept range.
5. **The artifact's content hash, not its path.** A path names a slot, not a thing.
   In one project a single model filename carried three different sets of weights
   over two weeks; the repository's copy and the reported number came from
   different ones, and nothing errored. Assert the hash before use and write it
   into the output.
6. **What you did not check.** Reliably the most useful line in the message.

And if the number moved since last time, **say which direction and by how much.**

---

## The trajectory check — do this *before* the per-artifact checks

Everything above, and every checklist in this package, validates an artifact **in
isolation**. That is necessary and it is not sufficient.

A scheduled validation pass examined a fresh training run. It verified train/test
disjointness, seeding, metric arithmetic, and that the threshold had been tuned on
the validation set rather than the test set. All passed, and it concluded "core
results are sound." That verdict was correct about the arithmetic and blind to the
only two things that mattered:

- The **headline conclusion had reversed** since the previous run — an ablation
  that had shown a technique helping (0.858 vs 0.825) now showed it hurting (0.725
  vs 0.802).
- The **baseline had collapsed**, 0.721 → 0.424, because the script re-fit it from
  scratch instead of loading the registered one.

Neither is visible when you check one file against itself. Both are obvious the
moment you diff against last week.

**So: before checking any artifact, load the previous run's record and compute the
diff.** For every metric appearing in both:

1. **Did the value move?** Report the delta, always, even when small.
2. **Did the DIRECTION of a comparison reverse?** Lead with it. Do not bury a
   reversal under the arithmetic checks that passed.
3. **Did a BASELINE move?** A baseline that changes between runs means the
   comparison changed underneath you. A baseline is meant to be loaded, not
   recomputed — if it moved, ask whether it was loaded.
4. **Is it backed by the same artifact?** Compare content hashes, not paths.
5. **If two results on disk disagree and neither is marked superseded, say so.**
   Two live results files in one project asserted opposite winners for the same
   comparison. Filenames are not a supersession mechanism; a results index is
   (`docs/results-provenance-checklist.md`).

**Verdicts you are encouraged to reach:** "this reversed and I cannot tell which
run is right"; "these two results are not comparable"; "the baseline changed so the
delta is meaningless." Each is more useful than a clean pass on an arithmetic
check.

> A number that moved is a finding. A number that reversed is a headline.

---

## Look at the figure

A result you have never viewed is a number, not an observation.

An agent reported detection results for eight days while carrying a note saying it
could not see images. The note had been true when written and was fixed the same
day; nothing re-checked it. It had working vision the entire time and never opened
a single one of the visualisations it was reporting on. See
[agent-failure-modes.md](agent-failure-modes.md#a-stated-limitation-is-a-claim-with-a-date-on-it).
