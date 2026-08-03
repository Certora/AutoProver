"""Guards against `Field("some prose")` in pydantic schemas.

`Field`'s only positional parameter is ``default``, so a bare string there does not
document the field — it makes the field *optional* and gives it that prose as its
value. For the schemas we hand to an LLM that is doubly wrong: the model is never
told what the field means, and when it omits the field we silently get the sentence
back as data (a "path" that is a sentence, an invariant "name" that is a sentence).
Nothing type-checks it, so the bad value only surfaces far downstream.

A genuine string default is spelled `Field(default="...")`, so a string literal in
the positional slot is always the description/default confusion. `Field(...)`
(Ellipsis, i.e. explicitly required) is fine.
"""
import ast
from pathlib import Path

PACKAGE_ROOT = Path(__file__).resolve().parent.parent / "composer"


def _positional_string_fields(tree: ast.AST) -> list[tuple[int, str]]:
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call) or not node.args:
            continue
        func = node.func
        # matches both `Field(...)` and `pydantic.Field(...)`
        name = func.id if isinstance(func, ast.Name) else getattr(func, "attr", None)
        if name != "Field":
            continue
        first = node.args[0]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            found.append((node.lineno, first.value))
    return found


def test_no_prose_in_field_default_slot() -> None:
    offenders = []
    for path in sorted(PACKAGE_ROOT.rglob("*.py")):
        tree = ast.parse(path.read_text(), filename=str(path))
        for lineno, value in _positional_string_fields(tree):
            rel = path.relative_to(PACKAGE_ROOT.parent)
            offenders.append(f"{rel}:{lineno}: Field({value!r}) — did you mean description=?")
    assert not offenders, "\n".join(offenders)
