"""Ready-made :class:`SandboxPolicy` recipes.

The seam (:mod:`composer.sandbox.policy`) is mechanism- *and* workload-agnostic;
this module holds opinionated builders for common workloads. :func:`rust_build_policy`
covers "compile and/or run Rust" (``cargo build-sbf``, ``cargo build``, ``crucible
run``): it grants the workdir read-write, the discoverable Rust/Solana toolchains
read-only, the device nodes the toolchain needs, and an env allowlist — with the
network off. Any Rust backend reuses it; Crucible adds its own paths via ``extra_ro``.

Paths are included only if they exist, so the same recipe works across machines
with different toolchain layouts (and the escape-test gate can prove exactly what
was and wasn't granted).
"""

from __future__ import annotations

import os
from pathlib import Path

from composer.sandbox.policy import SandboxPolicy

# Benign build vars passed through to the child (values read from the current env).
# Never secrets — the whole point is that secrets are *not* inherited.
DEFAULT_ENV_PASSTHROUGH: tuple[str, ...] = (
    "PATH",
    "HOME",
    "TERM",
    "LANG",
    "LC_ALL",
    "USER",
    "LOGNAME",
    "TMPDIR",
    "CARGO_HOME",
    "RUSTUP_HOME",
    "SSL_CERT_FILE",
    "SSL_CERT_DIR",
)

# Read-only system directories the toolchain + its dynamic linker need. ``/etc`` is
# included because glibc NSS (``getpwuid`` via ``getuser``, CA-cert lookup) reads
# ``/etc/passwd`` / ``/etc/nsswitch.conf``; it holds no AutoProver secret (those are
# in the scrubbed env and in files we never grant). The escape gate must therefore
# probe a *planted* host file / the parent's environ, not ``/etc/passwd``.
_SYSTEM_RO: tuple[str, ...] = ("/usr", "/bin", "/sbin", "/lib", "/lib64", "/etc")

# Device nodes the toolchain opens (rw so ``/dev/null`` writes work). Granting the
# node files — not the whole ``/dev`` tree; ``mknod`` stays blocked (no capability).
_DEV_NODES: tuple[str, ...] = (
    "/dev/null",
    "/dev/zero",
    "/dev/full",
    "/dev/random",
    "/dev/urandom",
    "/dev/tty",
)


def rust_build_policy(
    workdir: str | Path,
    *,
    extra_ro: tuple[Path, ...] = (),
    extra_rw: tuple[Path, ...] = (),
    env_passthrough: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH,
    offline: bool = True,
    mem_bytes: int | None = None,
    cpu_seconds: int | None = None,
    nproc: int | None = None,
    fsize_bytes: int | None = None,
) -> SandboxPolicy:
    """Build a network-off policy for compiling/running Rust in ``workdir``.

    Grants: ``workdir`` + the device nodes (+ ``extra_rw``) read-write; the Rust
    (``RUSTUP_HOME``/``CARGO_HOME``) and Solana platform-tool directories, the system
    dirs, and ``extra_ro`` read-only. Non-existent paths are dropped.

    With ``offline`` (the default — the sandbox has no network, §5), ``CARGO_NET_OFFLINE=1``
    is set in the child env. That one var forces *every* cargo invocation offline,
    including the nested ``cargo`` that ``crucible run`` spawns to build the harness —
    so the deps must already be warm in ``CARGO_HOME`` (see :func:`warm_cargo_cache`,
    run *outside* the sandbox first).
    """
    home = Path.home()
    rustup = Path(os.environ.get("RUSTUP_HOME", home / ".rustup"))
    cargo = Path(os.environ.get("CARGO_HOME", home / ".cargo"))

    ro_candidates: list[Path] = [Path(p) for p in _SYSTEM_RO]
    ro_candidates += [
        rustup,
        cargo,
        # cargo-build-sbf's downloaded sBPF platform-tools (layout varies by version).
        home / ".cache" / "solana",
        home / ".local" / "share" / "solana",
    ]
    ro_candidates += list(extra_ro)
    ro_paths = tuple(p for p in ro_candidates if p.exists())

    dev = tuple(Path(d) for d in _DEV_NODES if Path(d).exists())
    rw_paths = (Path(workdir), *dev, *extra_rw)

    env = {name: os.environ[name] for name in env_passthrough if name in os.environ}
    if offline:
        env["CARGO_NET_OFFLINE"] = "1"

    return SandboxPolicy(
        rw_paths=rw_paths,
        ro_paths=ro_paths,
        env_allowlist=env,
        network=False,
        mem_bytes=mem_bytes,
        cpu_seconds=cpu_seconds,
        nproc=nproc,
        fsize_bytes=fsize_bytes,
    )
