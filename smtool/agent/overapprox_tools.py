"""Over-approximation agent tools — the `OverApproxProject` mutation/inspection surface as graphcore
LLM tools, mirroring `agent/tools.py` but collapsed to the ONE hole this loop has: the predicate `Phi`.

`set_phi` takes CVL SURFACE TEXT for a target's `Phi(params, res)` body (parsed via `cvl_parse`, same as
the model loop's free-form holes), so the tool schema stays tiny and the agent writes natural CVL. The
`Project` is bound out-of-band (`OverApproxDeps`) via `.bind(deps).as_tool(name)`; the mutation persists
on the bound reference across calls. `check_consistency` and `verify` are bound closures (the refine
runner supplies them), so the agent drives fill→verify→refine in ONE conversation.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import override, Callable, Awaitable

from pydantic import Field
from langchain_core.tools import BaseTool

from graphcore.tools.schemas import WithAsyncDependencies

from ..overapprox_project import OverApproxProject
from ..project import Result


@dataclass(frozen=True)
class OverApproxDeps:
    """The bound dependency every over-approx tool reads — the project the `set_phi` mutation operates on,
    plus (optionally) `full_typecheck` (write + run the REAL certoraRun typechecker, returns (ok, diag))
    and `verify` (run the prover; returns PASS / failures+counterexamples). Both are set by the refine
    loop; None in config-free contexts (then check_consistency is structural-only and verify is absent)."""
    project: OverApproxProject
    full_typecheck: Callable[[], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None
    verify: Callable[[], Awaitable[str]] | None = None


def _res(r: Result) -> str:
    if r.ok:
        return f"ok: {r.message}"
    return f"REJECTED: {r.message}" + (f" | {'; '.join(r.violations)}" if r.violations else "")


class _Tool(WithAsyncDependencies[str, OverApproxDeps]):
    """Base: bind an OverApproxDeps, run returns a str for the LLM."""


class SetPhi(_Tool):
    """Set (or replace) the predicate `Phi(<params>, <rt> res)` body for a target function — THE hole.
    Provide CVL statements ending in `return <bool expression>`; you MAY declare locals and use `require`
    (e.g. the byte-extraction idiom `uint248 v; require to_bytes31(v) == res; return (v >> 240) == 3;`).
    Phi must be a boolean predicate over the function's params and the result `res`. Make it as STRONG as
    the goal wants; the prover will tell you (via a counterexample) if it is too strong, and you WEAKEN
    it. Re-calling replaces the previous body."""
    fn: str = Field(description="the target function whose Phi to set, e.g. 'sqrt'")
    body: str = Field(description="the Phi body as CVL statements ending in `return <bool>`")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(d.project.set_phi(self.fn, self.body))


class CheckConsistency(_Tool):
    """Coherence check (no prover run): every target's Phi is filled and type-checks, and — when a full
    typechecker is bound — the whole conformance bundle passes the REAL certoraRun CVL typecheck (catches
    the semantic errors the standalone jar misses). Returns the problems ([] == consistent). CALL THIS
    before `verify`."""

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            problems = d.project.check_consistency()
            if problems:
                return "problems:\n- " + "\n- ".join(problems)
            if d.full_typecheck is not None:
                res = d.full_typecheck()
                ok, diag = await res if asyncio.iscoroutine(res) else res
                if not ok:
                    return "typecheck failed — fix the CVL at the reported file:line:\n" + diag
            return "consistent"


class Verify(_Tool):
    """Run the PROVER on the current Phi(s) (the trust anchor, cloud, minutes/call): verifies each
    target's `overApprox_<fn>` conformance rule — that the REAL function's output satisfies Phi. Returns
    ALL-VERIFIED, or the failing rules with their COUNTEREXAMPLES (a concrete input `x` whose real output
    violates Phi → Phi is too strong there, WEAKEN it) / TIMEOUTs (Phi too heavy → simplify/pivot).
    Call once check_consistency is clean; FIX what it reports and call verify AGAIN; only call `result`
    after verify reports ALL targets verified (or you have justified a weaker-but-sound Phi)."""

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            if d.verify is None:
                return "verify is not available in this run"
            return await d.verify()


class RenderPhi(_Tool):
    """Return a target's current Phi spec as CVL text."""
    fn: str = Field(description="the target function whose Phi spec to render")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return d.project.render_phi(self.fn)


class RenderConformance(_Tool):
    """Return a target's conformance spec as CVL text (the `overApprox_<fn>` rule proved against the real
    function)."""
    fn: str = Field(description="the target function whose conformance spec to render")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return d.project.render_conformance(self.fn)


class RenderSummary(_Tool):
    """Return a target's installable summary spec as CVL text (`<fn>CVL` + the methods{} binding) — the
    deliverable a downstream proof imports once conformance passes."""
    fn: str = Field(description="the target function whose summary spec to render")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return d.project.render_summary(self.fn)


_TOOLS: list[tuple[type[_Tool], str]] = [
    (SetPhi, "set_phi"),
    (CheckConsistency, "check_consistency"),
    (RenderPhi, "render_phi"),
    (RenderConformance, "render_conformance"),
    (RenderSummary, "render_summary"),
]


def overapprox_tools(deps: OverApproxDeps) -> list[BaseTool]:
    """The over-approx tool set, each bound to `deps`. The `verify` tool is included only when a verify
    closure is bound (the refine runner sets it) — so the agent drives fill→verify→refine in ONE
    conversation with prover feedback arriving inline. CUT source-access tools (grep/read) + the CVL
    manual search are supplied separately as `extra_tools`."""
    tools = [cls.bind(deps).as_tool(name) for cls, name in _TOOLS]
    if deps.verify is not None:
        tools.append(Verify.bind(deps).as_tool("verify"))
    return tools
