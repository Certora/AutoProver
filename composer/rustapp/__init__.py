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

* :func:`composer.rustapp.host.run_rust_pipeline` — build the backend from a
  module name and run the shared driver.
* :func:`composer.rustapp.host.build_application` — synthesize the phase enum,
  labels, section order and backend factory for a frontend / ``main()``.
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
from composer.rustapp.adapter import RustBackend, RustFormalizer, RustPreparedSystem
from composer.rustapp.store import RustArtifactStore
from composer.rustapp.host import (
    RustApplication,
    build_application,
    build_backend,
    build_core_phases,
    build_phase_enum,
    load_descriptor,
    load_module,
    run_rust_pipeline,
)

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
    "RustArtifactStore",
    "RustApplication",
    "build_application",
    "build_backend",
    "build_core_phases",
    "build_phase_enum",
    "load_descriptor",
    "load_module",
    "run_rust_pipeline",
]
