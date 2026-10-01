"""Does the Prover's model drop writes the real program makes?

``docs/cvlr-todo.md`` L1. Read from the Prover source, an opaque or summarized call returns an
arbitrary value and writes nothing beyond the cells its summary names. Two consequences are worth
measuring rather than inferring, because each one would let a rule verify a property the program
violates:

* ``AccountInfo::realloc`` is summarized by the starting layer as its ``Result`` alone, so after a
  resize the model may still hold the old length.
* A lamport transfer by CPI, through ``invoke`` (which reaches the summarized
  ``invoke_signed_unchecked``) and through Anchor's ``system_program::transfer`` (opaque under the
  ``anchor_lang`` blanket), may leave the payer's lamports where they were.

Each call is probed by a canary, which asserts that nothing changed and is false of the real
program, and a companion, which asserts what really happens (``tests/data/dropped_writes_probe.rs``).
A VERIFIED canary is a dropped write. Sanity checking stays on, so an unreachable branch is reported
as vacuous rather than as a pass.

Marked ``expensive``: it submits a real cloud job, and it needs a Rust and Solana platform toolchain.
It skips, naming the missing piece, when one is absent.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from composer.cargo.metadata import Workspace
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.conf import SelectRules, dump_conf
from composer.prover.core import make_prover_options
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    Submission,
    SubmissionFailed,
    prepare_submission,
    run_submission,
)
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.scaffold import SPECS_DIR, apply, plan_scaffold
from composer.spec.cvlr.reference import SOLANA

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl"
PROBE = Path(__file__).parent / "data" / "dropped_writes_probe.rs"
PACKAGE = "vault"
STEM = "dropped_writes"

#: Canary and companion, per call probed.
PAIRS = (
    ("rule_canary_realloc_keeps_the_old_length", "rule_realloc_sets_the_new_length"),
    ("rule_canary_resize_keeps_the_old_length", "rule_resize_sets_the_new_length"),
    ("rule_canary_invoke_transfer_moves_nothing", "rule_invoke_transfer_debits_the_payer"),
    ("rule_canary_anchor_transfer_moves_nothing", "rule_anchor_transfer_debits_the_payer"),
)
#: Satisfy rules: whether each call's success branch is reachable at all.
REACHABLE = (
    "rule_realloc_can_succeed",
    "rule_resize_can_succeed",
    "rule_invoke_transfer_can_succeed",
    "rule_anchor_transfer_can_succeed",
)
#: The caller's deserialized `Account<T>` across a CPI, with its reachability rule. The assertion is
#: true of the real program.
ACROSS_A_CPI = (
    "rule_account_field_survives_an_invoke",
    "rule_account_field_across_an_invoke_is_reachable",
)
RULES = tuple(rule for pair in PAIRS for rule in pair) + REACHABLE + ACROSS_A_CPI


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The scenario, scaffolded into a copy so a run never dirties the checked-in tree."""
    if os.environ.get("CERTORA"):
        pytest.skip("$CERTORA is set; rerun with `env -u CERTORA`")
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / SCENARIO.name
    shutil.copytree(SCENARIO, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


async def test_which_writes_the_model_drops(project, capsys):
    workspace = await Workspace.read(project)
    assert isinstance(workspace, Workspace), workspace
    package = workspace.member(PACKAGE)
    assert package is not None, f"no {PACKAGE} member in {project}"

    plan = plan_scaffold(workspace, package, SOLANA)
    assert plan.blocked == (), plan.blocked
    apply(plan, project)
    (package.root / SPECS_DIR / "mod.rs").write_text(PROBE.read_text())
    assert set(rule_names(PROBE.read_text())) == set(RULES)

    wanted = PLATFORM_TOOLS_VERSION
    if not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")

    session = CargoSession(workdir=project, sandbox=SandboxConfig.from_env())
    assert isinstance(await session.warm(manifest_dirs=(Path("programs") / PACKAGE,)), Warmed)
    fast = await session.check(package=PACKAGE, features=("certora",))
    assert fast.ok, fast.verdict

    prepared = await prepare_submission(
        session,
        Submission(
            manifest_path=package.root / "Cargo.toml",
            rules=SelectRules(RULES),
            stem=STEM,
            msg="AutoProver dropped-writes probe",
        ),
    )
    if isinstance(prepared, BuildRejected):
        outcome = prepared
    else:
        # `system_instruction::transfer` builds its account list in a loop no bound discharges
        # (`docs/cvlr-todo.md` U9), so the invoke pair would stop on the unwinding assertion
        # before reaching its own. Assuming loops finish changes how that loop is treated, not
        # whether the CPI's writes are kept, which is the question here.
        conf = {**json.loads(prepared.conf_path.read_text()), "optimistic_loop": True}
        prepared.conf_path.write_text(dump_conf(conf))
        outcome = await run_submission(
            session, prepared, prover_opts=make_prover_options(cloud=True, app="solana")
        )
    match outcome:
        case BuildRejected(build=build):
            pytest.fail(f"chain build failed: {getattr(build.verdict, 'diagnostics', build)}")
        case SubmissionFailed(reason=reason):
            pytest.fail(f"no results: {reason}")
        case Checked(build=build, report=report):
            assert isinstance(build.verdict, Built)

    saved = project / "dropped_writes_report.txt"
    saved.write_text(f"{report.link}\n\n{report.result_str}\n")
    with capsys.disabled():
        print(f"\nprover run: {report.link}\nfull report saved to {saved}\n{report.result_str}")

    status = report.rule_status
    missing = [rule for rule in RULES if rule not in status]
    assert missing == [], f"no verdict for {missing}. Report: {report.link}"

    # What the model does today, measured 2026-09-29. Each line is a tripwire: a change means the
    # Prover or the starting layer changed, and `docs/cvlr-todo.md` L1 says what depends on it.
    observed = {rule: status[rule] for rule in RULES}
    expected = {
        # `realloc`: inlined by the starting layer, and modelled faithfully. (Under the summary it
        # replaced, the canary verified: the length stayed the old one.)
        "rule_realloc_can_succeed": True,
        "rule_canary_realloc_keeps_the_old_length": False,
        "rule_realloc_sets_the_new_length": True,
        # `resize`: reachable, and the resize is dropped. It is not inlined, because inlining its
        # zero-fill crashes the Prover's memory partitioning and ends the whole job.
        "rule_resize_can_succeed": True,
        "rule_canary_resize_keeps_the_old_length": True,
        "rule_resize_sets_the_new_length": False,
        # `invoke`: reachable, and the transfer is dropped. The payer keeps its lamports.
        "rule_invoke_transfer_can_succeed": True,
        "rule_canary_invoke_transfer_moves_nothing": True,
        "rule_invoke_transfer_debits_the_payer": False,
        # Anchor's `system_program::transfer`: inlined by the Anchor layer, so reachable, and then
        # dropped like any `invoke`. (External under the `anchor_lang` blanket, it never succeeded
        # and both of its assertions held vacuously.)
        "rule_anchor_transfer_can_succeed": True,
        "rule_canary_anchor_transfer_moves_nothing": True,
        "rule_anchor_transfer_debits_the_payer": False,
        # The caller's deserialized `Account<T>` is untouched by an `invoke`, as in the program.
        # (The author's prompt said a CPI havocs it; that is not what the model does.)
        "rule_account_field_across_an_invoke_is_reachable": True,
        "rule_account_field_survives_an_invoke": True,
    }
    # `resize` is external by design (CERT-10184), so the job's alert report must name it. This is
    # also what shows the alert report was fetched with the results.
    assert "solana_account_info::AccountInfo::resize" in report.external_functions, (
        f"external functions reported: {report.external_functions}. Report: {report.link}"
    )

    changed = {rule: observed[rule] for rule in RULES if observed[rule] != expected[rule]}
    assert changed == {}, (
        f"the model's handling of these calls changed: {changed}. A canary that stops verifying "
        f"means a write is now kept; update `expected` and `docs/cvlr-todo.md` L1. Report: "
        f"{report.link}"
    )
