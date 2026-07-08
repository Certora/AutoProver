from pathlib import Path
import pytest

from composer.prover.core import run_prover_inner
from composer.prover.cloud import cloud_results
from composer.prover.results import read_and_format_run_result, RuleResult
from composer.prover.ptypes import RulePath

pytestmark = pytest.mark.expensive

async def _run_test_prover_job(
    *extra_args: str
) -> dict[str, RuleResult]:
    test_dir = Path(__file__).parent.parent / "test_scenarios" / "multi_contract_results"
    assert test_dir.exists() and test_dir.is_dir()

    async def swallow(s: str):
        pass
    prover_res, _ = await run_prover_inner(
        folder=test_dir,
        args=[
            "src/Bank.sol",
            "src/Token.sol",
            "--verify",
            "Bank:./certora/specs/messy.spec",
            "--loop_iter", "3",
            "--optimistic_loop",
            "--solc", "solc",
            "--server", "prover",
            "--prover_version", "master",
            "--wait_for_results", "none",
            *extra_args
        ],
        on_err=lambda _ret, _out, _err: None,
        on_stdout=swallow
    )
    assert not isinstance(prover_res, str) and prover_res is not None and prover_res["sort"] == "success", "Prover succeeded"
    assert not prover_res["is_local_link"] and prover_res["link"] is not None, "Not a cloud link?"
    
    async with cloud_results(
        prover_res["link"], poll_timeout=600
    ) as (dir, _runtime_ms):
        res = read_and_format_run_result(dir)
    assert not isinstance(res, str), "Parse failed"
    return res

@pytest.mark.asyncio
async def test_rule_parsing():
    prover_res = await _run_test_prover_job()

    # Fully-parametric run: both parametric rules instantiated per-method across
    # Bank and Token, plus the non-parametric rules and the envfree static check.
    expected: list[tuple[RulePath, str]] = [
        (RulePath(rule='envfreeFuncsStaticCheck'), 'VERIFIED'),
        (RulePath(rule='mintRaisesSupply'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Bank', method='Bank.withdraw(uint256)'), 'VIOLATED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Bank', method='Bank.transferShares(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Bank', method='Bank.deposit(uint256)'), 'VIOLATED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Bank', method='Bank.setOwner(address)'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Token', method='Token.mint(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Token', method='Token.transfer(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', contract='Token', method='Token.transferFrom(address,address,uint256)'), 'VERIFIED'),
        (RulePath(rule='sharesSumIsConsistent', method='constructor'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Bank', method='Bank.transferShares(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Bank', method='Bank.withdraw(uint256)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Bank', method='Bank.deposit(uint256)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Bank', method='Bank.setOwner(address)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Token', method='Token.mint(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Token', method='Token.transfer(address,uint256)'), 'VERIFIED'),
        (RulePath(rule='totalSharesStableOutsideBankFlows', contract='Token', method='Token.transferFrom(address,address,uint256)'), 'VERIFIED'),
        (RulePath(rule='vacuousOwnerRule'), 'SANITY_FAILED'),
        (RulePath(rule='withdrawDecrementsTotal'), 'VIOLATED'),
    ]

    # Keyed by RuleResult.name (== RulePath.pprint()); check keys, paths, and statuses at once.
    assert {name: (r.path, r.status) for name, r in prover_res.items()} == {
        path.pprint(): (path, status) for path, status in expected
    }


@pytest.mark.asyncio
async def test_rule_parsing_parametric():
    prover_res = await _run_test_prover_job(
        "--parametric_contracts", "Bank"
    )

    # Parametric only over Bank, so no Token method instantiations. The prover's
    # own naming is what it is (some instantiations qualify the contract, some
    # don't), so assert against the names verbatim rather than rebuilding paths.
    expected: dict[str, str] = {
        'envfreeFuncsStaticCheck': 'VERIFIED',
        'mintRaisesSupply': 'VERIFIED',
        'sharesSumIsConsistent for constructor': 'VERIFIED',
        'sharesSumIsConsistent for Bank.withdraw(uint256)': 'VIOLATED',
        'sharesSumIsConsistent for transferShares(address,uint256)': 'VERIFIED',
        'sharesSumIsConsistent for deposit(uint256)': 'VIOLATED',
        'sharesSumIsConsistent for setOwner(address)': 'VERIFIED',
        'totalSharesStableOutsideBankFlows for transferShares(address,uint256)': 'VERIFIED',
        'totalSharesStableOutsideBankFlows for withdraw(uint256)': 'VERIFIED',
        'totalSharesStableOutsideBankFlows for deposit(uint256)': 'VERIFIED',
        'totalSharesStableOutsideBankFlows for setOwner(address)': 'VERIFIED',
        'vacuousOwnerRule': 'SANITY_FAILED',
        'withdrawDecrementsTotal': 'VIOLATED',
    }

    assert {name: r.status for name, r in prover_res.items()} == expected

