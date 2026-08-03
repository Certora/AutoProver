# CVL Invariants and Quantifiers

> Where this document and the CVL manual or other Certora materials diverge,
> this document governs.

Owns all invariant and quantifier knowledge. The summarization knowledge base,
the baseline facts, and the recipes index defer to this document on these
topics; recipes-index entries marked `see` point at sections here.

## 1. The proof shape: induction

Proving invariant `I` checks, for every method `m` of every provided contract:
`require I; m(); assert I` — plus a base case in which the constructor runs
from an empty state and `I` is asserted after it.

**Strengthening the inductive hypothesis.** The induction step starts from an
*arbitrary* state satisfying `I`, not a reachable one. When `I` as stated is
not preserved — the step needs facts about the prestate that `I` does not
supply — strengthen the hypothesis. Two routes:

- *Strengthen the statement*: prove `I && Q` as one invariant. Every step now
  assumes both and must preserve both. Right when `Q` exists only to make `I`
  inductive and has no independent use.
- *Lemma invariants*: state `Q` as its own invariant and import it where
  needed with `requireInvariant` in `preserved` blocks (§2). Right when `Q`
  is independently meaningful, or shared across several invariants and rules.

The routes prove the same thing: importing `Q` into `I`'s preserved blocks
and `I` into `Q`'s is exactly proving `I && Q` inductive, so mutual imports
among proved invariants are sound. The recurring special case: an invariant
violated only from a start state the contract can never reach is not
inductive, and the strengthening conjoins the reachable-configuration facts —
often a disjunction of the legal states — that exclude it.

**`preserved` blocks.** A `preserved` block inserts *additional* prestate
requirements after the `require I`. The invariant itself is always assumed;
requiring `I` manually in its own preserved block is redundant. Forms:

```cvl
invariant inv(...) ... {
    preserved { ... }                                   // every method (induction step only)
    preserved transfer(address to, uint a) with (env e) { ... }  // per method
    preserved constructor() { ... }                     // the base case
    preserved onTransactionBoundary with (env e) { ... }
}
```

The generic block covers only the induction step; a failure on the base case
takes its assumptions from `preserved constructor()`.

Transient storage is cleared between transactions. For an invariant that
mentions transient state, that clearing is one more transition the induction
must cover: from a state satisfying the invariant, transient storage resets
to zero, and the invariant must still hold —
`preserved onTransactionBoundary with (env e)` supplies the assumptions for
exactly that step.

A method may be excluded from such an invariant's check via `filtered` only
when contract code makes it unreachable as a transaction entry point — its
guard requires transient state (a held lock) that is always clear at a
boundary, so called standalone it reverts, and its effects are covered inside
the checked entry-point methods that invoke it. This is still a §4 theorem
edit: the exclusion and the code-enforced guard justifying it are stated.

## 2. Using invariants in proofs: `requireInvariant`

A proved invariant is consumed elsewhere with `requireInvariant`, legal in
rule bodies and in `preserved` blocks:

- `requireInvariant inv(args);` assumes the invariant's statement at the
  given arguments — for a parameterless quantified invariant, the full
  universal statement.
- The assumption is sound provided the cited invariant is itself proved;
  mutual citation between invariants is covered in §1.
- `requireInvariant` is enforced at rule boundaries — after function calls,
  subcalls, and (for `strong` invariants) havocs — not inline at arbitrary
  program points inside hooks or summaries.

## 3. Weak and strong invariants

By default an invariant is *weak*: checked before and after each method's
execution, unconstrained at intermediate points. A *strong* invariant is
additionally **asserted before** each havocked external call and **assumed
after** it:

```cvl
strong invariant myInvariant() ...;
```

Use `strong` when the property must hold at every point an external party
could interact with the state it constrains — callees reading that state
during a call-out being the standard case. The mid-execution assertion lands exactly where a weak invariant is
blind: the havoc.

## 4. Soundness audit for invariant proofs

Each item is an assumption channel that can mask a real violation:

1. **Raw `require` in `preserved` blocks.** Every `require` prunes
   executions. Prefer `requireInvariant` of a proved invariant.
2. **Filters.** A `filtered` clause removes methods from the check — a
   theorem edit the report does not highlight; state it.
3. **Reverting invariant expressions.** If evaluating the invariant
   expression itself reverts, the check passes vacuously; keep invariant
   expressions revert-free (guard indices, use environment-free reads).

## 5. Invariant parameters and the choice of form

Invariant parameters are implicitly universally quantified: a *pointwise*
property is written `invariant foo(uint i) p(i)`, not
`invariant foo() forall uint i. p(i)`.

For invariants over the *contents of a data structure* (mirrored arrays,
maps, sets), write the quantified (`forall`) form first: one
`requireInvariant` imports the full universal statement, no `preserved`
blocks are needed for cross-instance facts, and the Prover's grounding
supplies the instances. The parameterized form is the fallback, when:

1. the invariant body must call contract view functions, or
2. the body must access storage — both barred inside quantifiers (§7) — or
3. grounding fails to find the needed instances (§6 explains when).

The fallback restates the property with the quantified variables as
invariant parameters. A conjunction whose conjuncts constrain different
variables splits into separate invariants, one per conjunct, so each part can
be instantiated and cited independently.

Preservation of one instance may depend on the property at *other* instances.
The methods where this happens get `preserved` blocks, each supplying the
needed instances as `requireInvariant` lines with witnesses written out
explicitly — the method's own arguments, or prestate index expressions such
as the last occupied slot.

Declare index parameters as `mathint`. An out-of-range instantiation such as
`A(-1)` is then vacuously true. With a `uint256` parameter instead, the
witness expression needs a cast, and `require_uint256` on an out-of-range
witness silently prunes those executions — a vacuity trap inside the
soundness apparatus itself.

The cost of the fallback: every needed instance is identified by hand, and
the preserved blocks are maintained as the contract changes.

## 6. How grounding instantiates — and how to write for it

Quantifier grounding replaces a universal assumption by finitely many
instances. Instantiation is *syntactic*: it searches for values that build
**terms already present** in the verification condition. If the program reads
`a[j]`, a quantified formula whose body contains the term `a[i]` receives the
instance `i := j`, because the terms match. Two style rules follow.

**Rule 1 — quantified variables appear as direct access arguments.**
Arithmetic in the access position defeats the matching: with
`forall uint i. 1 <= i && i <= len => m[a[i - 1]] == i`, a program read of
`a[j]` requires inverting `i - 1 = j` — and inversion through arithmetic
(offsets, overflow cases, scaling, sums of variables) is not something the
instantiation heuristic performs. State the same fact with the variable
indexing directly:

```cvl
forall uint i. i < len => m[a[i]] == i + 1
```

Now `a[j]` matches `a[i]` and the needed instance appears. When a quantified
formula must relate positions, move the arithmetic *out* of the access and
into the guard or the conclusion.

**Rule 2 — close the invariant under the consequences the proofs will need.**
A true invariant can be useless if its consequences require chains of
instances. `forall uint i. a[i] <= a[i + 1]` implies `a[0] <= a[100]`, but
only through a hundred instantiations the heuristic will not produce (it
bounds instances to guarantee termination). State the transitively-closed
equivalent, so that any needed fact is a *single* instantiation:

```cvl
forall uint i. forall uint j. i <= j => a[i] <= a[j]
```

Sortedness is the canonical case; the general form of the rule: if the
property's use sites need `P(x, z)` while the natural statement gives
`P(x, y)` and `P(y, z)`, quantify the closed relation, not the generating
step.

## 7. Quantifier bodies, and the mirror pattern

Quantifier bodies contain no contract calls, no storage accesses, and no
`require_*`/`assert_*` casts. Direct storage access therefore covers
*unquantified* reads of contract state — and where a property must quantify
over stored contents, the storage is **mirrored into ghosts** by hooks, and
the quantifier ranges over the ghosts:

- one ghost per quantified-over field;
- `Sstore` hooks assign (propagating contract writes into the mirror);
- `Sload` hooks `require` equality (importing storage knowledge — without
  this direction, values read by the contract are unconstrained in the
  mirror).

Illustration, for a structure pairing an array with an inverse map (an
enumerable set):

```cvl
ghost mathint ghostLength;
ghost mapping(mathint => bytes32) ghostValues;
ghost mapping(bytes32 => mathint) ghostIndexes;

hook Sstore currentContract.set._inner._values.length uint256 newLength {
    ghostLength = newLength;
}
hook Sload uint256 length currentContract.set._inner._values.length {
    require ghostLength == length;
}
// ...analogous pairs for _values[INDEX uint256 i] and _indexes[KEY bytes32 v]...

invariant mirrorCoherence()
    (forall mathint i. 0 <= i && i < ghostLength
        => ghostIndexes[ghostValues[i]] == i + 1)
 && (forall bytes32 v. ghostIndexes[v] == 0
        || (ghostValues[ghostIndexes[v] - 1] == v
            && 1 <= ghostIndexes[v] && ghostIndexes[v] <= ghostLength));
```

Both quantified conjuncts follow §6's rules: variables index directly
(`ghostValues[i]`, `ghostIndexes[v]`), and each conjunct is stated so its use
sites need one instance. (The `ghostIndexes[v] - 1` inside the *second*
conjunct sits in an access position — tolerated here because the matching
term for that conjunct is `ghostIndexes[v]` itself; when such a formula
resists grounding, restating or the §5 fallback applies.) Ghosts mirroring
storage-like state stay regular, not `persistent`, so they havoc and revert
when the storage they mirror does.

## 8. Properties beyond first-order: closure relations

Some properties are defined by *unbounded iteration* of a base relation:
membership in a linked structure ("some number of successor steps from the
head"), reachability, anything of the form "there exists a chain." No
first-order formula over the base maps expresses these — the disjunction
`x = h ∨ x = f[h] ∨ x = f[f[h]] ∨ …` has no closed form. The encoding:
**make the closure itself a ghost relation**, and maintain it at every write
to the base map with a two-state update.

For a functional edge map `f` (each node one successor) and `R` its
reflexive-transitive closure, the write `f[a] := b` updates `R` as:

```cvl
ghost R(bytes32, bytes32) returns bool {
    init_state axiom forall bytes32 x. forall bytes32 y.
        R(x, y) == (x == y || y == to_bytes32(0));   // closure of the empty structure
}

definition updateEdge(bytes32 a, bytes32 b) returns bool =
    forall bytes32 x. forall bytes32 y. R@new(x, y) ==
        (x == y
         || (R@old(x, y) && !(R@old(x, a) && a != y && R@old(a, y)))
         || (R@old(x, a) && R@old(b, y)));

hook Sstore currentContract...next... bytes32 newNext {
    assert !R(newNext, key);          // side condition: the write creates no cycle
    ghostNext[key] = newNext;
    havoc R assuming updateEdge(key, newNext);
}
```

The three disjuncts: reflexivity; old paths that did not pass through `a`'s
outgoing edge survive; new paths route through the new edge. The pattern
generalizes:

- **Side conditions are asserted, not assumed.** Closure-update formulas are
  typically valid only under well-formedness (here: acyclicity). The in-hook
  `assert` converts the formula's precondition into a checked obligation at
  every write.
- **The base relation is recoverable from the closure** for functional maps:
  adjacency is "reachable, distinct, with nothing strictly between" —
  `succ(a,b) ⟺ R(a,b) ∧ a ≠ b ∧ (forall x. R(a,x) ∧ R(x,b) ⇒ x = a ∨ x = b)`
  — letting well-formedness invariants be stated over `R` alone.
- **Hooks can self-check.** An in-hook `assert` of a coherence definition
  before and after the ghost update verifies, at every write, that the
  maintained relation still matches its intended meaning.

The update formula shown is specific to *functional* step relations with
single-edge replacement; other write shapes — adding an edge in a
multi-successor graph, deleting an edge — need their own closure updates,
derived and guarded the same way. With that adaptation, the same shape serves
any closure-defined property: choose the ghost relation, derive its per-write
update formula, assert the update's side conditions, and state the
specification's properties over the relation.
