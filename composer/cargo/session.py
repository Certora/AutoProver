"""Reused workdir for a Rust compile loop, and the fast compile gate.

A compile sits in the authoring inner loop. Each run also gets a private
``CARGO_HOME`` so an untrusted ``build.rs`` cannot poison a later run
(:func:`~composer.sandbox.recipes.sandbox_cargo_home`). Fetching the dependency
graph on every edit is too slow, so the workdir is owned by a session and
:meth:`CargoSession.warm` fetches once.

Warm vs compile is a trust boundary (``docs/command-sandbox.md`` §5).
``cargo fetch`` downloads and does not execute, so it runs unconfined with the
network. Compile runs confined and offline, using the deps the warm already
fetched.

The fast tier is a ``cargo check``, not the chain build. By default it checks
the host target. A :class:`CrossCheck` checks the chain's target instead, with
the chain's compiler, when the host cannot compile the program the way the
chain build does.
"""

import logging
import time
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Literal

from composer.cargo.features import CargoFeature
from composer.sandbox.command import CommandResult, run_local_command
from composer.sandbox.config import BackendSpec, SandboxConfig
from composer.sandbox.recipes import sandbox_cargo_home

_log = logging.getLogger(__name__)

#: ``fast`` runs on every write; ``slow`` only before a prover submission.
type CompileTier = Literal["fast", "slow"]

#: A cold ``cargo fetch`` of a real program can take several minutes.
WARM_TIMEOUT_S = 900
#: Long enough for a cold graph; short enough that a stuck compiler times out
#: inside one authoring turn.
CHECK_TIMEOUT_S = 600


@dataclass(frozen=True)
class Compiled:
    pass


@dataclass(frozen=True)
class CompileFailed:
    """``diagnostics`` is rustc's human-format output, unparsed."""

    diagnostics: str
    exit_code: int


type CompileVerdict = Compiled | CompileFailed


@dataclass(frozen=True)
class CompileRun:
    tier: CompileTier
    duration_ms: int
    verdict: CompileVerdict
    #: Copied onto the result so an unconfined verdict is visible.
    #: :class:`CargoSession` logs the operator warning.
    confined: bool

    @property
    def ok(self) -> bool:
        return isinstance(self.verdict, Compiled)


@dataclass(frozen=True)
class Warmed:
    pass


@dataclass(frozen=True)
class WarmFailed:
    """Logged, not raised: a partial cache still compiles what it has, and the
    later build error names the missing crate."""

    diagnostics: str
    exit_code: int


type WarmOutcome = Warmed | WarmFailed


@dataclass(frozen=True)
class HostCheck:
    """``cargo check`` for the host target, with the session's rustup toolchain."""


@dataclass(frozen=True)
class CrossCheck:
    """``cargo check`` for another target, with a cargo and compiler rustup does not manage."""

    cargo: Path
    #: What makes the check compile what the chain build compiles: the target, and the profile.
    args: tuple[str, ...]
    #: The compiler and the tools build scripts reach for. They must be the chain build's own:
    #: a build script that reruns when one of them changes would otherwise be rebuilt, and every
    #: crate depending on it, each time the check and the build take turns in one target directory.
    env: Mapping[str, str]


type CheckWith = HostCheck | CrossCheck


@dataclass
class CargoSession:
    """A workdir reused across an authoring session's compiles.

    ``workdir`` is where every command runs, and the only path the confinement
    policy grants read-write. The caller chooses it and cleans it up; the session
    does not create or delete it.
    """

    workdir: Path
    sandbox: SandboxConfig
    #: The rustup toolchain every cargo command in this session runs with, in place of the one the
    #: project's ``rust-toolchain.toml`` pins. ``None`` follows the project.
    toolchain: str | None = None
    #: How :meth:`check` compiles.
    check_with: CheckWith = HostCheck()
    #: Keyed by the cargo binary. Two cargos do not share a git cache, so a
    #: cache filled by one is not warm for the other.
    _warmed: set[str] = field(default_factory=set, repr=False, compare=False)

    def __post_init__(self) -> None:
        if not self.sandbox.enabled:
            _log.warning(
                "cargo session in %s is UNCONFINED (COMPOSER_SANDBOX_PROVIDER=none): build scripts "
                "and proc-macros from the analyzed project run with this process's privileges. "
                "Development only — every result produced this way is marked unconfined.",
                self.workdir,
            )

    @property
    def confined(self) -> bool:
        return self.sandbox.enabled

    @property
    def toolchain_env(self) -> dict[str, str]:
        """What selects :attr:`toolchain` in a child's environment. rustup reads
        ``RUSTUP_TOOLCHAIN`` ahead of a ``rust-toolchain.toml``; a cargo rustup does not manage
        ignores it."""
        return {"RUSTUP_TOOLCHAIN": self.toolchain} if self.toolchain is not None else {}

    @property
    def check_cargo(self) -> str:
        """The cargo :meth:`check` runs, and so the one whose cache :meth:`warm` fills by default."""
        match self.check_with:
            case HostCheck():
                return "cargo"
            case CrossCheck(cargo=cargo):
                return str(cargo)

    def _env(self, extra: Mapping[str, str]) -> dict[str, str]:
        return {**self.toolchain_env, **extra}

    def _confinement(self, extra: Mapping[str, str] = {}) -> SandboxConfig:
        return replace(self.sandbox, env={**self.sandbox.env, **self._env(extra)})

    @property
    def cargo_home(self) -> Path | None:
        """Private per-run home when confined, else the process default.

        Warm runs outside the sandbox, so it has to use the same directory the
        confined build will read (:func:`~composer.sandbox.recipes.sandbox_cargo_home`).
        """
        return sandbox_cargo_home(self.workdir) if self.sandbox.enabled else None

    async def run_confined(
        self, program: str, args: list[str], *, timeout_s: int, env: Mapping[str, str] = {}
    ) -> CommandResult:
        """Run a command in the workdir under this session's confinement, with ``env`` added to
        the toolchain's.

        If the configured provider cannot confine, this raises instead of falling
        back.
        """
        return await run_local_command(
            program,
            args,
            {},
            workdir=self.workdir,
            timeout_s=timeout_s,
            provider=self.sandbox.resolve_provider() if self.sandbox.enabled else None,
            policy=self._confinement(env).build_policy(self.workdir),
            env_overlay=self._env(env) or None,
        )

    def build_can_write(self, path: Path) -> bool:
        """Whether a build in this session can write ``path``. Unconfined, it can write anything."""
        policy = self.sandbox.build_policy(self.workdir)
        if policy is None:
            return True
        resolved = path.resolve()
        return any(resolved.is_relative_to(granted.resolve()) for granted in policy.rw_paths)

    async def backend_spec(self, *, timeout_s: int) -> BackendSpec:
        """The confinement :meth:`run_confined` applies, as an argv prefix for a
        command launched later."""
        return await self._confinement().backend_spec(self.workdir, timeout_s=timeout_s)

    async def run_unconfined(
        self, program: str, args: list[str], *, timeout_s: int
    ) -> CommandResult:
        """Run a command in the workdir with the network and no confinement.

        For prep steps only (``docs/command-sandbox.md`` §5). Uses the session's
        cargo home so the confined build can read the cache.
        """
        home = self.cargo_home
        if home is not None:
            home.mkdir(parents=True, exist_ok=True)
        return await run_local_command(
            program,
            args,
            {},
            workdir=self.workdir,
            timeout_s=timeout_s,
            env_overlay=(
                {**self.toolchain_env, **({"CARGO_HOME": str(home)} if home is not None else {})}
                or None
            ),
        )

    async def warm(
        self,
        *,
        manifest_dirs: tuple[Path, ...] = (),
        cargo: Path | str | None = None,
        timeout_s: int = WARM_TIMEOUT_S,
    ) -> WarmOutcome:
        """Fetch this session's dependency graph, unconfined and online, once.

        ``manifest_dirs`` are directories with a ``Cargo.toml``, relative to the
        workdir. Empty means the workdir itself. Pass more than one when the
        verification crate sits outside the program's workspace: each root has
        its own graph.

        A cache is only warm for the cargo that filled it. The chain build uses
        the cargo inside platform-tools, not the one on ``PATH``, and the two
        store git dependencies in different places. ``None`` warms the cargo
        :meth:`check` runs.
        """
        cargo = self.check_cargo if cargo is None else cargo
        dirs = manifest_dirs or (Path("."),)
        for d in dirs:
            manifest = self.workdir / d / "Cargo.toml"
            fetched = await self.run_unconfined(
                str(cargo), ["fetch", "--manifest-path", str(manifest)], timeout_s=timeout_s
            )
            if fetched.exit_code != 0:
                _log.info("cargo fetch for %s failed (%s)", manifest, fetched.exit_code)
                return WarmFailed(
                    diagnostics=fetched.stderr.strip(), exit_code=fetched.exit_code
                )
        self._warmed.add(str(cargo))
        return Warmed()

    def already_warmed(self, cargo: Path | str) -> bool:
        return str(cargo) in self._warmed

    async def check(
        self,
        *,
        package: str | None = None,
        features: tuple[CargoFeature, ...] = (),
        manifest_dir: Path | None = None,
        timeout_s: int = CHECK_TIMEOUT_S,
    ) -> CompileRun:
        """``cargo check`` as :attr:`check_with` says, confined. Fast enough to run on every write."""
        match self.check_with:
            case HostCheck():
                cross_args, env = (), {}
            case CrossCheck(args=cross_args, env=env):
                pass
        args = ["check", "--quiet", *cross_args]
        if manifest_dir is not None:
            args += ["--manifest-path", str(self.workdir / manifest_dir / "Cargo.toml")]
        if package is not None:
            args += ["--package", package]
        if features:
            args += ["--features", ",".join(features)]
        started = time.perf_counter()
        checked = await self.run_confined(self.check_cargo, args, timeout_s=timeout_s, env=env)
        elapsed = int((time.perf_counter() - started) * 1000)
        verdict: CompileVerdict = (
            Compiled()
            if checked.exit_code == 0
            else CompileFailed(diagnostics=checked.stderr.strip(), exit_code=checked.exit_code)
        )
        return CompileRun(
            tier="fast", duration_ms=elapsed, verdict=verdict, confined=self.confined
        )
