"""Can a rule reach an Anchor program's own code under the Solana Prover?

Three rules, graded by how much of Anchor they go through:

* ``rule_vault_state_deserializes`` — the control. ``Account::try_from`` and
  arithmetic, no dispatch and no CPI. If this fails, the problem is not Anchor's.
* ``rule_deposit_credits_exactly_the_amount`` — the handler, with the accounts
  struct built by hand so ``try_accounts`` is skipped. It asserts the
  post-state and goes through the account-creating CPI.
* ``rule_dispatch_is_reachable`` — ``crate::entry``, the dispatch path. Not
  gated. It fails with [3308], and it calls ``entry``, which a real spec does
  not. It is a tripwire for that error being fixed.

The scaffold points the target at the Anchor fork
(:mod:`composer.spec.cvlr.forks`), as a production run does.

Marked ``expensive``. It submits a real cloud job and needs a Rust and Solana
platform toolchain. It skips, naming the missing piece, when one is absent.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from composer.cargo.metadata import Workspace
from composer.cargo.sbf import Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.core import make_prover_options
from composer.sandbox.config import SandboxConfig
from composer.sandbox.recipes import PLATFORM_TOOLS_ROOT
from composer.prover.conf import SelectRules, dump_conf
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    Submission,
    SubmissionFailed,
    prepare_submission,
    run_submission,
)
from composer.spec.cvlr.manifest import rule_names_in_elf
from composer.spec.cvlr.scaffold import SPECS_DIR, apply, plan_scaffold
from composer.spec.cvlr.reference import SOLANA_V2

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl"
PROBE = Path(__file__).parent / "data" / "anchor_reach_probe.rs"
PACKAGE = "vault"

#: Weakest first, so the first failure is where the boundary is.
TIERS = (
    "rule_vault_state_deserializes",
    "rule_deposit_credits_exactly_the_amount",
    "rule_dispatch_is_reachable",
)

#: The tiers this test requires. They match how CVLR specs are written.
REQUIRED = TIERS[:2]

#: Recorded and not required. It calls Anchor's ``entry``. The reference
#: harness does not: it uses ``Context::new``. The rule currently fails with
#: [3308] from the program's ``#[error_code]``. The assertion at the end fails
#: if it starts to verify.
UNGATED = "rule_dispatch_is_reachable"



def _shipped_cli_only() -> None:
    """Skip when ``$CERTORA`` is set.

    It points the CLI at a Prover source checkout. That checkout reports
    itself as "no package installed" and is refused before upload, and the
    refusal names neither the variable nor the checkout.
    """
    if os.environ.get("CERTORA"):
        pytest.skip("$CERTORA is set; rerun with `env -u CERTORA`")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """A copy of the scenario, scaffolded, with the probe in place of ``specs/mod.rs``.

    Scaffolding writes into the tree. The checked-in scenario stays untouched.
    """
    _shipped_cli_only()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / SCENARIO.name
    shutil.copytree(SCENARIO, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


async def test_a_rule_that_reaches_an_anchor_program_can_be_analyzed(project, capsys):
    workspace = await Workspace.read(project)
    assert isinstance(workspace, Workspace), workspace
    package = workspace.member(PACKAGE)
    assert package is not None, f"no {PACKAGE} member in {project}"

    plan = plan_scaffold(workspace, package, SOLANA_V2)
    assert plan.blocked == (), plan.blocked
    apply(plan, project)
    (package.root / SPECS_DIR / "mod.rs").write_text(PROBE.read_text())

    wanted = PLATFORM_TOOLS_VERSION
    if not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")

    session = CargoSession(workdir=project, sandbox=SandboxConfig.from_env())
    assert isinstance(await session.warm(manifest_dirs=(Path("programs") / PACKAGE,)), Warmed)

    # Compile on the host first. A probe that does not compile must not be submitted.
    fast = await session.check(package=PACKAGE, features=("certora",))
    assert fast.ok, fast.verdict

    into = project.parent / "submission"
    into.mkdir()
    prepared = await prepare_submission(
        session,
        Submission(
            manifest_path=package.root / "Cargo.toml",
            rules=SelectRules(TIERS),
            stem="anchor_reach",
            msg="AutoProver Anchor reach probe",
        ),
        into=into,
    )
    if isinstance(prepared, BuildRejected):
        outcome = prepared
    else:
        # Sanity off. It doubles the work per rule, and this run asks whether the
        # prover can analyze an Anchor call graph. The weakest tier asserts a
        # tautology, so a [3006] on it is unambiguous. No conf setting turns
        # sanity off, so the probe edits the file it submits.
        conf = {**json.loads(prepared.conf_path.read_text()), "rule_sanity": "none"}
        prepared.conf_path.write_text(dump_conf(conf))
        outcome = await run_submission(
            prepared,
            prover_opts=make_prover_options(cloud=True, app="solana"),
            cex=None,
        )

    match outcome:
        case BuildRejected(build=build):
            pytest.fail(f"chain build failed: {getattr(build.verdict, 'diagnostics', build)}")
        case SubmissionFailed(reason=reason):
            pytest.fail(f"no results: {reason}")
        case Checked(build=build, report=report):
            assert isinstance(build.verdict, Built)
            assert rule_names_in_elf(build.verdict.manifest.artifact) == set(TIERS)

    # Save the per-rule message. It is the only place a non-VERIFIED reason
    # appears, the job's output URL does not include it, and it is gone when
    # this process ends.
    saved = project / "anchor_reach_report.txt"
    saved.write_text(f"{report.link}\n\n{report.result_str}\n")
    with capsys.disabled():
        print(f"\nprover run: {report.link}\nfull report saved to {saved}\n{report.result_str}")

    # Weakest required tier first. A rule the prover cannot analyze is not
    # VERIFIED, whether the cause is [3006] or anything else. A green run means
    # the program's own code is reachable from a rule and a post-state property
    # about it can be proved.
    status = report.rule_status
    unreached = [name for name in REQUIRED if status.get(name) is not True]
    assert unreached == [], (
        f"rules that could not be verified against the program itself: {unreached}. Weakest tier "
        f"first, so the first entry is where the boundary is. Report: {report.link}\n"
        f"If the handler tier is the one failing, check how it observes post-state before "
        f"changing a prover setting. Anchor's `Account<T>` is a deserialized copy. A handler's "
        f"mutation of it reaches the account bytes only in `exit`, and a rule that calls the "
        f"handler directly never calls `exit`. Re-deserializing after the call reads pre-state, "
        f"and the property fails on a correct program. Read through the handle the context "
        f"borrowed.\n"
        f"The full report is saved next to the project; `Reports/treeView/` is fetchable from "
        f"https://prover.certora.com/v1/domain/jobs/<jobId>/f/outputs (a gzipped tar) if it is lost."
    )

    # The dispatch tier is expected to fail. If it verifies, [3308] was fixed
    # and this fixture should stop treating that failure as expected.
    assert status.get(UNGATED) is not True, (
        f"{UNGATED} now verifies. [3308] appears to be fixed, so move it into REQUIRED. "
        f"Report: {report.link}"
    )
