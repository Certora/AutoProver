# Repository conventions for coding agents

Guidance for AI coding agents (and humans) working in this repo. Keep changes consistent
with these unless a maintainer says otherwise.

## Pre-validating a commit

The routine pass, both parts, ~20s total:

```bash
uv run --no-sync pytest tests/ -m "not expensive" -q   # ~10s
uv run --no-sync pyright --pythonpath ./.venv/bin/python   # ~7s, must report 0 errors
```

Expect `N passed, M deselected`. `pyrightconfig.json` names the packages it checks (`tests/`
is deliberately not among them, so test stubs may be loose) and sets no `venv` or `pythonPath`.
Pyright then type-checks with whichever `python` is first on `PATH`. `--pythonpath` names this
worktree's interpreter. A foreign interpreter produces a large error count spread across files
the change never touched.

Both flags matter:

- **`-m "not expensive"` — do not omit this.** The `expensive` marker is *registered* in
  `pyproject.toml` but **not** deselected by `addopts`, so a bare `pytest tests/` runs the
  expensive tests. They submit jobs to the live Certora cloud prover and take minutes each.
  Never run the suite without this filter unless a maintainer asked for an expensive run
  specifically. The dollar cost of a run is the LLM calls. Prover jobs are cheap by comparison,
  so a tape that fakes the LLM and still builds and submits is the CVLR end-to-end shape to
  keep. An EVM tape that dies with `HarnessFakeLLM: tape lane … exhausted` and
  `prover validation not satisfied` can be a compiler failure. Generated confs name the
  compiler as bare `solc`, and `test_scenarios/autoprove_counter` is `pragma solidity ^0.8.29`,
  so the `solc` on `PATH` has to be 0.8.29. A `solc8.29` binary is a different name. The solc
  error is in `.certora_internal/autoProve/*.log`. A CVL phase that finishes in a few seconds
  never reached the cloud. Re-record a tape only after that log shows a real pipeline failure.
- **`--no-sync`.** A bare `uv run` re-syncs first, and a default sync *prunes* this venv:
  `pytest`/`testcontainers` (group `test`), `pyright` (group `ci`), `sentence-transformers`
  (group `ragbuild`) and `certora_cli`/`torch` (extras) all live outside uv's default groups, so
  syncing uninstalls them and the next run dies at collection with `ModuleNotFoundError: No
  module named 'certora_cli'`. To (re)build the env deliberately, sync everything at once —
  every non-default group the two commands above need, `ci` (pyright) included, or the sync
  that fixes one of them breaks the other:
  `uv sync --group test --group ci --group ragbuild --extra cpu --extra certora-cli`.
- **The Rust toolchain.** That sync's default `dev` group builds the maturin crates under
  `rust/`, so it needs cargo. If the toolchain `rust-toolchain.toml` pins is not installed
  yet, install it first and on its own — `rustup toolchain install --no-self-update`, no
  argument, so rustup reads the channel, profile and components from that file. uv builds
  those crates concurrently and rustup's on-demand install is not concurrency-safe, so
  letting the build trigger it races.
- **This worktree's venv.** `uv run --no-sync` does not create a missing `.venv`. If this
  checkout was never synced, `pytest` and `pyright` can resolve from another worktree that is
  earlier on `PATH`. That pytest can go green while pyright reports errors in untouched files,
  because the other venv's `graphcore` loses to this tree's submodule. Check `which pytest`
  and that `.venv/lib` is populated before trusting either result. Each worktree has its own
  `.venv`; a sync in one does nothing for another.
- **Stale Rust wheels.** `tests/test_rustapp.py` drives the installed wheel, not whatever
  `cargo test` just built. A `ValidationError: Field required`, or `invalid … JSON: missing
  field`, after a change to `composer/rustapp/wire.py` or `rust/autoprover-sdk`, means the
  wheel in this worktree's `.venv` is older than the sources. `uv run` rebuilds a crate whose
  `.rs` files changed (the crates declare uv cache keys). A rebase that moves the wheel still
  needs the full sync above, in the worktree whose tests you are about to run.

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

## Prompts the model reads

Agent instructions live in `composer/templates/*.j2` and in the strings a tool validator
returns when it rejects a call. A protocol change has to update both. Grep the **tool name**.
The rejection text often names the tool and shares no token with a flag you just renamed, so
a rename sweep edits the condition and leaves the sentence under it asking for the old step.

A tool docstring says what the call does, what the arguments are, and what the call itself
refuses. Guidance about when the tool is the right one goes in the system prompt, next to the
facts that decision depends on. The model reads the docstring after it has already chosen the
tool. Eligibility the tool enforces goes in its refusal message.

## Comments, commits, and pull requests

Code comments describe the code as it is, in plain English. Keep a comment that records a
constraint or a choice the code cannot show. Drop a comment that only restates a name, a
field, or a return type. Leave out change history and which pull request a line came from.
A comment must not name a module, type, or document that is not in this tree.

Commit subjects are a short area, then a fact: `cargo: leave the toolchain registration to the wheel that needs it`.

A pull request description says why the change is needed, how the parts fit together, and
where to start reading. Reviewers look at the diff as a whole, so the description does not
walk commits. Line-by-line detail stays in the code. When asked to leave a change
uncommitted, leave it uncommitted.

Local `master` can lag `origin/master`. List a pull request's files with
`origin/master...HEAD`.

A test should be able to fail because a real decision or an external fact was wrong. A test
that only restates a function's return value, or copies a constant the product code also
copies, does not catch a change.

## This repository is public

Certora/AutoProver is public. A document added here may say only what the client or Certora
has already published, and it should cite that public source. Unpublished repository names,
branches, rule counts, and spec details stay out of the tree. The same rule is stated for the
Solana branches in [docs/cvlr-landing-plan.md](docs/cvlr-landing-plan.md).
