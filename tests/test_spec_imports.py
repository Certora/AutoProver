"""Unit tests for the CVL import extractor.

``imports_in_cvl`` feeds prover-cache invalidation (a spec's imports are hashed into the cache key),
so *missing* a real import is a soundness bug — a changed import
would not invalidate the cache. These cases pin the char-level scan against
the line-based regex it replaced, which missed a second import on the same line and any import
preceded by an inline block comment.
"""

import pytest

from certora_autosetup.parsers.spec_imports import imports_in_cvl, parse_imports_from_spec


@pytest.mark.parametrize("text, expected", [
    # --- the cases the old line-regex got WRONG (soundness-critical) ---
    ('import "a.spec"; import "b.spec";\nrule r {}', ["a.spec", "b.spec"]),   # two on one line
    ('/* header */ import "a.spec";\nrule r {}', ["a.spec"]),                 # inline block comment before
    # --- comments must NOT yield phantom imports ---
    ('import "a.spec";\n// import "x.spec";\nrule r {}', ["a.spec"]),          # // commented out
    ('import "a.spec";\n/* import "x.spec"; */\nrule r {}', ["a.spec"]),       # /* */ commented out
    ('/* a\n import "x.spec";\n b */\nimport "real.spec";', ["real.spec"]),    # multi-line block comment
    # --- strings must NOT yield phantom imports, nor swallow real ones ---
    ('rule r { string s = "import \\"x.spec\\""; }', []),                      # import text inside a string
    ('methods { function _.u() external => f("a // b"); }\nimport "a.spec";', ["a.spec"]),  # // in a string
    # --- keyword boundary: importFoo is an identifier, not an import ---
    ('methods { function importFoo() external; }\nimport "a.spec";', ["a.spec"]),
    # --- resource (path'd) and sibling (bare) imports both surface ---
    ('import "shared.spec";\nimport "summaries/o.spec";\nrule r {}', ["shared.spec", "summaries/o.spec"]),
    ('rule r { assert true; }', []),                                          # none
])
def test_imports_in_cvl(text, expected):
    assert imports_in_cvl(text) == expected


def test_parse_imports_from_spec_recursive(tmp_path):
    # a -> b -> c (transitive); a also imports a missing file (skipped with a warning, not crash)
    (tmp_path / "c.spec").write_text("rule rc { assert true; }\n")
    (tmp_path / "b.spec").write_text('import "c.spec";\nrule rb { assert true; }\n')
    (tmp_path / "a.spec").write_text('import "b.spec"; import "gone.spec";\nrule ra { assert true; }\n')

    recursive = {p.name for p in parse_imports_from_spec(tmp_path / "a.spec", recursive=True)}
    assert recursive == {"b.spec", "c.spec"}  # transitive closure; unresolvable "gone.spec" dropped

    direct = {p.name for p in parse_imports_from_spec(tmp_path / "a.spec", recursive=False)}
    assert direct == {"b.spec"}  # only the resolvable direct import
