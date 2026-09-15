"""What the container image has to agree with the code about.

``docs/cvlr-backend-plan.md`` §7.8 gives the image the Rust toolchain and the Solana platform-tools
release a CVLR run builds with. Everything in [scripts/Dockerfile](../scripts/Dockerfile) that is
*also* stated in Python is a drift hazard, and the drift is silent in the worst way: the image
builds, the container starts, preflight passes — the fast tier is a plain ``cargo check`` and cares
about none of this — and the run fails at its first submission, several agents in, with a message
about a toolchain version nobody chose.

These are cheap text assertions rather than a build, because building the image takes tens of
minutes and downloads gigabytes. What they cover is the part a reviewer cannot see: two files that
have to say the same thing.
"""

import re
from pathlib import Path

import pytest

from composer.spec.cvlr.conf import TEMPLATE_BASE, tools_version

_DOCKERFILE = Path(__file__).parent.parent / "scripts" / "Dockerfile"
_ENTRYPOINT = Path(__file__).parent.parent / "scripts" / "autoprove-entrypoint.sh"


def _arg(name: str) -> str:
    """The default of a Dockerfile ``ARG``, which must exist and must have one."""
    match = re.search(rf"^ARG {re.escape(name)}=(\S+)$", _DOCKERFILE.read_text(), re.MULTILINE)
    assert match is not None, f"{_DOCKERFILE} declares no `ARG {name}=<default>`"
    return match[1]


def test_the_image_bakes_the_tools_version_the_conf_template_asks_for():
    """A confined build cannot fetch platform tools — no network, and the cache is granted
    read-only — so the version the image holds is the version a run can use. A project that pins a
    different one in its own base conf is a rebuild, and ``PlatformToolsMissing`` says so; a
    *default* the image does not hold is nobody's decision and fails every run."""
    assert _arg("SOLANA_TOOLS_VERSION") == tools_version(dict(TEMPLATE_BASE))


def test_the_solana_toolchain_is_installed_by_default():
    """Opting out gives an image whose ``console-solana`` cannot be repaired at run time, so it is
    an explicit build-time choice rather than the shipped one."""
    assert _arg("SOLANA_TOOLCHAIN") == "1"


@pytest.mark.parametrize("command", ["console-solana", "tui-solana"])
def test_the_entrypoint_guards_both_solana_front_ends(command):
    """The console and the TUI reach the same pipeline, so a guard on one of them is a guard the
    other run finds out about from a fail-closed provider error instead."""
    case = re.search(r"^  (\S*console-solana\S*)\)$", _ENTRYPOINT.read_text(), re.MULTILINE)
    assert case is not None, f"{_ENTRYPOINT} has no console-solana case"
    assert command in case[1].split("|")


def test_the_final_chmod_does_not_recurse_into_the_toolchain_trees():
    """``chmod`` makes overlayfs copy a file up into the layer that runs it whether or not the mode
    changes, so a recursive pass over the ~3 GB of Rust and platform tools would store a second copy
    of all of it. The trees are already world-readable (umask 022), so skipping them costs nothing
    — but the skip is invisible from the outside, and restoring the one-line ``chmod -R`` would
    double the image with no symptom anyone would notice."""
    text = _DOCKERFILE.read_text()
    assert "chmod -R go+rX $AUTOPROVE_HOME" not in text
    for var in ("RUSTUP_HOME", "CARGO_HOME", "CERTORA_PLATFORM_TOOLS_ROOT"):
        assert f'"${var}"' in text.split("# Final perms")[1]


def test_the_purge_keeps_the_host_c_toolchain_for_a_solana_image():
    """``cargo check`` links the project's build scripts for the host and the SBF build links every
    proc-macro, so an image with the Solana toolchain and no ``cc`` fails at the first compile with
    a linker error about a crate the author never wrote."""
    purge = re.search(r'^\s*purge="([^"]*)";', _DOCKERFILE.read_text(), re.MULTILINE)
    assert purge is not None, f"{_DOCKERFILE} no longer builds a purge list"
    assert "build-essential" not in purge[1].split()
