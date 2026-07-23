"""Unit tests for the command-sandbox provider seam (step 1).

Pure and fast — no Rust wheel, no subprocess, no Postgres/LLM. They pin the
tool-agnostic contract (``docs/command-sandbox.md`` §4/§7) that every provider
must honor: the ``none`` passthrough is byte-for-byte today's behavior, the
registry resolves/rejects names, and the fail-closed check fires only when a
provider reports itself unavailable.
"""

from pathlib import Path

import pytest

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


def test_policy_defaults_are_locked_down():
    """A default policy denies everything: no paths, empty env, network off, no caps."""
    p = SandboxPolicy()
    assert p.rw_paths == ()
    assert p.ro_paths == ()
    assert dict(p.env_allowlist) == {}
    assert p.network is False
    assert p.mem_bytes is None and p.cpu_seconds is None
    assert p.nproc is None and p.fsize_bytes is None


def test_policy_is_frozen():
    p = SandboxPolicy(rw_paths=(Path("/work"),))
    with pytest.raises((AttributeError, TypeError)):
        p.network = True  # type: ignore[misc]


def test_none_provider_is_a_passthrough():
    """``none`` execs the command verbatim and inherits the env (env is None)."""
    spec = NoneProvider().wrap(SandboxPolicy(), "cargo", ["build", "--offline"])
    assert spec == LaunchSpec(argv=("cargo", "build", "--offline"), env=None)


def test_none_provider_ignores_policy():
    """Passthrough grants no isolation, so a rich policy must not alter its argv."""
    rich = SandboxPolicy(
        rw_paths=(Path("/work"),),
        ro_paths=(Path("/usr"),),
        env_allowlist={"PATH": "/usr/bin"},
        network=True,
        mem_bytes=1 << 32,
    )
    spec = NoneProvider().wrap(rich, "echo", ["hi"])
    assert spec.argv == ("echo", "hi")
    assert spec.env is None


@pytest.mark.asyncio
async def test_none_provider_available():
    assert await NoneProvider().available() == "ok"


def test_none_provider_satisfies_protocol():
    # Static structural conformance: pyright rejects this assignment if NoneProvider
    # stops implementing the seam. (No runtime isinstance — the protocol isn't
    # @runtime_checkable; the type checker is the gate.)
    provider: SandboxProvider = NoneProvider()
    assert provider.name == "none"


@pytest.mark.asyncio
async def test_ensure_available_passes_for_ok_provider():
    await ensure_available(NoneProvider())  # must not raise


@pytest.mark.asyncio
async def test_ensure_available_fails_closed():
    """An unavailable provider raises rather than letting the command run unconfined."""

    class _Unavailable:
        name = "landlock-missing"

        async def available(self) -> Availability:
            return Reason("kernel lacks Landlock (need Linux >= 5.13)")

        def wrap(self, policy: SandboxPolicy, program: str, args: list[str]) -> LaunchSpec:
            raise AssertionError("wrap must not be reached when unavailable")

    with pytest.raises(SandboxUnavailable) as ei:
        await ensure_available(_Unavailable())
    assert ei.value.provider == "landlock-missing"
    assert "Landlock" in ei.value.reason
    assert "unavailable" in str(ei.value)
