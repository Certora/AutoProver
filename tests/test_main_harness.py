"""Tests for the main-contract augmentation harness (composer.spec.source.harness).

Covers the pure pieces — pydantic cache back-compat (pre-change JSON without the
new optional fields must still validate), the `verify_contract_name`/`verify_contract_path`
accessors that thread the verify target through the pipeline, the prompt-facing
`api_lines`/`main_harness_view` helpers, the `GiveUpTool` sort classification defaults,
and rendering of the new/changed templates. No LLM / no prover / no DB.
"""

import pytest

from composer.spec.source.author import GaveUp
from composer.spec.source.harness import (
    AgentSystemDescription,
    HarnessDef,
    HarnessedContract,
    HelperDecomposition,
    MainHarnessPlan,
    MainHarnessView,
    SystemDescriptionHarnessed,
    UnstructuredSlotSpec,
    empty_main_harness_plan_error,
    main_harness_path_error,
)
from composer.spec.source.report.schema import GaveUpComponent
from composer.spec.system_model import HarnessedApplication, SourceApplication
from composer.templates.loader import load_jinja_template


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def _base_description_fields() -> dict:
    """The pre-change wire shape shared by both system-description models."""
    return {
        "non_trivial_state": "some non-trivial state",
        "transitive_closure": [],
        "erc20_contracts": [],
        "external_interfaces": [],
    }


def _plan() -> MainHarnessPlan:
    return MainHarnessPlan(
        unstructured_slots=[
            UnstructuredSlotSpec(
                getter_name="getVersionSlot",
                slot_derivation='keccak256("river.state.version") - 1',
                value_type="uint256",
                rationale="version invariants need to observe it",
            )
        ],
        decompositions=[
            HelperDecomposition(
                target_function="depositAndTransfer(address)",
                helpers={"helperDeposit": "performs the deposit accounting step"},
                rationale="each step must be verifiable in isolation",
            )
        ],
    )


def _main_harness_contract() -> HarnessedContract:
    return HarnessedContract(
        solidity_identifier="RiverHarness",
        link_fields=[],
        harness_definition=HarnessDef(
            harness_of="River",
            harness_source="contract RiverHarness is River {}",
        ),
        path="certora/harnesses/RiverHarness.sol",
    )


# ---------------------------------------------------------------------------
# Pydantic cache back-compat: pre-change JSON (no new fields) still validates
# ---------------------------------------------------------------------------

def test_agent_system_description_backcompat():
    desc = AgentSystemDescription.model_validate({
        **_base_description_fields(),
        "transitive_closure": [
            {"solidity_identifier": "Token", "link_fields": [], "num_instances": None}
        ],
    })
    assert desc.main_contract_harness is None
    assert not desc.needs_harnessing()


def test_system_description_harnessed_backcompat():
    desc = SystemDescriptionHarnessed.model_validate(_base_description_fields())
    assert desc.main_harness is None
    assert desc.main_harness_plan is None
    assert desc.main_harness_api() is None
    assert desc.main_harness_view() is None


def test_gave_up_backcompat_defaults_sort_other():
    gave_up = GaveUp.model_validate({"reason": "no solc"})
    assert gave_up.sort == "other"


def test_gave_up_component_backcompat():
    gc = GaveUpComponent.model_validate({"component": "Vault", "properties": []})
    assert gc.give_up_sort is None


# ---------------------------------------------------------------------------
# Verify-target accessors
# ---------------------------------------------------------------------------

def test_verify_contract_name_defaults_to_main_contract():
    desc = SystemDescriptionHarnessed.model_validate(_base_description_fields())
    assert desc.verify_contract_name("River") == "River"
    assert desc.verify_contract_path("src/River.sol") == "src/River.sol"


def test_verify_contract_name_prefers_main_harness():
    desc = SystemDescriptionHarnessed.model_validate(_base_description_fields())
    desc.main_harness = _main_harness_contract()
    desc.main_harness_plan = _plan()
    assert desc.verify_contract_name("River") == "RiverHarness"
    assert desc.verify_contract_path("src/River.sol") == "certora/harnesses/RiverHarness.sol"


def test_main_harness_api_and_view():
    desc = SystemDescriptionHarnessed.model_validate(_base_description_fields())
    desc.main_harness = _main_harness_contract()
    desc.main_harness_plan = _plan()

    api = desc.main_harness_api()
    assert api is not None
    # One line per getter, one per helper wrapper.
    assert len(api) == 2
    assert any("getVersionSlot" in line for line in api)
    assert any("helperDeposit" in line for line in api)

    view = desc.main_harness_view()
    assert view is not None
    assert view.name == "RiverHarness"
    assert view.harness_of == "River"
    assert view.api == api


def test_classifier_rejects_empty_harness_plan():
    # An all-empty plan should be delivered as null; this is the exact check the
    # classifier's result validator runs.
    err = empty_main_harness_plan_error(MainHarnessPlan(unstructured_slots=[], decompositions=[]))
    assert err is not None
    assert "deliver null" in err
    # A null plan and a non-empty plan both pass.
    assert empty_main_harness_plan_error(None) is None
    assert empty_main_harness_plan_error(_plan()) is None


def test_main_harness_delivery_confined_to_harness_dir():
    # The generation validator must reject deliveries outside certora/harnesses/
    # (write confinement alone doesn't stop delivering a pre-existing project file).
    assert main_harness_path_error("certora/harnesses/RiverHarness.sol") is None
    err = main_harness_path_error("src/River.sol")
    assert err is not None
    assert "certora/harnesses" in err


# ---------------------------------------------------------------------------
# Template rendering
# ---------------------------------------------------------------------------

class _Prop:
    sort = "safety"
    title = "version_is_monotone"
    description = "the version never decreases"


class _Spec:
    contract_name = "River"
    relative_path = "src/River.sol"


def test_main_harness_generation_prompt_renders():
    out = load_jinja_template(
        "main_harness_generation_prompt.j2",
        contract_name="River",
        relative_path="src/River.sol",
        harness_name="RiverHarness",
        plan=_plan(),
    )
    assert "contract RiverHarness is River" in out
    assert "getVersionSlot" in out
    assert "helperDeposit" in out
    assert "certora/harnesses/RiverHarness.sol" in out


def test_state_analysis_renders_main_harness_step():
    app = SourceApplication(application_type="Staking", description="desc", components=[])
    out = load_jinja_template(
        "state_analysis.j2",
        contract_name="River",
        relative_path="src/River.sol",
        context=app,
    )
    assert "main_contract_harness" in out
    assert "ERC-7201" in out


@pytest.mark.parametrize("with_api", [True, False])
def test_property_generation_prompt_harness_api(with_api: bool):
    api = _plan().api_lines() if with_api else None
    out = load_jinja_template(
        "property_generation_prompt.j2",
        properties=[_Prop()],
        contract_name="River",
        resources=[],
        context=None,
        harness_api=api,
    )
    assert ("<verification_harness>" in out) == with_api
    assert ("getVersionSlot" in out) == with_api


def test_property_judge_prompt_harness_api():
    out = load_jinja_template(
        "property_judge_prompt.j2",
        properties=[_Prop()],
        sort="existing",
        context=None,
        harness_api=_plan().api_lines(),
    )
    assert "<verification_harness>" in out
    assert "getVersionSlot" in out
    # Binding without harness_api at all (the NotRequired key) renders harness-free.
    out = load_jinja_template(
        "property_judge_prompt.j2",
        properties=[_Prop()],
        sort="existing",
        context=None,
    )
    assert "<verification_harness>" not in out


def test_judge_system_prompt_accepts_harness_augmentation_flag():
    # This PR only plumbs the variable; the gated wording lands separately. The
    # template must render regardless of the flag's value.
    for flag in (True, False):
        out = load_jinja_template(
            "property_judge_system_prompt.j2", sort="existing", harness_augmentation=flag,
        )
        assert out


@pytest.mark.parametrize("with_harness", [True, False])
def test_structural_invariant_prompt_main_harness(with_harness: bool):
    view = MainHarnessView(
        name="RiverHarness",
        path="certora/harnesses/RiverHarness.sol",
        harness_of="River",
        api=_plan().api_lines(),
    ) if with_harness else None
    app = HarnessedApplication(application_type="Staking", description="desc", components=[])
    out = load_jinja_template(
        "structural_invariant_prompt.j2",
        context=app,
        contract_spec=_Spec(),
        main_harness=view,
    )
    assert ("RiverHarness" in out) == with_harness
    assert ("getVersionSlot" in out) == with_harness
