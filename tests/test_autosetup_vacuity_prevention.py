"""
Tests for the autosetup-side vacuity prevention (plan Part D):

- NONDET recipes never match payable / state-mutating methods
  (``SummarySetup._nondet_ineligible`` + the recipe-level mutability bounds);
- unresolved low-level value transfers yield an ``optimistic_fallback``
  recommendation (``is_unresolved_value_transfer``);
- ``ConfigManager`` merges recommended flags only through its whitelist.
"""
import json

import pytest

from certora_autosetup.setup.call_resolution import is_unresolved_value_transfer
from certora_autosetup.setup.setup_summaries import Recipe, RecipeType, SummarySetup
from certora_autosetup.utils.enhanced_config_manager import ConfigManager

from prover_output_utility.models import CallResolutionInfo


# ---------------------------------------------------------------------------
# NONDET recipe eligibility
# ---------------------------------------------------------------------------


_NONDET_RECIPE = Recipe(
    recipe_type=RecipeType.CUSTOM,
    characteristic="anything",
    properties={},
    summary_type="NONDET",
)


def _method(name: str, mutability: str, contract: str = "Bank") -> dict:
    return {"contractName": contract, "name": name, "stateMutability": mutability}


class TestNondetIneligible:
    def test_payable_excluded(self):
        m = _method("deposit", "payable")
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, m, {("Bank", "deposit")})

    def test_payable_excluded_even_without_key(self):
        """The mutability fallback catches a payable method missing from the key set."""
        m = _method("deposit", "payable")
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, m, set())

    def test_state_mutating_excluded(self):
        m = _method("withdraw", "nonpayable")
        assert SummarySetup._nondet_ineligible(_NONDET_RECIPE, m, set())

    def test_view_and_pure_allowed(self):
        for mutability in ("view", "pure"):
            m = _method("peek", mutability)
            assert not SummarySetup._nondet_ineligible(_NONDET_RECIPE, m, set())

    def test_non_nondet_recipe_unaffected(self):
        recipe = Recipe(
            recipe_type=RecipeType.NEW_CONTRACT,
            characteristic="anything",
            properties={},
            summary_type="HAVOC_ALL_DELETE",
        )
        m = _method("deploy", "payable")
        assert not SummarySetup._nondet_ineligible(recipe, m, {("Bank", "deploy")})


def test_default_nondet_recipes_bounded_to_view_pure():
    """Every default NONDET recipe must declaratively restrict stateMutability to
    view/pure — the recipe-level arm of the payable-NONDET guard."""
    # Unbound call with a stub self: SummarySetup.__init__ requires prebuilt
    # compilation-analysis artifacts, and _build_recipes only touches self.log
    # (and only on the custom-recipe branch, unused here).
    class _StubSetup:
        def log(self, *args, **kwargs):
            pass

    for recipe in SummarySetup._build_recipes(_StubSetup(), None):
        if recipe.summary_type.upper() != "NONDET":
            continue
        bound = recipe.properties.get("stateMutability")
        bound_values = bound if isinstance(bound, list) else [bound]
        assert set(bound_values) <= {"view", "pure"}, (
            f"NONDET recipe {recipe.recipe_type} admits non-view/pure methods: {bound!r}"
        )


# ---------------------------------------------------------------------------
# optimistic_fallback recommendation predicate
# ---------------------------------------------------------------------------


def _call(snippet: str, callee: str = "[?].[?]", selector: str | None = None) -> CallResolutionInfo:
    return CallResolutionInfo(
        callee_name=callee,
        caller_name="Bank.withdraw(uint256)",
        call_site_snippet=snippet,
        source_location="src/Bank.sol:42",
        summary="AUTO havoc",
        is_warning=True,
        callee_resolution="UNRESOLVED",
        selector=selector,
    )


class TestUnresolvedValueTransfer:
    def test_call_value_flagged(self):
        assert is_unresolved_value_transfer(_call('recipient.call{value: amount}("")'))

    def test_call_value_with_spaces_flagged(self):
        assert is_unresolved_value_transfer(_call('recipient.call{ value : amount }("")'))

    def test_native_send_and_transfer_flagged(self):
        assert is_unresolved_value_transfer(_call("payable(to).send(amount)"))
        assert is_unresolved_value_transfer(_call("payable(to).transfer(amount)"))

    def test_erc20_transfer_with_selector_not_flagged(self):
        """High-level token.transfer carries a resolvable selector — not a native transfer."""
        call = _call(
            "token.transfer(to, amount)",
            callee="[?].transfer(address,uint256)",
            selector="0xa9059cbb",
        )
        assert not is_unresolved_value_transfer(call)

    def test_plain_unresolved_call_not_flagged(self):
        assert not is_unresolved_value_transfer(
            _call("oracle.latestAnswer()", callee="[?].latestAnswer()")
        )


# ---------------------------------------------------------------------------
# ConfigManager extra-flags whitelist
# ---------------------------------------------------------------------------


@pytest.fixture
def manager(tmp_path) -> ConfigManager:
    return ConfigManager(project_root=tmp_path)


def _create(manager, tmp_path, **kwargs):
    spec = tmp_path / "certora" / "specs" / "Bank.spec"
    spec.parent.mkdir(parents=True, exist_ok=True)
    spec.write_text("// spec")
    conf_path = tmp_path / "Bank.conf"
    return manager.create_config(
        "Bank", [], [], spec, conf_path=conf_path, **kwargs
    )


class TestExtraFlags:
    def test_create_config_merges_whitelisted(self, manager, tmp_path):
        created = _create(
            manager, tmp_path,
            extra_flags={"optimistic_fallback": True, "contract_recursion_limit": 2},
        )
        conf = json.loads(created.path.read_text())
        assert conf["optimistic_fallback"] is True
        assert conf["contract_recursion_limit"] == 2

    def test_create_config_rejects_non_whitelisted(self, manager, tmp_path):
        # rule_sanity in particular: vacuity detection depends on the forced value.
        for flag in ("rule_sanity", "optimistic_loop", "loop_iter"):
            with pytest.raises(ValueError):
                _create(manager, tmp_path, extra_flags={flag: True})

    def test_create_config_rejects_ill_typed_values(self, manager, tmp_path):
        with pytest.raises(ValueError):
            _create(manager, tmp_path, extra_flags={"optimistic_fallback": 1})
        with pytest.raises(ValueError):
            _create(manager, tmp_path, extra_flags={"contract_recursion_limit": True})
        with pytest.raises(ValueError):
            _create(manager, tmp_path, extra_flags={"contract_recursion_limit": 99})

    def test_apply_extra_flags_updates_existing_conf(self, manager, tmp_path):
        created = _create(manager, tmp_path)
        manager.apply_extra_flags(created.path, {"optimistic_fallback": True})
        conf = json.loads(created.path.read_text())
        assert conf["optimistic_fallback"] is True

    def test_apply_extra_flags_rejects_non_whitelisted(self, manager, tmp_path):
        created = _create(manager, tmp_path)
        with pytest.raises(ValueError):
            manager.apply_extra_flags(created.path, {"rule_sanity": "none"})
