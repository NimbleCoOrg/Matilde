# The baseline registry

A comparator is not a number. It is **four things welded together**, and dropping
any one of them produced a real invalid comparison:

| field | why it is in the record |
|---|---|
| `fn` | the *validated* callable, imported — never re-written inside a comparison script, and never chosen by matching a function name |
| `params` | the parameters it was **tuned to on the train split**, read out of the results file — not the function's signature defaults |
| `split` | the split it was scored on, identified by a **sha256 of the sorted member list**, plus the ground-truth count implied by `tp + fn` as a second fingerprint |
| `criterion` | how a prediction counts as a match, as a comparable structured record — not prose |

`compare(a, b)` refuses to produce a delta unless the **split** and the
**criterion** match. That refusal is the whole product. Everything else is
bookkeeping in service of it.

The case study those four fields come from is
[trustworthy-comparison.md](trustworthy-comparison.md). Read it first; every
docstring in the module refers to it.

**Implementation:** `matilde_plugin/engine/comparison.py` (stdlib only,
domain-neutral). **Tests:** `tests/test_comparison.py`.

---

## Why not just return a number?

The obvious design is a lookup table: `get_baseline("classical") -> 0.731`. It
would have prevented the first of the four failures and **none** of the other
three.

| failure | prevented by a number-returning registry? |
|---|---|
| 1. Hardcoded comparator printed by eight scripts | **Yes** — there would be one place to read it from |
| 2. Baseline reimplemented by hand inside the comparison script | No — the number is right, the arm is a different program |
| 3. Wrong one of two detector variants, chosen by name | No — the number is right, it describes a function that was not run |
| 4. Run at library signature defaults instead of the tuned parameters | No — the number is right, the re-scored arm is handicapped |

Worse, a number-returning registry makes 2–4 *harder* to catch, because the
comparator now looks authoritative. Every field beyond the number exists because
its absence let a specific comparison go out wrong.

---

## Register an arm

Preferred — params, metrics, split and criterion all read from one file, one
method key, in one call:

```python
from matilde_plugin.engine.comparison import register_baseline_from_results
import my_validated_module

register_baseline_from_results(
    "classical", "validation_results.json",
    criterion_keys=("iou_threshold", "overlap_threshold"),
    method="simple",
    fn=my_validated_module.detect,
    iou_op=">=",          # asserted by the caller — see "operators" below
)
```

Nothing above is typed in. The parameters, the per-split metrics, the split's
member list and the matching thresholds are all read from the results file at
registration time. Because the parameters and the metrics come from the *same*
declared method key, they provably describe the same run — which is what closes
failures 3 and 4 together.

### What the producing script must save

This is the contract. A results file must carry:

| key (default name) | what it holds |
|---|---|
| `results_by_method.<method>.parameters` | the tuned parameters |
| `results_by_method.<method>.aggregate.<role>` | metrics per split role (`test`, `train`, …) |
| `test_items` | the split's **member list** — filenames or ids, not a description |
| `matching_criteria` | the fields named in `criterion_keys` |
| `best_method` | *(optional)* which method key produced the headline number |

Every key name is a parameter (`methods_key`, `params_key`, `metrics_key`,
`split_key`, `criterion_block_key`, `role`, `n_units_keys`), so the shape maps
onto whatever your producing scripts already write. What is **not** optional is
that the values exist. If a results file saves none of them, the arm cannot be
registered without asserting fields from memory — which is the failure the
registry exists to stop, and the fix belongs in the producing script, not here.

`n_units_keys` defaults to `("tp", "fn")`: the ground-truth count is *derived*
from the recorded counts, never typed. For a task where the total is not `tp+fn`,
name the keys that sum to it.

### Manual registration, when the numbers come from elsewhere

```python
from matilde_plugin.engine.comparison import Criterion, register_baseline

register_baseline(
    name="learned_v4",
    fn=None,                                  # scored from a saved model
    params={},
    split=test_member_list,                   # hashed for you
    criterion=Criterion(iou_threshold=0.1, overlap_threshold=None, iou_op=">",
                        note="train.py:206 `if best_iou > 0.1`"),
    source="learned_v4_results.json",
    metrics={"test": {"f1": 0.775, "tp": 447, "fp": 130, "fn": 52}},
    provenance={"criterion_from": "asserted: train.py:206"},
)
```

Any provenance value starting with `asserted` — or containing `reconstructed` —
is surfaced in `compare()`'s `provenance_warnings`. A registry can *require* a
field it cannot *verify*; the honest response is to make the assertion visible
rather than let it pass as file-derived.

---

## Compare two arms

```python
res = compare("classical", "learned", metric="f1", split="test")
# {'split_id': '97fd273d86db', 'n_gt_units': 499,
#  'a': {'name': 'classical', 'value': 0.801, 'counts': {...}, 'params': {...}, ...},
#  'b': {'name': 'learned',   'value': 0.775, 'counts': {...}, ...},
#  'delta': 0.026, 'provenance_warnings': []}
```

Same split, same criterion → the delta is legitimate and both numbers came out
of files. **Check `provenance_warnings`:** a non-empty list means one arm carries
a field a human asserted rather than read from a results file.

When the arms do not match, `compare()` raises `IncomparableError` — a
`SystemExit` subclass, so an unguarded script dies at the point of the invalid
comparison instead of printing a number that gets quoted later. The message names
what differs and what to do:

```
REFUSING TO COMPARE 'classical' against 'learned': the arms are not comparable.

  SPLIT MISMATCH
    classical: split_id 97fd273d86db  (30 members, 376 GT units; test of validation_results.json)
    learned:   split_id 2c6db9a353dd  (30 members, 499 GT units; test of learned_v4_results.json)
    -> 16 member(s) only in classical, 16 only in learned
       only in classical: rec_0041
       ... and 13 more only in classical

  CRITERION MISMATCH
    classical: iou_op='>=', iou_threshold=0.3, overlap_threshold=0.5
    learned:   iou_op='>',  iou_threshold=0.1, overlap_threshold=None
    -> differs in: iou_op ('>=' vs '>'), iou_threshold (0.3 vs 0.1),
       overlap_threshold (0.5 vs None)

  WHAT TO DO — a delta between these is not a smaller or larger number, it has
  no meaning:
    1. Choose ONE split. Use its member list, not a description of how it was
       generated.
    2. Choose ONE Criterion(...) and score both arms under it.
    3. Re-score BOTH arms — the registered ones via get_baseline(<name>).run(...),
       which is bound to their tuned parameters.
    4. Register the re-scored arms and compare() those.
  Do not report the delta you were about to report.
```

Note that **both splits had 30 members**. The count matched; the contents did
not. That is why `split_id` is a hash over the sorted member list and not a
label — "the seed-42 50/50 split" was a true description of both.

There is a second, independent check. Same member list but a different
ground-truth count (376 vs 499) is refused separately, as
`GROUND-TRUTH COUNT MISMATCH`: identical split, different label extraction.

---

## Re-run an arm

```python
pred = get_baseline("classical").run(audio)   # bound to threshold=0.5, min_dur=10, gap=50
```

`run()` takes no parameter overrides. **An arm with different parameters is a
different arm**: tune it on the train split and `register_baseline()` it under a
new name. Overriding here reproduces failure 4, and binding one method's tuned
parameters to another method's callable raises rather than silently dropping the
ones the signature does not accept.

The strongest verification available is to re-run the registered callable with
the registered parameters under the registered criterion on the registered split
and confirm it **reproduces the recorded counts exactly**. If it does, the record
is provably the run that produced the number. Make that a test you can run on
demand; it is the only check that closes the gap between "recorded" and "true".

---

## On operators, and prose that is wrong

`Criterion.from_results()` takes the **numbers** from the results file and the
**operators** from the caller. That split is deliberate, and it comes from a real
discrepancy: the results file declared its criterion as `IoU > 0.3 OR
overlap > 50%` while the code that actually did the matching evaluated
`iou >= threshold or overlap >= threshold`. Parsing the operator out of the
sentence would have encoded the file's own error.

So the caller passes the operators, having read the scoring code, and the sentence
is kept in `Criterion.note`, which is **excluded from equality**. Free text can
be displayed but can never make two different criteria look equal — nor make two
identical ones look different because someone reworded the description.

```python
Criterion(iou_threshold=0.3, note="one sentence") == Criterion(iou_threshold=0.3, note="another")
# True — note is not compared

Criterion(iou_threshold=0.3, overlap_threshold=None) == Criterion(iou_threshold=0.3)
# False — "declared no fallback" and "never said" are different states
```

---

## What the registry still does NOT catch

The most useful section in this document. Be aware of all of it before trusting
a green `compare()`.

1. **A criterion its source file never recorded.** If the producing script wrote
   no criterion block, the criterion has to be read out of the scoring code by a
   human and asserted. The registry can *require* the field and *refuse a
   mismatch*; it cannot verify a value that exists nowhere on disk. It surfaces
   the assertion in `provenance_warnings` rather than hiding it — but the real
   fix is in the producing script.
2. **A wrong-but-well-formed criterion.** If you register `iou_threshold=0.3`
   for an arm that was actually scored at `IoU > 0.1`, both arms agree and
   `compare()` proceeds happily. **Structure makes disagreement visible, not
   truthful.**
3. **Tuned parameters bound to a callable that happens to accept them.** The
   params/callable check is one-directional: it catches a parameter name the
   function does not accept, but if the wrong variant accepts every name, the
   mismatch passes. Only `register_baseline_from_results()` — which reads the
   callable's method key, its parameters and its metrics in one step — closes
   that direction.
4. **Matching semantics beyond the declared fields.** A field like `match_order`
   is a *declared string*, not a verified behaviour. Two arms can share the
   label and still match differently — one taking the first best-scoring match
   per prediction in prediction order, the other doing greedy matching over
   candidates sorted by length. The criterion record says what someone claimed,
   not what the matcher did.
5. **The metrics themselves.** `compare()` reads recorded numbers. Only an
   explicit re-run path regenerates them, and only for arms that carry a
   callable — an arm scored from a saved model carries none.
6. **A badly chosen split.** `compare(..., split="all")` or `split="train"` is
   legal. The registry refuses a *missing* split, not a leaky one. Nothing here
   detects that 22 of 30 test files were in the other arm's training set; that
   is a fact about how the splits were built, and it belongs in the results
   file's provenance block and in the caveats you print beside the number.

Items 2, 4 and 6 are the ones to hold onto. They are the reason the registry is a
floor and not a guarantee: it removes the failures that come from *losing
information*, and it cannot remove the failures that come from information being
wrong at the source.
