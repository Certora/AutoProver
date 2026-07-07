# AutoProver Rust framework

Build AutoProver formalization backends / applications in Rust and run them
through the generic Python pipeline via PyO3. Design rationale:
[docs/rust-formalization-backends.md](../docs/rust-formalization-backends.md) and
[docs/rust-applications.md](../docs/rust-applications.md).

## Layout

| Crate | Role |
|---|---|
| [`autoprover-sdk`](autoprover-sdk) | The library a Rust application imports: the ABI (serde types), the `Application` / `FormalizeSession` traits, the FFI helpers, and the `export_app!` macro. |
| [`example-app`](example-app) | The `echoprover` demo — a complete, self-contained application built into a wheel and exercised by `tests/test_rustapp.py`. |

The Python side is [`composer/rustapp`](../composer/rustapp): it loads a wheel,
synthesizes the pipeline's phase enum from the descriptor, and drives the Rust
decider through the inversion-of-control loop (Python owns every async effect —
LLM, prover, cache, event streaming — Rust only decides the next one). No
`pyo3-async` bridge is involved.

## The FFI surface

A wheel exports exactly (all synchronous, JSON strings across the boundary):

```
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

4. Add a maturin `pyproject.toml` (`module-name = "my_app"`), then build:

   ```sh
   cd my-app && maturin develop      # or: maturin build --out dist
   ```

5. Run it from Python:

   ```python
   from composer.rustapp import run_rust_pipeline, build_application
   result = await run_rust_pipeline("my_app", source_input, ctx, handler_factory, env)
   ```

## Building & testing the demo

```sh
cd rust/example-app && maturin develop         # builds & installs the `echoprover` wheel
cd ../.. && python -m pytest tests/test_rustapp.py
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
