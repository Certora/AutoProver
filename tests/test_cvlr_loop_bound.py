"""Does raising the loop bound change a verdict, and does our own code recognize why it had to?

The end-to-end counterpart to the conf-editor plumbing in ``tests/test_cvlr_plumbing.py``. Those
tests prove an edit reaches the file the prover is handed; this one proves the edit does
something, and that the signal an author acts on is the signal our classifier produces.

The circuit it closes, in the order the authoring loop walks it:

1. A rule whose loop needs more unrollings than the conf allows comes back VIOLATED.
2. :func:`composer.prover.ptypes.classify_violation` recognizes the prover's own unwinding assertion
   and reports :class:`~composer.prover.ptypes.IncompleteCheck` — so it is explained to the author
   and kept out of the findings evidence, which is the split ``tests/test_cvlr_findings.py`` pins
   against recorded fixtures and this one pins against the prover.
3. ``adjust_prover_config``'s edit, applied through the same
   :func:`~composer.spec.cvlr.conf.with_loop_iter` the tool applies it with, turns that into a
   verdict.

Both arms verify **one artifact**: the program is built once and the two confs are written over it,
so the only variable is a single conf key — asserted, not assumed, before either job is submitted.
That discipline is the whole value of an A/B here, and ``docs/upstream-defects.md`` P8 is what it
cost to learn that a plausible-looking flag difference is not self-evidently the only difference.

Hand-written rather than agent-written, for the reason ``tests/test_cvlr_anchor_reach.py`` gives:
this asks what the platform does, so a loop run would measure the loop instead. Three drafts of the
probe compiled or verified away the loop they were about — the header of ``data/loop_bound_probe.rs``
records what each got wrong — which is why the artifact is disassembled here before anything is
submitted.

Marked ``expensive``: two real cloud jobs, and a Rust + Solana platform toolchain. It skips — naming
the missing piece — rather than failing when one is absent.
"""

import os
import shutil
import subprocess
from pathlib import Path
from typing import override

import pytest

from composer.cargo.metadata import read_workspace
from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.core import (
    CexProgressCallbacks,
    UnanalyzedCexHandler,
    make_prover_options,
)
from composer.prover.ptypes import IncompleteCheck, RuleResult, classify_violation
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import (
    SelectRules,
    load_base,
    read_conf,
    tools_version,
    with_loop_iter,
)
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    Prepared,
    Submission,
    SubmissionFailed,
    build_for_submission,
    run_submission,
    write_submission,
)
from composer.spec.cvlr.rules import rule_names
from composer.spec.cvlr.scaffold import SPECS_DIR, apply, plan_scaffold
from composer.spec.cvlr_reference import SOLANA

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

SCENARIO = Path(__file__).parent.parent / "test_scenarios" / "solana_vault_idl"
PROBE = Path(__file__).parent / "data" / "loop_bound_probe.rs"
PACKAGE = "vault"

#: Verified under both bounds. Its job is to localize a failure of the rule below: if the control is
#: red too, the bound is not what the run measured.
CONTROL = "rule_trip_count_within_the_default_bound"

#: Red under the default bound, green under the raised one. The measurement.
BEYOND = "rule_trip_count_beyond_the_default_bound"

#: Red under both, and not gated on: it is how the run reports whether unwinding conditions are
#: being generated at all. A draft of this fixture verified a loop it should not have, and with no
#: such row there was no way to tell "the bound did not bite" from "no loop reached the prover".
UNBOUNDED = "rule_trip_count_the_prover_cannot_bound"

RULES = (CONTROL, BEYOND, UNBOUNDED)

#: Generous rather than minimal — four unrollings is what the probe's assumed trip count needs, and
#: this fixture is an instrument, not the advice. The author's prompt says to raise the bound to the
#: smallest number the property needs; here a bound that is one short would report the *fixture* as
#: the finding. Cost is not a consideration on a loop this small.
RAISED = 8


def _objdump(version: str) -> Path | None:
    """The disassembler shipped beside the platform tools' own cargo.

    Located the way :func:`composer.cargo.sbf.platform_tools_cargos` locates that cargo, and kept
    here rather than there because nothing in production disassembles anything: this is a fixture
    checking its own premise.
    """
    for flavour in ("platform-tools-certora", "platform-tools"):
        candidate = PLATFORM_TOOLS_ROOT / version / flavour / "llvm" / "bin" / "llvm-objdump"
        if candidate.is_file():
            return candidate
    return None


def _has_a_loop(artifact: Path, objdump: Path) -> bool:
    """Whether anything in the built program branches backwards.

    The probe's premise, and the one thing about it that cannot be read off the source: two earlier
    drafts compiled to straight-line code — one close-formed, one fully unrolled against a
    fixed-size array — and both would have verified under every bound while appearing to measure the
    bound. Textual because a disassembly is text; `goto -` is how the SBF disassembler spells a
    backward branch.
    """
    disassembly = subprocess.run(
        [str(objdump), "-d", "--no-show-raw-insn", str(artifact)],
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    return "goto -" in disassembly


class _Recorded(UnanalyzedCexHandler):
    """The no-LLM handler, keeping what it was handed.

    ``ProverReport`` carries verdicts but no counterexamples, and the counterexample's assertion is
    what decides :class:`IncompleteCheck` — the distinction this test exists to check against a real
    prover rather than a recorded fixture.
    """

    def __init__(self) -> None:
        self.results: list[RuleResult] = []

    @override
    async def analyze(
        self,
        all_results: list[RuleResult],
        tool_call_id: str,
        callbacks: CexProgressCallbacks,
        report_dir: Path,
    ) -> str:
        self.results = list(all_results)
        return await super().analyze(all_results, tool_call_id, callbacks, report_dir)


def _shipped_cli_only() -> None:
    """``$CERTORA`` points the CLI at a Prover source checkout, which reports itself as "no package
    installed" and is refused before upload — naming neither the variable nor itself."""
    if os.environ.get("CERTORA"):
        pytest.skip("$CERTORA is set; rerun with `env -u CERTORA`")


@pytest.fixture
def project(tmp_path: Path) -> Path:
    """The scenario, copied: scaffolding writes to the tree, and a test that dirties a checked-in
    scenario is a test people learn not to run."""
    _shipped_cli_only()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / SCENARIO.name
    shutil.copytree(SCENARIO, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


def _violation(results: list[RuleResult], rule: str) -> RuleResult:
    matching = [r for r in results if r.name == rule and r.status == "VIOLATED"]
    assert matching, f"no violated result for {rule} among {[r.name for r in results]}"
    return matching[0]


async def test_raising_the_loop_bound_is_what_turns_the_rule_green(project, capsys):
    workspace = await read_workspace(project)
    assert workspace is not None, f"cargo reported no workspace at {project}"
    package = workspace.member(PACKAGE)
    assert package is not None, f"no {PACKAGE} member in {project}"

    plan = plan_scaffold(workspace, package, SOLANA)
    assert plan.blocked == (), plan.blocked
    apply(plan, project)
    (package.root / SPECS_DIR / "mod.rs").write_text(PROBE.read_text())

    declared = rule_names(PROBE.read_text())
    assert set(declared) == set(RULES), declared

    # Sanity off, as `tests/test_cvlr_anchor_reach.py` does and for the same reason: it doubles the
    # work per rule and answers a different question. Both properties here are tautologies by
    # design, which is exactly what a vacuity check is entitled to complain about.
    default = {**load_base(None), "rule_sanity": "none"}
    assert int(default["loop_iter"]) < 5, (
        f"the probe's measured loop takes five iterations to outrun the default bound, which is "
        f"now "
        f"{default['loop_iter']}. Raise the probe's trip count or this measures nothing."
    )
    raised = with_loop_iter(default, RAISED)

    wanted = tools_version(default)
    if wanted is not None and not platform_tools_installed(wanted):
        pytest.skip(f"Solana platform tools {wanted} are not installed under {PLATFORM_TOOLS_ROOT}")

    session = CargoSession(workdir=project, sandbox=SandboxConfig.from_env())
    assert isinstance(await session.warm(manifest_dirs=(Path("programs") / PACKAGE,)), Warmed)

    # The fast tier first: a probe that does not compile must not cost two submissions.
    fast = await session.check(package=PACKAGE, features=("certora",))
    assert fast.ok, fast.verdict

    def _submission(conf: dict, stem: str) -> Submission:
        return Submission(
            manifest_path=package.root / "Cargo.toml",
            base_conf=conf,
            rules=SelectRules(RULES),
            stem=stem,
            msg=f"AutoProver loop bound probe: {stem}",
        )

    # One build, two confs. Both arms verify the identical artifact.
    build = await build_for_submission(session, _submission(default, "loop_bound_default"))
    assert isinstance(build.verdict, Built), getattr(build.verdict, "diagnostics", build)

    # The probe's premise, checked against the artifact rather than against the source, and before
    # anything is submitted: a loop the compiler deleted costs two cloud jobs to discover and looks
    # exactly like a passing run while it does it.
    objdump = _objdump(wanted) if wanted is not None else None
    if objdump is None:
        with capsys.disabled():
            print(
                "\nno llvm-objdump beside the platform tools: submitting without checking that the "
                "probe's loop survived the compiler"
            )
    else:
        assert _has_a_loop(build.verdict.manifest.artifact, objdump), (
            "the built program contains no backward branch, so the probe's loop did not survive "
            "optimization and neither bound can fail on it. The fixture measures nothing until the "
            "loop's trip count is beyond what the compiler can bound — see the probe's own header "
            "for the two drafts that got this wrong."
        )

    confs = {
        arm: await write_submission(session, _submission(conf, f"loop_bound_{arm}"))
        for arm, conf in (("default", default), ("raised", raised))
    }
    written = {arm: read_conf(path) for arm, path in confs.items()}
    differing = {
        key: value
        for key, value in written["raised"].items()
        if written["default"].get(key) != value
    }
    # The A/B's premise, asserted before either job is submitted rather than diffed by hand after.
    # `msg` differs too and is excluded by name: it is how the two jobs are told apart in the
    # dashboard, and it reaches no part of the verification.
    differing.pop("msg", None)
    assert differing == {"loop_iter": str(RAISED)}, (
        f"the two arms differ in more than the loop bound: {differing}. Whatever this run "
        f"measures, "
        f"it is not the bound."
    )

    verdicts: dict[str, dict[str, bool]] = {}
    links: dict[str, str] = {}
    reports: dict[str, str] = {}
    recorders: dict[str, _Recorded] = {}
    for arm, conf_path in confs.items():
        recorders[arm] = _Recorded()
        outcome = await run_submission(
            session,
            Prepared(build, conf_path),
            prover_opts=make_prover_options(cloud=True, app="solana"),
            cex=recorders[arm],
            tool_call_id=f"loop-bound-{arm}",
        )
        match outcome:
            case BuildRejected():
                pytest.fail(f"{arm} arm: run_submission reported a build it was not asked to do")
            case SubmissionFailed(reason=reason):
                pytest.fail(f"{arm} arm produced no results: {reason}")
            case Checked(report=report):
                verdicts[arm] = report.rule_status
                links[arm] = report.link
                reports[arm] = report.result_str

    # Written to a file as well as printed, for the reason `tests/test_cvlr_anchor_reach.py` gives:
    # the prover's per-rule message is the only place a non-VERIFIED verdict's reason appears, it is
    # not among the artifacts the job's URL exposes, and it is gone once the process ends. Measured
    # again here — a first run of this test was invoked through a `tail` and lost both arms.
    saved = project / "loop_bound_report.txt"
    saved.write_text(
        "\n\n".join(f"=== {arm} ===\n{links[arm]}\n\n{reports[arm]}" for arm in confs)
    )
    with capsys.disabled():
        print(
            f"\nloop bound {default['loop_iter']} (default): {verdicts['default']}"
            f"\n{links['default']}"
            f"\nloop bound {RAISED} (raised):   {verdicts['raised']}\n{links['raised']}"
            f"\nfull reports saved to {saved}"
        )

    # The tripwire first: it decides whether any row below is worth reading.
    for arm in confs:
        assert not verdicts[arm][UNBOUNDED], (
            f"an unbounded loop verified under bound "
            f"{default['loop_iter'] if arm == 'default' else RAISED}, so the prover is not "
            f"modelling "
            f"this program's loops and no verdict here means what it says. Report: {links[arm]}"
        )

    # The control next, so a red row below localizes.
    assert verdicts["default"][CONTROL], (
        f"the control failed under the default bound, so this run says nothing about the bound. "
        f"Report: {links['default']}"
    )
    assert not verdicts["default"][BEYOND], (
        f"{BEYOND} verified under a bound its trip count should outrun. The prover may have "
        f"found a "
        f"closed form for the probe's loop, in which case the fixture no longer measures anything "
        f"and the probe needs a body that resists it. Report: {links['default']}"
    )

    # Why it failed, and by whose account. A rule stopped at the bound is the prover reporting its
    # own limit; recorded as evidence it becomes a written-up finding against the program.
    stopped = classify_violation(_violation(recorders["default"].results, BEYOND).counterexample)
    assert isinstance(stopped, IncompleteCheck), (
        f"an unwound loop bound classified as {type(stopped).__name__}, so it would reach the "
        f"findings synthesizer as a defect in the program. Report: {links['default']}"
    )
    assert stopped.assertion.startswith("Unwinding condition in a loop"), stopped.assertion

    # And the edit is what fixes it. The tripwire stays red under every bound by construction, so
    # the gated pair is what this compares.
    assert {rule: verdicts["raised"][rule] for rule in (CONTROL, BEYOND)} == {
        CONTROL: True,
        BEYOND: True,
    }, (
        f"raising the bound to {RAISED} did not produce verdicts: {verdicts['raised']}. "
        f"Report: {links['raised']}"
    )
