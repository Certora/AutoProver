"""The explorer system prompt is per-ecosystem: shared protocol, chain look-fors."""

import re
from pathlib import Path

import pytest

from composer.pipeline.ecosystem import EVM, SOLANA, SOROBAN
from composer.spec.code_explorer import (
    CodeExplorerPromptParams,
    PriorFindingsMode,
    code_explorer_sys_prompt,
)
from composer.spec.gen_types import TypedTemplate
from composer.templates.loader import load_jinja_template


PROTOCOL = "deliver your answer via the `result` tool"
DO_NOT_GUESS = "*DO NOT GUESS*"
ESTABLISHED = "established facts"
VERSIONED = "(potentially) out of date"

_MODES: tuple[PriorFindingsMode, ...] = ("none", "established", "versioned")


def _render(
    template: TypedTemplate[CodeExplorerPromptParams], mode: PriorFindingsMode
) -> str:
    return code_explorer_sys_prompt(template, mode)(load_jinja_template)


@pytest.mark.parametrize("ecosystem", [EVM, SOLANA, SOROBAN], ids=["evm", "solana", "soroban"])
@pytest.mark.parametrize("mode", _MODES)
def test_shared_protocol(ecosystem, mode: PriorFindingsMode):
    text = _render(ecosystem.code_explorer_prompt, mode)
    assert PROTOCOL in text
    assert DO_NOT_GUESS in text
    assert (ESTABLISHED in text) is (mode == "established")
    assert (VERSIONED in text) is (mode == "versioned")


def test_evm_cites_solidity_not_chain_lookfors():
    text = _render(EVM.code_explorer_prompt, "none")
    assert "function signatures" in text
    assert "state variable" in text
    assert "PDA" not in text
    assert "require_auth" not in text


def test_solana_cites_pdas_not_soroban_auth():
    text = _render(SOLANA.code_explorer_prompt, "none")
    assert "Cargo.toml" in text
    assert "PDA" in text
    assert "CPI" in text
    assert "require_auth" not in text


# Terms that belong on an ecosystem (or language) template, not in the shared
# explorer protocol. ``contract``/``program`` are the usual leaks: EVM/Soroban
# say one, Solana the other.
_CHAIN_TERMS = re.compile(
    r"\b(solidity|evm|solana|soroban|anchor|pda|cpi|require_auth|"
    r"msg\.sender|modifier|contract|program|instruction|signer|account)s?\b",
    re.IGNORECASE,
)

_SHARED_EXPLORER_DIR = Path(__file__).resolve().parent.parent / "composer" / "templates" / "code_explorer"
_SHARED_EXPLORER_TEMPLATES = (
    "common_fragment.j2",
    "index_addendum_fragment.j2",
    "versioned_index_addendum_fragment.j2",
    "prior_findings_fragment.j2",
    "rust/common_fragment.j2",
)


@pytest.mark.parametrize("rel", _SHARED_EXPLORER_TEMPLATES)
def test_shared_explorer_templates_are_chain_neutral(rel: str):
    text = (_SHARED_EXPLORER_DIR / rel).read_text()
    hits = sorted({m.group(0).lower() for m in _CHAIN_TERMS.finditer(text)})
    assert hits == [], f"{rel} leaks chain terminology: {hits}"


def test_soroban_cites_auth_and_storage_kind_not_pdas():
    text = _render(SOROBAN.code_explorer_prompt, "none")
    assert "Cargo.toml" in text
    assert "require_auth" in text
    assert "DataKey" in text
    assert "PDA" not in text


@pytest.mark.parametrize(
    "template,language",
    [
        ("application_analysis_system.j2", "Solidity"),
        ("solana/analysis_system.j2", "Rust"),
        ("solana/property_system.j2", "Rust"),
        ("soroban/analysis_system.j2", "Rust"),
        ("soroban/property_system.j2", "Rust"),
    ],
)
def test_caller_guidance_names_the_source_language(template: str, language: str):
    text = load_jinja_template(
        template, sort="existing", has_doc=False, backend_guidance=""
    )
    assert f"The {language} language itself" in text
    if language != "Solidity":
        assert "The Solidity language itself" not in text
