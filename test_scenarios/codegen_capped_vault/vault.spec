// CVL specification for CappedVault.
//
// Two rules. Both VIOLATED against a faithful first draft for *different*
// reasons, which drives the two parallel CEX analyses in the codegen harness:
//
//   - depositIncreasesTotal — conceptually correct; fails only because the
//     first-draft implementation forgets to update `totalDeposited`. Fixed by
//     editing the Solidity.
//   - depositRaisesBalance — over-strong as written (a zero-value deposit is a
//     legal no-op, so `>` is wrong). Fixed spec-side via cex_remediation, which
//     guards the assertion on `amount > 0`.

methods {
    function balance(address) external returns (uint256) envfree;
    function totalDeposited() external returns (uint256) envfree;
    function CAP() external returns (uint256) envfree;
    function deposit(uint256) external;
    function withdraw(uint256) external;
}

rule depositIncreasesTotal(uint256 amount) {
    env e;
    mathint before = totalDeposited();
    deposit(e, amount);
    assert to_mathint(totalDeposited()) == before + amount,
        "a successful deposit must increase totalDeposited by exactly amount";
}

rule depositRaisesBalance(uint256 amount) {
    env e;
    address caller = e.msg.sender;
    mathint before = balance(caller);
    deposit(e, amount);
    assert to_mathint(balance(caller)) > before,
        "a successful deposit must raise the caller's balance";
}
