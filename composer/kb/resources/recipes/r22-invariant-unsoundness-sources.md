> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R22. Invariant unsoundness sources `[CVL]`

**Trigger:** an invariant verifies, yet the contract can actually violate it.

The audit list — each item is an assumption channel that can mask a real
violation:

1. **Raw `require` in `preserved` blocks.** Every `require` prunes executions.
   Prefer `requireInvariant` of another invariant, which is sound provided the
   cited invariant is itself proved.
2. **Filters.** A `filtered` clause removes methods from the check entirely —
   it edits the theorem, and the report does not highlight the exclusion.
3. **Reverting invariant expressions.** If evaluating the invariant expression
   itself reverts, the check passes vacuously; keep invariant expressions
   revert-free (guard indices, use `envfree`-safe reads).
