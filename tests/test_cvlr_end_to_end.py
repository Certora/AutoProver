"""Phase 1b's exit criterion, as a test: a hand-written CVLR rule in, verdicts out.

``docs/cvlr-backend-plan.md`` §7.2 asks for exactly this — "given the Phase 1a reference project and
a hand-written CVLR rule, the system produces verdicts, with measured latency for both compile
tiers" — and the fixture makes it checkable rather than merely observable. `Certora/SolanaExamples
<https://github.com/Certora/SolanaExamples>`_ ships two minimal CVLR projects, each with a conf and
an **expected-verdict file** its own CI compares against. So the assertion is not "the plumbing
returned something"; it is "the plumbing returned what the project's authors say is correct",
including the rule that is meant to fail and the one that is meant to fail *sanity*.

Marked ``expensive``: it submits a real cloud job. It also needs a real Rust + Solana platform
toolchain, and it skips — naming the missing piece — rather than failing when one is absent, since a
machine without the toolchain is not a machine with a broken backend.
"""

import json
import os
import shutil
from pathlib import Path

import pytest

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.core import make_prover_options
from composer.sandbox.config import SandboxConfig
from composer.spec.cvlr.conf import read_conf, tools_version
from composer.spec.cvlr.prover import BuildRejected, Checked, Submission, submit

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]

#: Where the public examples repo is checked out. An env var rather than a vendored fixture: the
#: repo is the upstream artifact this test is *about*, and a copy in this tree would silently stop
#: tracking it.
EXAMPLES_ENV = "SOLANA_EXAMPLES_REPO"
DEFAULT_EXAMPLES = Path("~/src/SolanaExamples").expanduser()

#: The example whose conf ships an expected-verdict file covering all three outcomes that matter.
EXAMPLE = Path("cvlr_by_example/first_example")

#: ``Reports/output.json``'s vocabulary — what an expected-verdict file is written in — against the
#: treeView vocabulary the shared result parser produces. Two names for one outcome, and the
#: translation lives here because the expected file is the *fixture's* format, not the backend's.
EXPECTED_TO_TREEVIEW = {
    "SUCCESS": "VERIFIED",
    "FAIL": "VIOLATED",
    "SANITY_FAIL": "SANITY_FAILED",
}


def _examples_root() -> Path:
    root = Path(os.environ.get(EXAMPLES_ENV, DEFAULT_EXAMPLES)).expanduser()
    if not (root / EXAMPLE / "Cargo.toml").is_file():
        pytest.skip(
            f"no SolanaExamples checkout at {root}; clone "
            f"https://github.com/Certora/SolanaExamples and set ${EXAMPLES_ENV}"
        )
    return root


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A throwaway copy of the examples repo.

    A copy rather than the checkout itself because a session's workdir is written to — the private
    ``CARGO_HOME``, the build script, the conf, ``target/`` — and a test that dirties a developer's
    working tree is a test they learn to avoid running.
    """
    root = _examples_root()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / "SolanaExamples"
    shutil.copytree(root, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


async def test_the_examples_project_verifies_exactly_as_its_authors_expect(workdir, capsys):
    base_conf = read_conf(workdir / EXAMPLE / "certora" / "conf" / "Default.conf")
    expected = json.loads(
        (workdir / EXAMPLE / "certora" / "conf" / "expectedDefault.json").read_text()
    )["rules"]

    wanted_tools = tools_version(base_conf)
    if wanted_tools is not None and not platform_tools_installed(wanted_tools):
        pytest.skip(
            f"Solana platform tools {wanted_tools} are not installed under {PLATFORM_TOOLS_ROOT}"
        )

    session = CargoSession(workdir=workdir, sandbox=SandboxConfig.from_env())
    assert isinstance(await session.warm(manifest_dirs=(EXAMPLE,)), Warmed)

    # The fast tier, measured against the same crate the slow tier builds — the two numbers side by
    # side are what open question 1 is decided on, and the ratio is the whole argument for two tiers.
    fast = await session.check(package="first_example", features=("certora",))
    assert fast.ok, fast.verdict

    submission = Submission(
        manifest_path=workdir / EXAMPLE / "Cargo.toml",
        base_conf=base_conf,
        stem="first_example",
        msg="AutoProver CVLR plumbing gate",
    )
    outcome = await submit(
        session, submission, prover_opts=make_prover_options(cloud=True, app="solana")
    )
    assert not isinstance(outcome, BuildRejected), outcome
    assert isinstance(outcome, Checked), outcome
    assert isinstance(outcome.build.verdict, Built)

    with capsys.disabled():
        print(
            f"\ncompile tiers (confined={session.confined}): "
            f"fast {fast.duration_ms} ms, slow {outcome.build.duration_ms} ms"
            f"\nprover run: {outcome.link}"
        )

    actual = {path.rule: status for path, status in outcome.report.raw_rule_status.items()}
    assert actual == {
        rule: EXPECTED_TO_TREEVIEW[verdict] for rule, verdict in expected.items()
    }
