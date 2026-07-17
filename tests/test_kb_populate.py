"""
Static validation of the KB seed data in composer/scripts/kb_populate.py.

The module initializes the indexed store at import time (requires Postgres),
so instead of importing it we extract the CVL_HELP_MESSAGES literal via AST
parsing and validate its shape: every entry has non-empty title/symptom/body
strings, and titles are unique (they are the store keys).
"""
import ast
from pathlib import Path

KB_POPULATE_PATH = (
    Path(__file__).parent.parent / "composer" / "scripts" / "kb_populate.py"
)

REQUIRED_KEYS = {"title", "symptom", "body"}


def _load_literal(name: str):
    tree = ast.parse(KB_POPULATE_PATH.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == name
        ):
            assert node.value is not None, f"{name} has no value"
            # the seed data is pure literals (dicts of implicitly-concatenated
            # string constants), so literal_eval reconstructs it exactly
            return ast.literal_eval(node.value)
    raise AssertionError(f"{name} assignment not found in kb_populate.py")


def _load_messages() -> list[dict[str, str]]:
    return _load_literal("CVL_HELP_MESSAGES")


def test_entries_are_well_formed():
    messages = _load_messages()
    assert len(messages) > 0
    for entry in messages:
        assert set(entry.keys()) == REQUIRED_KEYS, (
            f"entry {entry.get('title', '<untitled>')!r} has keys {set(entry.keys())}"
        )
        for key in REQUIRED_KEYS:
            value = entry[key]
            assert isinstance(value, str) and value.strip(), (
                f"entry {entry.get('title', '<untitled>')!r} has empty {key!r}"
            )


def test_titles_are_unique():
    # titles are the store keys — a duplicate would silently shadow an article
    titles = [entry["title"] for entry in _load_messages()]
    assert len(titles) == len(set(titles)), (
        f"duplicate titles: {sorted(t for t in titles if titles.count(t) > 1)}"
    )


# Bodies cross-reference other articles with the fixed phrase
# `article titled "<exact title>"` so a following agent can KBGet by exact
# key. The graph is declared as data (KB_CROSS_REFERENCES in kb_populate.py)
# rather than recovered from the prose, so the test only checks declared
# facts: every declared reference names an existing article and its citing
# phrase appears verbatim in the declaring body. A cross-reference added to a
# body without a matching map entry is not detected — keep the map in sync.
def test_cross_references_resolve():
    messages = _load_messages()
    cross_refs: dict[str, list[str]] = _load_literal("KB_CROSS_REFERENCES")
    bodies = {entry["title"]: entry["body"] for entry in messages}
    titles = set(bodies)
    for title in titles:
        assert '"' not in title, (
            f"title {title!r} contains a double quote, breaking the "
            f"cross-reference convention"
        )
    for source, referenced_titles in cross_refs.items():
        assert source in titles, (
            f"KB_CROSS_REFERENCES declares unknown source article {source!r}"
        )
        assert referenced_titles, (
            f"KB_CROSS_REFERENCES entry for {source!r} is empty — drop it"
        )
        for referenced in referenced_titles:
            assert referenced in titles, (
                f"article {source!r} declares a reference to unknown article "
                f"{referenced!r}"
            )
            assert f'article titled "{referenced}"' in bodies[source], (
                f"article {source!r} declares a reference to {referenced!r} "
                f"but its body lacks the citing phrase"
            )
