"""Make an experimental comparison refusable.

Stdlib only. Domain-neutral. This is the generalized form of a module written
after one comparison — "the new model beats the classical baseline" — turned out
to be invalid four separate ways across three attempts, each fix moving the bug
rather than removing it. `docs/trustworthy-comparison.md` is the case study;
`docs/baseline-registry.md` is the operator walkthrough.

The single idea: **a comparator is not a number.** It is a callable, the
parameters that callable was tuned to, the split it was scored on, and the
criterion used to score it. Drop any one of those four and you get one of the
four failures. So they are kept welded together here, and `compare()` refuses to
subtract two of them unless the split and the criterion agree.

A registry that returned only a *number* would have prevented the first failure
and none of the others. That is why the record has four fields, not one.

Nothing in this module knows what your task is. You supply the criterion's
fields, the callable, and the keys your results files use. What the module owns
is the refusal.
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import subprocess
import sys
from dataclasses import dataclass, field, replace as _dc_replace
from datetime import datetime, timezone
from typing import Any, Callable, Iterable, Mapping, Sequence

__all__ = [
    "IncomparableError",
    "Criterion",
    "Split",
    "Baseline",
    "split_id_of",
    "n_units_from_counts",
    "register_baseline",
    "register_baseline_from_results",
    "get_baseline",
    "list_baselines",
    "clear_registry",
    "compare",
    "assert_sweep_interior",
    "set_all_seeds",
    "run_stamped",
    "provenance_block",
]


class IncomparableError(SystemExit):
    """Two arms were asked for a delta without sharing a split and a criterion.

    Subclasses `SystemExit` on purpose. An unguarded script should die loudly at
    the point of the invalid comparison rather than print a number that will be
    quoted later — the original failure reached a job-completion notification and
    was read back as a measurement. Tests can still catch it.
    """


# ---------------------------------------------------------------------------
# Split identity
# ---------------------------------------------------------------------------

def split_id_of(items: Iterable[str], digest_len: int = 12) -> str:
    """Content-derived id for a split: short sha256 over the sorted members.

    THE INCIDENT THIS PREVENTS: two scripts both said `random.seed(42)` and
    "50/50 split", and both produced exactly 30 test files — one totalling 499
    ground-truth units, the other 376. One sorted the filenames before shuffling
    and the other did not, so 16 of the 30 differed. The *label* "the seed-42
    50/50 split" compared equal. The splits did not.

    So a split is identified by its contents, never by a name a human chose. Two
    runs that describe their split identically but shuffled differently get
    different ids here, and `compare()` then refuses them.
    """
    if isinstance(items, (str, bytes)):
        raise TypeError(
            "split_id_of() wants the collection of split members, not a single "
            "string. A split id must be derived from split CONTENTS — passing a "
            "label is the failure this function exists to prevent.")
    members = sorted(items)
    if not members:
        raise ValueError("refusing to hash an empty split")
    return hashlib.sha256("\n".join(members).encode()).hexdigest()[:digest_len]


def n_units_from_counts(block: Mapping | None,
                        keys: Sequence[str] = ("tp", "fn")) -> int | None:
    """Ground-truth unit count implied by recorded counts, e.g. tp + fn.

    Derived, never typed. This is the second, independent fingerprint on a
    split: the same member list can still yield a different ground-truth count
    if the label extraction changed. It is what first exposed the failure above
    — 499 against 376 — before anyone noticed the file lists differed.

    Pass the keys that sum to the ground-truth total for your task.
    """
    if not block:
        return None
    total = 0
    for k in keys:
        v = block.get(k)
        if v is None:
            return None
        total += int(v)
    return total


# ---------------------------------------------------------------------------
# Criterion
# ---------------------------------------------------------------------------

class Criterion:
    """A scoring criterion as comparable data, not prose.

    THE INCIDENT THIS PREVENTS: the criterion travelled as a sentence.
    `"IoU > 0.1"` and `"IoU >= 0.3 OR overlap > 50%, greedy by segment length"`
    are both non-empty strings, both truthy, and no code path compared them — so
    an arm scored one way and an arm scored the other way were subtracted and
    the difference was published.

    Fields are whatever your task needs; this module never inspects them:

        Criterion(iou_threshold=0.3, overlap_threshold=0.5, iou_op=">=",
                  match_order="greedy_by_length",
                  note="IoU > 0.3 OR overlap > 50%")

    `note` is reserved and **excluded from equality**, so free text can be
    carried for humans without ever making two different criteria look the same
    — or two identical ones look different because someone reworded the
    sentence.

    A field set to `None` is not the same as a field absent. `overlap_threshold=None`
    means "no overlap fallback", which is a genuinely different criterion from
    the same IoU threshold *with* a fallback, and compares unequal here.
    """

    __slots__ = ("_fields", "note")

    def __init__(self, note: str = "", **fields: Any) -> None:
        if not fields:
            raise ValueError(
                "Criterion() needs at least one field. An empty criterion "
                "compares equal to every other empty criterion, which is the "
                "free-text failure with extra steps.")
        object.__setattr__(self, "_fields", tuple(sorted(fields.items(),
                                                         key=lambda kv: kv[0])))
        object.__setattr__(self, "note", note)

    def __setattr__(self, name: str, value: Any) -> None:
        raise AttributeError("Criterion is immutable")

    @property
    def fields(self) -> dict:
        return dict(self._fields)

    def get(self, key: str, default: Any = None) -> Any:
        return self.fields.get(key, default)

    def __eq__(self, other: object) -> bool:
        if not isinstance(other, Criterion):
            return NotImplemented
        return self._fields == other._fields

    def __hash__(self) -> int:
        return hash(self._fields)

    def __str__(self) -> str:
        return ", ".join(f"{k}={v!r}" for k, v in self._fields)

    def __repr__(self) -> str:
        return f"Criterion({self})"

    def differences(self, other: "Criterion") -> list[tuple[str, Any, Any]]:
        """Field-by-field diff, so a refusal can say WHICH part disagrees.

        A key present in one and absent in the other is reported with the
        sentinel string `"<absent>"` rather than `None`, because "declared None"
        and "never declared" are different states and conflating them is how a
        missing overlap fallback passes for a declared one.
        """
        mine, theirs = self.fields, other.fields
        out = []
        for k in sorted(set(mine) | set(theirs)):
            a = mine.get(k, "<absent>") if k in mine else "<absent>"
            b = theirs.get(k, "<absent>") if k in theirs else "<absent>"
            if a != b:
                out.append((k, a, b))
        return out

    def as_dict(self) -> dict:
        d = self.fields
        d["note"] = self.note
        return d

    @classmethod
    def from_results(cls, source: str | Mapping, keys: Sequence[str],
                     block_key: str = "matching_criteria",
                     note_key: str = "method",
                     **asserted: Any) -> "Criterion":
        """Read named criterion values out of a results file.

        Takes the values named in `keys` from the file, and any `**asserted`
        fields from the caller, who is expected to have read the scoring code.
        That split is deliberate. In the original incident the results file said
        `method: "IoU > 0.3 OR overlap > 50%"` while the code that did the
        matching evaluated `iou >= iou_threshold or overlap >= overlap_threshold`
        — the prose was off by a boundary condition. Parsing the operator out of
        that sentence would have encoded the file's own error. So: numbers from
        the file, operators from the caller, and the sentence kept in `note`
        where it cannot affect equality.

        Refuses when a key in `keys` is absent. A results file that does not
        record how it scored cannot be an arm in a comparison — the criterion
        would have to be asserted from memory, which is the first failure.
        """
        if isinstance(source, Mapping):
            block = dict(source)
            origin = "<mapping>"
        else:
            with open(source) as fh:
                block = dict(json.load(fh).get(block_key) or {})
            origin = os.path.basename(source)

        missing = [k for k in keys if k not in block]
        if missing:
            raise IncomparableError(
                f"\nREFUSING TO BUILD A CRITERION: {origin} has no "
                f"{block_key}.{missing[0]!r}"
                + (f" (also missing: {missing[1:]})" if len(missing) > 1 else "")
                + ".\n"
                f"  A results file that does not record how it scored cannot be "
                f"an arm in a comparison — the criterion would have to be "
                f"asserted from memory, which is the failure this exists to "
                f"stop.\n"
                f"  Either add the field to the producing script, or pass an "
                f"explicit Criterion(...) and record "
                f"provenance={{'criterion_from': 'asserted:<file>:<line>'}} so "
                f"compare() can surface it.\n")

        fields = {k: block[k] for k in keys}
        fields.update(asserted)
        return cls(note=str(block.get(note_key) or ""), **fields)


# ---------------------------------------------------------------------------
# Split
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Split:
    """A split identified by its contents, with the member list kept for diffs.

    `items` is retained (not just the hash) so a refusal can say *which* members
    differ. When only an id is available the list is empty and the error says
    the diff is unavailable rather than pretending the splits are close.

    `n_units` is the second, independent fingerprint — see `n_units_from_counts`.
    """

    split_id: str
    items: tuple[str, ...] = ()
    n_units: int | None = None
    role: str = "test"
    source: str = "<unspecified>"
    provenance: str = "recorded"

    @classmethod
    def from_items(cls, items: Sequence[str], n_units: int | None = None,
                   role: str = "test", source: str = "<unspecified>",
                   provenance: str = "recorded") -> "Split":
        members = tuple(sorted(items))
        return cls(split_id=split_id_of(members), items=members,
                   n_units=n_units, role=role, source=source,
                   provenance=provenance)

    def diff(self, other: "Split") -> tuple[list[str], list[str]]:
        return (sorted(set(self.items) - set(other.items)),
                sorted(set(other.items) - set(self.items)))

    def describe(self) -> str:
        n = f"{len(self.items)} members" if self.items else "member list unavailable"
        units = "" if self.n_units is None else f", {self.n_units} GT units"
        return f"{self.split_id}  ({n}{units}; {self.role} of {self.source})"


# ---------------------------------------------------------------------------
# Baseline
# ---------------------------------------------------------------------------

@dataclass(frozen=True)
class Baseline:
    """An arm you can actually re-run: callable + tuned params + split + criterion.

    "Baseline" here means any arm of the comparison, including the new model.
    Every field is present because leaving it out caused a real invalid
    comparison:

      `fn`         — one attempt re-wrote the baseline by hand inside the
                     comparison script (recall 0.636 -> 0.098) and the next
                     imported the wrong one of two variants (0.731 vs 0.442).
                     The validated callable now travels with the record, so
                     there is nothing to re-derive from a name.
      `params`     — one attempt called the callable with library signature
                     defaults instead of the parameters it had been tuned to.
      `split`      — the published comparison put a 499-unit split against a
                     376-unit one.
      `criterion`  — and scored one arm at IoU > 0.1, the other at
                     IoU >= 0.3 OR overlap > 50%.
      `metrics`    — read from `source`, so a number can never be typed in.
      `provenance` — records, per field, whether it came from the results file
                     or was asserted by a human. `compare()` surfaces the
                     asserted ones, because a registry can require a field and
                     still not verify it.
    """

    name: str
    fn: Callable | None
    params: Mapping[str, Any]
    split: Split
    criterion: Criterion
    source: str
    metrics: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    fn_ref: str = "<none>"
    provenance: Mapping[str, str] = field(default_factory=dict)

    @property
    def split_id(self) -> str:
        return self.split.split_id

    def metric(self, key: str = "f1", split: str | None = None) -> float:
        """Fetch a recorded metric, or fail naming what the file actually has."""
        split = split or self.split.role
        block = self.metrics.get(split)
        if block is None:
            raise IncomparableError(
                f"\nREFUSING: arm {self.name!r} has no {split!r} metrics "
                f"recorded (has: {sorted(self.metrics) or 'nothing'}).\n"
                f"  Source: {self.source}\n"
                f"  Do not substitute the 'all' or 'train' figure for a held-out "
                f"one — that is how a train-split score gets quoted as test.\n")
        if key not in block:
            raise IncomparableError(
                f"\nREFUSING: arm {self.name!r} has no {key!r} in its "
                f"{split!r} metrics (has: {sorted(block)}).\n")
        return block[key]

    def counts(self, split: str | None = None) -> dict:
        """Every integer-valued entry in the recorded metrics block."""
        split = split or self.split.role
        block = self.metrics.get(split) or {}
        return {k: v for k, v in block.items() if isinstance(v, int)}

    def run(self, *args, **overrides):
        """Run this arm with ITS TUNED PARAMETERS. There is no other way in.

        THE INCIDENT THIS PREVENTS: the original run grid-searched the baseline
        on the train split and saved the winners (threshold=0.5,
        min_duration_ms=10, merge_gap_ms=50). Every re-run afterwards called the
        function positionally and silently got the signature defaults
        (0.7 / 30 / 20), so the baseline arm was handicapped before a single
        unit was matched.

        Parameters cannot be overridden here. An arm run with different
        parameters is a different arm and needs its own tuning on the train
        split and its own registration.
        """
        if overrides:
            raise IncomparableError(
                f"\nREFUSING TO RUN {self.name!r} with overrides "
                f"{sorted(overrides)}.\n"
                f"  An arm is bound to the parameters it was tuned to "
                f"({dict(self.params)}). Overriding them here reproduces the "
                f"untuned-parameter failure: the arm runs at values nobody "
                f"tuned, and the score is then compared as if it had been.\n"
                f"  If you want other parameters: tune them on the TRAIN split "
                f"and register_baseline() the result under a new name.\n")
        if self.fn is None:
            raise IncomparableError(
                f"\nREFUSING TO RUN {self.name!r}: no callable registered.\n"
                f"  This record carries recorded metrics only (source: "
                f"{self.source}), so it can be compared but not re-run.\n")
        _check_params_accepted(self.fn, self.params, self.name)
        return self.fn(*args, **dict(self.params))

    def describe(self) -> str:
        prov = ", ".join(f"{k}={v}" for k, v in sorted(self.provenance.items()))
        metrics = ", ".join(
            f"{k}: " + " ".join(f"{mk}={mv}" for mk, mv in sorted(v.items()))
            for k, v in sorted(self.metrics.items()))
        return "\n".join([
            f"arm {self.name!r}",
            f"  callable   {self.fn_ref}",
            f"  params     {dict(self.params)}",
            f"  split      {self.split.describe()}",
            f"  criterion  {self.criterion}",
            f"  source     {os.path.basename(self.source)}",
            f"  metrics    {metrics or '(none recorded)'}",
            f"  provenance {prov or '(none recorded)'}",
        ])


def _accepted_kwargs(fn: Callable) -> set[str]:
    code = getattr(fn, "__code__", None)
    if code is None:          # builtin / C callable: cannot introspect
        return set()
    return set(code.co_varnames[:code.co_argcount + code.co_kwonlyargcount])


def _check_params_accepted(fn: Callable, params: Mapping[str, Any],
                           name: str) -> None:
    """Refuse to bind tuned parameters a callable will not accept.

    THE INCIDENT THIS PARTLY PREVENTS: one variant's tuned winners included two
    parameters the other variant's function does not accept. The original runner
    filtered unknown keys out with a `co_varnames` comprehension, so handing one
    method's parameters to the other's callable silently dropped two of them and
    scored something nobody had tuned. Dropping a tuned parameter is now an
    error, not a filter.

    Honest limitation: this is one-directional. If the wrong callable happens to
    accept every parameter name, the mismatch passes. Only
    `register_baseline_from_results()` — which reads the callable and the params
    from one declared method key at once — closes that.
    """
    accepted = _accepted_kwargs(fn)
    if not accepted:
        return
    unknown = sorted(set(params) - accepted)
    if unknown:
        raise IncomparableError(
            f"\nREFUSING TO REGISTER/RUN {name!r}: tuned parameter(s) "
            f"{unknown} are not accepted by "
            f"{getattr(fn, '__module__', '?')}.{getattr(fn, '__name__', fn)}"
            f"{tuple(sorted(accepted))}.\n"
            f"  The tuned parameters and the callable do not belong to the same "
            f"method. Silently dropping them means the arm runs parameters "
            f"nobody tuned, under a function nobody validated.\n"
            f"  Check which method key produced those parameters in the results "
            f"file and register that method's callable.\n")


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_REGISTRY: dict[str, Baseline] = {}


def clear_registry() -> None:
    """Empty the registry. For tests and for scripts that build their own set."""
    _REGISTRY.clear()


def register_baseline(name: str, fn: Callable | None, params: Mapping[str, Any],
                      split: "Split | Sequence[str] | str",
                      criterion: Criterion, source: str,
                      metrics: Mapping | None = None,
                      n_units: int | None = None,
                      fn_ref: str | None = None,
                      provenance: Mapping[str, str] | None = None,
                      replace: bool = False) -> Baseline:
    """Register an arm as callable + params + split + criterion + source.

    THE INCIDENT THIS PREVENTS: the comparator used to be the string
    `"Baseline: P=0.860, R=0.636, F1=0.731"`, printed by eight scripts. It
    reached a job-completion notification, was read back as a measurement, and
    became "the new model still beats the baseline" — comparing IoU > 0.1 on a
    499-unit split against IoU >= 0.3 OR overlap > 50% on a 376-unit split.
    There is no way to register a bare number here.

    `split` accepts a `Split`, a sequence of member ids (hashed into one), or a
    bare id string. A bare id disables the "which members differ" diff, so pass
    the member list when you have it.
    """
    if isinstance(split, Split):
        sp = split
        if sp.n_units is None and n_units is not None:
            sp = _dc_replace(sp, n_units=n_units)
    elif isinstance(split, str):
        sp = Split(split_id=split, n_units=n_units, source=source,
                   provenance="id-only (no member list; diffs unavailable)")
    else:
        sp = Split.from_items(split, n_units=n_units, source=source)

    if not isinstance(criterion, Criterion):
        raise IncomparableError(
            f"\nREFUSING TO REGISTER {name!r}: criterion must be a Criterion, "
            f"got {type(criterion).__name__} {criterion!r}.\n"
            f"  A free-text criterion is the original failure — 'IoU > 0.1' and "
            f"'IoU >= 0.3 OR overlap > 50%' are both truthy strings and nothing "
            f"compares them. Use Criterion(...) or Criterion.from_results(...).\n")

    params = dict(params or {})
    if fn is not None:
        _check_params_accepted(fn, params, name)
        fn_ref = fn_ref or (f"{getattr(fn, '__module__', '?')}."
                            f"{getattr(fn, '__name__', repr(fn))}")
    if name in _REGISTRY and not replace:
        raise IncomparableError(
            f"\nREFUSING TO REGISTER {name!r}: already registered from "
            f"{_REGISTRY[name].source}.\n"
            f"  Two different records under one name is how 'the baseline' came "
            f"to mean three different things. Pick a distinct name, or pass "
            f"replace=True if you really mean to shadow it.\n")

    bl = Baseline(name=name, fn=fn, params=params, split=sp,
                  criterion=criterion, source=source,
                  metrics=dict(metrics or {}), fn_ref=fn_ref or "<none>",
                  provenance=dict(provenance or {}))
    _REGISTRY[name] = bl
    return bl


def register_baseline_from_results(
        name: str, results_path: str, criterion_keys: Sequence[str],
        method: str | None = None, fn: Callable | None = None,
        methods_key: str = "results_by_method",
        params_key: str = "parameters",
        metrics_key: str = "aggregate",
        split_key: str = "test_items",
        criterion_block_key: str = "matching_criteria",
        role: str = "test",
        n_units_keys: Sequence[str] = ("tp", "fn"),
        criterion: Criterion | None = None,
        replace: bool = False,
        **asserted_criterion_fields: Any) -> Baseline:
    """Register an arm whose params, split, criterion and metrics all come from one file.

    THE INCIDENT THIS PREVENTS (two failures at once): the headline number was
    produced by one function with one tuned parameter set. A later script picked
    its callable by *name* — landing on a sibling variant that scores 0.442
    where the real one scores 0.731 — and its parameters from the function
    signature defaults. Here the parameters and the metrics are read out of the
    same declared method key in one step, so they provably describe the same
    run, and the callable is checked against those parameters.

    Everything except the callable and the asserted criterion fields is
    file-derived, and `provenance` records which is which.

    WHAT THE PRODUCING SCRIPT MUST SAVE for this to work:
      - `{methods_key}.{method}.{params_key}` — the tuned parameters
      - `{methods_key}.{method}.{metrics_key}.{role}` — the metrics for the split
      - `{split_key}` — the split's member list, not a description of it
      - `{criterion_block_key}` — the fields named in `criterion_keys`
    If it saves none of those, the arm cannot be registered without asserting
    fields, which is the failure the registry exists to stop.
    """
    with open(results_path) as fh:
        data = json.load(fh)
    base = os.path.basename(results_path)

    node = data
    if method is not None:
        by_method = data.get(methods_key) or {}
        if method not in by_method:
            raise IncomparableError(
                f"\nREFUSING: {base} has no {methods_key}[{method!r}] "
                f"(has: {sorted(by_method)}).\n")
        node = by_method[method]

    params = node.get(params_key)
    if params is None:
        raise IncomparableError(
            f"\nREFUSING TO REGISTER {name!r}: no {params_key!r} block for "
            f"{method!r} in {base}.\n"
            f"  An arm without its tuned parameters runs at library signature "
            f"defaults, and is then compared as if it had been tuned.\n")

    metrics = dict(node.get(metrics_key) or {})
    if role not in metrics:
        raise IncomparableError(
            f"\nREFUSING TO REGISTER {name!r}: no {role!r} block in "
            f"{metrics_key!r} (has: {sorted(metrics)}).\n")

    members = data.get(split_key)
    if not members:
        raise IncomparableError(
            f"\nREFUSING TO REGISTER {name!r}: {base} does not record "
            f"{split_key!r}.\n"
            f"  Without the split contents the split can only be identified by "
            f"a label, and two different splits both labelled 'seed 42, 50/50' "
            f"is the original failure. Have the producing script save its "
            f"member lists.\n")

    split = Split.from_items(
        members, n_units=n_units_from_counts(metrics.get(role), n_units_keys),
        role=role, source=base, provenance="file")

    crit = criterion or Criterion.from_results(
        results_path, criterion_keys, block_key=criterion_block_key,
        **asserted_criterion_fields)

    prefix = f"{base}:{methods_key}.{method}" if method else base
    prov = {
        "params_from": f"{prefix}.{params_key}",
        "metrics_from": f"{prefix}.{metrics_key}",
        "split_from": f"{base}:{split_key}",
        "criterion_from": (
            "asserted by caller" if criterion is not None
            else f"{base}:{criterion_block_key}"
                 + (f" (asserted: {sorted(asserted_criterion_fields)})"
                    if asserted_criterion_fields else "")),
        "method_key": str(method),
    }
    if "best_method" in data:
        prov["best_method_in_file"] = str(data["best_method"])

    return register_baseline(name, fn, params, split, crit, results_path,
                             metrics=metrics, provenance=prov, replace=replace)


def get_baseline(name: str) -> Baseline:
    """Look up a registered arm. Never reconstruct one by hand instead.

    THE INCIDENT THIS PREVENTS: when the comparator was not obtainable in one
    call, each comparison script built its own — one re-wrote the baseline from
    scratch (recall 0.636 -> 0.098), the next imported the wrong variant (0.731
    vs 0.442). One call, one record, four welded fields.
    """
    if name not in _REGISTRY:
        raise IncomparableError(
            f"\nREFUSING: no arm registered as {name!r} "
            f"(registered: {sorted(_REGISTRY)}).\n"
            f"  Register it with register_baseline_from_results(...) so its "
            f"params, split, criterion and metrics all come from the file that "
            f"produced them. Do not inline a number.\n")
    return _REGISTRY[name]


def list_baselines() -> list[str]:
    return sorted(_REGISTRY)


def _fmt(v: Any) -> str:
    return "None" if v is None else repr(v)


def compare(a: "Baseline | str", b: "Baseline | str", metric: str = "f1",
            split: str = "test", max_members_shown: int = 3) -> dict:
    """Compute a delta, or refuse and say exactly why. THE load-bearing function.

    A delta must be *uncomputable* when the arms are not comparable, so this
    raises `IncomparableError` unless `a.split_id == b.split_id` and
    `a.criterion == b.criterion`, plus a separate ground-truth-count check that
    catches "same members, different label extraction".

    THE INCIDENT THIS PREVENTS: "the new model beats the baseline" was computed
    between an arm scored at IoU > 0.1 on a 499-unit split and an arm scored at
    IoU >= 0.3 OR overlap > 50% on a 376-unit split. Every arithmetic step was
    correct. The subtraction was meaningless, and nothing in the pipeline was
    positioned to say so. This is that position.

    Returns both arms' metrics, the delta, the shared split id and criterion,
    and `provenance_warnings` for any field that was asserted or reconstructed
    by a human rather than read from a results file — because a registry can
    require a field it cannot verify.
    """
    a = a if isinstance(a, Baseline) else get_baseline(a)
    b = b if isinstance(b, Baseline) else get_baseline(b)

    problems: list[str] = []

    if a.split_id != b.split_id:
        only_a, only_b = a.split.diff(b.split)
        lines = ["  SPLIT MISMATCH",
                 f"    {a.name}: split_id {a.split.describe()}",
                 f"    {b.name}: split_id {b.split.describe()}"]
        if a.split.items and b.split.items:
            lines.append(f"    -> {len(only_a)} member(s) only in {a.name}, "
                         f"{len(only_b)} only in {b.name}")
            for label, items in ((a.name, only_a), (b.name, only_b)):
                for m in items[:max_members_shown]:
                    lines.append(f"       only in {label}: {m}")
                if len(items) > max_members_shown:
                    lines.append(f"       ... and {len(items) - max_members_shown} "
                                 f"more only in {label}")
        else:
            lines.append("    -> member lists not both recorded, so the "
                         "per-member diff is unavailable")
        problems.append("\n".join(lines))
    else:
        na, nb = a.split.n_units, b.split.n_units
        if na is not None and nb is not None and na != nb:
            problems.append(
                f"  GROUND-TRUTH COUNT MISMATCH on one split_id ({a.split_id})\n"
                f"    {a.name}: {na} GT units\n"
                f"    {b.name}: {nb} GT units\n"
                f"    -> identical split members, different ground truth. The "
                f"label extraction differs between the two runs.")

    crit_diff = a.criterion.differences(b.criterion)
    if crit_diff:
        problems.append(
            f"  CRITERION MISMATCH\n"
            f"    {a.name}: {a.criterion}\n"
            f"    {b.name}: {b.criterion}\n"
            f"    -> differs in: " + ", ".join(
                f"{f} ({_fmt(va)} vs {_fmt(vb)})" for f, va, vb in crit_diff))

    if problems:
        raise IncomparableError(
            f"\nREFUSING TO COMPARE {a.name!r} against {b.name!r}: the arms are "
            f"not comparable.\n\n" + "\n\n".join(problems) + "\n\n"
            f"  WHAT TO DO — a delta between these is not a smaller or larger "
            f"number, it has no meaning:\n"
            f"    1. Choose ONE split. Use its member list, not a description "
            f"of how it was generated.\n"
            f"    2. Choose ONE Criterion(...) and score both arms under it.\n"
            f"    3. Re-score BOTH arms — the registered ones via "
            f"get_baseline(<name>).run(...), which is bound to their tuned "
            f"parameters.\n"
            f"    4. Register the re-scored arms and compare() those.\n"
            f"  Do not report the delta you were about to report.\n")

    va, vb = a.metric(metric, split), b.metric(metric, split)
    warnings = [f"{arm.name}.{k} = {v}" for arm in (a, b)
                for k, v in sorted(arm.provenance.items())
                if str(v).startswith("asserted") or "reconstructed" in str(v)]
    return {
        "metric": metric,
        "split": split,
        "split_id": a.split_id,
        "n_gt_units": a.split.n_units,
        "criterion": a.criterion.as_dict(),
        "a": {"name": a.name, "value": va, "counts": a.counts(split),
              "source": os.path.basename(a.source), "params": dict(a.params),
              "callable": a.fn_ref},
        "b": {"name": b.name, "value": vb, "counts": b.counts(split),
              "source": os.path.basename(b.source), "params": dict(b.params),
              "callable": b.fn_ref},
        "delta": va - vb,
        "provenance_warnings": warnings,
    }


# ---------------------------------------------------------------------------
# Guards
# ---------------------------------------------------------------------------

def assert_sweep_interior(results: Sequence[Mapping], key: str = "f1",
                          param_key: str = "threshold") -> None:
    """Refuse to report an optimum sitting at the edge of the swept range.

    THE INCIDENT THIS PREVENTS: two runs both selected threshold 0.3 — the
    *lowest* value tested — so the true operating point was never located and
    the headline number was the boundary of where the sweep stopped, not the
    model's best. It was then read as "the model is confident about a lot of
    boundaries", which inverts the meaning: needing a low threshold means the
    metric only peaks once low-confidence predictions are accepted, which is
    evidence of weak calibration, not strength.

    Raises `IncomparableError` (a `SystemExit` subclass) rather than returning a
    flag, because the alternative is a number that gets quoted.
    """
    if len(results) < 2:
        return
    values = sorted(r[param_key] for r in results)
    best = max(results, key=lambda r: r[key])
    if best[param_key] in (values[0], values[-1]):
        edge = "lowest" if best[param_key] == values[0] else "highest"
        raise IncomparableError(
            f"\nREFUSING TO REPORT: best {key} is at {param_key}="
            f"{best[param_key]}, the {edge} value swept ({values}).\n"
            f"  The optimum is at the edge of the range, so the real optimum "
            f"was never tested.\n"
            f"  Extend the sweep past {best[param_key]} and re-run before "
            f"quoting this number.\n")


# ---------------------------------------------------------------------------
# Reproducibility / provenance
# ---------------------------------------------------------------------------

def set_all_seeds(seed: int = 42) -> dict:
    """Seed every RNG that touches a result, and report what was seeded.

    THE INCIDENT THIS PREVENTS: `random.seed(seed)` alone fixed only the
    file-level split. Weight init, dropout and dataloader shuffling all drew
    from numpy/torch, which nothing seeded — so the same script on the same data
    produced F1 0.7675 where it had produced 0.7839, and the published number
    could not be regenerated. The discrepancy was then explained as "a different
    random split", which was the one thing that *was* controlled.

    Call it first thing in `main()` and put the return value in the provenance
    block. `numpy` and `torch` are optional — what is seeded is reported, so an
    absent library cannot look like a seeded one.
    """
    info: dict = {"seed": seed, "seeded": ["random"]}
    random.seed(seed)
    try:
        import numpy as np
        np.random.seed(seed)
        info["seeded"].append("numpy")
        info["numpy_version"] = np.__version__
    except ImportError:
        pass
    try:
        import torch
        torch.manual_seed(seed)
        torch.cuda.manual_seed_all(seed)
        try:
            # cuDNN autotuning picks different kernels run to run. A no-op on
            # CPU, but it costs nothing and makes a later GPU move reproducible.
            torch.backends.cudnn.deterministic = True
            torch.backends.cudnn.benchmark = False
        except Exception:
            pass
        info["seeded"].append("torch")
        info["torch_version"] = torch.__version__
    except ImportError:
        pass
    return info


def run_stamped(path: str, when: "datetime | None" = None) -> str:
    """Insert a UTC run stamp before the extension.

    THE INCIDENT THIS PREVENTS: a rerun wrote to the same fixed filename as the
    run whose F1 a public page cited, replacing it in place. The published
    number then existed nowhere on disk. An artifact behind a published figure
    must be immutable, so results go to a new path every run.
    """
    when = when or datetime.now(timezone.utc)
    root, ext = os.path.splitext(path)
    return f"{root}.{when:%Y%m%dT%H%M%SZ}{ext}"


def _git_sha(cwd: str | None = None) -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "--short", "HEAD"],
                             cwd=cwd or os.getcwd(), capture_output=True,
                             text=True, timeout=5)
        return out.stdout.strip() or None
    except Exception:
        return None


def provenance_block(seed_info: Mapping | None = None,
                     cwd: str | None = None, **extra) -> dict:
    """The block every results JSON must carry to be reproducible from itself.

    A result is not finished until a second script can regenerate its headline
    number from the JSON alone. That needs the seed, the library versions, the
    script, the git sha, and every data-reduction decision — not just the
    metrics. See `docs/results-provenance-checklist.md` for the full list; this
    function supplies the parts that can be collected automatically. The rest
    (the split member lists, the criterion, how the operating point was chosen,
    every truncation and cap) you must pass in as `**extra`, because only the
    calling script knows them.
    """
    p = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "script": os.path.basename(sys.argv[0]) or "<interactive>",
        "git_sha": _git_sha(cwd),
        "python": sys.version.split()[0],
    }
    if seed_info:
        p.update(dict(seed_info))
    p.update(extra)
    return p
