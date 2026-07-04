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


def _load_messages() -> list[dict[str, str]]:
    tree = ast.parse(KB_POPULATE_PATH.read_text())
    for node in tree.body:
        if (
            isinstance(node, ast.AnnAssign)
            and isinstance(node.target, ast.Name)
            and node.target.id == "CVL_HELP_MESSAGES"
        ):
            assert node.value is not None, "CVL_HELP_MESSAGES has no value"
            # entries are pure literals (dicts of implicitly-concatenated
            # string constants), so literal_eval reconstructs them exactly
            return ast.literal_eval(node.value)
    raise AssertionError("CVL_HELP_MESSAGES assignment not found in kb_populate.py")


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
# key. The marker + closing quote delimit the title verbatim (titles contain
# no double quotes, enforced below), so plain splitting recovers it exactly.
CROSS_REF_MARKER = 'article titled "'


def test_cross_references_resolve():
    messages = _load_messages()
    titles = {entry["title"] for entry in messages}
    for title in titles:
        assert '"' not in title, (
            f"title {title!r} contains a double quote, breaking the "
            f"cross-reference convention"
        )
    for entry in messages:
        for chunk in entry["body"].split(CROSS_REF_MARKER)[1:]:
            referenced = chunk.split('"', 1)[0]
            assert referenced in titles, (
                f"article {entry['title']!r} references unknown article "
                f"{referenced!r}"
            )
