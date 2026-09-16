"""Which Prover CLI a run submits to, and what a caller may name."""

import pytest

from composer.certora_env import CertoraEnvironmentError, import_prover_entry, prover_app
from composer.prover.core import ProverOptions, make_prover_options


def test_the_solana_cli_is_reachable_under_the_name_a_run_selects_it_by():
    assert import_prover_entry("solana").__name__ == "run_solana_prover"


def test_the_evm_cli_is_reachable_under_its_own_name():
    assert import_prover_entry("evm").__name__ == "run_certora"


def test_a_run_defaults_to_the_evm_prover():
    assert ProverOptions().app == "evm"
    assert make_prover_options(cloud=False).app == "evm"
    assert make_prover_options(cloud=False, app="solana").app == "solana"


def test_an_unknown_prover_app_is_named_at_the_process_boundary():
    """The wrapper's ``argv`` is the one place an unchecked app name arrives, and a message that
    does not repeat it leaves the reader guessing which argument was wrong."""
    with pytest.raises(CertoraEnvironmentError, match="solanna"):
        prover_app("solanna")
