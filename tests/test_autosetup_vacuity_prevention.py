"""
Tests for the autosetup-side vacuity prevention:

- NONDET recipes never match state-mutating (incl. payable) methods
  (``SummarySetup._nondet_ineligible`` + the recipe-level mutability bounds);
- native value transfers are detected from the solc AST dump, not source text
  (``native_value_transfers``), and only selector-less unresolved calls count as
  unresolvable (``unresolved_selectorless_calls``);
- ``ConfigManager`` merges recommended flags only through its whitelist.
"""
import json
from typing import cast

import pytest

from certora_autosetup.setup.call_resolution import unresolved_selectorless_calls
from certora_autosetup.setup.native_value_transfers import (
    ValueTransferKind,
    classify_native_value_transfer,
    find_native_value_transfer_sites,
)
from certora_autosetup.setup.setup_summaries import Recipe, RecipeType, SummarySetup
from certora_autosetup.utils.enhanced_config_manager import ConfigManager

from prover_output_utility.models import CallResolutionInfo, StoragePathInfo


# ---------------------------------------------------------------------------
# NONDET recipe eligibility
# ---------------------------------------------------------------------------


_NONDET_RECIPE = Recipe(
    recipe_type=RecipeType.CUSTOM,
    characteristic="anything",
    properties={},
    summary_type="NONDET",
)


def _method(name: str, mutability: str | None, contract: str = "Vault") -> dict:
    method = {"contractName": contract, "name": name}
    if mutability is not None:
        method["stateMutability"] = mutability
    return method


class TestNondetIneligible:
    def test_payable_excluded(self):
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, _method("deposit", "payable"))

    def test_state_mutating_excluded(self):
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, _method("withdraw", "nonpayable"))

    def test_missing_mutability_excluded(self):
        """Fail-closed: no stateMutability at all is treated as state-mutating."""
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, _method("mystery", None))

    def test_view_and_pure_allowed(self):
        for mutability in ("view", "pure"):
            assert not SummarySetup._nondet_ineligible(_NONDET_RECIPE, _method("peek", mutability))

    def test_non_nondet_recipe_unaffected(self):
        recipe = Recipe(
            recipe_type=RecipeType.NEW_CONTRACT,
            characteristic="anything",
            properties={},
            summary_type="HAVOC_ALL_DELETE",
        )
        assert not SummarySetup._nondet_ineligible(recipe, _method("deploy", "payable"))

    def test_unknown_summary_type_unaffected(self):
        """A custom recipe with an unrecognized summary type isn't NONDET-producing."""
        recipe = Recipe(
            recipe_type=RecipeType.CUSTOM,
            characteristic="anything",
            properties={},
            summary_type="SOMETHING_ELSE",
        )
        assert not SummarySetup._nondet_ineligible(recipe, _method("deposit", "payable"))

    def test_case_insensitive_like_emit_path(self):
        recipe = Recipe(
            recipe_type=RecipeType.CUSTOM,
            characteristic="anything",
            properties={},
            summary_type="nondet",
        )
        assert SummarySetup._nondet_ineligible(recipe, _method("deposit", "payable"))


def test_default_nondet_recipes_bounded_to_view_pure():
    """Every default NONDET recipe must declaratively restrict stateMutability to
    view/pure — the recipe-level arm of the match-time filter."""
    # Unbound call with a stub self: SummarySetup.__init__ requires prebuilt
    # compilation-analysis artifacts, and _build_recipes only touches self.log
    # (and only on the custom-recipe branch, unused here).
    class _StubSetup:
        def log(self, *args, **kwargs):
            pass

    stub = cast(SummarySetup, _StubSetup())
    for recipe in SummarySetup._build_recipes(stub, None):
        if recipe.summary_type.upper() != "NONDET":
            continue
        bound = recipe.properties.get("stateMutability")
        bound_values = bound if isinstance(bound, list) else [bound]
        assert set(bound_values) <= {"view", "pure"}, (
            f"NONDET recipe {recipe.recipe_type} admits non-view/pure methods: {bound!r}"
        )


# ---------------------------------------------------------------------------
# Native value-transfer AST classification
# ---------------------------------------------------------------------------


def _typed(type_identifier: str) -> dict:
    return {"nodeType": "Identifier", "typeDescriptions": {"typeIdentifier": type_identifier}}


def _member_call(member: str, base: dict, arguments: list | None = None) -> dict:
    return {
        "nodeType": "FunctionCall",
        "expression": {"nodeType": "MemberAccess", "memberName": member, "expression": base},
        "arguments": arguments if arguments is not None else [_typed("t_uint256")],
    }


def _call_with_options(names: list[str], arguments: list, base_type: str = "t_address_payable") -> dict:
    return {
        "nodeType": "FunctionCall",
        "expression": {
            "nodeType": "FunctionCallOptions",
            "names": names,
            "expression": {
                "nodeType": "MemberAccess",
                "memberName": "call",
                "expression": _typed(base_type),
            },
        },
        "arguments": arguments,
    }


_EMPTY_STRING_LITERAL = {"nodeType": "Literal", "kind": "string", "value": "", "hexValue": ""}


class TestClassifyNativeValueTransfer:
    def test_transfer_on_address_payable(self):
        node = _member_call("transfer", _typed("t_address_payable"))
        assert classify_native_value_transfer(node) is ValueTransferKind.TRANSFER

    def test_send_on_address_payable(self):
        node = _member_call("send", _typed("t_address_payable"))
        assert classify_native_value_transfer(node) is ValueTransferKind.SEND

    def test_call_with_value_and_empty_payload(self):
        node = _call_with_options(["value"], [_EMPTY_STRING_LITERAL])
        assert classify_native_value_transfer(node) is ValueTransferKind.CALL_WITH_VALUE

    def test_call_with_value_and_gas(self):
        node = _call_with_options(["gas", "value"], [_EMPTY_STRING_LITERAL], base_type="t_address")
        assert classify_native_value_transfer(node) is ValueTransferKind.CALL_WITH_VALUE

    def test_erc20_transfer_not_flagged(self):
        """`token.transfer(...)` is a plain external call on a contract-typed expression."""
        node = _member_call("transfer", _typed("t_contract$_Token_$123"))
        assert classify_native_value_transfer(node) is None

    def test_call_without_value_option_not_flagged(self):
        node = _call_with_options(["gas"], [_EMPTY_STRING_LITERAL])
        assert classify_native_value_transfer(node) is None

    def test_call_with_value_and_nonliteral_payload_not_flagged(self):
        """Whether a bytes variable is empty isn't statically decidable — no overclaiming."""
        node = _call_with_options(["value"], [_typed("t_bytes_memory_ptr")])
        assert classify_native_value_transfer(node) is None

    def test_call_with_value_and_nonempty_literal_not_flagged(self):
        payload = {"nodeType": "Literal", "kind": "hexString", "value": None, "hexValue": "1234"}
        node = _call_with_options(["value"], [payload])
        assert classify_native_value_transfer(node) is None

    def test_non_call_nodes_not_flagged(self):
        assert classify_native_value_transfer({"nodeType": "MemberAccess"}) is None
        assert classify_native_value_transfer("not a node") is None
        assert classify_native_value_transfer(None) is None


# ---------------------------------------------------------------------------
# Site discovery over the AST dump
# ---------------------------------------------------------------------------


_VAULT_SOURCE = (
    "contract Vault {\n"
    "    function withdraw(address payable to, uint256 amount) external {\n"
    "        to.transfer(amount);\n"
    "    }\n"
    "}\n"
)


def _write_ast_dump(tmp_path, stamped_contract: str = "Vault", units: int = 1):
    """A minimal all_asts.json with one transfer site in src/Vault.sol."""
    source_path = tmp_path / "src" / "Vault.sol"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(_VAULT_SOURCE)

    offset = _VAULT_SOURCE.index("to.transfer(amount)")
    node = _member_call("transfer", _typed("t_address_payable"))
    node["src"] = f"{offset}:19:0"
    node["certora_contract_name"] = stamped_contract

    dump = {
        f"unit{i}.sol": {str(source_path): {"7": node, "8": {"nodeType": "PragmaDirective"}}}
        for i in range(units)
    }
    ast_path = tmp_path / "all_asts.json"
    ast_path.write_text(json.dumps(dump))
    return ast_path


class TestFindNativeValueTransferSites:
    def test_site_found_with_line(self, tmp_path):
        ast_path = _write_ast_dump(tmp_path)
        sites = find_native_value_transfer_sites(ast_path, {"Vault"}, tmp_path)
        assert len(sites) == 1
        site = sites[0]
        assert site.contract == "Vault"
        assert site.file == "src/Vault.sol"
        assert site.line == 3
        assert site.kind is ValueTransferKind.TRANSFER

    def test_contract_not_in_scene_ignored(self, tmp_path):
        ast_path = _write_ast_dump(tmp_path, stamped_contract="Unrelated")
        assert find_native_value_transfer_sites(ast_path, {"Vault"}, tmp_path) == []

    def test_deduped_across_compilation_units(self, tmp_path):
        ast_path = _write_ast_dump(tmp_path, units=3)
        sites = find_native_value_transfer_sites(ast_path, {"Vault"}, tmp_path)
        assert len(sites) == 1

    def test_missing_dump_is_empty(self, tmp_path):
        assert find_native_value_transfer_sites(tmp_path / "nope.json", {"Vault"}, tmp_path) == []


# ---------------------------------------------------------------------------
# Selector-less unresolved calls
# ---------------------------------------------------------------------------


def _call(selector: str | None = None, storage_path: StoragePathInfo | None = None) -> CallResolutionInfo:
    return CallResolutionInfo(
        callee_name="[?].[?]",
        caller_name="Vault.withdraw(uint256)",
        call_site_snippet="",
        source_location="src/Vault.sol:3",
        summary="AUTO havoc",
        is_warning=True,
        callee_resolution="UNRESOLVED",
        selector=selector,
        storage_path=storage_path,
    )


class TestUnresolvedSelectorlessCalls:
    def test_selectorless_call_counts(self):
        calls = [_call()]
        assert unresolved_selectorless_calls(calls) == calls

    def test_selector_carrying_call_excluded(self):
        """The dispatcher can act on a selector — not unresolvable, just unimplemented."""
        assert unresolved_selectorless_calls([_call(selector="0xa9059cbb")]) == []

    def test_storage_path_call_excluded(self):
        """The linker can act on a storage path."""
        path = StoragePathInfo(base_contract="Vault", path="token", alternative_callees=[])
        assert unresolved_selectorless_calls([_call(storage_path=path)]) == []


# ---------------------------------------------------------------------------
# ConfigManager extra-flags whitelist
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path) -> ConfigManager:
    return ConfigManager(project_root=tmp_path)


def _create(manager, tmp_path, **kwargs):
    spec = tmp_path / "certora" / "specs" / "Vault.spec"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("// spec")
    conf_path = tmp_path / "Vault.conf"
    return manager.create_config(
        "Vault", [], [], spec, conf_path=conf_path, **kwargs
    )


class TestExtraFlags:
    def test_create_config_merges_whitelisted(self, manager, tmp_path):
        created = _create(manager, tmp_path, extra_flags={"optimistic_fallback": True})
        conf = json.loads(created.path.read_text())
        assert conf["optimistic_fallback"] is True

    def test_create_config_rejects_non_whitelisted(self, manager, tmp_path):
        # rule_sanity in particular: vacuity detection depends on the forced value.
        for flag in ("rule_sanity", "optimistic_loop", "loop_iter", "contract_recursion_limit"):
            with pytest.raises(ValueError):
                _create(manager, tmp_path, extra_flags={flag: True})

    def test_create_config_rejects_ill_typed_values(self, manager, tmp_path):
        with pytest.raises(ValueError):
            _create(manager, tmp_path, extra_flags={"optimistic_fallback": 1})

    def test_apply_extra_flags_updates_existing_conf(self, manager, tmp_path):
        created = _create(manager, tmp_path)
        manager.apply_extra_flags(created.path, {"optimistic_fallback": True})
        conf = json.loads(created.path.read_text())
        assert conf["optimistic_fallback"] is True

    def test_apply_extra_flags_rejects_non_whitelisted(self, manager, tmp_path):
        created = _create(manager, tmp_path)
        with pytest.raises(ValueError):
            manager.apply_extra_flags(created.path, {"rule_sanity": "none"})
