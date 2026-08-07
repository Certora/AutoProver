"""detsummary — the deterministic-memo ghost-summary builder (AST). Fast, no LLM/prover/scene.

Guards: the persistent ghost FUNCTION (the memo), array-prefix keying (length + first K elems, K
parametric), scalar keying directly on param types (reusing the model's principle), the internal
`returns`-carrying bindings (+ also_bind), and the absence of any injectivity axiom. Generic
identifiers only (a `bytes31`-backed id computed from an element array — no specific project's code)."""
from smtool.detsummary import MemoTarget, render, tag_high_byte


def test_array_target_prefix_keyed_memo():
    t = MemoTarget(cut="M", fn="digest", params=[("M.Item[]", "xs")], ret="M.Id",
                   key_len=4, also_bind=("_alt",),
                   ret_pin=("uint248", "to_bytes31"), phi_of=tag_high_byte(2**240, 3))
    txt = render(t)
    # the memo: a PERSISTENT ghost FUNCTION keyed on (uint256 len, 4x uint256) — no store, no flag
    assert "persistent ghost digestGhost(uint256, uint256, uint256, uint256, uint256) returns M.Id;" in txt
    # bounded-prefix key extraction with the element cast + out-of-bounds default 0
    assert "uint256 n = xs.length;" in txt
    assert "uint256 a0 = n > 0 ? assert_uint256(xs[0]) : 0;" in txt
    assert "uint256 a3 = n > 3 ? assert_uint256(xs[3]) : 0;" in txt
    # deterministic load, then Phi re-imposed via the byte-pin (top byte == 3, as division by 2^240)
    assert "M.Id res = digestGhost(n, a0, a1, a2, a3);" in txt
    assert "require(to_bytes31(v) == res" in txt
    assert f"require(v / {2**240} == 3" in txt
    # INTERNAL bindings (where the cost is) for the primary + sibling, carrying `returns`
    assert "function M.digest(M.Item[] memory _xs) internal returns (M.Id) => digestCVL(_xs);" in txt
    assert "function M._alt(M.Item[] memory _xs) internal returns (M.Id) => digestCVL(_xs);" in txt
    # determinism ONLY — no injectivity axiom
    assert "forall" not in txt and "Inv" not in txt


def test_key_len_defaults_to_3():
    t = MemoTarget(cut="M", fn="digest", params=[("M.Item[]", "xs")], ret="M.Id")
    txt = render(t)
    assert "persistent ghost digestGhost(uint256, uint256, uint256, uint256) returns M.Id;" in txt  # len + 3
    assert "uint256 a2 = n > 2 ? assert_uint256(xs[2]) : 0;" in txt and "a3" not in txt


def test_scalar_target_keys_on_param_types_directly():
    """Scalar inputs: key the ghost on the param CVL types directly (like driver._nested_ghost) — no
    prefix, no cast."""
    t = MemoTarget(cut="M", fn="price", params=[("uint256", "id"), ("address", "who")], ret="uint256")
    txt = render(t)
    assert "persistent ghost priceGhost(uint256, address) returns uint256;" in txt
    assert "uint256 res = priceGhost(id, who);" in txt          # keyed directly on the params
    assert "xs.length" not in txt and "?" not in txt            # no array-prefix machinery
    assert "function M.price(uint256 _id, address _who) internal returns (uint256) => priceCVL(_id, _who);" in txt
