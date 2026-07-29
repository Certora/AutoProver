"""The command-sandbox provider seam (``docs/command-sandbox.md`` §4, §7).

Every ``RunCommand`` invocation compiles and/or runs *untrusted native code* (an
LLM-authored harness, a user program's ``build.rs``), so it must run confined —
no network, no inherited secrets, only its own inputs on the filesystem. This
module is the **tool-agnostic isolation layer** that makes that confinement
*swappable*:

- :class:`SandboxPolicy` — the confinement *intent* (rw/ro paths, env allowlist,
  network on/off, resource caps). It names **no mechanism**, so swapping the
  sandbox tool never changes the policy.
- :class:`SandboxProvider` — maps a policy + a command to a concrete
  :class:`LaunchSpec` (the argv/env to actually exec). The mechanism (a
  Landlock+seccomp launcher, or an off-the-shelf tool like ``landrun`` /
  ``sandlock``) lives entirely behind this protocol.

Because :func:`composer.sandbox.command.run_local_command` will depend only on
this seam — never on a concrete tool — a provider can be swapped without touching
the command runner, ``RealEffects``, or the escape-test gate. It lives outside
``rustapp`` so Python-based backends can use it too, not just the Rust-IoC ones.

This module ships the policy, the protocol, and the ``none`` passthrough provider.
Concrete providers are declared as ``composer.sandbox_providers`` entry points
(pyproject.toml) and resolved by name in
:meth:`composer.sandbox.config.SandboxConfig.resolve_provider`, which imports the
selected mechanism's module lazily — so this seam never imports a concrete mechanism.
The ``run-confined`` launcher provider (Landlock + seccomp) is one such entry point
(``launcher`` → :mod:`composer.sandbox.launcher`).

**Trust boundary** (``docs/command-sandbox.md`` §7.2): the policy and the emitted
``LaunchSpec`` are authored by trusted Python — never the LLM, which controls only
file *contents*.
"""

from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, Protocol


class SandboxUnavailable(RuntimeError):
    """A sandbox provider was requested but cannot confine the command.

    Raised (fail-closed) instead of silently running unconfined — untrusted input
    must never run without the sandbox (``docs/command-sandbox.md`` §7). Carries the
    provider name + a human reason so the caller can surface a prominent message.
    """

    def __init__(self, provider: str, reason: str):
        self.provider = provider
        self.reason = reason
        super().__init__(f"command sandbox provider {provider!r} is unavailable: {reason}")


@dataclass(frozen=True)
class SandboxPolicy:
    """The confinement *intent* — tool-agnostic (``docs/command-sandbox.md`` §7).

    Every :class:`SandboxProvider` consumes *this* shape, so a mechanism swap needs
    no policy change. ``program``/``args`` are passed per-call to
    :meth:`SandboxProvider.wrap`, not stored here. Resource caps default to ``None``
    (unset); a provider maps them to its own limit mechanism (rlimits for the
    launcher).
    """

    rw_paths: tuple[Path, ...] = ()  # writable: the workdir (+ any scratch)
    ro_paths: tuple[Path, ...] = ()  # read+exec: toolchains, crucible checkout, /usr…
    env_allowlist: Mapping[str, str] = field(default_factory=dict)
    network: bool = False  # egress allowed? default off
    mem_bytes: int | None = None  # RLIMIT_AS
    cpu_seconds: int | None = None  # RLIMIT_CPU
    nproc: int | None = None  # RLIMIT_NPROC
    fsize_bytes: int | None = None  # RLIMIT_FSIZE


@dataclass(frozen=True)
class LaunchSpec:
    """How :func:`run_local_command` should actually launch the (confined) command.

    ``argv`` is the full argument vector to exec; ``env`` is the environment to pass
    (``None`` = inherit the parent's, i.e. today's unconfined behavior). Both are
    authored by trusted code, never the LLM.
    """

    argv: tuple[str, ...]
    env: Mapping[str, str] | None = None


@dataclass(frozen=True)
class Reason:
    """Why a provider *cannot* confine here — the payload of an unavailable result
    (e.g. the launcher binary is missing, or the kernel lacks Landlock)."""

    reason: str


# A provider's availability: the literal ``"ok"`` when it can confine here, or a
# :class:`Reason` when it cannot. Modeling it as a union rather than a ``(bool, str)``
# pair makes the illegal "available *and* has a reason" state unrepresentable — the
# ``"ok"`` arm has nowhere to put one, and the ``Reason`` arm always carries one.
Availability = Literal["ok"] | Reason


class SandboxProvider(Protocol):
    """Maps a :class:`SandboxPolicy` + a command to a concrete :class:`LaunchSpec`.

    The one seam every sandbox mechanism implements. Implementations are pure with
    respect to :meth:`wrap` (argv construction only — no subprocess), so they are
    trivially unit-testable; the actual confinement happens in the launched process.
    """

    @property
    def name(self) -> str:
        """A short, stable identifier for this mechanism (e.g. ``"none"``, ``"launcher"``)."""
        ...

    async def available(self) -> Availability:
        """Whether this provider can confine a command in the current environment.

        Async because a real provider may probe the environment out-of-process (the
        launcher shells out to ``run-confined --probe``); awaiting keeps that off the
        event loop."""
        ...

    def argv_prefix(self, policy: SandboxPolicy) -> list[str]:
        """The confinement argv that precedes the command — everything a launcher
        needs *except* the ``program args`` themselves, so that any process (Python
        via :meth:`wrap`, or a Rust backend via
        :meth:`composer.sandbox.config.SandboxConfig.backend_spec`) can launch a
        confined command as ``[*argv_prefix(policy), program, *args]``.

        Empty for a passthrough provider (no confinement wrapper). This is the single
        place a mechanism encodes its flags, so the confined launch stays mechanism-agnostic
        for callers that only get to prepend an argv (``docs/command-sandbox.md`` §4)."""
        ...

    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
        """Translate ``policy`` into how to launch ``program args`` confined."""
        ...


class NoneProvider:
    """Passthrough — **no confinement**. Exec the command directly, inheriting the
    environment: byte-for-byte today's behavior.

    An *explicit, logged* choice for the trusted EVM/Foundry callers and
    trusted-input dev runs. It is never reached as a silent fallback from a failed
    real sandbox (``docs/command-sandbox.md`` §7) — the caller selects it on purpose.
    """

    name = "none"

    async def available(self) -> Availability:
        return "ok"

    def argv_prefix(self, policy: SandboxPolicy) -> list[str]:
        # No confinement wrapper: the command runs directly, so there is no prefix.
        return []

    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
        # Policy is intentionally ignored: this provider provides no isolation.
        return LaunchSpec(argv=(program, *args), env=None)


async def ensure_available(provider: SandboxProvider) -> None:
    """Fail-closed check: raise :class:`SandboxUnavailable` unless ``provider`` can
    confine here. Call before running untrusted input under a real provider."""
    avail = await provider.available()
    if avail != "ok":
        raise SandboxUnavailable(provider.name, avail.reason)
