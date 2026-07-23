# Repository conventions for coding agents

Guidance for AI coding agents (and humans) working in this repo. Keep changes consistent
with these unless a maintainer says otherwise.

## Python

### Do NOT use `from __future__ import annotations`

Never add `from __future__ import annotations` to a module. If you touch a file that has it
and it's reasonable to do so, remove it.

Why:
- It's a dead-end feature. PEP 563 (the string-annotations behaviour this import enables) was
  never made the default and has effectively been superseded by PEP 649 / PEP 749 (lazy
  evaluation) from Python 3.14 on. Relying on the `__future__` behaviour is betting on a path
  the language is moving away from — it will change/break under you in future versions.
- It doesn't actually solve the problem it's reached for. Stringizing *all* annotations breaks
  anything that introspects them at runtime — pydantic, dataclasses, `typing.get_type_hints`,
  and our own annotation-driven graph wiring (see `composer/rustapp/_llm_agent.py`, which had
  to stay eager precisely because stringized `NotRequired[T]` broke pydantic unwrapping). It
  trades one set of problems for a subtler set.

What to do instead (we target Python 3.12+):
- Modern syntax works at runtime without the future import: `X | None`, `list[str]`,
  `dict[str, int]`, `tuple[int, ...]`, PEP 695 generics (`class Foo[T]:` / `def f[T]()`).
- For a genuine forward reference (a name not yet defined where the annotation is evaluated —
  e.g. a dataclass field typed as a class defined later in the file), quote just that one
  annotation: `backend: "RustBackend"`. Quote the specific ref; don't stringize the whole module.
