"""The inversion-of-control effect loop (Tier 2).

The Rust session is a *pure synchronous decider*: ``session.resume(obs_json)``
returns the next command as JSON. This module owns the async event loop and
performs each effect the command asks for, then feeds the result back as the
next observation — exactly mirroring the ``PureFunctionGenerator`` decide/do
split the CVL author already uses, relocated across the FFI boundary. There is
no ``pyo3-async`` bridge: every ``resume`` is a fast synchronous call, and all
awaiting happens here, in Python.

The loop is deliberately decoupled from the real services via the :class:`Effects`
protocol, so it can be driven by a fake in tests and by :class:`RealEffects`
(see :mod:`composer.rustapp.adapter`) in production.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Protocol

from pydantic import BaseModel

_log = logging.getLogger(__name__)

# A hard backstop on turns. Rust owns the real turn budget (and should emit
# GiveUp when it is exhausted); this only guards against a decider that loops
# forever, which would otherwise hang the pipeline.
DEFAULT_MAX_STEPS = 500


class GaveUp(BaseModel):
    """The loop's give-up signal. Mirrors ``composer.pipeline.core.GaveUp`` so the
    adapter can hand it straight to the driver; kept here to keep this module
    importable without the pipeline."""

    reason: str


class Effects(Protocol):
    """The async services the loop performs on the decider's behalf. Every method
    is coarse-grained — one call per turn / tool-invocation, never per token."""

    async def call_llm(self, messages: Any) -> str:
        """Perform one LLM turn; return the reply text."""
        ...

    async def run_prover(self, spec: str, config: Any, rules: list[str] | None) -> dict:
        """Run the verifier over ``spec``; return a backend-shaped result dict."""
        ...

    async def run_command(
        self, program: str, args: list[str], files: dict[str, str]
    ) -> dict:
        """Materialize ``files`` into the session workdir, run ``program args`` there
        (no shell), and return ``{exit_code, stdout, stderr}``. The command line is the
        decider's; only file *contents* may be LLM-derived."""
        ...

    async def run_feedback(self, spec: str, skipped: Any, rebuttals: Any) -> dict:
        """Run the feedback judge; return a backend-shaped result dict."""
        ...

    async def cache_get(self, key: str) -> Any | None:
        """Read the loop's scratch cache (``None`` on miss)."""
        ...

    async def cache_put(self, key: str, value: Any) -> None:
        """Write the loop's scratch cache."""
        ...

    async def emit(self, event_kind: str, payload: dict) -> None:
        """Stream a domain event to this task's frontend panel."""
        ...


class SessionProto(Protocol):
    """The Rust ``RustSession`` pyclass: a single synchronous decision step."""

    def resume(self, observation_json: str, /) -> str: ...


async def drive_session(
    session: SessionProto,
    effects: Effects,
    *,
    max_steps: int = DEFAULT_MAX_STEPS,
) -> RustFormalized | GaveUp:
    """Drive ``session`` to a terminal command, performing each effect via
    ``effects``. Returns the published ``Formalized`` payload (as a
    :class:`RustFormalized`) or :class:`GaveUp`."""

    observation: dict = {"kind": "start"}
    for _ in range(max_steps):
        command = json.loads(session.resume(json.dumps(observation)))
        kind = command.get("kind")

        if kind == "call_llm":
            text = await effects.call_llm(command.get("messages"))
            observation = {"kind": "llm_reply", "text": text}
        elif kind == "run_prover":
            data = await effects.run_prover(
                command["spec"], command.get("config"), command.get("rules")
            )
            observation = {"kind": "prover_result", "data": data}
        elif kind == "run_feedback":
            data = await effects.run_feedback(
                command["spec"], command.get("skipped"), command.get("rebuttals")
            )
            observation = {"kind": "feedback_result", "data": data}
        elif kind == "run_command":
            result = await effects.run_command(
                command["program"],
                command.get("args") or [],
                command.get("files") or {},
            )
            observation = {
                "kind": "command_result",
                "exit_code": result.get("exit_code", -1),
                "stdout": result.get("stdout", ""),
                "stderr": result.get("stderr", ""),
            }
        elif kind == "cache_get":
            value = await effects.cache_get(command["key"])
            observation = {"kind": "cached", "value": value}
        elif kind == "cache_put":
            await effects.cache_put(command["key"], command.get("value"))
            observation = {"kind": "ack"}
        elif kind == "emit":
            await effects.emit(command["event_kind"], command.get("payload") or {})
            observation = {"kind": "ack"}
        elif kind == "publish":
            return RustFormalized(command.get("result") or {})
        elif kind == "give_up":
            return GaveUp(reason=command.get("reason", "backend gave up"))
        else:
            raise ValueError(f"Rust backend returned unknown command kind: {kind!r}")

    raise RuntimeError(
        f"Rust formalize session did not terminate within {max_steps} steps "
        "(the decider is not converging to publish/give_up)."
    )


class RustFormalized:
    """A thin wrapper around the ``Formalized`` dict a session publishes, so the
    adapter can convert it into the pydantic ``RustFormalResult`` without this
    module importing it (keeps the loop dependency-light and unit-testable)."""

    __slots__ = ("data",)

    def __init__(self, data: dict):
        self.data = data
