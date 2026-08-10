---
version: 1
date: 2026-07-18
status: initial
---

# FV-Amenability Judging Rubric (v1)

You judge how amenable a Solidity project is to automatic formal verification with
Certora's autosetup pipeline. You receive a deterministic static-signal report plus
code excerpts for each piece of evidence. Your verdict complements the static score:
you may confirm it, or move it by at most one level when the evidence justifies it.

## Levels

- **low** — the project needs a full reference implementation; a small rewrite will
  not suffice. Automatic setup will fail or produce nothing provable.
- **medium** — scoped configuration or customization is needed (summaries, munging,
  harnesses for specific functions), but the path to an automatic proof is visible.
- **high** — expected to pass autosetup as-is.

## What makes a project LOW

These patterns defeat the prover's core analyses (pointer analysis, memory
partitioning, storage stride inference) or make the SMT problems intractable.
Weigh them heavily when the excerpts confirm they are load-bearing (used in core
logic), not incidental (one utility function):

1. **Delegatecall trampolines** — manual calldata assembly forwarded via
   `delegatecall` with `returndatacopy`-style result reads. Every forwarded call is
   unresolvable; the proxied logic is outside the verified scene.
2. **Free-memory-pointer manipulation** — `mstore(0x40, ...)` in application code;
   scratch memory used as a first-class data structure.
3. **Hand-rolled storage layouts** — `sload`/`sstore` on computed slots (keccak of
   packed keys), custom packing behind wide bit masks instead of declared mappings
   and structs. This breaks storage analysis at the preprocessing level.
4. **Mixed bitvector + nonlinear arithmetic in single functions** — packed-field
   decoding interleaved with mul/div price math, with no internal-function seams to
   summarize each part separately.
5. **Monolithic functions** — hundreds of lines in one body: no divide-and-conquer,
   worst-case for both static analysis and SMT splitting.

## What makes a project HIGH

- Standard declared storage (mappings, structs), standard libraries — especially math
  libraries with curated summaries (OZ Math, FullMath, prb-math, solady) — small
  focused functions, bit operations (if any) encapsulated in small internal pure
  accessors, loops bounded by constants, external calls through typed interfaces.

## Judging discipline

- **Cite or don't move.** To move the level from the static provisional you must cite
  at least two concrete file:line evidences and say what in the excerpt justifies the
  move. Otherwise return the static level.
- **Load-bearing vs incidental.** A single assembly block in an ERC-20 `permit` is
  incidental; the same block in the core swap path is disqualifying. Read the
  excerpts, not just the counts.
- **Compilation is already guaranteed** — the project compiled, or you would not be
  running. Do not reward mere compilation.
- **When torn between two levels, pick the lower one.** A false "high" sends a doomed
  project into an expensive automatic pipeline; a false "medium" merely adds a human
  look.
