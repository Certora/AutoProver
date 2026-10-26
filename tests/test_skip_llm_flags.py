"""--skip-llm turns off every LLM call; --skip-llm-summaries turns off the LLM summary generation."""

from pathlib import Path

from certora_autosetup.autosetup.cli_args import create_parser
from certora_autosetup.setup.setup_prover import SetupProver


def _setup_prover(*, skip_llm: bool, skip_llm_summaries: bool) -> SetupProver:
    return SetupProver(
        log=lambda msg, level="INFO": None,
        certora_dir=Path("certora"),
        script_dir=Path("."),
        additional_contracts=[],
        extra_args=[],
        skip_llm=skip_llm,
        force_llm_regenerate=False,
        stop_after_summaries=False,
        scope=None,
        skip_llm_summaries=skip_llm_summaries,
    )


def test_flags_default_off() -> None:
    args = create_parser().parse_args(["--main-contract", "src/A.sol:A"])
    assert not args.skip_llm
    assert not args.skip_llm_summaries


def test_flags_parse_independently() -> None:
    args = create_parser().parse_args(["--skip-llm-summaries", "--main-contract", "src/A.sol:A"])
    assert args.skip_llm_summaries and not args.skip_llm
    args = create_parser().parse_args(["--skip-llm", "--main-contract", "src/A.sol:A"])
    assert args.skip_llm and not args.skip_llm_summaries


def test_setup_prover_keeps_both_flags() -> None:
    sp = _setup_prover(skip_llm=False, skip_llm_summaries=True)
    assert sp.skip_llm_summaries and not sp.skip_llm
