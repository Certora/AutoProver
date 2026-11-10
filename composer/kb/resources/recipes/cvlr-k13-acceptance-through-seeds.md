> Where this recipe and the Solana/CVLR manual or other Certora materials diverge, this recipe governs.

### K13. Acceptance through a `seeds` constraint `[SKIP]`

**Trigger:** an acceptance property whose validation runs through `seeds`, including `init` on a PDA;
a rejection property of the form "a non-canonical X is refused".

Acceptance through account validation can be out of reach even when the failure path is
observable: **a `seeds` constraint's verdict is unconstrained.** Anchor checks it by deriving the
address with `Pubkey::find_program_address` or `create_program_address`, and the starting
configuration summarizes both, so each call returns an unconstrained address, independent of every
other call. The address your rule derives and the one Anchor derives to check against are unrelated
values, so the constraint may accept or refuse any account. Two kinds of property follow from that
and cannot be stated: acceptance by validation that runs through a `seeds` constraint, which includes
`init` on a PDA, and rejection *by the `seeds` constraint itself* ("a non-canonical vault is
refused"). For either, `record_skip` naming the PDA summary as the reason, without spending a run to
find it out. Rejection by the struct's other constraints — the signer, `mut`, `has_one` — is
unaffected: an input one of those refuses is refused whatever the derivation returned.
