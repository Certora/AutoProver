"""A hand-written CVLR rule in, verdicts out, and the time both compile tiers take.

`Certora/SolanaExamples <https://github.com/Certora/SolanaExamples>`_ ships small
CVLR projects. Each has a conf and an expected-verdict file that its CI checks.
This test checks those verdicts, including the rule that should fail and the
one that should fail its sanity check.

Marked ``expensive``. It submits a real cloud job and needs a Rust toolchain
and Solana platform tools. It skips, and names the missing piece, when either
is absent.

The build runs under the production sandbox (``cvlr_confinement``). An
unconfined build is a different path, so a missing sandbox skips the test.
"""

import json
import os
import shutil
from pathlib import Path

import json5
import pytest
import pytest_asyncio

from composer.cargo.sbf import PLATFORM_TOOLS_ROOT, Built, platform_tools_installed
from composer.cargo.session import CargoSession, Warmed
from composer.prover.core import CexHandler, CexProgressCallbacks, make_prover_options
from composer.prover.ptypes import RuleResult
from composer.prover.conf import dump_conf
from composer.sandbox.config import SandboxConfig
from composer.sandbox.policy import SandboxUnavailable, ensure_available
from composer.spec.cvlr.conf import PLATFORM_TOOLS_VERSION
from composer.spec.cvlr.prover import (
    BuildRejected,
    Checked,
    Submission,
    prepare_submission,
    run_submission,
)

pytestmark = [pytest.mark.expensive, pytest.mark.asyncio]


class _NoAnalysis(CexHandler):
    """Returns no explanation.

    A rule in the examples is meant to fail, so this handler runs. The test
    has no LLM.
    """

    async def analyze(
        self,
        all_results: list[RuleResult],
        tool_call_id: str,
        callbacks: CexProgressCallbacks,
        report_dir: Path,
    ) -> str:
        return ""

#: Checkout of the public examples repo. Not vendored: a copy in this tree
#: would stop tracking upstream.
EXAMPLES_ENV = "SOLANA_EXAMPLES_REPO"
DEFAULT_EXAMPLES = Path("~/src/SolanaExamples").expanduser()

#: The example whose expected-verdict file covers success, violation, and a sanity failure.
EXAMPLE = Path("cvlr_by_example/first_example")

#: Expected-verdict names (``Reports/output.json``) to the treeView names the
#: result parser returns. The expected file uses the fixture's names.
EXPECTED_TO_TREEVIEW = {
    "SUCCESS": "VERIFIED",
    "FAIL": "VIOLATED",
    "SANITY_FAIL": "SANITY_FAILED",
}


def _shipped_cli_only() -> None:
    """Skip when ``$CERTORA`` points at a Prover source checkout.

    ``$CERTORA`` makes :func:`composer.certora_env.import_prover_entry` import
    the CLI from a local Prover build (:mod:`composer.certora_env`). That build
    reports itself as "no package installed", so the run is rejected before
    upload unless it also names a ``prover_version``. A local branch need not
    match the release the fixture's verdicts were recorded against, and the
    failure names neither ``$CERTORA`` nor that file.
    """
    if os.environ.get("CERTORA"):
        pytest.skip(
            "$CERTORA points this run at a Prover source checkout, whose verdicts need not match "
            "the fixture's expected file. Rerun with `env -u CERTORA` to gate the shipped CLI."
        )


def _examples_root() -> Path:
    root = Path(os.environ.get(EXAMPLES_ENV, DEFAULT_EXAMPLES)).expanduser()
    if not (root / EXAMPLE / "Cargo.toml").is_file():
        pytest.skip(
            f"no SolanaExamples checkout at {root}; clone "
            f"https://github.com/Certora/SolanaExamples and set ${EXAMPLES_ENV}"
        )
    return root


@pytest_asyncio.fixture
async def cvlr_confinement() -> SandboxConfig:
    """The production sandbox, or a skip naming why it cannot confine here."""
    config = SandboxConfig(provider="launcher")
    try:
        await ensure_available(config.resolve_provider())
    except SandboxUnavailable as exc:
        pytest.skip(str(exc))
    return config


@pytest.fixture
def workdir(tmp_path: Path) -> Path:
    """A throwaway copy of the examples repo.

    The session writes a private ``CARGO_HOME``, the build script, the conf,
    and ``target/`` into the workdir.
    """
    _shipped_cli_only()
    root = _examples_root()
    if shutil.which("cargo") is None:
        pytest.skip("cargo is not on PATH")
    destination = tmp_path / "SolanaExamples"
    shutil.copytree(root, destination, ignore=shutil.ignore_patterns(".git", "target"))
    return destination


async def test_the_examples_project_verifies_exactly_as_its_authors_expect(
    workdir, cvlr_confinement, capsys
):
    # The authors' own conf, since their expectations were recorded under it. JSON5: it has comments
    # and trailing commas.
    authors_conf = json5.loads(
        (workdir / EXAMPLE / "certora" / "conf" / "Default.conf").read_text()
    )
    expected = json.loads(
        (workdir / EXAMPLE / "certora" / "conf" / "expectedDefault.json").read_text()
    )["rules"]

    wanted_tools = PLATFORM_TOOLS_VERSION
    if not platform_tools_installed(wanted_tools):
        pytest.skip(
            f"Solana platform tools {wanted_tools} are not installed under {PLATFORM_TOOLS_ROOT}"
        )

    session = CargoSession(workdir=workdir, sandbox=cvlr_confinement)
    assert isinstance(await session.warm(manifest_dirs=(EXAMPLE,)), Warmed)

    # Same crate as the slow build below. Both durations are printed.
    fast = await session.check(package="first_example", features=("certora",))
    assert fast.ok, fast.verdict

    submission = Submission(
        manifest_path=workdir / EXAMPLE / "Cargo.toml",
        stem="first_example",
        msg="AutoProver CVLR plumbing gate",
    )
    prepared = await prepare_submission(session, submission)
    assert not isinstance(prepared, BuildRejected), prepared
    # Keep the authors' settings. Drop `files`: it names their prebuilt `.so`,
    # and the prover rejects that next to a build script.
    ours = json.loads(prepared.conf_path.read_text())
    conf = {
        **{k: v for k, v in authors_conf.items() if k != "files"},
        "build_script": ours["build_script"],
        "msg": ours["msg"],
    }
    prepared.conf_path.write_text(dump_conf(conf))
    outcome = await run_submission(
        session,
        prepared,
        prover_opts=make_prover_options(cloud=True, app="solana"),
        cex=_NoAnalysis(),
    )
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
