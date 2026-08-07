"""Piece B: parse CVL surface TEXT into `composer.cvl.schema` AST, so the fill agent can emit natural
CVL (small tool schemas) instead of AST-as-JSON (~75k tokens/turn — impractical).

We don't write a CVL parser: `ASTExtraction.jar syntax-check --raw` (via
`certora_autosetup...summary_resolver.extract_cvl_ast`) already parses CVL text to JSON — but in the
Prover's verbose `spec.cvlast` dialect (FQN discriminators + range/scope/tag metadata), NOT
`composer.cvl.schema`. This module is the DIALECT TRANSLATION: `spec.cvlast`-JSON -> composer schema,
bounded to the node types model bodies/expressions use. `--raw` is syntax-only (tolerates free
identifiers), so we wrap fragments in a stub function and pull the piece back out.

Public: `parse_expression(text) -> S.Expression`, `parse_commands(text) -> list[S.Command]`.
"""
from __future__ import annotations

import composer.cvl.schema as S
from . import cvlx as x
from certora_autosetup.setup.summary_resolver import extract_cvl_ast


class CVLParseError(ValueError):
    pass


def _disc(j: dict) -> str:
    """Last segment of the node's FQN discriminator (e.g. 'AddExp', 'Definition', 'UIntK')."""
    return (j.get("cmd_type") or j.get("type") or "").split(".")[-1]


# spec.cvlast binary-op node -> composer operator string (as cvlx/pretty_print use)
_BINOP = {
    "AddExp": "add", "SubExp": "sub", "MulExp": "mul", "DivExp": "div", "ModExp": "mod",
    "ExponentExp": "exponent", "PowExp": "exponent",
    "LandExp": "and", "LorExp": "or", "ImpliesExp": "implies", "IffExp": "iff",
    "EqExp": "eq", "NeExp": "ne", "GtExp": "gt", "GeExp": "ge", "LtExp": "lt", "LeExp": "le",
}


def _msg(d) -> str:
    """A CVL command's description field -> plain message (the jar keeps the surrounding quotes)."""
    if not d:
        return ""
    d = str(d)
    return d[1:-1] if len(d) >= 2 and d[0] == d[-1] == '"' else d


def _type(j: dict) -> str:
    """spec.cvlast PureCVLType -> CVL type-name string."""
    t = _disc(j)
    if t == "UIntK":
        return f"uint{j.get('k', 256)}"
    if t == "IntK":
        return f"int{j.get('k', 256)}"
    if t == "BytesK":
        return f"bytes{j.get('k')}"
    if t == "Bool":
        return "bool"
    if t == "AccountIdentifier":
        return "address"
    if t == "Mathint":
        return "mathint"
    return j.get("name") or t          # user-defined / fallback


def _expr(j: dict) -> "S.Expression":
    t = _disc(j)
    if t == "VariableExp":
        return x.ident(j["id"])
    if t == "NumberLit":
        return x.num(int(str(j["n"]), 16))   # the jar serializes NumberLit.n as unprefixed HEX (10 -> "a")
    if t == "BoolLit":
        return x.boollit(j["b"])
    if t == "LNotExp":
        return x.unop_not(_expr(j["e"]))
    if t == "CondExp":
        return x.cond(_expr(j["c"]), _expr(j["e1"]), _expr(j["e2"]))
    if t == "ArrayDerefExp":
        return x.idx(_expr(j["array"]), _expr(j["index"]))
    if t == "FieldSelectExp":          # struct/env field access: premiumDelta.sharesDelta, e.msg.sender
        return x.field(_expr(j["structExp"]), j["fieldName"])
    if t == "CastExpr":                # to_mathint(...) / require_uint256(...) / assert_uint256(...)
        # castType is TO (safe/widening: to_mathint, to_bytesN) | REQUIRE | ASSERT — one cast-fn family
        # each. Mapping TO to `require_` (the old default) corrupts the round-trip (require_bytes31 /
        # require_mathint are not CVL functions), so decode the kind explicitly.
        ct = str(j.get("castType", "")).upper()
        cast = {"TO": "to", "ASSERT": "assert"}.get(ct, "require")
        return x.call(f"{cast}_{_type(j['toCastType'])}", [_expr(j["arg"])])
    if t == "UnresolvedApplyExp":      # a CVL function call: methodId(args...)
        base = j.get("base")
        host = base["id"] if isinstance(base, dict) and _disc(base) == "VariableExp" else None
        return x.call(j["methodId"], [_expr(a) for a in j.get("args", [])], host=host)
    if t in _BINOP:
        return x.binop(_BINOP[t], _expr(j["l"]), _expr(j["r"]))
    raise CVLParseError(f"unhandled expression node: {j.get('type')}")


def _lhs(j: dict):
    t = _disc(j)
    if t == "Id":
        return S.IdLhs(type="id", name=j["id"])
    if t == "Array":
        return S.ArrayAccessLhs(type="array_access", base=_lhs(j["innerLhs"]), index=_expr(j["index"]))
    raise CVLParseError(f"unhandled LHS node: {j.get('type')}")


def _block(j) -> list:
    """An if/else BRANCH -> list of decoded commands. The jar emits a braced branch as a
    Composite.Block `{block:[...]}`, but a BRACE-LESS single statement (`if (c) revert();`) as the bare
    command node itself (no `block` key). Handle both, else the brace-less body is silently dropped
    (a no-op guard — e.g. `if (b==0) revert();` becoming `if (b==0) {}`, which caused div-by-zero)."""
    if not j:
        return []
    if "block" in j:
        return [_cmd(c) for c in j["block"] if _disc(c) != "Nop"]
    if _disc(j) == "Nop":          # the jar's sentinel for an absent else / empty branch
        return []
    if j.get("cmd_type") or j.get("type"):     # a bare single-statement branch
        return [_cmd(j)]
    return []


def _cmd(j: dict) -> "S.Command":
    c = _disc(j)
    if c == "Definition":
        exp = _expr(j["exp"])
        if isinstance(j.get("type"), dict) and j["type"] and len(j["idL"]) == 1 and _disc(j["idL"][0]) == "Id":
            return x.declare(_type(j["type"]), j["idL"][0]["id"], exp)   # declaration with init
        return S.AssignmentCmd(type="assignment",                        # (re)assignment, maybe indexed/multi
                               left_hand_sides=[_lhs(l) for l in j["idL"]], expression=exp)
    if c == "Declaration":
        return x.declare(_type(j["cvlType"]), j["id"])
    if c == "Assignment":
        return S.AssignmentCmd(type="assignment",
                               left_hand_sides=[_lhs(l) for l in j["idL"]], expression=_expr(j["exp"]))
    if c == "If":
        return x.if_(_expr(j["cond"]), _block(j.get("thenCmd")), _block(j.get("elseCmd")) or None)
    if c == "Return":
        return x.ret([_expr(e) for e in j.get("exps", [])])
    if c == "Revert":
        reason = j.get("reason")
        return x.revert(reason if isinstance(reason, str) else None)
    if c == "Assume":                  # `require(cond, "msg")` — parsed; the linter judges where it's legal
        return x.require(_expr(j["exp"]), _msg(j.get("description")))
    if c == "Assert":                  # `assert cond, "msg";`
        return x.assert_(_expr(j["exp"]), _msg(j.get("description")))
    if c in ("Apply", "ApplyCmd"):     # bare call statement (side-effecting helper)
        return x.apply(_expr(j["exp"]))
    raise CVLParseError(f"unhandled command node: {j.get('cmd_type')}")


def _parse(frag: str):
    root = extract_cvl_ast(frag)
    if root is None:
        raise CVLParseError(f"jar rejected fragment (syntax error):\n{frag}")
    subs = root["ast"]["subs"]
    if not subs:
        raise CVLParseError(f"no function parsed from:\n{frag}")
    return subs[0]["block"]


def _decode(fn):
    """Run a decode, converting any decoder/validation error into a CVLParseError (so tools reject
    cleanly instead of crashing)."""
    try:
        return fn()
    except CVLParseError:
        raise
    except Exception as e:                                 # e.g. a pydantic ValidationError on a bad node
        raise CVLParseError(f"could not decode parsed CVL ({type(e).__name__}: {e})")


def parse_commands(text: str, params: list[tuple[str, str]] = ()) -> list["S.Command"]:
    """Parse a CVL statement sequence (a function body) into composer Command AST. `params` declares
    the free params the body references so names are in scope (types are for the wrapper only)."""
    ps = ", ".join(f"{t} {n}" for t, n in params)
    block = _parse(f"function _b({ps}) {{ {text} }}")
    return _decode(lambda: [_cmd(c) for c in block])


def parse_expression(text: str) -> "S.Expression":
    """Parse a single CVL expression into composer Expression AST (free identifiers OK)."""
    block = _parse(f"function _e() returns uint256 {{ return {text}; }}")
    return _decode(lambda: _expr(block[-1]["exps"][0]))
