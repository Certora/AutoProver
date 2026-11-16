> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K9. A halted job, and which rule halted it `[RULE]` `[EDIT]` `[SKIP]`

**Trigger:** a job that reaches its time limit, with no verdicts or with rules cut off as TIMEOUT; a
rule that still halts the job when submitted alone.

Find the rule first: the next submission is the same draft without the rule you added last, or that
rule alone. Leave `loop_iter` where it is, and add no summary, until that submission says which rule
it was. Resubmitting the halted set unchanged will halt again. Measured: four submissions of
handler-level rules, at `loop_iter` 2, 3 and 4 and after two rounds of summaries, each halted with
no verdicts; the same draft with those rules removed verified the rest in minutes. One extra rule in
a draft that otherwise verified did the same.

When the rule by itself still halts, look for a loop whose trip count comes from data rather than
from `loop_iter`. Raising the bound does not finish it: the iteration the prover reports rises with
the bound. Measured cases:
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

Once the job returns verdicts, a rule that still comes back TIMEOUT is recipe K8's case.
