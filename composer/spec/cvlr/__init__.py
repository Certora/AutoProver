"""The CVLR backend — properties formalized as Rust rules and checked by the Certora Solana Prover.

``docs/cvlr-backend-plan.md`` is the plan; §4.5 puts the code here, mirroring
:mod:`composer.spec.source` (the CVL backend) file-for-file where the domain allows, because the
goal is to reuse that authoring machinery rather than to reimplement it.

**One backend, two chains.** Solana ships first and Soroban follows by supplying only what
demonstrably differs — a build recipe, a conf shape, a prover CLI, some templates and two strings
(§4.2). There is deliberately no per-chain bundle: each concern is shared by whatever mechanism
already fits it, so nothing here is written against a guess about how Soroban will differ.

Everything in this package is chain-neutral unless its name says otherwise. The Solana build itself
is not here at all — it is Cargo knowledge, shared with the Rust wheels through
:mod:`composer.cargo`, since reading a manifest and building a program is the same work whoever
wants the result (§4.3).
"""
