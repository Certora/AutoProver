"""The shared conf layering, and the CVL run conf built on it."""

import json
from pathlib import Path

from composer.prover.conf import ExcludeRules, InheritRules, SelectRules, dump_conf, with_rules
from composer.spec.source.prover import (
    BOTH_RULE_SCOPES, prover_config_overlay, rule_selection, setup_prover_config_in,
)


_BASE = {"files": ["C.sol"], "rule": ["base_rule"], "exclude_rule": ["skipped"], "loop_iter": 3}


def test_each_rule_selection_writes_only_its_key():
    assert with_rules(_BASE, InheritRules()) == _BASE
    assert with_rules(_BASE, SelectRules(("r",))) == {**_BASE, "rule": ["r"]}
    assert with_rules(_BASE, ExcludeRules(("r",))) == {**_BASE, "exclude_rule": ["r"]}


def test_cvl_overlay_forces_its_settings_over_the_base():
    """CVL overrides the base's own sanity and loop settings, whatever they are."""
    base = {**_BASE, "rule_sanity": "advanced", "optimistic_loop": False}
    assert prover_config_overlay(base, main_contract="C", verify_target="C:x.spec") == {
        **_BASE,
        "verify": "C:x.spec",
        "parametric_contracts": "C",
        "optimistic_loop": True,
        "rule_sanity": "basic",
    }


def test_rule_selection_from_the_tool_arguments():
    assert rule_selection(None, None) == InheritRules()
    assert rule_selection(["a"], None) == SelectRules(("a",))
    assert rule_selection(None, ["b"]) == ExcludeRules(("b",))
    assert rule_selection(["a"], ["b"]) == BOTH_RULE_SCOPES


def test_cvl_run_conf_on_disk(tmp_path: Path):
    """``msg`` and other extras land in the written conf; an exclusion keeps the base's ``rule``."""
    with setup_prover_config_in(
        working_dir=str(tmp_path), config=_BASE, spec_contents="rule r { assert true; }",
        main_contract="C", rules=ExcludeRules(("r",)), msg="iteration 1",
    ) as (conf_path, config):
        written = (tmp_path / conf_path).read_text()
    assert written == dump_conf(config)
    parsed = json.loads(written)
    assert parsed["rule"] == ["base_rule"]
    assert parsed["exclude_rule"] == ["r"]
    assert parsed["msg"] == "iteration 1"
    assert parsed["rule_sanity"] == "basic"
