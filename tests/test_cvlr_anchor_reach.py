"""Can a rule reach an Anchor program's own code under the Solana Prover?

``docs/cvlr-backend-plan.md`` §7.5.6. The authoring loop's first real run published nineteen rules
and none of them invoked the program: two of three units reported prover error **[3006] "illegal
store of a stack pointer"** and worked around it by verifying a reimplementation of the handler
instead. Every gate passed that, because every verdict in it was honest.

This is the instrument for the question the loop cannot answer about itself. It is hand-written on
purpose: an error the author cannot act on consumes its whole budget — three recorded instances in
one phase — so a loop run measures the loop rather than the hypothesis. Here the harness is fixed and
the only variable is the platform's configuration.

Three rules, graded by how much of Anchor they go through, so a failure localizes instead of merely
happening:

* ``rule_vault_state_deserializes`` — the control. ``Account::try_from`` and arithmetic, no dispatch
  and no CPI. If this fails, the problem is not Anchor's.
* ``rule_deposit_credits_exactly_the_amount`` — the handler, with the accounts struct built by hand
  so ``try_accounts`` is skipped, asserting the post-state property the loop reported as out of
  reach. This one goes through the account-creating CPI.
* ``rule_dispatch_is_reachable`` — ``crate::entry``, the whole dispatch path. What [3006] was
  attributed to. **Not gated**: it fails with [3308] and it probes something no real spec does, so it
  is carried as a tripwire for that error being fixed rather than as a requirement.

The configuration is no longer a variable: the scaffold points the target at the Anchor fork
(:mod:`composer.spec.cvlr.munge`), which is what production does, so this test exercises the same
dependency graph a real project verifies against.

Marked ``expensive``: it submits a real cloud job, and it needs a Rust and Solana platform toolchain.
It skips — naming the missing piece — rather than failing when one is absent.
"""

import os
import shutil
from pathlib import Path

import pytest

from composer.cargo.metadata import read_workspace
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.core import make_prover_options
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import SelectRules, load_base, tools_version
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    Submission,
    SubmissionFailed,
    submit,
)
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.scaffold import SPECS_DIR, apply, plan_scaffold
from composer.spec.cvlr_reference import SOLANA

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl"
PROBE = Path(__file__).parent / "data" / "anchor_reach_probe.rs"
PACKAGE = "vault"

#: Weakest first, so the report reads as a boundary rather than a list.
TIERS = (
    "rule_vault_state_deserializes",
    "rule_deposit_credits_exactly_the_amount",
    "rule_dispatch_is_reachable",
)

#: The tiers this test gates on — the ones that correspond to how CVLR specs are actually written.
REQUIRED = TIERS[:2]

#: ``rule_dispatch_is_reachable`` is recorded and not gated. It calls Anchor's ``entry``, and the
#: reference project's harness does that zero times against four uses of ``Context::new``, so it
#: probes something no real spec does. It currently fails with [3308] out of the program's own
#: ``#[error_code]`` (``docs/upstream-defects.md`` P4). Kept because it is free to run alongside the
#: others and it is how we will learn that [3308] is fixed — see the tripwire at the end.
UNGATED = "rule_dispatch_is_reachable"



def _shipped_cli_only() -> None:
    """``$CERTORA`` points the CLI at a Prover source checkout, which reports itself as "no package
    installed" and is refused before upload — naming neither the variable nor itself."""
    if os.environ.get("CERTORA"):
        pytest.skip("$CERTORA is set; rerun with `env -u CERTORA`")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The scenario, scaffolded, with the probe harness in place of the empty ``specs/mod.rs``.

    A copy: scaffolding writes to the tree, and a test that dirties a checked-in scenario is a test
    people learn not to run.
    """
    _shipped_cli_only()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / SCENARIO.name
    shutil.copytree(SCENARIO, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


async def test_a_rule_that_reaches_an_anchor_program_can_be_analyzed(project, capsys):
    workspace = await read_workspace(project)
    assert workspace is not None, f"cargo reported no workspace at {project}"
    package = workspace.member(PACKAGE)
    assert package is not None, f"no {PACKAGE} member in {project}"

    plan = plan_scaffold(workspace, package, SOLANA)
    assert plan.blocked == (), plan.blocked
    apply(plan, project)
    (package.root / SPECS_DIR / "mod.rs").write_text(PROBE.read_text())

    declared = rule_names(PROBE.read_text())
    assert set(declared) == set(TIERS), declared

    base = load_base(None)
    # Sanity off. It doubles the work per rule and answers a different question: this run asks
    # whether the prover can analyze an Anchor call graph at all, and the weakest tier asserts a
    # tautology on purpose so that a [3006] on it would be unambiguous.
    base = {**base, "rule_sanity": "none"}

    wanted = tools_version(base)
    if wanted is not None and not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")

    session = CargoSession(workdir=project, sandbox=SandboxConfig.from_env())
    assert isinstance(await session.warm(manifest_dirs=(Path("programs") / PACKAGE,)), Warmed)

    # The fast tier first: a probe that does not compile must not cost a submission, and the harness
    # is hand-written rather than agent-written so there is no revision loop to catch it later.
    fast = await session.check(package=PACKAGE, features=("certora",))
    assert fast.ok, fast.verdict

    outcome = await submit(
        session,
        Submission(
            manifest_path=package.root / "Cargo.toml",
            base_conf=base,
            rules=SelectRules(TIERS),
            stem="anchor_reach",
            msg="AutoProver Anchor reach probe",
        ),
        prover_opts=make_prover_options(cloud=True, app="solana"),
    )

    match outcome:
        case BuildRejected(build=build):
            pytest.fail(f"chain build failed: {getattr(build.verdict, 'diagnostics', build)}")
        case SubmissionFailed(reason=reason):
            pytest.fail(f"no results: {reason}")
        case Checked(build=build, report=report):
            assert isinstance(build.verdict, Built)

    # Written to a file as well as printed. The prover's per-rule message is the only place the
    # reason for a non-VERIFIED verdict appears, it is not among the artifacts the job's output URL
    # exposes, and it is gone once the process ends — a run invoked through a `tail` has already
    # cost one submission's worth of that message.
    saved = project / "anchor_reach_report.txt"
    saved.write_text(f"{report.link}\n\n{report.result_str}\n")
    with capsys.disabled():
        print(f"\nprover run: {report.link}\nfull report saved to {saved}\n{report.result_str}")

    # The property, weakest tier first. A rule the prover cannot analyze is not VERIFIED, so this is
    # the same assertion for a [3006] as for any other failure to produce a verdict — which is the
    # point: a green run here means an Anchor program's own code is reachable from a rule, and that
    # a post-state property about it can be proved.
    status = report.rule_status
    unreached = [name for name in REQUIRED if status.get(name) is not True]
    assert unreached == [], (
        f"rules that could not be verified against the program itself: {unreached}. Weakest tier "
        f"first, so the first entry is where the boundary is. Report: {report.link}\n"
        f"If the handler tier is the one failing, check *how* it observes post-state before "
        f"reaching for a prover setting: Anchor's `Account<T>` is a deserialized copy whose "
        f"mutations reach the account bytes only in `exit`, and nothing calls `exit` when a rule "
        f"invokes a handler directly. Re-deserializing after the call reads pre-state and the "
        f"property fails against a correct program. Read through the handle the context borrowed "
        f"(§7.6.2).\n"
        f"The full report is saved next to the project; `Reports/treeView/` is fetchable from "
        f"https://prover.certora.com/v1/domain/jobs/<jobId>/f/outputs (a gzipped tar) if it is lost."
    )

    # A tripwire rather than an assertion about the current state: the dispatch tier is expected to
    # fail, so the case worth catching is it *starting to pass*. That means [3308] was fixed
    # upstream, and this fixture should stop describing it as a bound.
    assert status.get(UNGATED) is not True, (
        f"{UNGATED} now verifies. [3308] appears to be fixed, so move it into REQUIRED and drop the "
        f"P4 entry from docs/upstream-defects.md. Report: {report.link}"
    )
