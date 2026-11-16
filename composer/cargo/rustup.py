"""Running cargo with a named rustup toolchain in place of the project's pin."""

import os
import shutil
import subprocess


class ToolchainMissing(RuntimeError):
    """The rustup toolchain a command was asked to run with is not installed."""

    def __init__(self, toolchain: str, detail: str):
        self.toolchain = toolchain
        super().__init__(
            f"Rust toolchain {toolchain} is not installed. Cargo runs with it in place of the "
            f"project's own pin. Install it with `rustup toolchain install {toolchain}`."
            + (f"\n{detail}" if detail else "")
        )


def toolchain_env(toolchain: str | None) -> dict[str, str]:
    """The environment that runs cargo with ``toolchain``. ``None`` follows the project.

    rustup reads ``RUSTUP_TOOLCHAIN`` before ``rust-toolchain.toml``. A cargo that rustup does not
    manage ignores both variables.

    ``RUSTUP_AUTO_INSTALL=0`` makes a missing toolchain an error rather than a download. The
    download is slow, two runs that start it at once race, and a container's read-only rustup home
    cannot take it at all. rustup before 1.28 ignores the variable and installs.
    """
    if toolchain is None:
        return {}
    return {"RUSTUP_TOOLCHAIN": toolchain, "RUSTUP_AUTO_INSTALL": "0"}


def require_toolchain(toolchain: str) -> None:
    """Raise :class:`ToolchainMissing` unless rustup has ``toolchain`` installed.

    Without rustup on ``PATH`` there is nothing to check: the cargo there ignores
    ``RUSTUP_TOOLCHAIN``.
    """
    rustup = shutil.which("rustup")
    if rustup is None:
        return
    probe = subprocess.run(
        [rustup, "which", "--toolchain", toolchain, "cargo"],
        capture_output=True,
        text=True,
        env={**os.environ, **toolchain_env(toolchain)},
    )
    if probe.returncode != 0:
        raise ToolchainMissing(toolchain, probe.stderr.strip())
