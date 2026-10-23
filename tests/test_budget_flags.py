"""The two flags that can carry a run budget: ``--budget`` (a file, with per-phase
caps) and ``--budget-total`` (the pool as a bare USD number).

The scalar is not a second budget mechanism — it must land on exactly the ``RunBudget``
a one-key budget file produces, so everything downstream (the caps-over-pool counters,
the ``budget`` data-log record) sees one shape regardless of which flag was used.
"""

import json
import pathlib

import pytest

from composer.foundry.entry import _build_parser as _foundry_parser
from composer.pipeline.cli import (
    BUDGET_PHASES,
    parse_budget_file,
    parse_budget_scalar,
    reject_unpriced_budget,
    resolve_budget,
)
from composer.llm.pricing import price_provider_for


def test_scalar_matches_a_pool_only_budget_file(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"total": 25.0}))
    assert parse_budget_scalar(25.0) == parse_budget_file(path)


def test_scalar_leaves_every_phase_bounded_by_the_pool_alone() -> None:
    budget = parse_budget_scalar(25.0)
    assert budget.total == 25.0
    # Caps are ceilings, not allotments: at the pool value none of them can trip
    # before the pool does.
    assert budget.caps == {phase: 25.0 for phase in BUDGET_PHASES}


@pytest.mark.parametrize("total", [0.0, -1.0])
def test_scalar_rejects_a_non_positive_pool(total: float) -> None:
    # A *cap* of 0.0 is legal (that phase starts inside its wrap-up window); a pool
    # of 0.0 is not — there would be nothing to spend.
    with pytest.raises(ValueError, match="--budget-total"):
        parse_budget_scalar(total)


def test_resolve_rejects_both_flags(tmp_path: pathlib.Path) -> None:
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"total": 25.0}))
    with pytest.raises(ValueError, match="mutually exclusive"):
        resolve_budget(str(path), 25.0)


def test_resolve_without_either_flag_is_unbudgeted() -> None:
    assert resolve_budget(None, None) is None


def test_foundry_parser_exposes_the_scalar_flag() -> None:
    args = _foundry_parser().parse_args(["proj", "src/C.sol:C", "--budget-total", "25"])
    assert (args.budget, args.budget_total) == (None, 25.0)

    args = _foundry_parser().parse_args(["proj", "src/C.sol:C"])
    assert (args.budget, args.budget_total) == (None, None)


def test_foundry_parser_rejects_both_budget_flags(
    tmp_path: pathlib.Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """argparse owns the exclusivity, so a human gets the error at parse time and sees
    the choice in --help, rather than reaching ``resolve_budget``'s backstop."""
    path = tmp_path / "budget.json"
    path.write_text(json.dumps({"total": 25.0}))
    with pytest.raises(SystemExit):
        _foundry_parser().parse_args(
            ["proj", "src/C.sol:C", "--budget", str(path), "--budget-total", "25"]
        )
    assert "not allowed with argument --budget" in capsys.readouterr().err


def test_budget_on_an_unpriced_model_is_rejected() -> None:
    """An unpriced model's calls cost $0, so a budget over it could never trip."""
    with pytest.raises(ValueError, match="gpt-5.3"):
        reject_unpriced_budget(parse_budget_scalar(25.0), {
            "gpt-5.3": price_provider_for("gpt-5.3"),
            "gpt-5.5": price_provider_for("gpt-5.5"),
        })


def test_budget_on_priced_models_is_accepted() -> None:
    reject_unpriced_budget(parse_budget_scalar(25.0), {
        "gpt-5.5": price_provider_for("gpt-5.5"),
        "gpt-5.4-mini": price_provider_for("gpt-5.4-mini"),
    })


def test_unbudgeted_run_may_use_an_unpriced_model() -> None:
    reject_unpriced_budget(None, {"gpt-5.3": price_provider_for("gpt-5.3")})
