"""Runtime selection of the command-sandbox provider + policy.

A backend constructs a :class:`SandboxConfig` (usually via :meth:`from_env`) and
hands it to the command path (``RealEffects`` / ``build_program``), which turns it
into a concrete ``(provider, policy)`` per command via :meth:`resolve_provider` and
:meth:`build_policy`. Keeping selection here — rather than in :func:`run_local_command`
— means the runner stays mechanism-agnostic (``docs/command-sandbox.md`` §4/§7).

Default provider is ``"none"`` (passthrough), so wiring a config through changes
nothing until a backend opts in (``COMPOSER_SANDBOX_PROVIDER=launcher`` or an
explicit ``provider=``). Flipping the default to ``launcher`` happens with the
escape-test gate (§9 step 5), once the policy is proven complete.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from composer.sandbox.policy import SandboxPolicy, SandboxProvider, get_provider
from composer.sandbox.recipes import DEFAULT_ENV_PASSTHROUGH, rust_build_policy

_ENV_VAR = "COMPOSER_SANDBOX_PROVIDER"


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
    def from_env(cls, **overrides) -> "SandboxConfig":
        """Read the provider from ``$COMPOSER_SANDBOX_PROVIDER`` (default ``none``);
        remaining fields come from ``overrides`` (e.g. a backend's ``extra_ro``)."""
        return cls(provider=os.environ.get(_ENV_VAR, "none"), **overrides)

    @property
    def enabled(self) -> bool:
        return self.provider != "none"

    def resolve_provider(self) -> SandboxProvider:
        # Importing the launcher module registers the "launcher" provider; the seam
        # itself never imports a concrete mechanism (docs/command-sandbox.md §6).
        if self.provider == "launcher":
            import composer.sandbox.launcher  # noqa: F401
        return get_provider(self.provider)

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
