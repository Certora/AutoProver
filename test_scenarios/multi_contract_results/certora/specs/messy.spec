/*
 * Deliberately messy multi-contract spec — a fixture for exercising prover-result
 * parsing (sanity failures, parametric instantiation, multi-contract). Run from
 * the scenario root:
 *
 *     certoraRun messy.conf
 *
 * Intended mix of verdicts (best-effort CVL — sanity-check the syntax before relying on it):
 *   - mintRaisesSupply                  : plain rule, VERIFIED
 *   - withdrawDecrementsTotal           : plain rule, VIOLATED (the withdraw bug)
 *   - sharesSumIsConsistent             : invariant w/ preserved blocks, VIOLATED
 *   - vacuousOwnerRule                  : vacuous -> SANITY failure (rule_sanity)
 *   - totalSharesStableOutsideBankFlows : parametric over Bank AND Token
 *                                         (parametric_contracts) -> one
 *                                         instantiation per method, across both
 *                                         contracts
 */

using Token as token;

methods {
    function totalShares() external returns (uint256) envfree;
    function shares(address) external returns (uint256) envfree;
    function owner() external returns (address) envfree;
    function token.totalSupply() external returns (uint256) envfree;
    function token.balanceOf(address) external returns (uint256) envfree;
}

/// Ghost mirror of the sum of every `shares` entry, maintained by a store hook.
ghost mathint sumShares {
    init_state axiom sumShares == 0;
}

hook Sstore shares[KEY address a] uint256 newVal (uint256 oldVal) {
    sumShares = sumShares + newVal - oldVal;
}

/// Invariant with preserved blocks. VIOLATED: `withdraw` decrements a user's
/// shares without decrementing `totalShares`, so the ghost and the counter drift.
invariant sharesSumIsConsistent()
    sumShares == to_mathint(totalShares())
    {
        preserved withdraw(uint256 amount) with (env e) {
            require shares(e.msg.sender) >= amount;
        }
        preserved {
            requireInvariant sharesSumIsConsistent();
        }
    }

/// Plain rule, should VERIFY: mint raises the token supply by exactly `amount`.
rule mintRaisesSupply(env e, address to, uint256 amount) {
    mathint before = token.totalSupply();
    token.mint(e, to, amount);
    assert to_mathint(token.totalSupply()) == before + amount;
}

/// Plain rule, VIOLATED by the `withdraw` bug: `totalShares` is not decremented.
rule withdrawDecrementsTotal(env e, uint256 amount) {
    require amount > 0;
    mathint before = totalShares();
    withdraw(e, amount);
    assert to_mathint(totalShares()) == before - amount;
}

/// VACUOUS -> sanity failure: the two `require`s contradict, so the assertion is
/// never reached and `rule_sanity` flags the rule.
rule vacuousOwnerRule(env e) {
    require owner() == e.msg.sender;
    require owner() != e.msg.sender;
    assert false;
}

/// Parametric rule ranging over BOTH contracts (see `parametric_contracts` in
/// the conf): one instantiation per method of Bank and of Token.
rule totalSharesStableOutsideBankFlows(method f, env e, calldataarg args)
    filtered { f -> !f.isView }
{
    mathint before = totalShares();
    f(e, args);
    mathint after = totalShares();
    assert
        (f.selector != sig:deposit(uint256).selector &&
         f.selector != sig:withdraw(uint256).selector)
        => after == before;
}
