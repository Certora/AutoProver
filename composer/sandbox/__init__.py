"""Run local commands, optionally confined to an unprivileged in-kernel sandbox.

A backend-agnostic home for the ``RunCommand`` execution primitive
(:func:`run_local_command`) and the swappable sandbox seam
(``docs/command-sandbox.md``). It lives outside ``rustapp`` so **any** backend —
Rust-IoC *or* Python — can run untrusted native code (a compiler, a fuzzer)
confined: no network, no inherited secrets, only its own inputs on disk.

- :mod:`composer.sandbox.policy` — the tool-agnostic seam: :class:`SandboxPolicy`
  (confinement intent), :class:`SandboxProvider` (maps policy+command → a
  :class:`LaunchSpec`), the ``none`` passthrough, and the fail-closed helpers.
  Providers are declared as ``composer.sandbox_providers`` entry points and resolved
  by :meth:`composer.sandbox.config.SandboxConfig.resolve_provider`.
- :mod:`composer.sandbox.launcher` — the ``run-confined`` launcher provider
  (Landlock + seccomp), wired in as the ``launcher`` ``composer.sandbox_providers``
  entry point and loaded lazily; the *seam* deliberately never imports a mechanism.
- :mod:`composer.sandbox.command` — :func:`run_local_command`, the single choke
  point that materializes files into a workdir and runs a command there.
"""

from composer.sandbox.command import (
    DEFAULT_TIMEOUT_S,
    NOT_FOUND_EXIT,
    CommandResult,
    UnsafePath,
    run_local_command,
)
from composer.sandbox.config import SandboxConfig
from composer.sandbox.policy import (
    Availability,
    LaunchSpec,
    NoneProvider,
    Reason,
    SandboxPolicy,
    SandboxProvider,
    SandboxUnavailable,
    ensure_available,
)
from composer.sandbox.recipes import DEFAULT_ENV_PASSTHROUGH, rust_build_policy

__all__ = [
    # command runner
    "run_local_command",
    "CommandResult",
    "UnsafePath",
    "DEFAULT_TIMEOUT_S",
    "NOT_FOUND_EXIT",
    # sandbox seam
    "SandboxPolicy",
    "SandboxProvider",
    "LaunchSpec",
    "Availability",
    "Reason",
    "NoneProvider",
    "SandboxUnavailable",
    "ensure_available",
    # config + recipes
    "SandboxConfig",
    "rust_build_policy",
    "DEFAULT_ENV_PASSTHROUGH",
]
