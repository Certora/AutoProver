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

The fast tier is a host-target ``cargo check``, not the chain build.
"""

import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from composer.sandbox.command import CommandResult, run_local_command
from composer.sandbox.config import SandboxConfig
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


@dataclass
class CargoSession:
    """A workdir reused across an authoring session's compiles.

    ``workdir`` is where every command runs, and the only path the confinement
    policy grants read-write. The caller chooses it and cleans it up; the session
    does not create or delete it.
    """

    workdir: Path
    sandbox: SandboxConfig
    #: Keyed by binary because two cargos do not share a git cache
    #: (see :func:`~composer.cargo.sbf.platform_tools_cargos`).
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
    def cargo_home(self) -> Path | None:
        """Private per-run home when confined, else the process default.

        Warm runs outside the sandbox, so it has to use the same directory the
        confined build will read (:func:`~composer.sandbox.recipes.sandbox_cargo_home`).
        """
        return sandbox_cargo_home(self.workdir) if self.sandbox.enabled else None

    async def run_confined(
        self, program: str, args: list[str], *, timeout_s: int
    ) -> CommandResult:
        """Run a command in the workdir under this session's confinement.

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
            policy=self.sandbox.build_policy(self.workdir),
        )

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
            env_overlay={"CARGO_HOME": str(home)} if home is not None else None,
        )

    async def warm(
        self,
        *,
        manifest_dirs: tuple[Path, ...] = (),
        cargo: Path | str = "cargo",
        timeout_s: int = WARM_TIMEOUT_S,
    ) -> WarmOutcome:
        """Fetch this session's dependency graph, unconfined and online, once.

        ``manifest_dirs`` are directories with a ``Cargo.toml``, relative to the
        workdir. Empty means the workdir itself. Pass more than one when the
        verification crate sits outside the program's workspace: each root has
        its own graph.

        A cache is only warm for the cargo that filled it. The chain build uses
        the cargo inside platform-tools, not the one on ``PATH``, and the two
        store git dependencies in different places.
        """
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
        features: tuple[str, ...] = (),
        manifest_dir: Path | None = None,
        timeout_s: int = CHECK_TIMEOUT_S,
    ) -> CompileRun:
        """Host-target ``cargo check``, confined. Fast enough to run on every write."""
        args = ["check", "--quiet"]
        if manifest_dir is not None:
            args += ["--manifest-path", str(self.workdir / manifest_dir / "Cargo.toml")]
        if package is not None:
            args += ["--package", package]
        if features:
            args += ["--features", ",".join(features)]
        started = time.perf_counter()
        checked = await self.run_confined("cargo", args, timeout_s=timeout_s)
        elapsed = int((time.perf_counter() - started) * 1000)
        verdict: CompileVerdict = (
            Compiled()
            if checked.exit_code == 0
            else CompileFailed(diagnostics=checked.stderr.strip(), exit_code=checked.exit_code)
        )
        return CompileRun(
            tier="fast", duration_ms=elapsed, verdict=verdict, confined=self.confined
        )
