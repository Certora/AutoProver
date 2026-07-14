"""Host for Rust-based AutoProver applications (PyO3).

This package is the Python side of the seam described in
``docs/rust-backend-api.md``. A Rust application is a wheel built with
``autoprover-sdk`` (see ``rust/``) exposing a small, synchronous, JSON FFI surface
— a **passive service** the pipeline drives:

    descriptor() -> str                         # the AppDescriptor (declarative spine)
    validate_preconditions(args_json) -> str|None
    units(input_json) -> str                    # the report rows / fuzz targets
    author_prompt(input_json, failure_json|None) -> str
    judge_prompt(input_json, spec) -> str|None
    compile(input_json, spec, workdir, sandbox_json) -> str      # BLOCKING (run-confined)
    validate(input_json, spec, unit, workdir, sandbox_json) -> str  # BLOCKING (run-confined)
    finalize(outcomes_json) -> str|None

The host loads that module, synthesizes the pipeline's phase enum from the
descriptor, and wraps the module in a :class:`PipelineBackend` whose ``formalize``
runs the author→compile→judge→validate loop (:mod:`composer.rustapp.adapter`) —
Python owns the loop and every LLM turn; the two blocking callouts run the toolchain
via ``run-confined``. No IoC ``resume`` protocol and no ``pyo3-async`` bridge.

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
    DeliverableMode,
    EventKind,
    PhaseSpec,
    SetupSpec,
)
from composer.rustapp.result import RustArtifact, RustFormalResult
from composer.rustapp.adapter import (
    RustBackend,
    RustFormalizer,
    RustPreparedSystem,
    as_report_backend,
    author_and_compile,
    make_emitter,
    unique_slugs,
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
    "DeliverableMode",
    "EventKind",
    "PhaseSpec",
    "SetupSpec",
    "RustArtifact",
    "RustFormalResult",
    "RustBackend",
    "RustFormalizer",
    "RustPreparedSystem",
    "as_report_backend",
    "author_and_compile",
    "make_emitter",
    "unique_slugs",
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
