"""Host for Rust-based AutoProver applications (PyO3).

This package is the Python side of the seam described in
``docs/rust-applications.md`` and ``docs/rust-formalization-backends.md``. A Rust
application is a wheel built with ``autoprover-sdk`` (see ``rust/``) exposing a
small, synchronous, JSON FFI surface:

    descriptor() -> str                         # the AppDescriptor (declarative spine)
    validate_preconditions(args_json) -> str|None
    new_session(input_json) -> RustSession      # .resume(observation_json) -> command_json
    fetch_verdicts(input_json) -> str
    finalize(outcomes_json) -> str|None

The host loads that module, synthesizes the pipeline's phase enum from the
descriptor, and wraps the module in a :class:`PipelineBackend` whose
``formalize`` drives the Rust decider through the inversion-of-control loop in
:mod:`composer.rustapp.loop` — Python performs every async effect (LLM, prover,
cache, event streaming); Rust only decides the next one. No ``pyo3-async``
bridge is involved.

Entry points:

* :func:`composer.rustapp.cli.tui_main` / ``console_main`` — a complete runnable
  application from a module name (the descriptor drives argparse, the entry point,
  the frontend, and ``main()``). This is the whole vertical.
* :func:`composer.rustapp.host.build_application` — synthesize the phase enum,
  labels, section order and backend factory for a frontend / ``main()``.
* :func:`composer.rustapp.entry.rust_entry_point` — the async entry point context
  manager (services + ``WorkflowContext``), yielding the Executor.
* :func:`composer.rustapp.host.run_rust_pipeline` — headless: build the backend
  from a module name and run the shared driver directly.
"""

from composer.rustapp.descriptor import (
    AppDescriptor,
    ArgDefault,
    ArgSpec,
    ArtifactLayout,
    CoreSlot,
    EventKind,
    PhaseSpec,
)
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.rustapp.loop import Effects, GaveUp, drive_session
from composer.rustapp.adapter import (
    RustBackend,
    RustFormalizer,
    RustPreparedSystem,
    as_report_backend,
)
from composer.rustapp.store import RustArtifactStore
from composer.rustapp.host import (
    BackendOptions,
    RustApplication,
    StoreFactory,
    build_application,
    build_backend,
    build_core_phases,
    build_phase_enum,
    load_descriptor,
    load_module,
    resolve_ecosystem,
    run_application,
    run_rust_pipeline,
)
from composer.rustapp.entry import (
    EnvBuilder,
    RustRunner,
    build_arg_parser,
    build_neutral_env,
    rust_entry_point,
)
from composer.rustapp.frontend import (
    GenericRustApp,
    GenericRustConsoleHandler,
    GenericRustTaskHandler,
)

# NOTE: composer.rustapp.cli is intentionally NOT imported here — it runs
# `import composer.bind` (import-time DI / test-tape bootstrap), which the
# built-in apps only trigger from their `main` modules. Import it explicitly:
#     from composer.rustapp.cli import tui_main, console_main

__all__ = [
    "AppDescriptor",
    "ArgDefault",
    "ArgSpec",
    "ArtifactLayout",
    "CoreSlot",
    "EventKind",
    "PhaseSpec",
    "RustArtifact",
    "RustFormalResult",
    "Effects",
    "GaveUp",
    "drive_session",
    "RustBackend",
    "RustFormalizer",
    "RustPreparedSystem",
    "as_report_backend",
    "RustArtifactStore",
    "RustApplication",
    "BackendOptions",
    "StoreFactory",
    "build_application",
    "build_backend",
    "build_core_phases",
    "build_phase_enum",
    "load_descriptor",
    "load_module",
    "resolve_ecosystem",
    "run_application",
    "run_rust_pipeline",
    "EnvBuilder",
    "RustRunner",
    "build_arg_parser",
    "build_neutral_env",
    "rust_entry_point",
    "GenericRustApp",
    "GenericRustConsoleHandler",
    "GenericRustTaskHandler",
]
