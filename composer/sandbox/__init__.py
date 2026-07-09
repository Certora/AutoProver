"""Run local commands, optionally confined to an unprivileged in-kernel sandbox.

A backend-agnostic home for the ``RunCommand`` execution primitive
(:func:`run_local_command`) and the swappable sandbox seam
(``docs/command-sandbox.md``). It lives outside ``rustapp`` so **any** backend —
Rust-IoC *or* Python — can run untrusted native code (a compiler, a fuzzer)
confined: no network, no inherited secrets, only its own inputs on disk.

- :mod:`composer.sandbox.policy` — the tool-agnostic seam: :class:`SandboxPolicy`
  (confinement intent), :class:`SandboxProvider` (maps policy+command → a
  :class:`LaunchSpec`), the ``none`` passthrough, the provider registry, and the
  fail-closed helpers.
- :mod:`composer.sandbox.launcher` — the ``run-confined`` launcher provider
  (Landlock + seccomp). Import it to register the ``"launcher"`` provider; the
  *seam* deliberately never imports a concrete mechanism.
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
from composer.sandbox.policy import (
    Availability,
    LaunchSpec,
    NoneProvider,
    SandboxPolicy,
    SandboxProvider,
    SandboxUnavailable,
    ensure_available,
    get_provider,
    register_provider,
)

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
    "NoneProvider",
    "SandboxUnavailable",
    "get_provider",
    "register_provider",
    "ensure_available",
]
