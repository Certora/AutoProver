"""Cargo — the build system of every Rust chain we target.

Chain knowledge, not backend knowledge. Reading a workspace, warming a dependency graph and
compiling are the same work whoever asks — a Python backend submitting a program to the Prover, a
Rust wheel loading one into LiteSVM — so the answer lives here rather than inside either product.

The split inside is by *what varies*:

* :mod:`composer.cargo.metadata` — reading a workspace. Chain-neutral: ``cargo metadata`` says the
  same thing whatever the target is.
* :mod:`composer.cargo.session` — a warm workdir plus the host-target ``cargo check``. Chain-neutral
  for the same reason. The workdir is an object rather than a parameter because with a compile in
  the authoring inner loop, the private ``CARGO_HOME`` a sandboxed build needs must be warmed once
  per session, not once per compile.
* :mod:`composer.cargo.sbf` — Solana's verification build (``cargo certora-sbf``). The one
  chain-specific piece; Soroban's wasm build is its peer, not its subclass.
* :mod:`composer.cargo.depinfo` — which sources a build actually compiled. Chain-neutral: the
  dep-info it reads is rustc's, and rustc writes it whatever the target.
* :mod:`composer.cargo.symbols` — the functions a built program defines, spelled the way the
  Solana Prover's tuning files spell them. Chain-specific, like the build that produced them.
"""
