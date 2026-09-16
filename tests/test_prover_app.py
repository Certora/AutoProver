"""Which Prover CLI a run submits to, and what a caller may name.

``certora_cli`` ships one entry point per Prover — ``certoraRun``, ``certoraSolanaProver``,
``certoraSorobanProver`` — each with its own build step, since one compiles Solidity and another
compiles a Rust crate. What makes them interchangeable everywhere else is the signature they share,
``list[str] -> CertoraRunResult | None``: cloud polling, the treeView parse and the verdict roll-up
never learn which one ran.
"""

import pytest

from composer.certora_env import CertoraEnvironmentError, import_prover_entry, prover_app
from composer.prover.core import ProverOptions, make_prover_options


def test_the_solana_cli_is_reachable_under_the_name_a_run_selects_it_by():
    assert import_prover_entry("solana").__name__ == "run_solana_prover"


def test_the_evm_cli_is_the_one_it_always_was():
    assert import_prover_entry("evm").__name__ == "run_certora"


def test_a_run_defaults_to_the_evm_prover():
    assert ProverOptions().app == "evm"
    assert make_prover_options(cloud=False).app == "evm"
    assert make_prover_options(cloud=False, app="solana").app == "solana"


def test_an_unknown_prover_app_is_named_at_the_process_boundary():
    """The wrapper reads the app out of its own ``argv``, which is the one place an unchecked
    string arrives. A message that does not repeat the bad name leaves the reader guessing which
    of the arguments was wrong."""
    with pytest.raises(CertoraEnvironmentError, match="solanna"):
        prover_app("solanna")
