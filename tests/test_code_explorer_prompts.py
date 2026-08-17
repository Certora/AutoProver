"""The explorer system prompt is per-ecosystem: shared protocol, chain look-fors."""

import re
from pathlib import Path

import pytest

from composer.pipeline.ecosystem import EVM, SOLANA, SOROBAN
from composer.spec.agent_index import AgentIndex
from composer.spec.code_explorer import PriorFindingsMode, render_code_explorer_prompt
from composer.templates.loader import load_jinja_template


def _norm(text: str) -> str:
    return re.sub(r"\s+", " ", text).strip()


# Pre-template EVM explorer prompt (composer/spec/code_explorer.py + live_explorer.py).
_EVM_BASE = """\
You are a code exploration assistant analyzing smart contract source code.
You have access to file tools (list_files, get_file, grep_files) to explore the project.

Your job is to answer a specific question about the codebase thoroughly and precisely.

Guidelines:
- Ground every claim in what you find in the source code.
- Include relevant function signatures, state variable declarations, or code snippets in your answer.
- If the question asks about behavior, trace through the actual implementation rather than speculating.
- Be concise: the caller needs a dense, actionable answer, not a walkthrough of your exploration process.
- If you discover you do not have enough information to fully answer the question, 
  (e.g., there is a reference to code not available to you) *DO NOT GUESS*. Indicate in your final answer
  that you cannot fully answer the question due to incomplete information.

If asked a question that cannot be answered by simply looking at the code (e.g., about some completely unrelated
topic) you must decline to answer, indicating it is out of scope for what you're capable of answering.

When complete, deliver your answer via the `result` tool.
"""

_EVM_ESTABLISHED = _EVM_BASE + f"""
You have access to findings from prior analyses of this codebase.
These findings were produced by earlier agents investigating the same contracts
and are established facts — do not re-derive or re-verify them.

{AgentIndex.WITH_INDEX_SYS_COMMON}
"""

_EVM_VERSIONED = _EVM_BASE + """

You may be provided with other question/answer pairs that were found to be similar
to the question you are asked. These question/answer pairs *may* have been derived
on a prior version of the codebase that you are exploring now; such pairs will be clearly
marked as being (potentially) out of date. Use the following protocol to use these
prior results effectively:

1. If a prior finding is *not* marked as out of date, and directly answers the question you are asked,
   use that answer as is; do not rephrase, re-investigate, or "verify" the answer
2. If a prior finding is *not* marked as out of date, and *partially* answers the question you are asked,
   use that answer as a verified starting point and fill in any missing details.

If a prior question/answer pair that is marked as (potentially stale)
either completely or partially answers the question posed to you, you *should*
use your source tools to determine if the substantive and relevant details of the answer
are still true on this version of the code. If you verify that these details
remain true, you may reuse (in part or in whole) the existing answer as you would
an up-to-date answer.
"""


PROTOCOL = "deliver your answer via the `result` tool"
DO_NOT_GUESS = "*DO NOT GUESS*"
ESTABLISHED = "established facts"
VERSIONED = "(potentially) out of date"

_MODES: tuple[PriorFindingsMode, ...] = ("none", "established", "versioned")


@pytest.mark.parametrize("ecosystem", [EVM, SOLANA, SOROBAN], ids=["evm", "solana", "soroban"])
@pytest.mark.parametrize("mode", _MODES)
def test_shared_protocol(ecosystem, mode: PriorFindingsMode):
    text = render_code_explorer_prompt(ecosystem.code_explorer_prompt, mode)
    assert PROTOCOL in text
    assert DO_NOT_GUESS in text
    assert (ESTABLISHED in text) is (mode == "established")
    assert (VERSIONED in text) is (mode == "versioned")


@pytest.mark.parametrize(
    "mode,expected",
    [
        ("none", _EVM_BASE),
        ("established", _EVM_ESTABLISHED),
        ("versioned", _EVM_VERSIONED),
    ],
)
def test_evm_prompt_matches_pre_template_text(mode: PriorFindingsMode, expected: str):
    # Wording must stay the EVM original; only whitespace may drift through j2 includes.
    assert _norm(render_code_explorer_prompt(EVM.code_explorer_prompt, mode)) == _norm(expected)


def test_evm_cites_solidity_not_chain_lookfors():
    text = render_code_explorer_prompt(EVM.code_explorer_prompt, "none")
    assert "function signatures" in text
    assert "state variable" in text
    assert "PDA" not in text
    assert "require_auth" not in text


def test_solana_cites_pdas_not_soroban_auth():
    text = render_code_explorer_prompt(SOLANA.code_explorer_prompt, "none")
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
    "_common.j2",
    "_index_addendum.j2",
    "_versioned_index_addendum.j2",
    "_prior_findings.j2",
    "rust/_common.j2",
)


@pytest.mark.parametrize("rel", _SHARED_EXPLORER_TEMPLATES)
def test_shared_explorer_templates_are_chain_neutral(rel: str):
    text = (_SHARED_EXPLORER_DIR / rel).read_text()
    hits = sorted({m.group(0).lower() for m in _CHAIN_TERMS.finditer(text)})
    assert hits == [], f"{rel} leaks chain terminology: {hits}"


def test_soroban_cites_auth_and_storage_kind_not_pdas():
    text = render_code_explorer_prompt(SOROBAN.code_explorer_prompt, "none")
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
