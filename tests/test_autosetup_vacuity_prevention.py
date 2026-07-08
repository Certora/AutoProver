"""
Tests for the autosetup-side vacuity prevention:

- native value transfers are detected from the solc AST dump, not source text
  (``native_value_transfers``), and only selector-less unresolved calls count as
  unresolvable (``unresolved_selectorless_calls``);
- ``ConfigManager`` merges recommended flags only through its whitelist.
"""
import json

import pytest

from certora_autosetup.setup.call_resolution import unresolved_selectorless_calls
from certora_autosetup.setup.native_value_transfers import (
    ValueTransferKind,
    classify_native_value_transfer,
    find_native_value_transfer_sites,
)
from certora_autosetup.utils.enhanced_config_manager import ConfigManager

from prover_output_utility.models import CallResolutionInfo, StoragePathInfo


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
