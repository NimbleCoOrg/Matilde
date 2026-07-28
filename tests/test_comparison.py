"""The comparison registry must refuse the four invalidities it was built for.

Each test names the failure it reproduces. The registry's value is entirely in
what it *refuses*, so almost every assertion here is that a call raises rather
than returns a number. See docs/trustworthy-comparison.md for the case study and
docs/baseline-registry.md for the API walkthrough.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from matilde_plugin.engine.comparison import (  # noqa: E402
    Baseline,
    Criterion,
    IncomparableError,
    Split,
    assert_sweep_interior,
    clear_registry,
    compare,
    get_baseline,
    list_baselines,
    n_units_from_counts,
    provenance_block,
    register_baseline,
    register_baseline_from_results,
    run_stamped,
    set_all_seeds,
    split_id_of,
)

STRICT = dict(iou_threshold=0.3, overlap_threshold=0.5, iou_op=">=")
LOOSE = dict(iou_threshold=0.1, overlap_threshold=None, iou_op=">")

ITEMS_A = [f"rec_{i:03d}" for i in range(30)]
ITEMS_B = ITEMS_A[:-1] + ["rec_999"]          # same size, one member swapped


@pytest.fixture(autouse=True)
def _clean_registry():
    clear_registry()
    yield
    clear_registry()


def _detector(data, threshold=0.7, min_duration_ms=30, merge_gap_ms=20):
    """Stand-in for a tuned classical detector. Signature defaults are the
    untuned values — the same trap as the real one."""
    return [threshold, min_duration_ms, merge_gap_ms, data]


def _register(name, items, criterion_fields, metrics=None, fn=None,
              params=None, provenance=None):
    return register_baseline(
        name, fn, params or {}, items, Criterion(**criterion_fields),
        source=f"{name}_results.json",
        metrics=metrics or {"test": {"f1": 0.5, "tp": 100, "fp": 10, "fn": 20}},
        n_units=n_units_from_counts((metrics or {}).get("test")
                                   or {"tp": 100, "fn": 20}),
        provenance=provenance)


# ---------------------------------------------------------------------------
# Split identity — "the seed-42 50/50 split" described two different splits
# ---------------------------------------------------------------------------

def test_split_id_is_content_derived_and_order_independent():
    assert split_id_of(ITEMS_A) == split_id_of(list(reversed(ITEMS_A)))


def test_one_swapped_member_is_a_different_split_at_the_same_size():
    assert len(ITEMS_A) == len(ITEMS_B)
    assert split_id_of(ITEMS_A) != split_id_of(ITEMS_B)


def test_a_label_cannot_be_a_split_id():
    # The root cause of failure 1: the split travelled as the *description*
    # "seed 42, 50/50", which two different splits both satisfied.
    with pytest.raises(TypeError) as e:
        split_id_of("the seed-42 50/50 split")
    assert "CONTENTS" in str(e.value)


def test_empty_split_is_refused():
    with pytest.raises(ValueError):
        split_id_of([])


# ---------------------------------------------------------------------------
# Criterion — prose is not comparable; structure is
# ---------------------------------------------------------------------------

def test_note_is_excluded_from_equality_in_both_directions():
    a = Criterion(note="IoU > 0.3 OR overlap > 50%", **STRICT)
    b = Criterion(note="a completely different sentence", **STRICT)
    assert a == b                      # rewording cannot split a criterion
    assert hash(a) == hash(b)
    assert Criterion(**STRICT) != Criterion(**LOOSE)


def test_declared_none_differs_from_absent():
    # "no overlap fallback" and "never said whether there is one" are different
    # states; conflating them lets a missing fallback pass for a declared one.
    declared = Criterion(iou_threshold=0.3, overlap_threshold=None)
    absent = Criterion(iou_threshold=0.3)
    assert declared != absent
    # Assert the SEMANTICS, not the sentinel's value. This previously asserted the
    # literal string "<absent>", which coupled the test to an implementation detail
    # — and that detail was the bug: a field whose value was the string "<absent>"
    # was indistinguishable from an absent field, so differences() could return []
    # for unequal criteria. The marker is now a unique object.
    diff = dict((f, (a, b)) for f, a, b in declared.differences(absent))
    assert "overlap_threshold" in diff
    mine, theirs = diff["overlap_threshold"]
    assert mine is None                      # declared, explicitly no fallback
    assert theirs is not None                # absent — some marker, not None
    assert not isinstance(theirs, (int, float, str, bool))


def test_absent_marker_cannot_collide_with_a_real_value():
    """A field whose value is the string "<absent>" must still count as a difference.

    Regression: `differences()` used the literal string "<absent>" as its
    missing-field marker, so this pair compared unequal via `__eq__` while
    `differences()` returned [] — and `compare()`, which gated on `differences()`,
    computed a delta between two incomparable arms. Found in review.
    """
    tricky = Criterion(iou_threshold=0.3, extra="<absent>")
    plain = Criterion(iou_threshold=0.3)
    assert tricky != plain
    assert tricky.differences(plain), (
        "a field valued '<absent>' must not be mistaken for an absent field")


def test_differences_names_which_field_disagrees():
    diff = dict((f, (a, b)) for f, a, b in
                Criterion(**STRICT).differences(Criterion(**LOOSE)))
    assert diff["iou_threshold"] == (0.3, 0.1)
    assert diff["iou_op"] == (">=", ">")


def test_empty_criterion_is_refused():
    with pytest.raises(ValueError):
        Criterion()


def test_criterion_is_immutable():
    with pytest.raises(AttributeError):
        Criterion(**STRICT).iou_threshold = 0.9


def test_from_results_takes_numbers_from_file_and_operators_from_caller(tmp_path):
    # The file's own prose said "IoU > 0.3" while the scoring code evaluated
    # `>=`. Parsing the operator out of the sentence would encode that error.
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"matching_criteria": {
        "iou_threshold": 0.3, "overlap_threshold": 0.5,
        "method": "IoU > 0.3 OR overlap > 50%"}}))
    c = Criterion.from_results(str(p), ("iou_threshold", "overlap_threshold"),
                               iou_op=">=")
    assert c.get("iou_threshold") == 0.3
    assert c.get("iou_op") == ">="              # asserted by the caller
    assert "IoU > 0.3" in c.note                 # the wrong prose, quarantined
    assert c == Criterion(**STRICT)


def test_from_results_refuses_a_file_that_records_no_criterion(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"best": {"f1": 0.78}}))
    with pytest.raises(IncomparableError) as e:
        Criterion.from_results(str(p), ("iou_threshold",))
    assert "asserted from memory" in str(e.value)


# ---------------------------------------------------------------------------
# Failure 1 — a hardcoded comparator, and the mismatches it hid
# ---------------------------------------------------------------------------

def test_a_bare_number_cannot_be_registered():
    with pytest.raises(IncomparableError) as e:
        register_baseline("x", None, {}, ITEMS_A, "IoU >= 0.3 OR overlap > 50%",
                          source="notification stdout")
    assert "must be a Criterion" in str(e.value)


def test_compare_refuses_a_split_mismatch_and_names_the_members():
    _register("classical", ITEMS_A, STRICT)
    _register("learned", ITEMS_B, STRICT)
    with pytest.raises(IncomparableError) as e:
        compare("classical", "learned")
    msg = str(e.value)
    assert "SPLIT MISMATCH" in msg
    assert "1 member(s) only in classical" in msg
    assert "rec_999" in msg                      # says which


def test_compare_refuses_a_criterion_mismatch_and_names_the_field():
    _register("classical", ITEMS_A, STRICT)
    _register("learned", ITEMS_A, LOOSE)
    with pytest.raises(IncomparableError) as e:
        compare("classical", "learned")
    msg = str(e.value)
    assert "CRITERION MISMATCH" in msg
    assert "iou_threshold (0.3 vs 0.1)" in msg


def test_compare_refuses_same_members_different_ground_truth():
    # The 499-vs-376 discrepancy surfaced here first, before anyone noticed the
    # member lists differed: identical split, different label extraction.
    _register("a", ITEMS_A, STRICT,
              metrics={"test": {"f1": 0.73, "tp": 239, "fp": 39, "fn": 137}})
    _register("b", ITEMS_A, STRICT,
              metrics={"test": {"f1": 0.77, "tp": 449, "fp": 224, "fn": 50}})
    with pytest.raises(IncomparableError) as e:
        compare("a", "b")
    msg = str(e.value)
    assert "GROUND-TRUTH COUNT MISMATCH" in msg
    assert "376 GT units" in msg and "499 GT units" in msg


def test_the_refusal_says_what_to_do():
    _register("a", ITEMS_A, STRICT)
    _register("b", ITEMS_B, LOOSE)
    msg = str(pytest.raises(IncomparableError, compare, "a", "b").value)
    assert "Choose ONE split" in msg
    assert "Do not report the delta you were about to report" in msg


def test_a_matched_pair_compares_and_carries_its_provenance():
    _register("classical", ITEMS_A, STRICT,
              metrics={"test": {"f1": 0.801, "tp": 363, "fp": 90, "fn": 136}})
    _register("learned", ITEMS_A, STRICT,
              metrics={"test": {"f1": 0.775, "tp": 447, "fp": 130, "fn": 52}},
              provenance={"criterion_from": "asserted: train.py:206"})
    res = compare("classical", "learned")
    assert res["delta"] == pytest.approx(0.026, abs=1e-9)
    assert res["split_id"] == split_id_of(ITEMS_A)
    # A field a human asserted is surfaced, not hidden: the registry can require
    # a criterion and still not verify one its source file never recorded.
    assert res["provenance_warnings"] == ["learned.criterion_from = asserted: train.py:206"]


def test_an_id_only_split_says_the_diff_is_unavailable():
    register_baseline("a", None, {}, "deadbeefcafe", Criterion(**STRICT), "a.json",
                      metrics={"test": {"f1": 0.5}})
    register_baseline("b", None, {}, ITEMS_A, Criterion(**STRICT), "b.json",
                      metrics={"test": {"f1": 0.6}})
    msg = str(pytest.raises(IncomparableError, compare, "a", "b").value)
    assert "per-member diff is unavailable" in msg


def test_train_metrics_are_not_silently_substituted_for_test():
    _register("a", ITEMS_A, STRICT, metrics={"train": {"f1": 0.83}})
    _register("b", ITEMS_A, STRICT, metrics={"train": {"f1": 0.80}})
    msg = str(pytest.raises(IncomparableError, compare, "a", "b").value)
    assert "no 'test' metrics recorded" in msg
    assert "how a train-split score gets quoted as test" in msg


def test_two_records_cannot_share_one_name():
    # "the baseline" came to mean three different things this way.
    _register("baseline", ITEMS_A, STRICT)
    with pytest.raises(IncomparableError) as e:
        _register("baseline", ITEMS_B, LOOSE)
    assert "already registered" in str(e.value)
    assert list_baselines() == ["baseline"]


def test_an_unregistered_arm_is_refused_not_invented():
    msg = str(pytest.raises(IncomparableError, get_baseline, "baseline").value)
    assert "Do not inline a number" in msg


# ---------------------------------------------------------------------------
# Failures 2 and 3 — reimplementation, and the wrong function
# ---------------------------------------------------------------------------

def test_the_registered_callable_is_the_validated_one():
    bl = _register("classical", ITEMS_A, STRICT, fn=_detector,
                   params={"threshold": 0.5})
    assert bl.fn is _detector
    assert bl.fn_ref.endswith("_detector")


def test_params_from_one_method_cannot_be_bound_to_another_callable():
    # The other variant's tuned winners carry parameters this callable does not
    # accept. The original runner filtered them out with a co_varnames
    # comprehension, so the mismatch was silent and the arm ran untuned values.
    with pytest.raises(IncomparableError) as e:
        _register("wrong", ITEMS_A, STRICT, fn=_detector,
                  params={"threshold": 0.1, "n_fft": 256, "hop": 128})
    msg = str(e.value)
    assert "['hop', 'n_fft']" in msg
    assert "do not belong to the same" in msg


def test_from_results_reads_params_metrics_split_and_criterion_from_one_file(tmp_path):
    p = tmp_path / "validation.json"
    p.write_text(json.dumps({
        "best_method": "simple",
        "test_items": ITEMS_A,
        "matching_criteria": {"iou_threshold": 0.3, "overlap_threshold": 0.5,
                              "method": "IoU > 0.3 OR overlap > 50%"},
        "results_by_method": {
            "simple": {
                "parameters": {"threshold": 0.5, "min_duration_ms": 10,
                               "merge_gap_ms": 50},
                "aggregate": {"test": {"f1": 0.7309, "tp": 239, "fp": 39, "fn": 137},
                              "train": {"f1": 0.8265, "tp": 362, "fp": 61, "fn": 91}}}}}))
    bl = register_baseline_from_results(
        "simple", str(p), ("iou_threshold", "overlap_threshold"),
        method="simple", fn=_detector, iou_op=">=")

    # Nothing was typed in: params, metrics, split and thresholds are file-derived.
    assert bl.params == {"threshold": 0.5, "min_duration_ms": 10, "merge_gap_ms": 50}
    assert bl.metric("f1") == 0.7309
    assert bl.split.n_units == 376                      # derived tp+fn, not typed
    assert bl.split_id == split_id_of(ITEMS_A)
    assert bl.criterion == Criterion(**STRICT)
    assert bl.provenance["best_method_in_file"] == "simple"
    assert "matching_criteria" in bl.provenance["criterion_from"]


def test_from_results_refuses_a_file_with_no_member_list(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "matching_criteria": {"iou_threshold": 0.3},
        "results_by_method": {"m": {"parameters": {},
                                    "aggregate": {"test": {"f1": 0.7}}}}}))
    with pytest.raises(IncomparableError) as e:
        register_baseline_from_results("m", str(p), ("iou_threshold",), method="m")
    assert "identified by a label" in str(e.value)


def test_from_results_refuses_a_method_without_tuned_parameters(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({
        "test_items": ITEMS_A,
        "matching_criteria": {"iou_threshold": 0.3},
        "results_by_method": {"m": {"aggregate": {"test": {"f1": 0.7}}}}}))
    with pytest.raises(IncomparableError) as e:
        register_baseline_from_results("m", str(p), ("iou_threshold",), method="m")
    assert "library signature defaults" in str(e.value)


def test_from_results_names_the_methods_it_does_have(tmp_path):
    p = tmp_path / "r.json"
    p.write_text(json.dumps({"results_by_method": {"simple": {}, "windowed": {}}}))
    with pytest.raises(IncomparableError) as e:
        register_baseline_from_results("x", str(p), ("iou_threshold",), method="typo")
    assert "['simple', 'windowed']" in str(e.value)


# ---------------------------------------------------------------------------
# Failure 4 — untuned parameters
# ---------------------------------------------------------------------------

def test_run_uses_the_tuned_parameters_not_the_signature_defaults():
    bl = _register("classical", ITEMS_A, STRICT, fn=_detector,
                   params={"threshold": 0.5, "min_duration_ms": 10,
                           "merge_gap_ms": 50})
    assert bl.run("data") == [0.5, 10, 50, "data"]      # not 0.7 / 30 / 20


def test_run_refuses_parameter_overrides():
    bl = _register("classical", ITEMS_A, STRICT, fn=_detector,
                   params={"threshold": 0.5})
    with pytest.raises(IncomparableError) as e:
        bl.run("data", threshold=0.7)
    assert "bound to the parameters it was tuned to" in str(e.value)
    assert "tune them on the TRAIN split" in str(e.value)


def test_an_arm_with_no_callable_can_be_compared_but_not_re_run():
    bl = _register("learned", ITEMS_A, STRICT)
    with pytest.raises(IncomparableError) as e:
        bl.run("data")
    assert "no callable registered" in str(e.value)


# ---------------------------------------------------------------------------
# Sweep boundary
# ---------------------------------------------------------------------------

def test_an_optimum_at_the_bottom_of_the_sweep_is_refused():
    sweep = [{"threshold": t, "f1": f} for t, f in
             [(0.3, 0.784), (0.5, 0.72), (0.7, 0.61)]]
    with pytest.raises(IncomparableError) as e:
        assert_sweep_interior(sweep)
    assert "the lowest value swept" in str(e.value)
    assert "Extend the sweep" in str(e.value)


def test_an_optimum_at_the_top_of_the_sweep_is_refused():
    sweep = [{"threshold": t, "f1": f} for t, f in
             [(0.3, 0.61), (0.5, 0.72), (0.7, 0.784)]]
    assert "the highest value swept" in str(
        pytest.raises(IncomparableError, assert_sweep_interior, sweep).value)


def test_an_interior_optimum_passes():
    assert_sweep_interior([{"threshold": t, "f1": f} for t, f in
                           [(0.1, 0.53), (0.4, 0.80), (0.6, 0.79)]])


def test_a_single_point_sweep_is_not_treated_as_an_optimum():
    assert_sweep_interior([{"threshold": 0.5, "f1": 0.9}])


# ---------------------------------------------------------------------------
# Provenance helpers
# ---------------------------------------------------------------------------

def test_set_all_seeds_reports_only_what_it_actually_seeded():
    info = set_all_seeds(7)
    assert info["seed"] == 7
    assert "random" in info["seeded"]
    # An absent optional library must not look like a seeded one.
    for lib in ("numpy", "torch"):
        assert (lib in info["seeded"]) == (f"{lib}_version" in info)


def test_run_stamped_never_returns_the_input_path():
    out = run_stamped("/tmp/results.json")
    assert out != "/tmp/results.json"
    assert out.startswith("/tmp/results.") and out.endswith(".json")
    assert "Z.json" in out


def test_provenance_block_carries_the_automatic_fields_and_passes_extras():
    p = provenance_block(set_all_seeds(42), matching_criterion="IoU >= 0.3",
                         duration_cap_s=180)
    for k in ("generated_utc", "script", "python", "seed", "seeded"):
        assert k in p
    assert p["duration_cap_s"] == 180          # only the caller knows this one
    assert p["matching_criterion"] == "IoU >= 0.3"


# ---------------------------------------------------------------------------
# describe() is the human-readable audit surface
# ---------------------------------------------------------------------------

def test_describe_shows_all_four_welded_fields():
    bl = _register("classical", ITEMS_A, STRICT, fn=_detector,
                   params={"threshold": 0.5})
    text = bl.describe()
    for expected in ("callable", "params", "split", "criterion", "source",
                     "metrics", "provenance"):
        assert expected in text
    assert "threshold=0.5" in text.replace("'threshold': 0.5", "threshold=0.5")


def test_split_describe_admits_when_the_member_list_is_missing():
    assert "member list unavailable" in Split(split_id="abc").describe()
