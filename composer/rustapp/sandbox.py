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

Because :func:`composer.rustapp.command.run_local_command` will depend only on
this seam — never on a concrete tool — a provider can be swapped without touching
the command runner, ``RealEffects``, or the escape-test gate.

**Step 1 (this module)** ships the policy, the protocol, and the ``none``
passthrough provider. The ``run-confined`` launcher provider (Landlock +
``seccompiler``) lands in step 2 as another entry behind :func:`get_provider`.

**Trust boundary** (``docs/command-sandbox.md`` §7.2): the policy and the emitted
``LaunchSpec`` are authored by trusted Python — never the LLM, which controls only
file *contents*.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Protocol, runtime_checkable


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
class Availability:
    """Result of :meth:`SandboxProvider.available` — whether the provider can
    actually confine here (e.g. the launcher probes the kernel's Landlock ABI)."""

    ok: bool
    reason: str = ""


@runtime_checkable
class SandboxProvider(Protocol):
    """Maps a :class:`SandboxPolicy` + a command to a concrete :class:`LaunchSpec`.

    The one seam every sandbox mechanism implements. Implementations are pure with
    respect to :meth:`wrap` (argv construction only — no subprocess), so they are
    trivially unit-testable; the actual confinement happens in the launched process.
    """

    name: str

    def available(self) -> Availability:
        """Whether this provider can confine a command in the current environment."""
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

    def available(self) -> Availability:
        return Availability(ok=True)

    def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
        # Policy is intentionally ignored: this provider provides no isolation.
        return LaunchSpec(argv=(program, *args), env=None)


# Provider registry. Step 2 registers the ``run-confined`` launcher provider here;
# step 3 selects among these via ``CommandConfig.sandbox_provider``.
_PROVIDERS: dict[str, Callable[[], SandboxProvider]] = {
    "none": NoneProvider,
}


def register_provider(name: str, factory: Callable[[], SandboxProvider]) -> None:
    """Register a provider factory under ``name`` (used by later steps to add the
    launcher / off-the-shelf providers without this module importing them)."""
    _PROVIDERS[name] = factory


def get_provider(name: str) -> SandboxProvider:
    """Construct the provider registered under ``name``. Raises ``ValueError`` for an
    unknown name (a config error, distinct from a provider being *unavailable*)."""
    try:
        factory = _PROVIDERS[name]
    except KeyError:
        raise ValueError(
            f"unknown sandbox provider {name!r}; known: {sorted(_PROVIDERS)}"
        ) from None
    return factory()


def ensure_available(provider: SandboxProvider) -> None:
    """Fail-closed check: raise :class:`SandboxUnavailable` unless ``provider`` can
    confine here. Call before running untrusted input under a real provider."""
    avail = provider.available()
    if not avail.ok:
        raise SandboxUnavailable(provider.name, avail.reason)
