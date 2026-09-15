"""Unit tests for the null Soroban backend (``composer/spec/soroban/null_backend.py``).

The null backend is a pure test double for the Soroban front half — it records extracted
properties without verifying them. These tests exercise it in isolation: no LLM, no Postgres,
no prover. (The end-to-end live gate that drives real models through it is
``tests/test_soroban_gate.py``, marked ``expensive``.)
"""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from composer.spec.soroban.model import (
    SorobanApplication,
    SorobanComponentInstance,
    SorobanContractInstance,
    SorobanContract,
    SorobanFunction,
    ContractComponent,
)

from composer.spec.soroban.null_backend import (
    NullArtifact,
    NullResult,
    NullSorobanArtifactStore,
    NullSorobanBackend,
    NullSorobanFormalizer,
    NullSorobanPrepared,
    SOROBAN_NULL_GUIDANCE,
    SorobanPhase,
)
from composer.spec.types import ProgramName, PropertyFormulation, RustIdentifier

from .test_soroban_components import _app

def _program_instance() -> SorobanContractInstance:
    return SorobanContractInstance(0, _app())


def _unit() -> SorobanComponentInstance:
    """The backend's ``Unit`` — a component, not the program (``Main`` is not a ``FeatureUnit``)."""
    return SorobanComponentInstance(0, _program_instance())


def _props() -> list[PropertyFormulation]:
    return [
        PropertyFormulation(
            title="balance_conserved", sort="invariant",
            description="The vault balance equals the sum of recorded deposits.",
        ),
        PropertyFormulation(
            title="only_authority_withdraws", sort="safety_property",
            description="Only the stored authority can reduce the vault balance.",
        ),
    ]


def _backend(project_root: str) -> NullSorobanBackend:
    return NullSorobanBackend(NullSorobanArtifactStore(project_root))


@pytest.mark.asyncio
async def test_formalize_echoes_properties_into_result():
    feat = _unit()
    props = _props()

    result = await NullSorobanFormalizer().formalize(
        "batch", feat, props, cast(Any, None), cast(Any, None), cast(Any, None)
    )

    assert isinstance(result, NullResult)
    # Every property is echoed back verbatim as its own single-rule mapping.
    assert result.property_checks() == [
        ("balance_conserved", ["balance_conserved"]),
        ("only_authority_withdraws", ["only_authority_withdraws"]),
    ]
    # Commentary records the unit and the count.
    assert feat.display_name in result.commentary
    assert "2 properties" in result.commentary
    # artifact_text is well-formed JSON carrying the same properties; there is no output link.
    parsed = json.loads(result.artifact_text)
    assert parsed["properties"] == [
        ["balance_conserved", ["balance_conserved"]],
        ["only_authority_withdraws", ["only_authority_withdraws"]],
    ]
    assert result.output_link is None


@pytest.mark.asyncio
async def test_formalize_with_no_properties_records_empty():
    result = await NullSorobanFormalizer().formalize(
        "batch", _unit(), [], cast(Any, None), cast(Any, None), cast(Any, None)
    )
    assert isinstance(result, NullResult)
    assert result.property_checks() == []
    assert "0 properties" in result.commentary


@pytest.mark.asyncio
async def test_fetch_verdicts_is_empty():
    # The null backend never verifies, so it surfaces no verdicts.
    assert await NullSorobanFormalizer().fetch_verdicts(cast(Any, None)) == {}


@pytest.mark.asyncio
async def test_prepare_system_locates_main_and_builds_formalizer(tmp_path):
    feat = _program_instance()
    backend = _backend(str(tmp_path))
    run = cast(Any, SimpleNamespace(source=SimpleNamespace(contract_name="vault")))

    prepared = await backend.prepare_system(feat.app, run, await backend.preflight(run))

    assert isinstance(prepared, NullSorobanPrepared)
    # prepare_system routes through SOROBAN.locate_main, so main is the matched program.
    assert isinstance(prepared.main, SorobanContractInstance)
    assert prepared.main.app == feat.app

    formalizer = await prepared.prepare_formalization(cast(Any, None))
    assert isinstance(formalizer, NullSorobanFormalizer)


def test_to_artifact_id_uses_unit_slug(tmp_path):
    feat = _unit()
    artifact = _backend(str(tmp_path)).to_artifact_id(feat)
    assert isinstance(artifact, NullArtifact)
    assert artifact.slug == feat.slug
    assert artifact.artifact_file == f"null_{feat.slug}.json"


def test_backend_declares_soroban_front_half(tmp_path):
    backend = _backend(str(tmp_path))
    assert backend.backend_guidance is SOROBAN_NULL_GUIDANCE
    assert backend.analysis_spec.analysis_key == "soroban-analysis"
    assert backend.analysis_spec.properties_key == "soroban-properties"
    assert {p.value for p in SorobanPhase} == {
        "analysis", "extraction", "formalization", "report",
    }
