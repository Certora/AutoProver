"""`covering_run_links` — which runs' results account for the published spec.

Mirrors what `_is_completion_history` accepts: completion may be reached piecemeal across
several runs against one authoring state, so the report has to read all of them.
"""

from composer.spec.source.prover import ProverHistoryItem, ProverRunLog, NagMarker, covering_run_links


def _run(
    *,
    digest: str = "sd",
    link: str | None = "L",
    with_link: bool = True,
) -> ProverHistoryItem:
    run = ProverRunLog(
        tool_call_id="tc",
        prover_results=[],
        rules=None,
        spec_digest="d",
        sort="run",
        declared_rules=["r1"],
        state_digest=digest,
    )
    if with_link:
        run["link"] = link
    return run


def _nag() -> ProverHistoryItem:
    return NagMarker(sort="nag", nagged_rules=[])


def test_newest_first():
    history = [_run(link="older"), _run(link="newer")]
    assert covering_run_links(history, "sd") == ["newer", "older"]


def test_stops_at_a_different_state():
    # The run against "other" changed the spec, so nothing at or before it counts.
    history = [_run(link="stale", digest="other"), _run(link="current")]
    assert covering_run_links(history, "sd") == ["current"]


def test_nag_markers_are_transparent():
    history = [_run(link="older"), _nag(), _run(link="newer")]
    assert covering_run_links(history, "sd") == ["newer", "older"]


def test_a_run_recorded_before_the_link_field_is_skipped():
    history = [_run(with_link=False), _run(link="newer")]
    assert covering_run_links(history, "sd") == ["newer"]


def test_a_run_whose_link_is_none_is_skipped():
    history = [_run(link=None), _run(link="newer")]
    assert covering_run_links(history, "sd") == ["newer"]


def test_no_matching_run():
    assert covering_run_links([_run(digest="other")], "sd") == []
