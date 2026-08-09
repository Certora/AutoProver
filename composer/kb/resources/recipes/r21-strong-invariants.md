> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R21. Strong invariants `[CVL]`

**Trigger:** an invariant must hold *across* unresolved external calls — a
havocked call mid-execution can break it and the default check does not
notice.

**Formula:** by default an invariant is *weak*: checked before and after each
method's execution, with no constraint at intermediate points. A *strong*
invariant is additionally **asserted before** each havocked external call and
**assumed after** it:

```cvl
strong invariant myInvariant() ...;
```

Use `strong` when the property must hold at every point an external party
could observe it — e.g. state read by callees during a call-out. The
mid-execution assert interacts directly with havoc summaries: the havoc is
exactly where a weak invariant's blind spot sits.
