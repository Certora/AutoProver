"""Unit tests for the null Solana backend (``composer/spec/solana/null_backend.py``).

The null backend is a pure test double for the Solana front half — it records extracted
properties without verifying them. These tests exercise it in isolation: no LLM, no Postgres,
no prover. (The end-to-end live gate that drives real models through it is
``tests/test_solana_gate.py``, marked ``expensive``.)
"""

import json
from types import SimpleNamespace
from typing import Any, cast

import pytest

from composer.spec.solana.model import (
    SolanaApplication,
    SolanaProgram,
    SolanaProgramInstance,
)
from composer.spec.solana.null_backend import (
    NullArtifact,
    NullResult,
    NullSolanaArtifactStore,
    NullSolanaBackend,
    NullSolanaFormalizer,
    NullSolanaPrepared,
    SOLANA_NULL_GUIDANCE,
    SolanaPhase,
)
from composer.spec.types import PropertyFormulation


def _program_instance() -> SolanaProgramInstance:
    program = SolanaProgram(
        name="Vault",
        program_identifier="vault",
        description="Holds deposits and releases them to the authority.",
        instructions=[],
    )
    app = SolanaApplication(
        application_type="Vault",
        description="A single-program token vault.",
        components=[program],
    )
    return SolanaProgramInstance(0, app)


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


def _backend(project_root: str) -> NullSolanaBackend:
    return NullSolanaBackend(NullSolanaArtifactStore(project_root))


@pytest.mark.asyncio
async def test_formalize_echoes_properties_into_result():
    feat = _program_instance()
    props = _props()

    result = await NullSolanaFormalizer().formalize(
        "batch", feat, props, cast(Any, None), cast(Any, None)
    )

    assert isinstance(result, NullResult)
    # Every property is echoed back verbatim as its own single-rule mapping.
    assert result.property_units() == [
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
    result = await NullSolanaFormalizer().formalize(
        "batch", _program_instance(), [], cast(Any, None), cast(Any, None)
    )
    assert isinstance(result, NullResult)
    assert result.property_units() == []
    assert "0 properties" in result.commentary


@pytest.mark.asyncio
async def test_fetch_verdicts_is_empty():
    # The null backend never verifies, so it surfaces no verdicts.
    assert await NullSolanaFormalizer().fetch_verdicts(cast(Any, None)) == {}


@pytest.mark.asyncio
async def test_prepare_system_locates_main_and_builds_formalizer(tmp_path):
    feat = _program_instance()
    backend = _backend(str(tmp_path))
    run = cast(Any, SimpleNamespace(source=SimpleNamespace(contract_name="vault")))

    prepared = await backend.prepare_system(feat.app, run)

    assert isinstance(prepared, NullSolanaPrepared)
    # prepare_system routes through SOLANA.locate_main, so main is the matched program.
    assert isinstance(prepared.main, SolanaProgramInstance)
    assert prepared.main.program.program_identifier == "vault"

    formalizer = await prepared.prepare_formalization(cast(Any, None))
    assert isinstance(formalizer, NullSolanaFormalizer)


def test_to_artifact_id_uses_unit_slug(tmp_path):
    feat = _program_instance()
    artifact = _backend(str(tmp_path)).to_artifact_id(feat)
    assert isinstance(artifact, NullArtifact)
    assert artifact.slug == feat.slug
    assert artifact.artifact_file == f"null_{feat.slug}.json"


def test_backend_declares_solana_front_half(tmp_path):
    backend = _backend(str(tmp_path))
    assert backend.backend_guidance is SOLANA_NULL_GUIDANCE
    assert backend.analysis_spec.analysis_key == "solana-analysis"
    assert backend.analysis_spec.properties_key == "solana-properties"
    assert {p.value for p in SolanaPhase} == {
        "analysis", "extraction", "formalization", "report",
    }
