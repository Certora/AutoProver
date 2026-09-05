"""Unit tests for the multi-spec-buffer substrate."""

from composer.spec.source.spec_buffers import (
    NamedBuffer,
    buffer_digest,
    import_closure,
    merge_buffers,
    run_targets,
    validate_coverage,
    validate_disjoint_rules,
)

SHARED = "ghost g(uint) returns uint;\n"
EASY = 'import "shared.spec";\nrule r_easy { assert true; }\n'
HARD = 'import "shared.spec";\nrule r_hard { assert g(0) >= 0; }\n'


def _buffers(**overrides):
    b = {
        "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
        "easy": NamedBuffer(
            name="easy", cvl=EASY, property_rules={"P-easy": ["r_easy"]}, imports=("shared",)
        ),
        "hard": NamedBuffer(
            name="hard", cvl=HARD, property_rules={"P-hard": ["r_hard"]}, imports=("shared",)
        ),
    }
    b.update(overrides)
    return b


# --- model -----------------------------------------------------------------


def test_owned_rules_and_properties_derive_from_mapping():
    b = NamedBuffer(name="g", cvl="", property_rules={"P1": ["a", "b"], "P2": ["c"]})
    assert b.properties == {"P1", "P2"}
    assert b.owned_rules == {"a", "b", "c"}


def test_shared_buffer_owns_nothing():
    b = _buffers()["shared"]
    assert b.owned_rules == frozenset()
    assert b.is_run_target is False


# --- import closure --------------------------------------------------------


def test_import_closure_includes_transitive_imports():
    b = _buffers()
    b["mid"] = NamedBuffer(name="mid", cvl="// mid\n", imports=("shared",), is_run_target=False)
    b["top"] = NamedBuffer(name="top", cvl="// top\n", property_rules={"P-top": ["r_top"]}, imports=("mid",))
    names = {x.name for x in import_closure(b, "top")}
    assert names == {"top", "mid", "shared"}


def test_import_closure_tolerates_cycles_and_dangling():
    b = {
        "a": NamedBuffer(name="a", cvl="a", imports=("b", "missing")),
        "b": NamedBuffer(name="b", cvl="b", imports=("a",)),
    }
    names = {x.name for x in import_closure(b, "a")}
    assert names == {"a", "b"}  # cycle terminates; unknown "missing" skipped


# --- digest ----------------------------------------------------------------


def test_digest_changes_when_shared_import_changes():
    b = _buffers()
    before = buffer_digest(b, "easy")
    b["shared"] = NamedBuffer(name="shared", cvl=SHARED + "ghost h(uint) returns uint;\n", is_run_target=False)
    after = buffer_digest(b, "easy")
    assert before != after  # editing an imported buffer invalidates the importer


def test_digest_stable_and_independent_across_buffers():
    b = _buffers()
    assert buffer_digest(b, "easy") == buffer_digest(b, "easy")  # deterministic
    b["hard"] = NamedBuffer(name="hard", cvl=HARD + "// tweak\n", property_rules={"P-hard": ["r_hard"]}, imports=("shared",))
    assert buffer_digest(_buffers(), "easy") == buffer_digest(b, "easy")  # editing hard doesn't touch easy


def test_digest_folds_in_extra_parts():
    b = _buffers()
    assert buffer_digest(b, "easy", extra_parts=["skipped:P-x"]) != buffer_digest(b, "easy")


# --- run targets -----------------------------------------------------------


def test_run_targets_excludes_shared():
    assert [b.name for b in run_targets(_buffers())] == ["easy", "hard"]


# --- coverage --------------------------------------------------------------


def test_coverage_ok():
    assert validate_coverage(_buffers(), all_properties={"P-easy", "P-hard"}, skipped=set()) is None


def test_coverage_missing_property():
    err = validate_coverage(_buffers(), all_properties={"P-easy", "P-hard", "P-extra"}, skipped=set())
    assert err is not None and "no buffer" in err


def test_coverage_duplicate_property():
    b = _buffers()
    b["hard"] = NamedBuffer(name="hard", cvl=HARD, property_rules={"P-easy": ["r_hard"]}, imports=("shared",))
    err = validate_coverage(b, all_properties={"P-easy", "P-hard"}, skipped=set())
    assert err is not None and "more than one buffer" in err


def test_coverage_skipped_not_required_nor_assignable():
    ok = {
        "shared": NamedBuffer(name="shared", cvl=SHARED, is_run_target=False),
        "easy": NamedBuffer(name="easy", cvl=EASY, property_rules={"P-easy": ["r_easy"]}, imports=("shared",)),
    }
    assert validate_coverage(ok, all_properties={"P-easy", "P-hard"}, skipped={"P-hard"}) is None
    err = validate_coverage(_buffers(), all_properties={"P-easy", "P-hard"}, skipped={"P-hard"})
    assert err is not None and "skipped" in err


def test_coverage_unknown_property():
    b = _buffers()
    b["hard"] = NamedBuffer(name="hard", cvl=HARD, property_rules={"P-ghost": ["r_hard"]}, imports=("shared",))
    err = validate_coverage(b, all_properties={"P-easy", "P-hard"}, skipped=set())
    assert err is not None and "unknown" in err


# --- buffers-map reducer ---------------------------------------------------


def test_merge_buffers_right_wins_and_adds():
    left = {"a": NamedBuffer(name="a", cvl="A")}
    right = {"a": NamedBuffer(name="a", cvl="A2"), "b": NamedBuffer(name="b", cvl="B")}
    out = merge_buffers(left, right)
    assert out["a"].cvl == "A2" and out["b"].cvl == "B"


def test_merge_buffers_none_deletes():
    left = {"a": NamedBuffer(name="a", cvl="A"), "b": NamedBuffer(name="b", cvl="B")}
    out = merge_buffers(left, {"b": None})
    assert set(out) == {"a"}


def test_merge_buffers_does_not_mutate_left():
    left = {"a": NamedBuffer(name="a", cvl="A")}
    merge_buffers(left, {"a": None, "b": NamedBuffer(name="b", cvl="B")})
    assert set(left) == {"a"}


# --- disjoint rules --------------------------------------------------------


def test_disjoint_rules_ok():
    assert validate_disjoint_rules(_buffers()) is None


def test_disjoint_rules_detects_shared_rule_name():
    b = _buffers()
    b["hard"] = NamedBuffer(name="hard", cvl=HARD, property_rules={"P-hard": ["r_easy"]}, imports=("shared",))
    err = validate_disjoint_rules(b)
    assert err is not None and "r_easy" in err
