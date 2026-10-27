"""What the container images have to agree with the code about.

``docs/cvlr-backend-plan.md`` §7.8 gives the Solana image the Rust toolchain and the platform-tools
release a CVLR run builds with. Everything in [scripts/Dockerfile.solana](../scripts/Dockerfile.solana)
that is *also* stated in Python is a drift hazard, and the drift is silent in the worst way: the
image builds, the container starts, preflight passes — the fast tier is a plain ``cargo check`` and
cares about none of this — and the run fails at its first submission, several agents in, with a
message about a toolchain version nobody chose.

These are cheap text assertions rather than a build, because building the image takes tens of
minutes and downloads gigabytes. What they cover is the part a reviewer cannot see: files that have
to say the same thing.
"""

import re
from pathlib import Path

import pytest

from composer.cargo.sbf import SBF_SUBCOMMAND_VERSION
from composer.cargo.symbols import RUSTFILT_VERSION
from composer.scripts.cvlr_api_docs import PINNED_NIGHTLY
from composer.scripts.solana_toolchain import (
    REQUIRED_ENV,
    RemoveTree,
    RequireDir,
    Run,
    api_manifest_steps,
    toolchain_steps,
)
from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN, PLATFORM_TOOLS_VERSION

_SCRIPTS = Path(__file__).parent.parent / "scripts"
_BASE_DOCKERFILE = _SCRIPTS / "Dockerfile"
_SOLANA_DOCKERFILE = _SCRIPTS / "Dockerfile.solana"
_ENTRYPOINT = _SCRIPTS / "autoprove-entrypoint.sh"


def test_the_solana_image_states_no_version_of_its_own():
    """The versions come from ``composer.scripts.solana_toolchain``, which reads them from the code
    that uses them. A version ARG here would be a second source the cloud image does not share."""
    text = _SOLANA_DOCKERFILE.read_text()
    for name in ("RUST_VERSION", "CARGO_CERTORA_SBF_VERSION", "SOLANA_TOOLS_VERSION", "RUST_NIGHTLY"):
        assert f"ARG {name}" not in text


@pytest.mark.parametrize("step", ["toolchain", "api-manifest", "gitconfig"])
def test_the_solana_image_runs_every_toolchain_step(step):
    assert f"python -m composer.scripts.solana_toolchain {step}" in _SOLANA_DOCKERFILE.read_text()


def test_the_solana_image_sets_what_the_toolchain_steps_and_the_sandbox_read():
    env = re.search(r"^ENV (.*?)(?:\n\n|\Z)", _SOLANA_DOCKERFILE.read_text(), re.MULTILINE | re.DOTALL)
    assert env is not None
    for name in REQUIRED_ENV:
        assert f"{name}=" in env[1]


def test_the_toolchain_steps_install_the_versions_every_build_uses():
    """A confined build cannot fetch platform tools or a toolchain: the image holds what a run can
    use, and the runtime user cannot write the image's rustup home."""
    steps = toolchain_steps(
        {"CARGO_HOME": "/c", "CERTORA_PLATFORM_TOOLS_ROOT": "/t", "RUSTUP_HOME": "/r"}
    )
    commands = " ".join(" ".join(s.argv) for s in steps if isinstance(s, Run))
    assert f"--default-toolchain {HOST_RUST_TOOLCHAIN}" in commands
    assert f"--tools-version {PLATFORM_TOOLS_VERSION}" in commands
    assert f"--version {SBF_SUBCOMMAND_VERSION}" in commands
    assert RequireDir(Path("/t") / PLATFORM_TOOLS_VERSION) in steps


def test_the_base_image_carries_no_rust_toolchain():
    """Each chain's toolchain is its own image layered on the base one, so that an EVM run does not
    carry gigabytes it never invokes and two chains need not agree on a Rust version. Folding this
    one back into the base would work, and would quietly take that back."""
    assert "rustup" not in _BASE_DOCKERFILE.read_text()


@pytest.mark.parametrize("command", ["console-solana", "tui-solana"])
def test_the_entrypoint_guards_both_solana_front_ends(command):
    """The console and the TUI reach the same pipeline, so a guard on one of them is a guard the
    other run finds out about from a fail-closed provider error instead."""
    case = re.search(r"^  (\S*console-solana\S*)\)$", _ENTRYPOINT.read_text(), re.MULTILINE)
    assert case is not None, f"{_ENTRYPOINT} has no console-solana case"
    assert command in case[1].split("|")


def test_the_solana_image_does_not_chmod_its_toolchain_trees():
    """``chmod`` makes overlayfs copy a file up into the layer that runs it whether or not the mode
    changes, so a recursive pass over the ~3 GB of Rust and platform tools would store a second copy
    of all of it. The installers write under umask 022, so the runtime user can already read them —
    but that is invisible from the outside, and adding a tidy ``chmod -R`` here would double the
    image with no symptom anyone would notice."""
    assert "chmod -R" not in _SOLANA_DOCKERFILE.read_text()


def test_the_solana_image_restores_the_host_c_toolchain():
    """``cargo check`` links the project's build scripts for the host and the SBF build links every
    proc-macro, so an image with the Solana toolchain and no ``cc`` fails at the first compile with
    a linker error about a crate the author never wrote. The base image purges ``build-essential``
    once its own wheels are built, which makes restoring it this image's job."""
    assert "build-essential" in _SOLANA_DOCKERFILE.read_text()


def _run_instructions(dockerfile: Path) -> list[str]:
    """Each ``RUN`` of a Dockerfile, its continuation lines joined and its comments dropped."""
    lines = [line for line in dockerfile.read_text().splitlines() if not line.startswith("#")]
    joined = re.sub(r"\\\n", " ", "\n".join(lines))
    return [line for line in joined.splitlines() if line.startswith("RUN ")]


def _api_manifest_run() -> str:
    runs = [
        run
        for run in _run_instructions(_SOLANA_DOCKERFILE)
        if "composer.scripts.solana_toolchain api-manifest" in run
    ]
    assert len(runs) == 1, f"{_SOLANA_DOCKERFILE} should generate the cvlr_api_kb manifest in one RUN"
    return runs[0]


def test_the_entrypoint_ingests_the_api_manifest_the_solana_image_bakes():
    """The manifest is written at build time and read at setup-db time, by two files that name its
    path separately. If they disagree, setup-db takes the branch for an image that carries none —
    a clean skip, by design, so the Solana image would ship an API corpus nothing ever ingests."""
    output = re.search(r"--output \"(\$AUTOPROVE_HOME/\S+\.rag\.json)\"", _api_manifest_run())
    assert output is not None, "the cvlr_api_docs step writes no manifest under $AUTOPROVE_HOME"
    entrypoint = _ENTRYPOINT.read_text()
    assert f'cvlr_api_manifest="{output[1]}"' in entrypoint
    assert 'composer.scripts.rag_import --replace "$cvlr_api_manifest"' in entrypoint


def test_setup_db_empties_each_corpus_before_ingesting_it():
    """Ingest only inserts. Without --replace a second setup-db dies on the CVL manual's unique index
    after inserting part of it, and never reaches the CVLR corpora."""
    entrypoint = _ENTRYPOINT.read_text()
    ingests = re.findall(r"python -m composer\.scripts\.(?:ragbuild|rag_import)[^\n]*", entrypoint)
    assert len(ingests) == 3, ingests
    assert all("--replace" in line for line in ingests), ingests


def test_setup_db_can_leave_the_cvl_manual_alone():
    """A shared database's CVL manual belongs to the EVM deployment; seeding CVLR must not rebuild it."""
    entrypoint = _ENTRYPOINT.read_text()
    assert "--cvlr-only) cvlr_only=true" in entrypoint
    cvl = entrypoint.index('"$AUTOPROVE_HOME/prover-docs/cvl.html"')
    guard = entrypoint.rfind('if [[ "$cvlr_only" == true ]]', 0, cvl)
    assert guard != -1 and "fi" not in entrypoint[guard:cvl].split()


_MANIFEST_STEPS = api_manifest_steps({"CARGO_HOME": "/c"}, Path("/out.json"))
_GENERATE = next(
    s for s in _MANIFEST_STEPS
    if isinstance(s, Run) and "composer.scripts.cvlr_api_docs" in s.argv
)


def test_the_api_manifest_step_discards_its_toolchain_and_registry():
    """Only the manifest is needed at run time. The nightly toolchain and the warmed registry have
    to go in the same ``RUN`` that made them, because a later layer that deletes them hides them
    without shrinking the image."""
    assert Run(("rustup", "toolchain", "uninstall", PINNED_NIGHTLY)) in _MANIFEST_STEPS
    assert RemoveTree(Path("/c/registry")) in _MANIFEST_STEPS
    assert _MANIFEST_STEPS.index(_GENERATE) < _MANIFEST_STEPS.index(RemoveTree(Path("/c/registry")))


def test_the_api_manifest_step_fetches_its_own_crates():
    """The registry is cold in a fresh layer, and the producer resolves offline unless told
    otherwise, so the step has to ask it to fetch rather than warm the registry by hand."""
    assert "--fetch" in _GENERATE.argv


def test_the_api_manifest_is_generated_by_a_dated_nightly():
    """The producer accepts exactly one rustdoc JSON ``format_version``, so a floating nightly would
    break the image build the day the format moves. The toolchain installed, the one rustdoc runs
    under and the one removed must all be the pinned one."""
    assert re.fullmatch(r"nightly-\d{4}-\d{2}-\d{2}", PINNED_NIGHTLY)
    assert Run(("rustup", "toolchain", "install", PINNED_NIGHTLY, "--profile", "minimal")) in (
        _MANIFEST_STEPS
    )
    assert _GENERATE.argv[_GENERATE.argv.index("--toolchain") + 1] == PINNED_NIGHTLY


def test_the_toolchain_steps_install_the_demangler_the_prover_uses():
    """Without ``rustfilt`` the check that each summary directive matches a symbol in the build is
    skipped with only a warning, so an image that lacks it runs every unit without that check and
    nothing fails."""
    steps = toolchain_steps({"CARGO_HOME": "/c", "CERTORA_PLATFORM_TOOLS_ROOT": "/t"})
    install = Run(("cargo", "install", "--locked", "rustfilt", "--version", RUSTFILT_VERSION))
    assert steps.index(install) < steps.index(RemoveTree(Path("/c/registry")))
