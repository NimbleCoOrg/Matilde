# Trustworthy experimental comparison

A measured number is a claim. A **difference** between two measured numbers is
two claims plus an assumption — that the two numbers were produced under the
same conditions — and that assumption is the one nobody checks.

This document is the generalized method from an audit of one comparison. The
comparison was *"the new model beats the classical baseline."* It was invalid
**four different ways across three attempts by two different agents**, and each
fix moved the bug rather than removing it. Every arithmetic step was correct
every time. No number was fabricated. The subtraction was meaningless.

The failure modes are not specific to that task. They are: comparator
provenance, protocol matching, split identity, tuning leakage, dead constants,
unseeded runs, and silent failure. The rules at the bottom are checkable in ten
minutes on any comparison you are about to publish.

Related: [results-provenance-checklist.md](results-provenance-checklist.md) for
what a results file must carry, [baseline-registry.md](baseline-registry.md) for
the code that enforces the refusals.

---

## The case: one comparison, invalid four ways

The setting is generic — an event-detection task, scored as precision / recall /
F1 against annotated ground truth, with a **classical signal-processing
detector** as the baseline arm and a **small CNN** as the new arm. A detection
counts as a true positive if it matches a ground-truth event under some
overlap criterion.

### Attempt 1 — the comparator was a hardcoded string

The comparison script printed:

```python
print(f"Baseline: P=0.860, R=0.636, F1=0.731")
```

An **f-string with no interpolation**. It printed identically on every run
regardless of data, parameters, or split. Eight scripts carried a hardcoded
comparator: seven printed that same non-interpolated string, and an eighth fed a
different constant into a subtraction to produce a delta. It went to
stdout, stdout went into a job-completion notification, and the notification was
read back as a measurement — producing the sentence "the CNN still beats the
baseline" in a single turn with zero files opened.

The two numbers were not comparable. The new arm was scored at `IoU > 0.1` over
a 499-event split; that baseline figure came from `IoU >= 0.3 OR overlap > 50%`
over a **376-event** split. Both splits had exactly 30 files. Both scripts said
`random.seed(42)` and "50/50 split". One sorted the filenames before shuffling
and the other did not, so 16 of the 30 files differed.

> **Nothing in the pipeline was positioned to notice.** The criterion travelled
> as a sentence — `"IoU > 0.1"` and `"IoU >= 0.3 OR overlap > 50%"` are both
> non-empty strings, both truthy, and no code path compared them. The split
> travelled as a *description*, which two different splits both satisfied.

### Attempt 2 — the fix reimplemented the baseline

Told the comparator was hardcoded, the next attempt removed the constant and
re-wrote the baseline detector **inside the comparison script** instead of
importing the validated one.

Recall fell **0.636 → 0.098** (F1 0.731 → 0.170) and the new arm "won" by
+0.285. The number was now genuinely measured, and more wrong than before. A
hardcoded constant is at least a *record* of something that once ran; a fresh
reimplementation is a new, unvalidated program wearing the old one's name.

### Attempt 3 — the next fix imported the wrong function

Told to import rather than reimplement, the next attempt imported the baseline
module — and picked the wrong one of its two detector variants. It chose by
**name**: the variant whose name looked canonical. The results file recorded
`best_method: "simple"`, and the 0.731 figure came from that one. The variant
that got imported scores **0.442** on the same split.

The provenance of 0.731 was quoted while a different function was run.

### Attempt 4 — and ran it at library defaults

The original run had grid-searched the baseline on the **train** split and saved
the winners: `threshold=0.5, min_duration_ms=10, merge_gap_ms=50`. The re-runs
called the function positionally — `detector(data, sr)` — and silently got the
signature defaults: `0.7 / 30 / 20`.

The baseline arm was handicapped before a single event was matched. And because
the original runner filtered unrecognised keyword arguments out with a
`co_varnames` comprehension, passing one variant's tuned parameters to the
other's function *dropped two of them without a word*.

### What the valid comparison said — and then said again, differently

The fifth attempt fixed the protocol: one split, one criterion, both arms scored
by the same function. On that split the classical detector led:

| Matching criterion | Classical detector | CNN | Δ |
|---|---|---|---|
| Validated matcher (IoU ≥ 0.3 **or** overlap ≥ 0.5) | **0.801** @ threshold 0.4 | 0.775 @ threshold 0.2 | −0.026 |
| IoU ≥ 0.3 only | **0.762** | 0.747 | −0.014 |

Two caveats were attached to it, both correct:

- **Both arms were oracle-best** — each operating point was chosen by maximising
  F1 *on the test set*. Upper bounds, not held-out estimates. (See rule 4.)
- **22 of the 30 test files had leaked** into the CNN's training or validation
  set. Only 8 were genuinely held out for that arm.

A sixth run then fixed both caveats: the model's true held-out 15 files, zero
leakage, each arm's threshold chosen on a *validation* split. **The direction
reversed, on both criteria:**

| Matching criterion | Classical detector | CNN | Δ |
|---|---|---|---|
| Validated matcher | 0.721 @ 0.7 | **0.859** @ 0.2 | **+0.138** |
| IoU ≥ 0.3 only | 0.630 | **0.847** | **+0.218** |

The clean margin is five times the size of the leaky one, pointing the other way.

**And the caveat contained a false inference — which is the most instructive part
of this whole episode.** The leakage note originally read: the CNN's number is
inflated by leakage, *therefore* removing the leakage makes the classical
detector's win stronger evidence. That is a plausible confound story, and it was
wrong. Empirically the CNN scored **higher** on the clean split (0.859) than on
the leaked one (0.775). Leakage was not inflating it. The reasoning was never
checked against a measurement — it was asserted because it sounded like the
conservative direction, which is exactly what **rule 14** warns against, written
three pages further down the same document.

So the honest finding is not "the classical detector wins." It is:

> On this dataset the comparison was invalid four different ways, a fifth
> attempt fixed the protocol and favoured the classical detector, and a sixth
> attempt fixed the remaining confounds and reversed the result. The margin is
> not stable across splits, and the test sets are small (246–499 annotated units).

If you need a number to quote, quote the sixth: CNN ≈ 0.86 against ≈ 0.72, with
the threshold chosen on validation and no leakage, on 15 files / 246 units — and
say that a differently-drawn split gave the opposite sign.

That is a weaker claim than either single run appears to support, and it is the
only one the evidence carries. **A result that flips when you fix the protocol was
never a result; it was an artefact with a plausible story attached.**

---

## Why every fix moved the bug

Each attempt fixed the *symptom named in the last correction* and left the
structure that produced it intact.

| Attempt | What was fixed | What was still true |
|---|---|---|
| 1 | — | comparator was a string; split and criterion were prose |
| 2 | the constant was removed | the callable was not the validated one |
| 3 | the callable was imported | it was the wrong callable, chosen by name |
| 4 | the right callable | run at parameters nobody tuned |

The structural cause is one sentence: **the comparator was never represented as
data.** It existed as a number, then as a function name, then as a call — never
as a record carrying its own provenance. So each fix had to re-derive, from
memory or from a name, whatever the previous fix had lost.

The fix that finally held was to make the comparator a **record of four welded
fields** — the callable, the parameters it was tuned to, the split it was scored
on, and the criterion it was scored under — and to make the subtraction *refuse*
unless the split and the criterion match. See
[baseline-registry.md](baseline-registry.md).

A registry that returned only a **number** would have prevented attempt 1 and
none of the others. That is the whole argument for four fields instead of one.

---

## Five more failure modes from the same audit

Each is generic, and each has a mechanism you can check for.

### A declared-but-unused constant is a live hazard

A training script declared `SR = 22050` at the top and **never referenced it
again** — the name appears exactly once in the file. The model was trained on
audio at its native 48 kHz; the sample rate came from the file reader, not the
constant.

Later scripts read that constant as a statement of intent and helpfully
resampled their input to 22050 before inference. The model got input it had
never seen. Same model, same 499-event split, same matching function, same
score threshold of 0.10:

| Input | True positives | F1 |
|---|---|---|
| resampled to 22050 Hz | 215 | 0.539 |
| native 48 kHz | **449** | **0.766** |

A **2.1× difference in true positives**, from a constant that did nothing —
holding the split, the matcher and the score threshold fixed, so the resampling
is the only difference. Across the scripts that reported on this model, its F1
appeared as low as 0.457 and as high as 0.766 — a spread that mixes two
variables (resampled vs native input, and different confidence thresholds), which
is itself the point: nothing recorded which was which. Every one of those numbers was
correctly computed.

> **Rule:** a constant that is declared and never read is not documentation, it
> is a false claim about the pipeline. Grep every configuration constant for a
> second occurrence. If there is none, delete it or use it — never leave it for
> the next reader to believe.

### A label column that was not a label

Four scripts used column 3 of the annotation rows as the class label. Column 3
was **a frequency in Hz** — the row is `[id, start_ms, end_ms, f_mid, f_min,
f_max, …]`. The result was **829 distinct "classes" over 829 examples**: an
830-way softmax with one example each, and a `labels` field in every results
file that was a list of frequencies presented as a taxonomy.

The real taxonomy was **one file away**, in an adjacent annotation file in the
same directory: 13 families and 74 labels, with a usable class distribution.

> **Rule:** before using a column as a label, print its distinct-value count and
> its five most common values. **If the number of unique values is close to the
> number of examples, it is a continuous measurement, not a class.** Make that a
> hard guard, not a habit. And when a dataset ships a companion file next to the
> one you parsed, open it.

### An optimum on the boundary of a sweep

The reported best operating point was `threshold = 0.3`. It was also the
**lowest value swept**. An optimum sitting on the edge of the search range means
the search never located the optimum — the number is the boundary of where you
stopped looking.

It was then read as *"the model is confident about a lot of boundaries."* That
inverts the meaning. Needing a *low* threshold to maximise F1 means the metric
only peaks once low-confidence predictions are accepted, which is evidence of
weak calibration, not strength.

> **Rule:** if the best value is at either end of the swept range, extend the
> range and re-run before quoting the number. `assert_sweep_interior()` refuses
> to report it.

### Unseeded stochasticity

`random.seed(42)` was called — and fixed only the file-level split. Weight
initialisation, dropout, and dataloader shuffling all drew from RNGs nothing
seeded. The same script on the same data produced **F1 0.7839, then 0.7675**.
The rerun also wrote to the same fixed output filename, so it *overwrote the
artifact the published figure had been read from*. The published number then
existed nowhere on disk.

The discrepancy was explained as "a different random train/test split." That was
checkable and false: the seed call precedes the split, so the split is
deterministic by construction, and `tp + fn = 499` in **both** runs — the
identical test set. The explanation reached for the one component that *was*
controlled and missed the one that wasn't.

> **Rule:** seed every RNG you import, record which ones you seeded and their
> versions, and never write a rerun into an existing output filename. And when
> you explain a discrepancy, **name the check that would falsify your
> explanation, and run it.** If you cannot think of one, you are telling a
> story, not diagnosing.

### Anything that cannot fail loudly will eventually be believed

A helper script printed the HTTP response body of each request and exited `0`
regardless of status. A `405 Method Not Allowed` was therefore indistinguishable
from a success: same shape of output, same exit code. A confident, detailed, and
entirely wrong root-cause analysis was written from that symptom.

This generalizes past HTTP. A pipeline stage that catches broadly and continues,
a matcher that returns an empty list when its input is malformed, a plot that
renders a legend for a series it does not contain, a validation whose refutation
branch is mathematically unreachable — all of these produce output that *looks
like* a result. Given time, the output is quoted.

> **Rule:** every stage must have a reachable failure. Check the status, not the
> body. Exit non-zero. Assert the free invariants and let them kill the run:
> per-file counts sum to the aggregate; `tp + fn` is constant across a threshold
> sweep; combined counts equal the sum of the per-class counts; the evaluation
> preprocessing configuration is identical to the training one. One assertion
> would have caught each bug above.

---

## The rules

1. **A number that arrives as text is a claim, not evidence.** Before quoting
   any comparator, open the file that produced it. Notification bodies, chat
   logs, stdout, and your own recall are leads.
2. **Never hardcode a comparator.** Load it from the results file that produced
   it. An f-string with no interpolation is the tell.
3. **Import the validated implementation; never reimplement it in the
   comparison script.** A reimplementation is a new program with an old name.
4. **Ask the file which method produced the number** — the results file's own
   `best_method`, not whichever function name looks canonical.
5. **Run a comparator at the parameters it was tuned to.** Positional calls
   silently pick up signature defaults. An arm run at different parameters is a
   different arm and needs its own tuning on the train split.
6. **Identify a split by its contents, never by a label.** Hash the sorted
   member list. "The seed-42 50/50 split" described two different splits with
   the same size. Carry a second, independent fingerprint too — the ground-truth
   count implied by `tp + fn`, which catches "same files, different label
   extraction."
7. **Never select a hyperparameter on the set you report.** `max(sweep, key=f1)`
   over a test-set sweep is an upper bound, not a result. Use three-way
   train/dev/test. If a rerun is not worth it, **rename the key
   `test_set_oracle_best`** so nobody quotes it as held-out.
8. **Represent the matching criterion as comparable data, not prose.** Two
   criterion sentences never compare unequal. Two structured records do. Keep
   the prose in a field that is excluded from equality — and note that the prose
   may itself be wrong: in this case the results file said `IoU > 0.3` where the
   scoring code evaluated `>=`. Take the numbers from the file and the operators
   from whoever read the code.
9. **Write the comparability check out loud in the report,** and refuse to print
   the comparison if any row fails:

   | | Arm A | Arm B |
   |---|---|---|
   | Split (content hash) | | |
   | Ground-truth unit count | | |
   | Evaluation function | | |
   | Matching criterion | | |
   | How the operating point was chosen | | |
   | Preprocessing (sample rate, caps, decimation) | | |

10. **Truncate ground truth whenever you truncate input.** One script read the
    first 180 s of each recording and scored against full-file annotations: 39.8%
    of ground-truth events were unreachable by construction, capping recall at
    0.602 while 0.312 was reported as an algorithmic result. Record every
    data-reduction decision in the results file.
11. **Publish the diagnostic you computed.** A pipeline that computes a
    seed-label agreement of 35.8%, hedges it correctly in its own print
    statement, and then reports a 91% purity figure elsewhere has chosen the
    flattering number. That choice is the misconduct, not the low number.
12. **Report negative results.** A results file recording zero predictions and
    F1 = 0.0, and a docstring explaining why an approach was abandoned, are among
    the most valuable artifacts a project produces. "Our comparison reversed when
    we fixed the split" is also a finding — and a more useful one than either
    direction it pointed in.
13. **Validate the comparison before you calibrate the number.** This is the
    subtle one. A report can be *excellent in form* — recommending a range over
    a point estimate, flagging that 1.7 points at n=30 is within normal
    variation, offering careful language for a meeting — while the comparison
    underneath is invalid. The hedging was about one arm's variance; the claim
    that needed hedging was "beats the baseline," which no amount of
    range-quoting repairs.

    **Calibration language raises the credibility of whatever it is attached
    to.** That makes attaching it to an unchecked claim *more* dangerous than
    stating the claim bluntly, because a reader takes the hedging as evidence
    that the comparison was scrutinised. Ask: *if I am wrong here, is it because
    my number is noisy, or because my comparison is meaningless?* Only the first
    is fixed by a range.
14. **Audit the work you are currently doing, not just work you are asked to
    review.** The one-turn rule: before reporting a favourable result, spend one
    turn actively trying to break it. What confound would produce this number?
    What would I see if the labels were misaligned? Does the sample count
    reconcile with the previous run? Rigor applied to your own in-flight work is
    the only rigor that prevents anything.

---

## Where this is implemented

- `matilde_plugin/engine/comparison.py` — the registry and the guards.
  `compare()` raises rather than returning a delta when the arms are not
  comparable; `assert_sweep_interior()` refuses a boundary optimum;
  `set_all_seeds()`, `run_stamped()` and `provenance_block()` cover rules 6, 7
  and 11 of the [provenance checklist](results-provenance-checklist.md).
- `tests/test_comparison.py` — one test per failure, each named for the mistake
  it reproduces.
- [baseline-registry.md](baseline-registry.md) — the API, and an honest list of
  what it still does not catch.
