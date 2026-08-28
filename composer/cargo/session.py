"""The warm workdir a Rust compile loop runs in, and the fast tier of the two-tier gate.

``docs/cvlr-backend-plan.md`` §5.1 is the whole reason this is an object. CVL authoring gates every
edit on a sub-second typecheck; CVLR's only equivalent is a Rust build, so the gate moves into the
inner loop — and the sandbox recipe gives each *run* a private ``CARGO_HOME``
(:func:`~composer.sandbox.recipes.sandbox_cargo_home`), deliberately, so that an untrusted
``build.rs`` cannot poison a later run. Those two facts collide: a per-compile workdir would re-fetch
the dependency graph on every edit. So the workdir is owned by a **session** and reused across every
compile in that session's loop, and the fetch is paid once, in :meth:`CargoSession.warm`.

The split between warm and compile is the trust boundary, not an optimization
(``docs/command-sandbox.md`` §5): ``cargo fetch`` downloads and executes nothing, so it runs
unconfined *with* the network; everything that compiles runs confined and offline, where the deps it
needs are already present because the warm put them there.

Nothing here is Solana-specific. The fast tier is a host-target ``cargo check``, which is the same
question on any chain — "does this Rust say something the compiler understands" — and it is
deliberately the *cheap* half: whether it is a faithful proxy for the chain build is
``docs/cvlr-backend-plan.md``'s open question 1, answered by measurement rather than assumed here.
"""

import dataclasses
import logging
import time
from pathlib import Path
from typing import Literal

from composer.sandbox.command import CommandResult, run_local_command
from composer.sandbox.config import SandboxConfig
from composer.sandbox.recipes import sandbox_cargo_home

_log = logging.getLogger(__name__)

#: Which gate produced a run. The vocabulary is the plan's: the ``fast`` tier runs per write, the
#: ``slow`` tier only before a prover submission.
type CompileTier = Literal["fast", "slow"]

#: A fetch resolves and downloads a dependency graph; on a cold cache for a real program that is
#: minutes, not seconds.
WARM_TIMEOUT_S = 900
#: A host-target ``cargo check``. Long enough for a cold graph, short enough that a wedged compiler
#: surfaces as a timeout inside one authoring turn rather than stalling the run.
CHECK_TIMEOUT_S = 600


@dataclasses.dataclass(frozen=True)
class Compiled:
    """The compiler accepted the crate. Nothing to feed back."""


@dataclasses.dataclass(frozen=True)
class CompileFailed:
    """The compiler rejected it. ``diagnostics`` is what it said, verbatim.

    Verbatim rather than parsed: the consumer is an authoring agent, and rustc's human-format
    output — the span, the note, the suggested fix — is the most actionable form it can be given.
    """

    diagnostics: str
    exit_code: int


type CompileVerdict = Compiled | CompileFailed


@dataclasses.dataclass(frozen=True)
class CompileRun:
    """One invocation of one tier: what it decided, and what it cost.

    The cost rides along on every run because ``docs/cvlr-backend-plan.md`` §7.2 makes measured
    per-tier latency an exit criterion of this phase, and a number collected only when someone
    remembers to instrument is a number nobody has.
    """

    tier: CompileTier
    duration_ms: int
    verdict: CompileVerdict
    #: False when the command ran without confinement — the macOS development carve-out of §3 item
    #: 3. Carried on the result so a verdict produced unconfined is never mistaken for a production
    #: one; the operator-facing warning is emitted by :class:`CargoSession`.
    confined: bool

    @property
    def ok(self) -> bool:
        return isinstance(self.verdict, Compiled)


@dataclasses.dataclass(frozen=True)
class Warmed:
    """Dependencies are present in the session's cargo home; confined offline builds can proceed."""


@dataclasses.dataclass(frozen=True)
class WarmFailed:
    """The fetch did not complete.

    Non-fatal by design, and reported rather than raised: a partially warm cache still compiles
    everything it covers, and the failure the operator needs to see is the *build* that then cannot
    find a crate — which names the crate, where this names only the fetch."""

    diagnostics: str
    exit_code: int


type WarmOutcome = Warmed | WarmFailed


@dataclasses.dataclass
class CargoSession:
    """A workdir that stays warm across an authoring session's compiles.

    ``workdir`` is where every command runs and the only path the confinement policy grants
    read-write. It is the caller's to choose and the caller's to clean up: building in the analyzed
    project itself works (and is what a one-shot deterministic run does), while an authoring loop
    hands in a materialization of the project over its edit VFS, so the user's checkout is never
    written to. Either way the session does not create or destroy it.
    """

    workdir: Path
    sandbox: SandboxConfig

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
        """Where this session's crates live, or ``None`` to inherit the ambient one.

        A confined session must use the private per-run home the policy grants and forces
        (:func:`~composer.sandbox.recipes.sandbox_cargo_home`), so the warm — which runs *outside*
        the sandbox and would otherwise inherit the shared ``~/.cargo`` — has to be pointed at the
        same place, or it would warm a cache the build cannot read.
        """
        return sandbox_cargo_home(self.workdir) if self.sandbox.enabled else None

    async def run_confined(
        self, program: str, args: list[str], *, timeout_s: int
    ) -> CommandResult:
        """Run a command in the workdir under this session's confinement.

        The posture every command that *compiles* must use, and fail-closed: a configured provider
        that cannot confine here raises rather than falling back."""
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
        """Run a command in the workdir with the network and without confinement.

        For the prep steps only — a fetch resolves and downloads, and executes nothing
        (``docs/command-sandbox.md`` §5). It is still pointed at the session's cargo home, or it
        would warm a cache the confined build cannot read."""
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
        self, *, manifest_dirs: tuple[Path, ...] = (), timeout_s: int = WARM_TIMEOUT_S
    ) -> WarmOutcome:
        """Fetch this session's dependency graph, unconfined and online, once.

        ``manifest_dirs`` names the directories whose ``Cargo.toml`` should be fetched, relative to
        the workdir; empty means the workdir itself. More than one is the normal case when the
        verification artifact is its own crate outside the program's workspace — each root resolves
        its own graph, and warming only one leaves the confined build unable to reach the other.
        """
        dirs = manifest_dirs or (Path("."),)
        for d in dirs:
            manifest = self.workdir / d / "Cargo.toml"
            fetched = await self.run_unconfined(
                "cargo", ["fetch", "--manifest-path", str(manifest)], timeout_s=timeout_s
            )
            if fetched.exit_code != 0:
                _log.info("cargo fetch for %s failed (%s)", manifest, fetched.exit_code)
                return WarmFailed(
                    diagnostics=fetched.stderr.strip(), exit_code=fetched.exit_code
                )
        return Warmed()

    async def check(
        self,
        *,
        package: str | None = None,
        features: tuple[str, ...] = (),
        manifest_dir: Path | None = None,
        timeout_s: int = CHECK_TIMEOUT_S,
    ) -> CompileRun:
        """The fast tier: ``cargo check`` on the **host** target, confined.

        Host target, not the chain's: that is what makes it fast enough to gate every write, and it
        catches the class of error an LLM actually makes in a language it has thin training data for
        — an unknown macro, a wrong signature, a misused derive. It does not catch what only the
        chain build can, which is why the slow tier exists.
        """
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
