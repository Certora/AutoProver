> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K10. A rule the pointer analysis refuses `[MOCK]` `[SKIP]`

**Trigger:** a rule ends in an error rather than a verdict, with `[3308]` illegal dereference of an
absolute address (most often a program's own `#[error_code]` enum formatting its `#[msg]` string) or
`[3006]` illegal store of a stack pointer, on a path the rule reaches.

Work down these in order:

- **`summarize_for_prover(symbol_pattern, why)`**, which tells the prover to replace that symbol
  with an unconstrained stand-in instead of analyzing it. A directive matches *demangled symbols
  the build defines*, so it invalidates the prover stamp and `verify_rules` reports which of your
  directives matched **no symbol at all** — read that, it is the common failure. Do not summarize
  your program's own small functions: the compiler inlines them into their callers, so no spelling
  of them exists to match. Aim at the library function one level out, which survives with a real
  symbol; the report names candidates.

  **Give up on this remedy after about three directives down one path.** Measured: one unit spent
  nine submissions on eleven directives walking a formatting chain frame by frame — seven of them
  matched real symbols — and the error never moved, because the code the prover objects to had
  been inlined *into the handler itself* where no summary can reach it. If three have not cleared
  it, it is not summarizable at this level; weaken or skip;
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
