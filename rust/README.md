# AutoProver Rust framework

Build AutoProver formalization backends / applications in Rust and run them
through the generic Python pipeline via PyO3. Design rationale:
[docs/rust-formalization-backends.md](../docs/rust-formalization-backends.md) and
[docs/rust-applications.md](../docs/rust-applications.md).

## Layout

| Crate | Role |
| --- | --- |
| [`autoprover-sdk`](autoprover-sdk) | The library a Rust application imports: the ABI (serde types), the `Application` / `FormalizeSession` traits, the FFI helpers, and the `export_app!` macro. |
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
synthesizes the pipeline's phase enum from the descriptor, and drives the Rust
decider through the inversion-of-control loop (Python owns every async effect —
LLM, prover, cache, event streaming — Rust only decides the next one). No
`pyo3-async` bridge is involved.

## The FFI surface

A wheel exports exactly (all synchronous, JSON strings across the boundary):

```text
descriptor() -> str                          # the AppDescriptor
validate_preconditions(args_json) -> str|None
new_session(input_json) -> RustSession       # .resume(observation_json) -> command_json
fetch_verdicts(input_json) -> str
finalize(outcomes_json) -> str|None
```

`export_app!` generates all of these.

## Writing a new application

1. New crate: `cdylib`, depending on `autoprover-sdk` and `pyo3`
   (`features = ["extension-module", "abi3-py312"]`). See
   [example-app/Cargo.toml](example-app/Cargo.toml).

2. Implement `Application` (descriptor + `new_session` + `fetch_verdicts`) and a
   `FormalizeSession` — a **pure synchronous decider** whose `resume(Observation)`
   returns the next `Command` (`CallLlm` / `RunProver` / `CacheGet` / `Emit` / …
   / `Publish` / `GiveUp`). See [example-app/src/lib.rs](example-app/src/lib.rs).

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

* `FormalizeSession` is `Send + Sync` because PyO3 wraps it in a `#[pyclass]`; a
  state machine over plain owned data satisfies this without effort.
* Keep effects coarse-grained — one `resume` per turn / tool-call, never per
  token.
* A self-contained (Tier-1) backend that does verification inside Rust simply
  never emits `RunProver`/`RunFeedback`; a run-service-backed one surfaces those
  as effects and the deployment supplies the `prover=` / `feedback=` hooks to
  `RustFormalizer` (see `composer/rustapp/adapter.py`).
