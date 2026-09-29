"""A rule the Prover decides by static analysis alone keeps its verdict in the parse.

Such a rule arrives in the tree view as a bare root: a status, an output file, and no children.
The parser used to recurse into its children, find none, and yield nothing, so the rule vanished
from the results. Found with a satisfy rule whose branch was statically unreachable
(`tests/test_cvlr_dropped_writes.py`).
"""

from pathlib import Path

from composer.prover.results import RuleNodeModel, flatten_tree_view_root, trace_shape

STATIC = "The rule result was successfully determined without running SMT solver (i.e. solely by static analysis)"


def _static_root(status: str) -> RuleNodeModel:
    return RuleNodeModel.model_validate({
        "name": "rule_decided_statically",
        "output": ["rule_output_2.json"],
        "children": [],
        "status": status,
        "nodeType": "ROOT",
        "errors": [{"severity": "info", "message": STATIC}],
    })


def test_a_statically_violated_rule_keeps_its_verdict():
    results = list(flatten_tree_view_root(Path("."), _static_root("VIOLATED"), trace_shape("solana")))
    assert [(r.path.rule, r.status) for r in results] == [("rule_decided_statically", "VIOLATED")]


def test_a_statically_verified_rule_keeps_its_verdict():
    results = list(flatten_tree_view_root(Path("."), _static_root("VERIFIED"), trace_shape("solana")))
    assert [(r.path.rule, r.status) for r in results] == [("rule_decided_statically", "VERIFIED")]
