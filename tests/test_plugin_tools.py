"""Offline smoke tests for the Matilde Hermes plugin wiring.

Verifies the plugin loads, exposes the expected tools, gates correctly, coerces
arguments, and returns well-formed JSON envelopes on bad input — all without
touching the network (the live verification path is covered by
test_citations_integration.py).
"""
from __future__ import annotations

import importlib.util
import json
import os
import sys

ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, ROOT)


def _load_plugin():
    path = os.path.join(ROOT, "matilde_plugin", "__init__.py")
    spec = importlib.util.spec_from_file_location("matilde_plugin", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_plugin_registers_expected_tools():
    plugin = _load_plugin()
    names = [t[0] for t in plugin._TOOLS]
    assert names == [
        "matilde_verify_citation",
        "matilde_verify_bibliography",
        "matilde_check_retraction",
        "matilde_openneuro_dataset_info",
        "matilde_openneuro_search",
        "matilde_openneuro_list_files",
        "matilde_fetch_fulltext",
        "matilde_study_create",
        "matilde_study_run",
        "matilde_study_status",
        "matilde_study_list",
    ]


def test_register_calls_ctx_for_each_tool():
    plugin = _load_plugin()
    calls = []

    class Ctx:
        def register_tool(self, **kw):
            calls.append(kw)

    plugin.register(Ctx())
    assert len(calls) == len(plugin._TOOLS)
    assert all(c["toolset"] == "matilde" for c in calls)
    assert all(callable(c["handler"]) and callable(c["check_fn"]) for c in calls)
    # schema name must match the registered tool name
    assert all(c["name"] == c["schema"]["name"] for c in calls)


def test_check_available_true_when_engine_imports():
    plugin = _load_plugin()
    assert plugin._check_available() is True


def test_verify_citation_requires_doi_or_title():
    plugin = _load_plugin()
    out = json.loads(plugin._handle_verify_citation({}))
    assert out["success"] is False
    assert "doi" in out["error"] or "title" in out["error"]


def test_verify_bibliography_rejects_non_list():
    plugin = _load_plugin()
    out = json.loads(plugin._handle_verify_bibliography({"references": "nope"}))
    assert out["success"] is False


def test_check_retraction_requires_doi():
    plugin = _load_plugin()
    out = json.loads(plugin._handle_check_retraction({}))
    assert out["success"] is False


def test_openneuro_dataset_info_envelope_includes_message_and_fields(monkeypatch):
    plugin = _load_plugin()
    import matilde_plugin.engine.openneuro as on

    def fake_get_dataset(dsid, gql=None):
        return on.Dataset(id=dsid, name="Demo MEG", modalities=["meg"],
                          subjects=["0001"], size=123, latest_tag="1.0.0")

    monkeypatch.setattr(on, "get_dataset", fake_get_dataset)
    out = json.loads(plugin._handle_openneuro_dataset_info({"dataset_id": "ds000246"}))
    assert out["success"] is True
    assert out["id"] == "ds000246"
    assert out["modalities"] == ["meg"]
    assert "message" in out and "Demo MEG" in out["message"]


def test_openneuro_dataset_info_requires_id():
    plugin = _load_plugin()
    out = json.loads(plugin._handle_openneuro_dataset_info({}))
    assert out["success"] is False


def test_reference_from_args_coerces_string_authors_and_year():
    plugin = _load_plugin()
    ref = plugin._tools_mod._reference_from_args(
        {"title": "X", "authors": "Vaswani; Shazeer", "year": "2017", "doi": "10.1/x"}
    )
    assert ref.authors == ["Vaswani", "Shazeer"]
    assert ref.year == 2017
    assert ref.doi == "10.1/x"


def test_manifest_provides_tools_matches_registry():
    """plugin.yaml's ``provides_tools`` must equal the registered tool names.

    These drifted apart silently: the manifest declared 7 tools while the plugin
    registered 11, so the whole study pipeline (``matilde_study_*``) was invisible
    to any host that trusts the manifest to enumerate the capability surface. No
    test compared the two, which is why nobody noticed across six feature PRs.

    Parsed with a deliberately small reader rather than PyYAML: the offline suite
    has no third-party dependencies and this guard is not worth adding one.
    """
    plugin = _load_plugin()
    registered = [name for name, _schema, _handler, _emoji in plugin._TOOLS]

    manifest_path = os.path.join(ROOT, "matilde_plugin", "plugin.yaml")
    with open(manifest_path, encoding="utf-8") as fh:
        lines = fh.read().splitlines()

    declared = []
    in_block = False
    for line in lines:
        if line.startswith("provides_tools:"):
            in_block = True
            continue
        if in_block:
            stripped = line.strip()
            if stripped.startswith("- "):
                declared.append(stripped[2:].strip().strip('"').strip("'"))
            elif stripped and not stripped.startswith("#"):
                break  # a new top-level key ends the list

    assert declared, "could not parse provides_tools out of plugin.yaml"
    assert declared == registered, (
        "plugin.yaml provides_tools is out of sync with _TOOLS.\n"
        f"  declared only:   {sorted(set(declared) - set(registered))}\n"
        f"  registered only: {sorted(set(registered) - set(declared))}"
    )
