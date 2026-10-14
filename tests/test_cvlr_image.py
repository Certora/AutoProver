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

from composer.spec.cvlr.conf import HOST_RUST_TOOLCHAIN, PLATFORM_TOOLS_VERSION

_SCRIPTS = Path(__file__).parent.parent / "scripts"
_BASE_DOCKERFILE = _SCRIPTS / "Dockerfile"
_SOLANA_DOCKERFILE = _SCRIPTS / "Dockerfile.solana"
_ENTRYPOINT = _SCRIPTS / "autoprove-entrypoint.sh"


def _arg(name: str) -> str:
    """The default of a ``Dockerfile.solana`` ``ARG``, which must exist and must have one."""
    match = re.search(
        rf"^ARG {re.escape(name)}=(\S+)$", _SOLANA_DOCKERFILE.read_text(), re.MULTILINE
    )
    assert match is not None, f"{_SOLANA_DOCKERFILE} declares no `ARG {name}=<default>`"
    return match[1]


def test_the_image_bakes_the_tools_version_every_build_uses():
    """A confined build cannot fetch platform tools — no network, and the cache is granted
    read-only — so the version the image holds is the version a run can use. A version the image
    does not hold fails every run."""
    assert _arg("SOLANA_TOOLS_VERSION") == PLATFORM_TOOLS_VERSION


def test_the_image_bakes_the_host_toolchain_every_cargo_command_uses():
    """The runtime user cannot write the image's rustup home, so a toolchain the image lacks cannot
    be installed when a run asks for it, and cargo does not start."""
    assert _arg("RUST_VERSION") == HOST_RUST_TOOLCHAIN


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
        if "composer.scripts.cvlr_api_docs" in run
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
    assert 'composer.scripts.rag_import "$cvlr_api_manifest"' in entrypoint


def test_the_api_manifest_step_discards_its_toolchain_and_registry():
    """Only the manifest is needed at run time. The nightly toolchain and the warmed registry have
    to go in the same ``RUN`` that made them, because a later layer that deletes them hides them
    without shrinking the image."""
    run = _api_manifest_run()
    assert 'rustup toolchain uninstall "$RUST_NIGHTLY"' in run
    assert '"$CARGO_HOME/registry"' in run


def test_the_api_manifest_step_fetches_its_own_crates():
    """The registry is cold in a fresh layer, and the producer resolves offline unless told
    otherwise, so the step has to ask it to fetch rather than warm the registry by hand."""
    assert "--fetch" in _api_manifest_run()


def test_the_api_manifest_is_generated_by_a_dated_nightly():
    """The producer accepts exactly one rustdoc JSON ``format_version``, so a floating nightly would
    break the image build the day the format moves. The toolchain installed, the one rustdoc runs
    under and the one removed must all be the pinned one."""
    pin = re.search(r"^ARG RUST_NIGHTLY=(\S+)$", _SOLANA_DOCKERFILE.read_text(), re.MULTILINE)
    assert pin is not None and re.fullmatch(r"nightly-\d{4}-\d{2}-\d{2}", pin.group(1))
    run = _api_manifest_run()
    assert 'rustup toolchain install "$RUST_NIGHTLY"' in run
    assert '--toolchain "$RUST_NIGHTLY"' in run
