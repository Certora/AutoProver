"""smtool's mutation/inspection functions exposed as graphcore LLM tools (piece A).

Each tool is a pydantic model whose fields are the LLM-visible args (JSON schema auto-derived) and whose
`run()` calls the underlying smtool function. The `Project` is bound out-of-band as a dependency
(`SmtoolDeps`) via `WithAsyncDependencies.bind(deps).as_tool(name)` — the LLM never supplies it, and
because every mutation mutates the project in place (`mutations._commit`), the bound reference persists
across tool calls. No graph state is threaded.

Free-form holes (bodies, mirrors, expressions) take CVL SURFACE TEXT (str) and are parsed to
composer.cvl.schema via `cvl_parse` (piece B). Text keeps the tool schemas tiny (~hundreds of tokens vs
~75k for AST-as-JSON) and lets the agent write natural CVL; the parsed AST still flows through the full
discipline linter. A CVL syntax error comes back as a REJECTED result the agent can fix.

Build the tool list with `smtool_tools(deps)`; run them in an agent loop via smtool.agent.loop.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import override, Callable, Awaitable

from pydantic import Field
from langchain_core.tools import BaseTool

from graphcore.tools.schemas import WithAsyncDependencies

from ..project import Project, Result
from ..cvl_parse import parse_expression, parse_commands, CVLParseError
from .. import mutations as mut


@dataclass(frozen=True)
class SmtoolDeps:
    """The bound dependency every smtool tool reads — the Project the mutations operate on, and
    (optionally) `full_typecheck`: a bound closure that writes the project + runs the REAL certoraRun
    typechecker and returns (ok, diagnostics). When present, check_consistency runs it after the fast
    structural checks, so the agent catches the semantic CVL errors the standalone jar misses
    (assign-after-access, mathint casts) DURING fill, before calling result. Set by the refine loop
    (it has the run-config); None in config-free contexts (then check_consistency is structural-only)."""
    project: Project
    full_typecheck: Callable[[], Awaitable[tuple[bool, str]] | tuple[bool, str]] | None = None
    verify: Callable[[], Awaitable[str]] | None = None   # run the prover; returns PASS / failures+CEX


def _res(r: Result) -> str:
    """Render a mutation Result for the LLM: 'ok: ...' or 'REJECTED: ... | <violations>'."""
    if r.ok:
        return f"ok: {r.message}"
    return f"REJECTED: {r.message}" + (f" | {'; '.join(r.violations)}" if r.violations else "")


class _Tool(WithAsyncDependencies[str, SmtoolDeps]):
    """Base: bind a SmtoolDeps, run returns a str for the LLM."""


# ---------------------------------------------------------------- model content (holes)
class AddModelConstant(_Tool):
    """Add a named numeric constant to the model (HOLE-K), e.g. RAY. Emitted as a persistent ghost with
    a defining axiom `name == value`. Use for library constants the model math references."""
    name: str = Field(description="constant name, e.g. 'RAY'")
    ctype: str = Field(description="CVL type, e.g. 'uint256'")
    value: str = Field(description="the constant's value as a CVL expression, e.g. '10 ^ 27'")

    @override
    async def run(self) -> str:
        try:
            v = parse_expression(self.value)
        except CVLParseError as e:
            return f"REJECTED: CVL parse error in value: {e}"
        with self.tool_deps() as d:
            return _res(mut.add_model_constant(d.project, name=self.name, ctype=self.ctype, value_expr=v))


class AddModelFunction(_Tool):
    """Add a model helper CVL function (HOLE-M) — a math mirror (a library fn in its exact structural
    form) or any internal helper the <f>CVL bodies call. CVL-only (no real-contract calls); may
    read/write model ghosts."""
    name: str = Field(description="function name, e.g. 'mulDivCVL'")
    params: list[tuple[str, str]] = Field(description="params as [type, name] pairs")
    returns: list[str] = Field(description="return type names")
    body: str = Field(description="the function body as CVL statements, e.g. 'uint256 c = a * b; return c;'")

    @override
    async def run(self) -> str:
        try:
            cmds = parse_commands(self.body, self.params)
        except CVLParseError as e:
            return f"REJECTED: CVL parse error in body: {e}"
        with self.tool_deps() as d:
            return _res(mut.add_model_function(d.project, name=self.name, params=self.params,
                                               returns=self.returns, commands=cmds))


class SetModelMethodBody(_Tool):
    """Fill a model method body (HOLE-F): the <method>CVL body — a permissive revert-guard, the state
    effect (ghost writes), and the return via the math mirrors. CVL-only; may read/write ghosts."""
    method: str = Field(description="the CUT method whose model body to fill, e.g. 'draw'")
    body: str = Field(description="the body as CVL statements (declarations, if/revert, ghost writes, return)")

    @override
    async def run(self) -> str:
        try:
            cmds = parse_commands(self.body)
        except CVLParseError as e:
            return f"REJECTED: CVL parse error in body: {e}"
        with self.tool_deps() as d:
            return _res(mut.set_model_method_body(d.project, method=self.method, commands=cmds))


class AddModelGhostAxiom(_Tool):
    """Add a definitional axiom to a NON-glued internal model ghost (HOLE-A). REJECTED if it constrains a
    glued/pinned-to-real ghost (that must be a proved reachable invariant, not a model axiom)."""
    ghost_name: str = Field(description="the model ghost to axiomatize")
    axiom: str = Field(description="the axiom as a CVL boolean expression")
    initial: bool = Field(default=False, description="an `init_state` axiom?")

    @override
    async def run(self) -> str:
        try:
            e = parse_expression(self.axiom)
        except CVLParseError as ex:
            return f"REJECTED: CVL parse error in axiom: {ex}"
        with self.tool_deps() as d:
            return _res(mut.add_model_ghost_axiom(d.project, ghost_name=self.ghost_name,
                                                  axiom_expr=e, initial=self.initial))


# ---------------------------------------------------------------- reachability / conformance
class AddRequireInvariant(_Tool):
    """Declare a real-CUT reachable invariant AND requireInvariant it in the shared assumeReachable,
    atomically (never a bare/unproven assumption). Idempotent across methods. The invariant is a
    candidate — it must be discharged by the reachable conf before conformance results are trusted."""
    inv_name: str = Field(description="invariant name")
    inv_params: list[tuple[str, str]] = Field(description="invariant params as [type, name] pairs")
    inv_expr: str = Field(description="the invariant as a CVL boolean expression over REAL getters")
    require_args: list[str] = Field(description="args to pass in the requireInvariant call")

    @override
    async def run(self) -> str:
        try:
            e = parse_expression(self.inv_expr)
        except CVLParseError as ex:
            return f"REJECTED: CVL parse error in inv_expr: {ex}"
        with self.tool_deps() as d:
            return _res(mut.add_requireInvariant(d.project, inv_name=self.inv_name,
                                                 inv_params=self.inv_params, inv_expr=e,
                                                 require_args=self.require_args))


class AddNondet(_Tool):
    """Add a NONDET summary for a VIEW/PURE function in a method's conformance (a property-directed
    speedup). REJECTED for non-view/pure targets (NONDET drops side effects — unsound on state-changing
    fns; cross-checked against the scene when available)."""
    method: str = Field(description="the method whose conformance to add the NONDET to")
    contract: str | None = Field(description="contract qualifier, '_' for wildcard, or None")
    name: str = Field(description="the function to NONDET")
    param_types: list[str] = Field(description="param type names")
    return_types: list[str] = Field(description="return type names")
    mutability: str = Field(description="declared mutability (must be view/pure)")
    visibility: str = Field(default="external", description="external | internal")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(mut.add_nondet(d.project, method=self.method, contract=self.contract,
                                       name=self.name, param_types=self.param_types,
                                       return_types=self.return_types, mutability=self.mutability,
                                       visibility=self.visibility))


class AddHelperLemma(_Tool):
    """Insert a checked assert (a proof-decomposition lemma, e.g. accrue-idempotence) into a conformance
    rule, plus optional capture declarations. Never a require — only an added obligation; every call in
    the captures/assert must be view/pure."""
    method: str = Field(description="the method whose rule to add the lemma to")
    rule_name: str = Field(description="the conformance rule to insert into")
    captures: str = Field(default="", description="pre-SUT capture declarations as CVL statements")
    post_captures: str = Field(default="", description="post-SUT capture declarations as CVL statements")
    assert_expr: str = Field(default="", description="the lemma assertion as a CVL boolean expression")
    message: str = Field(default="helper lemma", description="the assert message")

    @override
    async def run(self) -> str:
        try:
            caps = parse_commands(self.captures) if self.captures else []
            post = parse_commands(self.post_captures) if self.post_captures else []
            aexp = parse_expression(self.assert_expr) if self.assert_expr else None
        except CVLParseError as ex:
            return f"REJECTED: CVL parse error: {ex}"
        with self.tool_deps() as d:
            return _res(mut.add_helper_lemma(d.project, method=self.method, rule_name=self.rule_name,
                                             captures=caps, post_captures=post, assert_expr=aexp,
                                             message=self.message))


# ---------------------------------------------------------------- removals (retract a wrong guess)
class RemoveHelperLemma(_Tool):
    """Remove a helper lemma (matched by its `message`) from a conformance rule — use when the lemma's
    assertion turned out FALSE (it VIOLATED), e.g. a preview-getter pivot that isn't exactly the return.
    Also prunes capture declarations the lemma left unused."""
    method: str = Field(description="the method whose rule holds the lemma")
    rule_name: str = Field(description="the conformance rule the lemma was inserted into")
    message: str = Field(description="the lemma's assert message (as given to add_helper_lemma)")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(mut.remove_helper_lemma(d.project, self.method, self.rule_name,
                                                message=self.message))


class RemoveModelGhostAxiom(_Tool):
    """Remove a definitional axiom (matched by its expression) from a NON-glued model ghost — retract a
    wrong axiom you added with add_model_ghost_axiom."""
    ghost_name: str = Field(description="the model ghost the axiom is on")
    axiom: str = Field(description="the axiom's CVL boolean expression (as given to add_model_ghost_axiom)")

    @override
    async def run(self) -> str:
        try:
            e = parse_expression(self.axiom)
        except CVLParseError as ex:
            return f"REJECTED: CVL parse error in axiom: {ex}"
        with self.tool_deps() as d:
            return _res(mut.remove_model_ghost_axiom(d.project, ghost_name=self.ghost_name, axiom_expr=e))


class RemoveNondet(_Tool):
    """Remove a NONDET summary for a function (by name) from a method's conformance — use when the NONDET
    was unsound, i.e. the checked output DID depend on it and the rule VIOLATED."""
    method: str = Field(description="the method whose conformance holds the NONDET")
    name: str = Field(description="the function name whose NONDET entry to remove")
    contract: str | None = Field(default=None, description="contract qualifier to scope the removal, or None for any")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(mut.remove_nondet(d.project, self.method, name=self.name, contract=self.contract))


class RemoveModelConstant(_Tool):
    """Remove a model constant/ghost YOU added with add_model_constant — the inverse. Removes the whole
    declaration (use this, not remove_model_ghost_axiom, to fully retract a constant — the latter only
    strips the axiom and leaves a bare, possibly name-colliding ghost). Refuses the template's observable
    ghosts."""
    name: str = Field(description="the constant/ghost name to remove (as given to add_model_constant)")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(mut.remove_model_constant(d.project, name=self.name))


class RemoveModelFunction(_Tool):
    """Remove a model helper function YOU added with add_model_function — the inverse. Refuses the
    template functions (per-binding readers and the <method>CVL bodies)."""
    name: str = Field(description="the model function name to remove (as given to add_model_function)")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return _res(mut.remove_model_function(d.project, name=self.name))


# ---------------------------------------------------------------- inspection (no mutation)
class CheckConsistency(_Tool):
    """Coherence check (no prover run): the discipline linter passes, each <f>CVL is present, and — when
    a full typechecker is bound — the whole bundle passes the REAL certoraRun CVL typecheck (catches
    assign-after-access, mathint-cast, type errors the fast structural check misses). Returns the list of
    problems (empty == consistent). CALL THIS before `result`: `result` should only be called when this
    says consistent. Reachable invariants show as 'pending proof' (expected — the prover discharges them
    later); that is NOT a problem to fix."""

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            problems = d.project.check_consistency()
            # H2 'assumed but not proven' is expected during fill (proof happens in the loop, not via a
            # tool) — don't treat it as a blocking problem or let it gate the (expensive) full typecheck.
            structural = [p for p in problems if "not proven" not in p]
            pending = len(problems) - len(structural)
            if structural:
                return "problems:\n- " + "\n- ".join(structural)
            if d.full_typecheck is not None:                 # the REAL typecheck (writes + certoraRun)
                res = d.full_typecheck()
                ok, diag = await res if asyncio.iscoroutine(res) else res
                if not ok:
                    return "typecheck failed — fix the CVL at the reported file:line:\n" + diag
            note = ("\n(note: reachable invariant(s) pending proof — the prover will discharge them; "
                    "not a problem to fix)" if pending else "")
            return "consistent" + note


class Verify(_Tool):
    """Run the PROVER on the current model (the real trust anchor, cloud, minutes/call): proves the
    shared reachable invariants, then verifies every method's conformance conf. Returns either an
    ALL-VERIFIED message or the failing rules with their COUNTEREXAMPLES (concrete inputs) / TIMEOUTs /
    dropped invariants. Call it once check_consistency is clean; FIX what it reports (per section 3 for
    timeouts) and call verify AGAIN; only call `result` after verify reports ALL methods verified."""

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            if d.verify is None:
                return "verify is not available in this run"
            return await d.verify()


class RenderModel(_Tool):
    """Return the current shared model spec as CVL text — inspect the ghosts/readers/<f>CVL bodies."""

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return d.project.render_model()


class RenderConformance(_Tool):
    """Return a method's current conformance spec as CVL text — inspect its glue + rules."""
    method: str = Field(description="the method whose conformance spec to render")

    @override
    async def run(self) -> str:
        with self.tool_deps() as d:
            return d.project.render_conformance(self.method)


# ---------------------------------------------------------------- assembly
_TOOLS: list[tuple[type[_Tool], str]] = [
    (AddModelConstant, "add_model_constant"),
    (AddModelFunction, "add_model_function"),
    (SetModelMethodBody, "set_model_method_body"),
    (AddModelGhostAxiom, "add_model_ghost_axiom"),
    (AddRequireInvariant, "add_require_invariant"),
    (AddNondet, "add_nondet"),
    (AddHelperLemma, "add_helper_lemma"),
    (RemoveHelperLemma, "remove_helper_lemma"),
    (RemoveNondet, "remove_nondet"),
    (RemoveModelGhostAxiom, "remove_model_ghost_axiom"),
    (RemoveModelConstant, "remove_model_constant"),
    (RemoveModelFunction, "remove_model_function"),
    (CheckConsistency, "check_consistency"),
    (RenderModel, "render_model"),
    (RenderConformance, "render_conformance"),
]


def smtool_tools(deps: SmtoolDeps) -> list[BaseTool]:
    """The smtool tool set, each bound to `deps` (the Project). Hand these to the agent loop.
    The `verify` tool (run the prover) is included only when a verify closure is bound (the refine
    runner sets it) — composer-style: the agent drives fill→verify→refine in ONE conversation, so
    prover feedback arrives inline as a tool result. CUT source-access tools (grep/read) are supplied
    separately as `extra_tools` (composer's fs_tools)."""
    tools = [cls.bind(deps).as_tool(name) for cls, name in _TOOLS]
    if deps.verify is not None:
        tools.append(Verify.bind(deps).as_tool("verify"))
    return tools
