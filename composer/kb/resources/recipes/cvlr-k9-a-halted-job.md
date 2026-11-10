> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K9. A halted job, and which rule halted it `[RULE]` `[EDIT]` `[SKIP]`

**Trigger:** a job that reaches its time limit, with no verdicts or with rules cut off as TIMEOUT; a
rule that still times out alone.

Find the rule first: the next submission is the same draft without the rule you added last, or that
rule alone. Leave `loop_iter` where it is, and add no summary, until that submission says which rule
it was. Resubmitting the halted set unchanged will halt again.

When the rule by itself still halts, look at what the handler does first.
- A `HashSet` insert goes through SipHash or `hashbrown`'s rehash. The iteration count tracks
  `loop_iter`, and `hash_one` is external, so `contains` can disagree with `insert`. Ask the
  editor to replace that set with a loop-free signer set.
- A bincode walk of a `[u8; 32]`, a `Pubkey` field of the account struct the handler serializes,
  did the same: iteration 2 at bound 2, iteration 4 at bound 4. Ask the editor to mock the state
  accessor with a value stand-in.
- A `Vec` whose bytes come from an allocator the prover treats as external, such as a vote
  account's credit history, is the same kind of loop.

Drive the program's own function below that handler, the accounting it wraps, and name that
function as the rule's subject. A copy of the handler written in the harness is a verdict about
your code. If the property is about the handler's own checks and there is no function below
them, `record_skip` and name the halted job. A summary of a function the compiler has inlined
into the handler matches no symbol, and the halt stays.

A rule that came back with a verdict, and stopped short of its own assertion, is a different
case. The other rules in the job already have results.
- A product of two `u64`s into a `u128` timed out twice with the factors unbounded, and verified
  below `2^32`. Bound both factors, and say in the commentary that the bound is the domain of
  the proof. A property about behaviour at the top of the `u64` range cannot take that bound.
- One rule ranging over several account-list lengths, or both pre-state variants, timed out.
  One rule per length or variant verified. Split it that way.
- `collect_signers` walking the account list the rule passed stops on the unwinding assertion
  until `loop_iter` equals that length. Raise the bound to the length the rule passed.
- A walk of one iteration per epoch, with nothing in the program bounding how far activation is
  from now, does not finish at a higher bound. Assume a small distance and record it as the
  domain.

Re-running an unchanged timing-out rule will time out again.
