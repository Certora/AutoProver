# AutoProver Rust framework

Build AutoProver formalization backends / applications in Rust and run them
through the generic Python pipeline via PyO3. Design rationale and the full seam:
[docs/rust-applications.md](../docs/rust-applications.md).

## Layout

| Crate | Role |
| --- | --- |
| [`autoprover-sdk`](autoprover-sdk) | The library a Rust application imports: the ABI (serde types), the `Backend` trait, the `run_confined` launcher helper, the FFI helpers, and the `export_app!` macro. |
| [`example-app`](example-app) | The `echoprover` demo — a complete, self-contained application built into a wheel and exercised by `tests/test_rustapp.py`. |
| [`run-confined`](run-confined) | The command-sandbox launcher (Landlock + seccomp). Not an extension module: it builds as a maturin *bin* wheel, so `uv sync` lands the binary in `.venv/bin`. See [docs/command-sandbox.md](../docs/command-sandbox.md). |

## Building

Nothing here is built by hand. Every crate that ships is a `uv` path dependency in the
root `pyproject.toml`'s `apps` group (reachable from the default `dev` group), so:

```sh
uv sync          # builds echoprover + run-confined into the venv
uv run pytest tests/test_rustapp.py tests/test_sandbox_launcher.py
```

Each project declares `[tool.uv] cache-keys` over its `.rs` sources, so uv rebuilds a
crate on the next `uv run` after you edit it — there is no `maturin develop` step. The
toolchain is pinned in [rust-toolchain.toml](../rust-toolchain.toml) and rustup installs
it on demand. The container image sets `UV_NO_DEV=1` and so builds none of this (its
final stage has no cargo).

The Python side is [`composer/rustapp`](../composer/rustapp): it loads a wheel,
synthesizes the pipeline's phase enum from the descriptor, and drives the wheel —
a **passive service** — through the author→compile→judge→validate loop. Python owns
the loop, every LLM turn, and all async I/O; the wheel answers pure questions and
runs its own toolchain. No `pyo3-async` bridge is involved.

## The FFI surface

A wheel exports exactly (all synchronous, JSON strings across the boundary):

```text
descriptor() -> str                                        # the AppDescriptor
validate_preconditions(args_json) -> str|None
checks(input_json) -> str                                  # the checks this input formalizes
author_prompt(input_json) -> str                           # one authoring session's prompt
check_syntax(input_json, spec) -> str|None                 # None ⇒ the spec may be written
judge_prompt(input_json, spec) -> str|None                 # None ⇒ no judge
compile(input_json, spec, workdir, sandbox_json) -> str    # BLOCKING (run-confined)
validate(input_json, spec, target, workdir, sandbox_json) -> str  # BLOCKING (run-confined)
workspace_prep(input_json) -> str                          # a plan the host executes
sandbox_grants(args_json) -> str
finalize(outcomes_json) -> str|None
```

`export_app!` generates all of these. `compile`/`validate` release the GIL while their
child process runs, so the host calls them with `asyncio.to_thread`.

## Writing a new application

1. New crate: `cdylib`, depending on `autoprover-sdk` and `pyo3`
   (`features = ["extension-module", "abi3-py312"]`). See
   [example-app/Cargo.toml](example-app/Cargo.toml).

2. Implement `Backend`. Required: `descriptor` + `checks` + `author_prompt` +
   `compile` + `validate`. Defaulted: `validate_preconditions`, `check_syntax`,
   `judge_prompt`, `workspace_prep`, `sandbox_grants`, `finalize`. See
   [example-app/src/lib.rs](example-app/src/lib.rs).

3. Export the module (ident must match the wheel/module name):

   ```rust
   autoprover_sdk::export_app!(my_app, MyApp);
   ```

4. Add a maturin `pyproject.toml` (`module-name = "my_app"`) with a `[tool.uv]
   cache-keys` block over its `.rs` sources — copy
   [example-app/pyproject.toml](example-app/pyproject.toml). Then wire it into the root
   `pyproject.toml` (one line in the `apps` group, one in `[tool.uv.sources]`) and
   `uv sync`; there is no separate build command.

5. Ship a CLI. The generic entry point + frontend are synthesized from the
   descriptor — a runnable app is a two-line `main()`:

   ```python
   # my_app_cli.py
   from composer.rustapp.cli import tui_main, console_main
   def main() -> int:      return tui_main("my_app")       # Textual TUI
   def main_console() -> int: return console_main("my_app")   # stdout
   ```

   Register them in `pyproject.toml` under `[project.scripts]`. No bespoke
   argparse, entry point, frontend, or `main()` to write — the descriptor drives
   all of it (CLI flags, precondition validation, phase labels, event rendering,
   artifact layout).

   For programmatic / headless use, the pipeline wrapper is also exposed directly:

   ```python
   from composer.rustapp import run_rust_pipeline
   result = await run_rust_pipeline("my_app", source_input, ctx, handler_factory, env)
   ```

## Testing the demo

```sh
uv run pytest tests/test_rustapp.py    # `uv sync` already built the echoprover wheel
```

## Notes

* `Backend` is `Send + Sync + 'static` (one instance per wheel, built once in
  `export_app!`); holding no per-call state satisfies this without effort.
* `compile`/`validate` author their own command line but never their own
  confinement: they prepend the opaque `Sandbox::argv_prefix` Python hands them,
  via `autoprover_sdk::run_confined`. Only file *contents* may derive from the LLM.
* This shape fits a backend whose checker is a **local tool**. One whose "validate"
  is a remote service has no host effect to call — see
  [docs/rust-applications.md §12](../docs/rust-applications.md).
