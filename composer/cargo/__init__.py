"""Cargo — the build system of every Rust chain we target.

Chain knowledge, not backend knowledge: what lives here is shared by the CVLR Python backend
(``composer.spec.cvlr``) and by the Rust wheels that reach it through
:data:`composer.rustapp.toolchain.PROJECT_TOOLCHAINS`. Both need to read a workspace, warm a
dependency graph, and compile — Crucible to load a program into LiteSVM, CVLR to submit one to
the Prover — and neither should own the answer.

The split inside is by *what varies*:

* :mod:`composer.cargo.metadata` — reading a workspace. Chain-neutral: ``cargo metadata`` says the
  same thing whatever the target is.
* :mod:`composer.cargo.session` — a warm workdir plus the host-target ``cargo check``. Chain-neutral
  for the same reason, and the reason the workdir is an object rather than a parameter is
  ``docs/cvlr-backend-plan.md`` §5.1: with a compile in the authoring inner loop, the private
  ``CARGO_HOME`` a sandboxed build needs must be warmed once per session, not once per compile.
* :mod:`composer.cargo.sbf` — Solana's verification build (``cargo certora-sbf``). The one
  chain-specific piece; Soroban's wasm build is its peer, not its subclass.
"""
