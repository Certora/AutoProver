"""render_model/render_conformance de-duplication: a repeat render returns only what CHANGED (UNCHANGED
or a unified diff) instead of re-dumping the whole spec, with a periodic full re-sync. Observation-only —
the model is untouched. Cuts the ~1/4 of agent turns spent re-rendering + the context they bloat."""
from types import SimpleNamespace
from smtool.agent.tools import _render_dedup, _RENDER_FULL_EVERY


def test_first_render_is_full():
    p = SimpleNamespace()
    assert _render_dedup(p, "model", "A\nB\nC") == "A\nB\nC"


def test_identical_rerender_is_unchanged_not_full():
    p = SimpleNamespace()
    _render_dedup(p, "model", "A\nB\nC")
    out = _render_dedup(p, "model", "A\nB\nC")
    assert "UNCHANGED" in out and "A\nB\nC" not in out


def test_changed_rerender_returns_a_diff():
    # a realistically-sized spec: a one-line change -> the diff is far smaller than the full text
    base = "\n".join(f"line {i}" for i in range(40))
    changed = base.replace("line 20", "line 20 CHANGED")
    p = SimpleNamespace()
    _render_dedup(p, "model", base)
    out = _render_dedup(p, "model", changed)
    assert "CHANGED" in out and "+line 20 CHANGED" in out and "-line 20" in out
    assert len(out) < len(changed)       # compressed: a diff, not the whole spec


def test_targets_are_independent():
    p = SimpleNamespace()
    _render_dedup(p, "model", "M1")
    # a different target's first render is still full, unaffected by the model cache
    assert _render_dedup(p, "conformance:f", "C1") == "C1"


def test_periodic_full_resync():
    p = SimpleNamespace()
    text = "A\nB\nC"
    outs = [_render_dedup(p, "model", text) for _ in range(_RENDER_FULL_EVERY + 2)]
    # outs[0] full; the next _RENDER_FULL_EVERY-1 are compressed; then a full re-sync reappears
    assert outs[0] == text
    assert any(o == text for o in outs[1:]), "must re-emit the full spec periodically (re-sync)"
    assert sum(1 for o in outs if "UNCHANGED" in o) >= 1


def test_force_full_returns_whole_spec_on_demand():
    p = SimpleNamespace()
    _render_dedup(p, "model", "A\nB\nC")
    # a plain re-render would be UNCHANGED; force_full overrides that
    out = _render_dedup(p, "model", "A\nB\nC", force_full=True)
    assert out == "A\nB\nC"
    # and it re-syncs the counter: the NEXT identical render compresses again
    assert "UNCHANGED" in _render_dedup(p, "model", "A\nB\nC")


def test_near_total_change_falls_back_to_full():
    p = SimpleNamespace()
    _render_dedup(p, "model", "a\nb\nc\nd")
    big = "\n".join(f"totally different line {i}" for i in range(20))
    assert _render_dedup(p, "model", big) == big   # diff >= full -> return full
