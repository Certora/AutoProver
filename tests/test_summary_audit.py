"""Tests for the payable view-summary audit over the typed CVL AST."""
import json

from composer.cvl.schema import (
    AlwaysSummary,
    BoolLiteral,
    CatchAllSummary,
    CVLFile,
    DispatcherSummary,
    ExpressionSummary,
    HavocingSummary,
    ImportedFunction,
    KeywordSummary,
    MethodReference,
    MethodSignature,
    MethodsBlock,
    NumberLiteral,
)
from composer.cvl.summary_audit import load_payable_methods, view_summary_violations


def _entry(method: str, contract: str | None, summary) -> ImportedFunction:
    return ImportedFunction(
        type="imported_function",
        signature=MethodSignature(
            method_ref=MethodReference(contract=contract, method_name=method),
            parameters=[],
            return_types=[],
            visibility="external",
            post_flags=[],
        ),
        summary=summary,
    )


def _spec(*entries) -> CVLFile:
    return CVLFile(
        import_specs=[],
        import_contract=[],
        blocks=[MethodsBlock(type="methods_block", method_entries=list(entries))],
    )


_NONDET = HavocingSummary(type="havocing", havoc_keyword="nondet")
_PAYABLE = {"Vault": frozenset({"deposit"}), "Router": frozenset({"swap"})}


class TestViewSummaryViolations:
    def test_nondet_on_payable_wildcard(self):
        spec = _spec(_entry("deposit", "_", _NONDET))
        violations = view_summary_violations(spec, _PAYABLE, "Main")
        assert len(violations) == 1 and "Vault.deposit" in violations[0]

    def test_nondet_on_payable_named_contract(self):
        spec = _spec(_entry("swap", "Router", _NONDET))
        assert len(view_summary_violations(spec, _PAYABLE, "Main")) == 1

    def test_current_contract_reference(self):
        spec = _spec(_entry("deposit", None, _NONDET))
        assert view_summary_violations(spec, _PAYABLE, "Vault")
        assert not view_summary_violations(spec, _PAYABLE, "Main")

    def test_view_method_allowed(self):
        spec = _spec(_entry("balanceOf", "_", _NONDET))
        assert not view_summary_violations(spec, _PAYABLE, "Main")

    def test_constant_and_always_flagged(self):
        constant = KeywordSummary(type="keyword", summary_keyword="constant")
        always = AlwaysSummary(type="always", expression=BoolLiteral(type="bool_literal", value=True))
        spec = _spec(_entry("deposit", "Vault", constant), _entry("swap", "Router", always))
        assert len(view_summary_violations(spec, _PAYABLE, "Main")) == 2

    def test_expression_dispatcher_and_havoc_allowed(self):
        expression = ExpressionSummary(type="expression", expression=NumberLiteral(type="number_literal", value="0"))
        dispatcher = DispatcherSummary(type="dispatcher", optimistic=True, use_fallback=True)
        havoc = HavocingSummary(type="havocing", havoc_keyword="havoc_ecf")
        spec = _spec(
            _entry("deposit", "Vault", expression),
            _entry("swap", "Router", dispatcher),
            _entry("deposit", "_", havoc),
        )
        assert not view_summary_violations(spec, _PAYABLE, "Main")

    def test_catch_all_over_payable_contract(self):
        spec = _spec(CatchAllSummary(type="catch_all", contract_name="Vault", summary=_NONDET))
        violations = view_summary_violations(spec, _PAYABLE, "Main")
        assert len(violations) == 1 and "deposit" in violations[0]

    def test_catch_all_over_view_only_contract(self):
        spec = _spec(CatchAllSummary(type="catch_all", contract_name="Oracle", summary=_NONDET))
        assert not view_summary_violations(spec, _PAYABLE, "Main")

    def test_unsummarized_entry_ignored(self):
        spec = _spec(_entry("deposit", "Vault", None))
        assert not view_summary_violations(spec, _PAYABLE, "Main")


class TestLoadPayableMethods:
    def test_loads_external_and_public_payable(self, tmp_path):
        inventory = [
            {"contractName": "Vault", "name": "deposit", "stateMutability": "payable", "visibility": "external"},
            {"contractName": "Vault", "name": "sweep", "stateMutability": "payable", "visibility": "public"},
            {"contractName": "Vault", "name": "peek", "stateMutability": "view", "visibility": "external"},
            {"contractName": "Vault", "name": "stash", "stateMutability": "payable", "visibility": "internal"},
        ]
        path = tmp_path / ".certora_internal" / "all_methods.json"
        path.parent.mkdir(parents=True)
        path.write_text(json.dumps(inventory))
        assert load_payable_methods(tmp_path) == {"Vault": frozenset({"deposit", "sweep"})}

    def test_missing_inventory_returns_none(self, tmp_path):
        assert load_payable_methods(tmp_path) is None
