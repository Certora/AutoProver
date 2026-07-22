"""Runtime selection of the command-sandbox provider + policy.

A backend constructs a :class:`SandboxConfig` (usually via :meth:`from_env`) and
hands it to the command path (``RealEffects`` / ``build_program``), which turns it
into a concrete ``(provider, policy)`` per command via :meth:`resolve_provider` and
:meth:`build_policy`. Keeping selection here — rather than in :func:`run_local_command`
— means the runner stays mechanism-agnostic (``docs/command-sandbox.md`` §4/§7).

The library default provider is ``"none"`` (passthrough). Backends that run
untrusted native code (Crucible) construct a config with ``provider="launcher"``
by default; override with ``COMPOSER_SANDBOX_PROVIDER=none`` for trusted-input dev.
"""

import os
from dataclasses import dataclass
from importlib.metadata import entry_points
from pathlib import Path
from typing import NotRequired, Self, TypedDict, Unpack

from composer.sandbox.policy import SandboxPolicy, SandboxProvider, ensure_available
from composer.sandbox.recipes import DEFAULT_ENV_PASSTHROUGH, rust_build_policy

_ENV_VAR = "COMPOSER_SANDBOX_PROVIDER"
# Providers are declared here (pyproject.toml) and resolved by name below.
_PROVIDER_GROUP = "composer.sandbox_providers"


class SandboxArgs(TypedDict):
    """Keyword overrides accepted by :meth:`SandboxConfig.from_env`.

    Mirrors the :class:`SandboxConfig` fields that a backend may override —
    ``provider`` is excluded because ``from_env`` reads it from the environment.
    Every field is :data:`~typing.NotRequired` so callers pass only what they set.
    """

    extra_ro: NotRequired[tuple[Path, ...]]
    extra_rw: NotRequired[tuple[Path, ...]]
    env_passthrough: NotRequired[tuple[str, ...]]
    offline: NotRequired[bool]
    mem_bytes: NotRequired[int | None]
    cpu_seconds: NotRequired[int | None]
    nproc: NotRequired[int | None]
    fsize_bytes: NotRequired[int | None]


@dataclass(frozen=True)
class SandboxConfig:
    """Which provider to use + the inputs for building its policy."""

    provider: str = "none"
    extra_ro: tuple[Path, ...] = ()
    extra_rw: tuple[Path, ...] = ()
    env_passthrough: tuple[str, ...] = DEFAULT_ENV_PASSTHROUGH
    offline: bool = True  # sandbox has no network → force cargo offline (§5)
    mem_bytes: int | None = None
    cpu_seconds: int | None = None
    nproc: int | None = None
    fsize_bytes: int | None = None

    @classmethod
    def from_env(cls, **overrides: Unpack[SandboxArgs]) -> Self:
        """Read the provider from ``$COMPOSER_SANDBOX_PROVIDER`` (default ``none``);
        remaining fields come from ``overrides`` (e.g. a backend's ``extra_ro``)."""
        return cls(provider=os.environ.get(_ENV_VAR, "none"), **overrides)

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    def resolve_provider(self) -> SandboxProvider:
        """Construct the provider named by ``self.provider`` from its
        ``composer.sandbox_providers`` entry point, importing that mechanism's module
        lazily — the seam itself never imports a concrete mechanism
        (docs/command-sandbox.md §6). Raises ``ValueError`` for an unknown name (a
        config error, distinct from a provider being *unavailable*)."""
        for ep in entry_points(group=_PROVIDER_GROUP, name=self.provider):
            return ep.load()()
        known = sorted(ep.name for ep in entry_points(group=_PROVIDER_GROUP))
        raise ValueError(f"unknown sandbox provider {self.provider!r}; known: {known}")

    def build_policy(self, workdir: str | Path) -> SandboxPolicy:
        """The concrete policy for a command running in ``workdir``. The ``none``
        provider ignores the policy, so a bare :class:`SandboxPolicy` suffices there."""
        if not self.enabled:
            return SandboxPolicy()
        return rust_build_policy(
            workdir,
            extra_ro=self.extra_ro,
            extra_rw=self.extra_rw,
            env_passthrough=self.env_passthrough,
            offline=self.offline,
            mem_bytes=self.mem_bytes,
            cpu_seconds=self.cpu_seconds,
            nproc=self.nproc,
            fsize_bytes=self.fsize_bytes,
        )

    def backend_spec(self, workdir: str | Path, *, timeout_s: int) -> dict:
        """The ``Sandbox`` JSON a Rust backend's ``compile``/``validate`` consume to build
        their own ``run-confined`` launch (`autoprover_sdk::Sandbox`). Python keeps ownership
        of the confinement *intent* (this policy); the backend only assembles it into an argv.

        For a real provider this resolves the ``run-confined`` path and is **fail-closed**
        (``ensure_available`` raises if the launcher can't confine here). The ``none`` provider
        yields ``run_confined=None`` — the backend runs the command directly (trusted input)."""
        if not self.enabled:
            return {"run_confined": None, "timeout_s": timeout_s}
        provider = self.resolve_provider()
        ensure_available(provider)  # fail-closed: raise before any untrusted code runs
        policy = self.build_policy(workdir)
        return {
            "run_confined": getattr(provider, "binary", None),
            "ro": [str(p) for p in policy.ro_paths],
            "rw": [str(p) for p in policy.rw_paths],
            "allow_env": [f"{k}={v}" for k, v in policy.env_allowlist.items()],
            "network": policy.network,
            "rlimits": {
                "mem_bytes": policy.mem_bytes,
                "cpu_seconds": policy.cpu_seconds,
                "nproc": policy.nproc,
                "fsize_bytes": policy.fsize_bytes,
            },
            "timeout_s": timeout_s,
        }
