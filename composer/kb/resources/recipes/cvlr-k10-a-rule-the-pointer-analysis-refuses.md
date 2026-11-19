> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K10. A rule the pointer analysis refuses `[MOCK]` `[SKIP]`

**Trigger:** a rule ends in an error rather than a verdict, with `[3308]` illegal dereference of an
absolute address (most often a program's own `#[error_code]` enum formatting its `#[msg]` string) or
`[3006]` illegal store of a stack pointer, on a path the rule reaches.

Work down these in order:

- **`summarize_for_prover(symbol_pattern, why, returns)`**, which tells the prover to replace that
  symbol with an unconstrained stand-in instead of analyzing it, at every call, including when it
  is one of your program's own functions. It invalidates the prover stamp. A directive matches
  *demangled symbols the build defines*, and `verify_rules` reports which of your directives
  matched **no symbol at all** — read that, it is the common failure. A small function of your
  program often has no symbol: the compiler inlined it into its callers, and no spelling of it
  exists to match. Aim at the nearest function out that still has one, often the library function
  one level out; the report names candidates. `returns` types what the function returns, `r0:num` for a scalar or a `Result`
  returned in the register.

  **Give up on this remedy after about three directives down one path.** If three directives that
  each matched a symbol have not moved the error, the code the prover objects to is most likely in
  a function the compiler inlined *into the handler itself*, and no summary short of the handler
  reaches it. Weaken or skip;
- **weaken the rule to a sub-property the prover can reach** — assume away the failing path if the
  program genuinely excludes it, or assert something strictly weaker, and say so;
- **skip the property** with the error code and what you tried.

A summary is the sharp tool here. The prover stops reasoning about what you summarize: the call
returns an unconstrained value and **writes nothing** beyond the locations its `returns` names —
every other store the real function makes, to its arguments, to account data, to anything
reachable, is dropped. So summarizing the code a
property is *about* can give you a rule that passes having checked nothing, with no trace in the
harness: state the function should have changed keeps its old value, and an assertion that it is
unchanged, or still within bounds, holds trivially. Summarize the formatting path, never the
handler, never a function that writes state you assert over, and never one whose result you assert
over.

**Never summarize a function that returns an enum, a `Result` or an `Option`.** The type lines can
say only that a word is a number, never which number, so the discriminant the stand-in returns is
any value — and a `Result` often keeps its `Ok` in a spare value of the error's own tag. Measured on
an Anchor program: a summary of its `From<ErrorEnum> for anchor_lang::error::Error` cleared the
[3308], and an overflow then came back from the handler as `Ok` with the balance unchanged. Aim one
call further in, at the function that returns the message as a plain `String`: `to_string`
(`^<T as alloc::string::SpecToString>::spec_to_string$`) with `returns` naming its three words,
`(*i64)(r1+0):ptr_heap`, `(*i64)(r1+8):num` and `(*i64)(r1+16):num`, cleared the same [3308] with
every rule verified. That symbol is the standard library's, not that program's, but your build may
not have it: the report after the next run says whether it matched.
