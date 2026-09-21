"""Which Prover CLI a run submits to, and what a caller may name."""

import pytest

from composer.certora_env import CertoraEnvironmentError, import_prover_entry, prover_app
from composer.prover.core import (
    DEFAULT_GLOBAL_TIMEOUT, ProverOptions, make_prover_options,
)


def test_the_solana_cli_is_reachable_under_the_name_a_run_selects_it_by():
    assert import_prover_entry("solana").__name__ == "run_solana_prover"


def test_the_evm_cli_is_reachable_under_its_own_name():
    assert import_prover_entry("evm").__name__ == "run_certora"


def test_a_run_names_the_prover_it_wants():
    """``app`` has no default: it selects the CLI, and so the build step, and the caller is the
    only one who knows which ecosystem the spec was written for."""
    assert make_prover_options(cloud=False, app="evm").app == "evm"
    assert make_prover_options(cloud=False, app="solana").app == "solana"


def test_an_unknown_prover_app_is_named_at_the_process_boundary():
    """The wrapper's ``argv`` is the one place an unchecked app name arrives, and a message that
    does not repeat it leaves the reader guessing which argument was wrong."""
    with pytest.raises(CertoraEnvironmentError, match="solanna"):
        prover_app("solanna")


def test_a_local_run_puts_nothing_on_the_command_line():
    """We impose no budget on a run we host ourselves; ``DEFAULT_GLOBAL_TIMEOUT`` bounds our own
    waiting and never reaches the CLI."""
    opts = make_prover_options(cloud=False, app="evm")
    assert opts.cli_args() == []
    assert not opts.cloud
    assert opts.global_timeout is None
    assert opts.runtime_bound == DEFAULT_GLOBAL_TIMEOUT


def test_a_timeout_is_not_the_cloud_s_to_carry():
    """``--global_timeout`` is a top-level conf property (jar flag ``-userGlobalTimeout``) that
    the Prover honours wherever it runs, so a caller may bound a local run too."""
    opts = ProverOptions(app="evm", global_timeout=900)
    assert opts.cli_args() == ["--global_timeout", "900"]
    assert not opts.cloud
    assert opts.runtime_bound == 900


def test_a_cloud_run_renders_the_server_and_timeout_it_carries():
    opts = ProverOptions(app="evm", server="production", global_timeout=900)
    assert opts.cli_args() == ["--global_timeout", "900", "--server", "production"]
    assert opts.cloud


def test_the_budget_the_prover_was_given_is_the_one_our_backstops_extend():
    """``run_prover`` derives both the subprocess bound and the poll bound from
    ``global_timeout``; reading a different number than the Prover was told would let us give up
    on a job that is still inside its own budget."""
    opts = ProverOptions(app="evm", server="production", global_timeout=900)
    rendered = opts.cli_args()
    assert rendered[rendered.index("--global_timeout") + 1] == str(opts.runtime_bound)
