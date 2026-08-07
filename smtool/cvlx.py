"""Thin ergonomic builders over composer.cvl.schema — a code-authoring DSL for CVL.

WHY this exists: composer.cvl.schema is an interchange/validation target for LLM-emitted CVL (a whole
AST as JSON via put_cvl, or surface text via put_cvl_raw) — nobody there assembles the tree node by
node, so composer never needed ergonomic constructors. smtool is the opposite: it GENERATES CVL in
code (the driver + recon agent-sims build specific AST), and doing that against the raw schema is
painful (verbose `type=`/discriminator boilerplate, many required fields, positional-`Field` gotchas).
These helpers smooth that. (Upstream-worthy: composer could adopt this as its blessed builder layer.)

Every helper returns a schema object; assemble them into a CVLFile and render with
composer.cvl.pretty_print.pretty_print. Validated against smoke_ast.py.
"""
from __future__ import annotations

import composer.cvl.schema as S

# ---------------------------------------------------------------- types
SPECIAL_TYPES = {"mathint", "env", "method", "calldataarg"}


def prim(name: str) -> S.PrimitiveType:
    return S.PrimitiveType(type="primitive", type_name=name)


def special(name: str) -> S.SpecialType:
    return S.SpecialType(type="special", type_name=name)


def mapping(key: str, val) -> S.MappingType:
    kt = prim(key) if isinstance(key, str) else key
    vt = prim(val) if isinstance(val, str) else val
    return S.MappingType(type="mapping", key_type=kt, value_type=vt)


def contract_type(host: str, type_name: str) -> S.ContractType:
    return S.ContractType(type="contract_type", host_contract=host, type_name=type_name)


def ty(name_or_type):
    if not isinstance(name_or_type, str):
        return name_or_type
    if name_or_type in SPECIAL_TYPES:
        return special(name_or_type)
    if "." in name_or_type:                       # a contract/struct type, e.g. IFoo.Bar
        host, tn = name_or_type.split(".", 1)
        return contract_type(host, tn)
    return prim(name_or_type)


def tid(type_name: str, name: str) -> S.TypeAndId:
    return S.TypeAndId(decl_type=ty(type_name), id=name)


def vmparam(type_name: str, name: str | None = None, location=None) -> S.VMParam:
    return S.VMParam(ty=S.VMType(base_type=ty(type_name), location=location), name=name)


# ---------------------------------------------------------------- expressions
def ident(name: str) -> S.Identifier:
    return S.Identifier(type="identifier", name=name)


def num(v) -> S.NumberLiteral:
    return S.NumberLiteral(type="number_literal", value=str(v))


def boollit(b: bool) -> S.BoolLiteral:
    return S.BoolLiteral(type="bool_literal", value=b)


def binop(op: str, l, r) -> S.BinaryOp:
    return S.BinaryOp(type="binary_op", operator=op, left=l, right=r)


def unop(op: str, operand) -> S.UnaryOp:
    return S.UnaryOp(type="unary_op", operator=op, operand=operand)


def unop_not(operand) -> S.UnaryOp:
    return unop("not", operand)


def idx(base, index) -> S.ArrayAccess:
    return S.ArrayAccess(type="array_access", base=base, index=index)


def field(base, field_name: str) -> S.FieldAccess:
    """A field-selection expression `base.field_name` (e.g. `e.msg.sender` nests two)."""
    return S.FieldAccess(type="field_access", base=base, field_name=field_name)


def call(name: str, params=(), host: str | None = None, annotation=None, state=None) -> S.FunctionCall:
    return S.FunctionCall(
        type="function_call",
        application=S.FunctionApplication(
            annotation=annotation, name=name, host_contract=host,
            params=list(params), state=state,
        ),
    )


def forall(var_type: str, var: str, body) -> S.QuantifierExp:
    return S.QuantifierExp(type="quantifier", is_forall=True, variable=tid(var_type, var), body=body)


def cond(c, then_e, else_e) -> S.ConditionalExp:
    return S.ConditionalExp(type="conditional", condition=c, then_expr=then_e, else_expr=else_e)


# ---------------------------------------------------------------- commands
def declare(type_name: str, name: str, init=None) -> S.DeclarationCmd:
    return S.DeclarationCmd(type="declaration", variable=tid(type_name, name), initial_value=init)


def assign(name: str, expr) -> S.AssignmentCmd:
    return S.AssignmentCmd(type="assignment",
                           left_hand_sides=[S.IdLhs(type="id", name=name)], expression=expr)


def assign_multi(names: list[str], expr) -> S.AssignmentCmd:
    return S.AssignmentCmd(type="assignment",
                           left_hand_sides=[S.IdLhs(type="id", name=n) for n in names], expression=expr)


def assign_index(name: str, indices: list, expr) -> S.AssignmentCmd:
    """`name[i0][i1]... = expr;`  (nested map/array store)."""
    lhs = S.IdLhs(type="id", name=name)
    for ix in indices:
        lhs = S.ArrayAccessLhs(type="array_access", base=lhs, index=ix)
    return S.AssignmentCmd(type="assignment", left_hand_sides=[lhs], expression=expr)


def _msg(m: str | None) -> str | None:
    """Sanitize a CVL command message (a human-readable label). pretty_print emits it inside double
    quotes with NO escaping, so a `"`/backslash/newline in the message produces an invalid spec that
    only the prover rejects (cryptically). Messages carry no semantics, so we neutralize those chars
    rather than escape them — this is the one free-text field an agent can supply that bypasses the
    cvl_parse validator."""
    if not m:
        return m
    return m.replace("\\", "/").replace('"', "'").replace("\n", " ").replace("\r", " ")


def require(expr, message: str) -> S.AssumeCmd:
    return S.AssumeCmd(type="assume", expression=expr, message=_msg(message))


def require_invariant(name: str, args=()) -> S.AssumeInvariantCmd:
    return S.AssumeInvariantCmd(type="assume_invariant", invariant_name=name, arguments=list(args))


def assert_(expr, message: str) -> S.AssertCmd:
    return S.AssertCmd(type="assert", expression=expr, message=_msg(message))


def ret(values=()) -> S.ReturnCmd:
    return S.ReturnCmd(type="return", values=list(values))


def revert(message: str | None = None) -> S.RevertCmd:
    return S.RevertCmd(type="revert", message=_msg(message))


def apply(fn_call: S.FunctionCall) -> S.ApplyCmd:
    return S.ApplyCmd(type="apply", target=fn_call.application)


def if_(cond, then_cmds, else_cmds=None) -> S.IfCmd:
    else_block = None
    if else_cmds is not None:
        else_block = S.Else(type="else", commands=list(else_cmds))
    return S.IfCmd(type="if", condition=cond, then_cmd=list(then_cmds), else_block=else_block)


def block(commands) -> S.CodeBlock:
    return S.CodeBlock(commands=list(commands))


# ---------------------------------------------------------------- top-level blocks
def ghost_mapping(name: str, key: str, val: str, *, persistent=True, axioms=()) -> S.GhostDef:
    return S.GhostDef(
        type="ghost_def", ghost_name=name, persistent=persistent,
        ghost_type=S.GhostVariable(type="ghost_type", base_type=mapping(key, val)),
        axioms=list(axioms),
    )


def ghost_scalar(name: str, val_type: str, *, persistent=True, axioms=()) -> S.GhostDef:
    return S.GhostDef(
        type="ghost_def", ghost_name=name, persistent=persistent,
        ghost_type=S.GhostVariable(type="ghost_type", base_type=prim(val_type)),
        axioms=list(axioms),
    )


def ghost_fn(name: str, param_types, ret: str, *, persistent=True, axioms=()) -> S.GhostDef:
    """A ghost FUNCTION `[persistent] ghost name(<param_types>) returns ret;` (optionally with axioms).
    Keyed on value types (like the model's ghosts — `driver._nested_ghost`; key = param types, nothing to
    resolve). Used by the deterministic-memo summary (detsummary): a read-only ghost function IS the memo
    — same key, same value — and persistent so a havoc/revert can't re-havoc it mid-proof."""
    return S.GhostDef(
        type="ghost_def", ghost_name=name, persistent=persistent,
        ghost_type=S.GhostFunction(type="ghost_fun", params=[ty(t) for t in param_types],
                                   result_type=ty(ret)),
        axioms=list(axioms),
    )


def axiom(exp, *, initial=False) -> S.GhostAxiom:
    return S.GhostAxiom(initial=initial, exp=exp)


def func(name: str, params, returns, commands) -> S.FunctionDef:
    """params: list[(type,name)]; returns: list[type-name]; commands: list[Command]"""
    return S.FunctionDef(
        type="func_def", name=name,
        params=[tid(t, n) for t, n in params],
        return_value=[ty(t) for t in returns],
        block=block(commands),
    )


def rule(name: str, params, commands, filtered=None) -> S.RuleBlock:
    return S.RuleBlock(
        type="rule", rule_name=name,
        rule_params=[tid(t, n) for t, n in params],
        filtered_block=filtered, block=block(commands),
    )


def invariant(name: str, params, expr, *, filter=None, proofs=()) -> S.Invariant:
    return S.Invariant(
        type="invariant", name=name,
        invariant_params=[tid(t, n) for t, n in params],
        invariant_expression=expr, filter=filter, proofs=list(proofs),
    )


def methods_block(entries) -> S.MethodsBlock:
    return S.MethodsBlock(type="methods_block", method_entries=list(entries))


def use_invariant(name: str) -> S.UseDirective:
    """`use invariant <name>;` — include an imported invariant in this spec's verification."""
    return S.UseDirective(type="use_directive", use_kind="invariant", name=name)


def m_envfree(contract: str | None, name: str, param_types, return_types) -> S.ImportedFunction:
    return S.ImportedFunction(
        type="imported_function",
        signature=S.MethodSignature(
            method_ref=S.MethodReference(contract=contract, method_name=name),
            parameters=[vmparam(t) for t in param_types],
            return_types=[vmparam(t) for t in return_types],
            visibility="external", post_flags=["envfree"],
        ),
        summary=None, with_env=None,
    )


def expect_types(type_names) -> S.ExpectType:
    return S.ExpectType(type="type",
                        expected_types=[S.VMType(base_type=ty(t), location=None) for t in type_names])


def m_expr_summary(contract: str | None, name: str, named_params, return_expect,
                   summary_call: S.FunctionCall, with_env: str | None = "e",
                   visibility="external") -> S.ImportedFunction:
    """A methods{} expression-summary entry:
    `function C.name(<named_params>) external [with (env e)] => summary_call expect (return_expect);`
    named_params: list[(type, name)]; return_expect: list[type names]."""
    return S.ImportedFunction(
        type="imported_function",
        signature=S.MethodSignature(
            method_ref=S.MethodReference(contract=contract, method_name=name),
            parameters=[vmparam(t, n) for t, n in named_params],
            return_types=[], visibility=visibility, post_flags=[]),
        summary=S.ExpressionSummary(type="expression", expression=summary_call,
                                    expect_clause=expect_types(return_expect)),
        with_env=with_env,
    )


def m_nondet(contract: str | None, name: str, param_types, return_types, visibility="external") -> S.ImportedFunction:
    return S.ImportedFunction(
        type="imported_function",
        signature=S.MethodSignature(
            method_ref=S.MethodReference(contract=contract, method_name=name),
            parameters=[vmparam(t) for t in param_types],
            return_types=[vmparam(t) for t in return_types],
            visibility=visibility, post_flags=[],
        ),
        summary=S.HavocingSummary(type="havocing", havoc_keyword="nondet"),
        with_env=None,
    )


def spec_file(*, imports=(), contracts=(), blocks=()) -> S.CVLFile:
    return S.CVLFile(
        import_specs=[S.ImportSpec(spec_file=i) for i in imports],
        import_contract=[S.ContractImport(contract_name=c, as_name=a) for c, a in contracts],
        blocks=list(blocks),
    )
