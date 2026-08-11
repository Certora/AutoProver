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

#: Registry protocol pinned across the *warm* (network) and *build* (offline) halves of a
#: sandboxed cargo build, so both read and write the same `registry/index/` layout. They run
#: under different cargos, and cargo's crates.io default flipped from the git index to sparse
#: in 1.70 — leaving it implicit means a pre-1.70 platform-tools cargo silently misses a cache
#: a newer cargo warmed. Sparse (rather than git) because it is what every cargo from 1.68 on
#: supports and it reuses the index a modern host cargo already has; a platform-tools older
#: than 1.68 would need `"git"` here instead.
CARGO_REGISTRY_PROTOCOL_VAR = "CARGO_REGISTRIES_CRATES_IO_PROTOCOL"
CARGO_REGISTRY_PROTOCOL = "sparse"

#: Names of the private, per-run scratch directories a sandboxed build gets *under the workdir*
#: (see :func:`sandbox_cargo_home`, :func:`sandbox_rustup_home`, and the ``TMPDIR`` redirect in
#: :func:`rust_build_policy` for why each one is private rather than shared). Named constants
#: because consumers outside this module must agree on the spellings — notably
#: ``composer.pipeline.ecosystem.RUST_FORBIDDEN_READ``, which hides them from the source tools'
#: file listing so the hundreds of MB they hold never reach the model's context.
SANDBOX_CARGO_DIR = ".sandbox_cargo"
SANDBOX_RUSTUP_DIR = ".sandbox_rustup"
SANDBOX_TMP_DIR = ".sandbox_tmp"

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
    return Path(workdir).resolve() / SANDBOX_CARGO_DIR


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


def sandbox_rustup_home(workdir: str | Path) -> Path:
    """The **private, per-run `RUSTUP_HOME`** for a sandboxed build, under the workdir.

    Same reasoning as :func:`sandbox_cargo_home`: even with a fully pre-installed
    toolchain, the ``rustup`` proxy (which ``cargo``/``rustc``/``cargo-build-sbf`` all
    are) writes scratch into ``$RUSTUP_HOME/tmp`` on every invocation — so a confined
    build against a **read-only** shared ``RUSTUP_HOME`` dies with ``could not create
    temp file …/.rustup/tmp/…: Permission denied``. Granting the *shared* rustup home
    read-write instead would expose it to untrusted ``build.rs`` across runs.

    A per-run home under the (already-writable, per-run) workdir fixes it without that
    exposure: the heavy, immutable ``toolchains`` dir is a **symlink** to the shared
    home (still granted read-only, so its bytes are shared, not copied), while
    ``tmp``/``downloads``/``update-hashes`` are this run's own writable scratch. On a
    host dev flow the shared ``RUSTUP_HOME`` is writable so the gap never showed; a
    shared read-only home (the container image's baked toolchain) is what surfaced it.
    """
    return Path(workdir).resolve() / SANDBOX_RUSTUP_DIR


def solana_toolchain_ro_paths(binary: str = "cargo-build-sbf") -> tuple[Path, ...]:
    """The install tree of the ``binary`` on ``PATH`` — so the *confined* build runs the same
    Solana toolchain the operator selected. Empty when it isn't on ``PATH``.

    Two reasons this needs granting beyond the well-known cache locations:

    * **Where the platform-tools live varies.** A tarball install keeps them next to the binary
      (``<root>/bin/sdk/sbf/dependencies/platform-tools``), not under ``~/.cache/solana``. Without
      the grant the build concludes they are missing and tries to *download* them, which offline
      fails as the thoroughly misleading ``Failed to remove ~/.cache/solana/<ver> while recovering
      from installation failure``.
    * **An unreadable binary is skipped, not refused.** ``execvp`` treats the sandbox's ``EACCES``
      as "keep looking" and runs the *next* ``cargo-build-sbf`` on ``PATH`` — so a confined build
      silently used a different (here: much older) toolchain than the one on ``PATH``, and failed on
      *its* platform-tools instead. Granting the tree keeps that choice honest.
    """
    exe = shutil.which(binary)
    if exe is None:
        return ()
    bin_dir = Path(exe).resolve().parent
    # `<root>/bin/<binary>` → grant `<root>`, so the sibling `sdk/` is readable too.
    return (bin_dir.parent if bin_dir.name == "bin" else bin_dir,)


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

    With ``offline`` (the default — the sandbox has no network, §5),
    ``CARGO_NET_OFFLINE=true`` is set in the child env. Spelled ``true`` because cargo parses
    this one as a config boolean and rejects anything else — ``=1`` aborts the build with
    "provided string was not `true` or `false`" *while still going to the network*, so the
    truthy-looking value is worse than no value at all. That one var forces every cargo offline,
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
        # …and the install tree of the `cargo-build-sbf` actually on PATH, which may be neither.
        *solana_toolchain_ro_paths(),
        # The git config, for a project with **git dependencies**: cargo resolves those
        # through libgit2, which opens the global config before doing anything with a repo —
        # even offline against an already-warm checkout. Without it cargo fails the whole
        # metadata read with a misleading `'~/.gitconfig' is locked: Permission denied`.
        # Config only, both spellings; the git *credential* stores (`~/.git-credentials`, a
        # helper's) stay ungranted, as `~/.cargo/credentials.toml` does.
        *(home / p for p in (".gitconfig", ".config/git/config")),
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
        # The spelling matters beyond our own cargo: the *target project* picks which one runs
        # the build (one real program's `rust-toolchain.toml` pins 1.74), so it has to be what
        # every version accepts.
        env["CARGO_NET_OFFLINE"] = "true"
    # Pin the registry protocol so the offline build reads the index layout the WARM STEP
    # WROTE. `CARGO_HOME` alone does not make them agree: the two run under different cargos
    # (the warm uses the program's own toolchain, the build uses platform-tools'), and cargo
    # switched its crates.io default from the git index to sparse in 1.70 — so a 1.68
    # platform-tools cargo would look in `registry/index/index.crates.io-6f17…` (git) while a
    # newer warm filled `…-1949…` (sparse), and the offline build fails with a bewildering
    # "no matching package named <some crate> found". Both sides now say sparse explicitly.
    # See `warm_cargo_cache`, which sets the same variable.
    env[CARGO_REGISTRY_PROTOCOL_VAR] = CARGO_REGISTRY_PROTOCOL
    # A private temp dir UNDER the (writable) workdir, so tools that need scratch space
    # — notably the linker, which writes to $TMPDIR (default /tmp) during `cargo build` —
    # work without granting the shared /tmp (which may hold host/other-run secrets and
    # would defeat the escape test). Created here so $TMPDIR points at an existing dir.
    sandbox_tmp = wd / SANDBOX_TMP_DIR
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

    # Point RUSTUP_HOME at a PRIVATE per-run home under the workdir (see
    # sandbox_rustup_home). The shared home stays read-only (granted above via
    # `rustup`); the per-run home symlinks `toolchains` back to it (so the RO grant
    # still covers the resolved toolchain files) and keeps rustup's writable scratch
    # — tmp/downloads/update-hashes — inside the (writable) workdir. `settings.toml`
    # is copied so default/override resolution still works.
    rustup_home = sandbox_rustup_home(wd)
    rustup_home.mkdir(parents=True, exist_ok=True)
    tc_link = rustup_home / "toolchains"
    shared_toolchains = rustup / "toolchains"
    # `exists()` follows the link, so a STALE link — a workdir left over from a run whose shared
    # rustup home has since moved — reads as absent, and `symlink_to` on it would raise
    # FileExistsError. Ask about the link itself, and re-point it.
    if tc_link.is_symlink() and tc_link.resolve() != shared_toolchains.resolve():
        tc_link.unlink()
    if not (tc_link.is_symlink() or tc_link.exists()) and shared_toolchains.is_dir():
        tc_link.symlink_to(shared_toolchains)
    src_settings = rustup / "settings.toml"
    if src_settings.is_file() and not (rustup_home / "settings.toml").exists():
        shutil.copy(src_settings, rustup_home / "settings.toml")
    for scratch in ("tmp", "downloads", "update-hashes"):
        (rustup_home / scratch).mkdir(exist_ok=True)
    env["RUSTUP_HOME"] = str(rustup_home)

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
