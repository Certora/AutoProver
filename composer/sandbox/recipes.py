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

import os
import shutil
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


def sandbox_cargo_home(workdir: str | Path) -> Path:
    """The **private, per-run `CARGO_HOME`** for a sandboxed build, under the workdir.

    Why a private cargo home rather than the shared `~/.cargo`:

    An offline `cargo build` doesn't just *read* the cache — it *writes* to `CARGO_HOME`
    (extracts crate sources into `registry/src`, takes `.package-cache` locks). To let
    the confined build do that we'd have to grant `CARGO_HOME` read-write. But the same
    build runs **untrusted `build.rs`/proc-macro code**, so a writable *shared* cargo
    home is a cross-run attack surface: a malicious build could overwrite an extracted
    source under `registry/src` and poison a *later* run that compiles that crate (cargo
    checksums the downloaded `.crate`, but trusts an already-extracted `registry/src`).

    A per-run home under the (already-writable, per-run) workdir removes that: any write
    the untrusted build makes touches only this run's throwaway cache, never a shared one.
    The cost is that deps are fetched per run (the warm step downloads into this home);
    a shared *read-only* index/cache to avoid re-download is a deferred optimization
    (command-sandbox.md §11 item 5).
    """
    return Path(workdir).resolve() / ".sandbox_cargo"


def shared_cargo_ro_paths(cargo_home: str | Path) -> tuple[Path, ...]:
    """RO subtrees of the *shared* cargo home that sandboxed builds may need.

    Never grants the cargo-home **root**: that directory often holds
    ``credentials.toml`` / ``credentials`` (crates.io and private-registry tokens).
    Landlock PathBeneath is hierarchical, so granting the root would leak those.

    Today only ``bin/`` is granted (the ``cargo`` / ``cargo-*`` shims on ``PATH``).
    Offline deps live in the private per-run :func:`sandbox_cargo_home`, so the
    shared ``registry/`` and ``git/`` trees are not required. A future shared
    read-only cache optimization can add specific cache subtrees here without
    re-opening the credentials file.
    """
    bin_dir = Path(cargo_home) / "bin"
    return (bin_dir,) if bin_dir.is_dir() else ()


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
    toolchain (``RUSTUP_HOME``), the shared cargo **bin/** only (not the cargo-home
    root — see :func:`shared_cargo_ro_paths`), Solana platform-tool directories, the
    system dirs, and ``extra_ro`` read-only. Non-existent paths are dropped.

    With ``offline`` (the default — the sandbox has no network, §5), ``CARGO_NET_OFFLINE=1``
    is set in the child env. That one var forces *every* cargo invocation offline,
    including the nested ``cargo`` that ``crucible run`` spawns to build the harness —
    so the deps must already be warm in the private ``CARGO_HOME`` (see
    :func:`warm_cargo_cache`, run *outside* the sandbox first).
    """
    home = Path.home()
    rustup = Path(os.environ.get("RUSTUP_HOME", home / ".rustup"))
    cargo = Path(os.environ.get("CARGO_HOME", home / ".cargo"))

    ro_candidates: list[Path] = [Path(p) for p in _SYSTEM_RO]
    ro_candidates += [
        rustup,
        # Shared cargo: bin/ only — never the home root (credentials.toml).
        *shared_cargo_ro_paths(cargo),
        # cargo-build-sbf's downloaded sBPF platform-tools (layout varies by version).
        home / ".cache" / "solana",
        home / ".local" / "share" / "solana",
    ]
    ro_candidates.extend(extra_ro)
    # Absolute paths only: the launcher opens each relative to *its* cwd (the workdir),
    # so a relative grant would resolve wrong. resolve() also canonicalizes symlinks.
    ro_paths = tuple(p.resolve() for p in ro_candidates if p.exists())

    dev = tuple(Path(d).resolve() for d in _DEV_NODES if Path(d).exists())
    wd = Path(workdir).resolve()
    rw_paths = (wd, *dev, *(p.resolve() for p in extra_rw))

    env = {name: os.environ[name] for name in env_passthrough if name in os.environ}
    if offline:
        env["CARGO_NET_OFFLINE"] = "1"
    # A private temp dir UNDER the (writable) workdir, so tools that need scratch space
    # — notably the linker, which writes to $TMPDIR (default /tmp) during `cargo build` —
    # work without granting the shared /tmp (which may hold host/other-run secrets and
    # would defeat the escape test). Created here so $TMPDIR points at an existing dir.
    sandbox_tmp = wd / ".sandbox_tmp"
    sandbox_tmp.mkdir(parents=True, exist_ok=True)
    for var in ("TMPDIR", "TMP", "TEMP"):
        env[var] = str(sandbox_tmp)

    # Point CARGO_HOME at a PRIVATE per-run cargo home under the workdir (see
    # sandbox_cargo_home for the reasoning). The shared ~/.cargo root is *not*
    # granted RO; only bin/ is (above). Copy the user's global cargo config into
    # the private home so registry mirrors / build settings still apply — that
    # copy is trusted-host code, not a Landlock grant of the secrets file.
    cargo_home = sandbox_cargo_home(wd)
    cargo_home.mkdir(parents=True, exist_ok=True)
    shared_cargo = Path(os.environ.get("CARGO_HOME", Path.home() / ".cargo"))
    for cfg in ("config.toml", "config"):
        src = shared_cargo / cfg
        if src.is_file() and not (cargo_home / cfg).exists():
            shutil.copy(src, cargo_home / cfg)
    env["CARGO_HOME"] = str(cargo_home)

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
