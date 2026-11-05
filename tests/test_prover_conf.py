"""The shared conf layering, and the CVL run conf built on it."""

import json
from pathlib import Path

from langgraph.checkpoint.serde.jsonplus import JsonPlusSerializer

import pytest

from composer.prover.conf import ExcludeRules, InheritRules, SelectRules, checked_rules, dump_conf
from composer.spec.source.prover import (
    BOTH_RULE_SCOPES, prover_config_overlay, rule_selection, setup_prover_config_in,
)


_BASE = {"files": ["C.sol"], "rule": ["base_rule"], "exclude_rule": ["skipped"], "loop_iter": 3}


def test_each_rule_selection_writes_only_its_key():
    assert InheritRules().apply_to(_BASE) == _BASE
    assert SelectRules(("r",)).apply_to(_BASE) == {**_BASE, "rule": ["r"]}
    assert ExcludeRules(("r",)).apply_to(_BASE) == {**_BASE, "exclude_rule": ["r"]}


def test_rule_selections_survive_a_checkpoint_round_trip():
    serde = JsonPlusSerializer()
    for sel in (InheritRules(), SelectRules(("a", "b")), ExcludeRules(("c",))):
        restored = serde.loads_typed(serde.dumps_typed(sel))
        assert restored == sel
        assert hash(restored) == hash(sel)


def test_rule_selections_ignore_name_order():
    assert SelectRules(("b", "a")) == SelectRules(("a", "b"))
    assert hash(ExcludeRules(("b", "a"))) == hash(ExcludeRules(("a", "b")))
    assert SelectRules(("a",)) != ExcludeRules(("a",))


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


_DECLARED = ["inv_supply", "inv_balance", "transfer_moves", "deposit_mints", "sanity"]


def test_a_conf_without_selection_checks_every_rule():
    assert checked_rules({"files": ["C.sol"]}, _DECLARED) == _DECLARED


def test_rule_and_exclude_rule_combine_and_read_globs():
    # certoraRun applies ``rule`` first, then drops ``exclude_rule``. Both take globs.
    conf = {"rule": ["inv_*", "transfer_moves"], "exclude_rule": ["inv_balance"]}
    assert checked_rules(conf, _DECLARED) == ["inv_supply", "transfer_moves"]
    assert checked_rules({"exclude_rule": ["*_m*"]}, _DECLARED) == [
        "inv_supply", "inv_balance", "sanity",
    ]


def test_a_single_name_reads_like_a_one_element_list():
    assert checked_rules({"rule": "sanity"}, _DECLARED) == ["sanity"]


def test_the_union_over_confs_shows_what_no_conf_checks():
    confs = [{"rule": ["inv_*"]}, {"rule": ["transfer_moves"]}]
    checked = {r for c in confs for r in checked_rules(c, _DECLARED)}
    assert [r for r in _DECLARED if r not in checked] == ["deposit_mints", "sanity"]


def test_a_malformed_selection_is_rejected():
    with pytest.raises(ValueError, match="'rule'"):
        checked_rules({"rule": 3}, _DECLARED)
