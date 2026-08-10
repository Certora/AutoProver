> Where this recipe and the CVL manual or other Certora materials diverge, this recipe governs.

### R18. Reentrancy `[CVL]`

**Trigger:** reentrancy safety to verify. Three recipes, by property:

1. **Read-only reentrancy** — the builtin rule:
   ```cvl
   use builtin rule viewReentrancy;
   ```
2. **"Any reentrant call reverts"** (guard verification) — a double-parametric
   rule with a `CALL`-opcode hook that simulates re-entering `g` at each
   external call made by `f`. The hook dispatches on a saved selector and must
   enumerate the contract's non-view methods concretely — per-contract
   boilerplate, generated per target:
   ```cvl
   persistent ghost bool called_extcall;
   persistent ghost bool g_reverted;
   persistent ghost uint32 g_sighash;

   hook CALL(uint g, address addr, uint value, uint argsOffset, uint argsLength,
             uint retOffset, uint retLength) uint rc {
       called_extcall = true;
       env e;
       if (g_sighash == sig:withdrawAll().selector) {
           withdrawAll@withrevert(e);
           g_reverted = lastReverted;
       } else if (g_sighash == sig:withdraw(uint256).selector) {
           calldataarg args;
           withdraw@withrevert(e, args);
           g_reverted = lastReverted;
       } else {
           // sound simulation only if no explicit `fallback` in target contract;
           // simulates the fallback behavior immediate reverting.
           g_reverted = true;
       }
   }

   rule no_reentrancy(method f, method g)
       filtered { f -> !f.isView, g -> !g.isView } {
       require !called_extcall && !g_reverted;
       env e; calldataarg args;
       require g_sighash == g.selector;
       f@withrevert(e, args);
       assert called_extcall => g_reverted;
   }
   ```
3. **"Storage accesses don't straddle external calls"** (guard-free safety) —
   classify every storage access as before/after the first external call and
   assert the combination never occurs:
   ```cvl
   persistent ghost bool called_extcall;
   persistent ghost bool access_before_call;
   persistent ghost bool access_after_call;

   hook CALL(uint g, address addr, uint value, uint argsOffset, uint argsLength,
             uint retOffset, uint retLength) uint rc {
       called_extcall = true;
   }
   hook ALL_SSTORE(uint loc, uint v) {
       if (!called_extcall) { access_before_call = true; }
       else                 { access_after_call  = true; }
   }
   hook ALL_SLOAD(uint loc) uint v {
       if (!called_extcall) { access_before_call = true; }
       else                 { access_after_call  = true; }
   }

   rule reentrancySafety(method f) {
       require !called_extcall && !access_before_call && !access_after_call;
       env e; calldataarg args;
       f(e, args);
       assert !(access_before_call && access_after_call);
   }
   ```
