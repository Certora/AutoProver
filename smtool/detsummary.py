"""Deterministic-ghost ("memo") summary builder — a SEPARATE piece from overapprox.py.

overapprox.py proves a per-output predicate Phi and emits the havoc summary `{T res; require Phi; return
res;}` (sound-by-construction, but non-deterministic). This module turns that into a summary that behaves
as a FUNCTION of its input, which consumer proofs usually need:

    persistent ghost <fn>Ghost(<key types>) returns <ret>;      // the memo — same key, same value
    function <fn>CVL(<params>) returns <ret> {
        <ret> res = <fn>Ghost(<key>);                           // deterministic load (no store/flag needed:
        [ <uintW> v; require to_bytesN(v) == res; ]             //  a ghost FUNCTION *is* the memo)
        [ require <Phi over v-or-res>; ]                        // re-impose the conformance-proven property
        return res;
    }
    methods { function <cut>.<fn>(...) internal returns (<ret>) => <fn>CVL(...); [+ also_bind] }

Determinism is correct-by-construction (a ghost function is deterministic; `persistent` keeps it stable
across havoc/revert). NO injectivity here (add later, and only when a consumer needs distinct outputs).

Keying reuses the model's principle (`driver._nested_ghost`: key on the param types, nothing to choose):
- SCALAR params -> key the ghost directly on their CVL types (UDVT included; ghosts accept value types).
- one ARRAY param -> key on a bounded PREFIX `(uint256 length, <elem key> x key_len)` (default 3), since
  CVL ghosts can't key on an array. Sound over inputs whose length <= key_len (holds via the consumer's
  loop bound). The out-of-bounds default needs the element as a scalar, so array elements are cast to
  `elem_key_type` (default uint256 via assert_uint256) — resolve the element's underlying type from the
  scene with `scene.canonical_arg_types` (reuses autosetup's parse_type_descriptor CANONICAL), or pass it.
"""
from dataclasses import dataclass, field
from typing import Callable

import composer.cvl.schema as S
from composer.cvl.pretty_print import pretty_print

from . import cvlx as x


def ghost_name(fn: str) -> str:
    return fn + "Ghost"


def summary_fn_name(fn: str) -> str:
    return fn + "CVL"


def _is_array(cvl_type: str) -> bool:
    return cvl_type.rstrip().endswith("[]")


@dataclass
class MemoTarget:
    """What to memo-summarize. `params` are (cvl_type, name); `ret` is the single return type.
    For an array target (exactly one array param) the ghost keys on (length, first `key_len` elements)."""
    cut: str
    fn: str
    params: list                      # list[(cvl_type, name)]
    ret: str
    key_len: int = 3                  # array-prefix length (parametric; default 3)
    elem_key_type: str = "uint256"    # ghost key type for array elements (their canonical/underlying base)
    elem_cast: str = "assert_uint256" # cast an element to elem_key_type; the out-of-bounds slot is `0`
    also_bind: tuple = ()             # extra internal method names sharing this memo (same params/ret)
    # optional re-imposition of the conformance-proven output property Phi:
    phi_of: Callable[[str], S.Expression] | None = None  # given a var name, the Phi boolean expression
    ret_pin: tuple | None = None      # (uintType, to_bytesFn) to pin a bytesN result to `v` before phi_of("v")

    @property
    def array_param(self):
        arrs = [(t, n) for t, n in self.params if _is_array(t)]
        return arrs[0] if (len(self.params) == 1 and arrs) else None


def _key_and_body_array(t: MemoTarget):
    """Array target: key = (uint256 len, elem x key_len); body extracts the bounded prefix."""
    atype, aname = t.array_param
    key_types = ["uint256"] + [t.elem_key_type] * t.key_len
    cmds = [x.declare("uint256", "n", x.field(x.ident(aname), "length"))]
    key_args = [x.ident("n")]
    for i in range(t.key_len):
        # a_i = n > i ? <cast>(legs[i]) : 0     (the out-of-bounds slots default to 0)
        elem = x.call(t.elem_cast, [x.idx(x.ident(aname), x.num(i))])
        cmds.append(x.declare(t.elem_key_type, f"a{i}",
                              x.cond(x.binop("gt", x.ident("n"), x.num(i)), elem, x.num(0))))
        key_args.append(x.ident(f"a{i}"))
    return key_types, cmds, key_args


def _key_and_body_scalar(t: MemoTarget):
    """Scalar target: key the ghost directly on the param types (like the model's ghosts)."""
    key_types = [ct for ct, _ in t.params]
    key_args = [x.ident(n) for _, n in t.params]
    return key_types, [], key_args


def build_memo_summary(t: MemoTarget) -> S.CVLFile:
    """The deterministic-memo summary as a CVL AST file: the persistent ghost function + `<fn>CVL` +
    the internal methods bindings. No injectivity."""
    g = ghost_name(t.fn)
    if t.array_param is not None:
        key_types, body, key_args = _key_and_body_array(t)
    else:
        key_types, body, key_args = _key_and_body_scalar(t)

    body = list(body)
    body.append(x.declare(t.ret, "res", x.call(g, key_args)))          # deterministic load from the memo
    # re-impose Phi (the conformance-proven output property), if any
    if t.ret_pin is not None:                                          # bytesN result: pin to an int `v`
        uint_ty, to_bytes = t.ret_pin
        body.append(x.declare(uint_ty, "v"))
        body.append(x.require(x.binop("eq", x.call(to_bytes, [x.ident("v")]), x.ident("res")),
                              "pin the bytes result to its integer value"))
        if t.phi_of is not None:
            body.append(x.require(t.phi_of("v"), "over-approx: result satisfies Phi"))
    elif t.phi_of is not None:
        body.append(x.require(t.phi_of("res"), "over-approx: result satisfies Phi"))
    body.append(x.ret([x.ident("res")]))

    summary_fn = x.func(summary_fn_name(t.fn), t.params, [t.ret], body)
    ghost = x.ghost_fn(g, key_types, t.ret)
    bindings = [_internal_binding(t, m) for m in [t.fn, *t.also_bind]]
    return x.spec_file(blocks=[ghost, summary_fn, x.methods_block(bindings)])


def _internal_binding(t: MemoTarget, method: str) -> S.ImportedFunction:
    """`function <cut>.<method>(<params, memory for arrays>) internal returns (<ret>) => <fn>CVL(args);`
    Bind the INTERNAL call site (that is where the prover-hostile cost lives), sharing the one summary."""
    params, args = [], []
    for ct, n in t.params:
        bn = "_" + n
        params.append(x.vmparam(ct, bn, location=("memory" if _is_array(ct) else None)))
        args.append(x.ident(bn))
    return S.ImportedFunction(
        type="imported_function",
        signature=S.MethodSignature(
            method_ref=S.MethodReference(contract=t.cut, method_name=method),
            # an INTERNAL summary must declare the real method's return arity on the signature
            # (`returns (<ret>)`); the `expect` clause is for external expression summaries.
            parameters=params, return_types=[x.vmparam(t.ret)], visibility="internal", post_flags=[]),
        summary=S.ExpressionSummary(type="expression",
                                    expression=x.call(summary_fn_name(t.fn), args),
                                    expect_clause=None),
        with_env=None,
    )


def render(t: MemoTarget) -> str:
    return pretty_print(build_memo_summary(t))


# ---- Phi helpers (build a conformance-proven output property as a cvlx expression) -------------------
def tag_high_byte(const_pow_240: int, value: int) -> Callable[[str], S.Expression]:
    """Phi: the top byte of the (uint) result == `value`, expressed as `v / 2^240 == value` (division,
    not a shift — matches the conformance-proven predicate). `const_pow_240` = 2**240."""
    return lambda v: x.binop("eq", x.binop("div", x.ident(v), x.num(const_pow_240)), x.num(value))
