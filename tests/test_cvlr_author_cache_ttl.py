"""The CVLR author's prompt-cache TTL.

This backend is the one that blocks on the cloud prover, and a job routinely runs longer than the
5-minute default TTL. When the cache expires mid-wait the next call re-writes the entire context at
the cache-*write* rate instead of reading it at the cache-*read* rate — on a 326K-token context
that is ~$2.13 a call against ~$0.17. On the 2026-09-24 stake run it accounted for roughly $173 of
$283 before anyone noticed, because the call site carried a comment saying "long cache" while the
argument that selects it had never been passed.

So the test is structural: it reads the call, not the behaviour. A behavioural test would need a
`ServiceHost` double threaded through the whole tool-building preamble, and would still not catch
the thing that actually went wrong — a keyword quietly absent from one call.
"""

import ast
import inspect

import composer.spec.cvlr.author as author


def _builder_heavy_calls(module: object) -> list[ast.Call]:
    tree = ast.parse(inspect.getsource(module))
    return [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "builder_heavy"
    ]


def test_the_author_asks_for_the_one_hour_cache():
    calls = _builder_heavy_calls(author)
    assert len(calls) == 1, "expected exactly one builder in the author; update this test if that changed"

    kw = {k.arg: k.value for k in calls[0].keywords}
    assert "cache_level" in kw, (
        "the author's builder fell back to the 5-minute default TTL; a prover wait evicts it and "
        "the next call pays cache-write rates for the whole context"
    )
    level = kw["cache_level"]
    assert isinstance(level, ast.Attribute) and level.attr == "LONG", ast.dump(level)


def test_long_means_an_hour():
    # The author's choice is only worth anything if this mapping still says what it said.
    from composer.llm.anthropic import level_to_ttl
    from composer.llm.types import CacheLevel

    assert level_to_ttl(CacheLevel.LONG) == "1h"
    assert level_to_ttl(CacheLevel.SHORT) == "5m"


def test_a_long_cache_is_costed_at_the_one_hour_write_rate():
    # Picking LONG makes writes dearer (and reads unchanged), so the accounting has to follow the
    # choice or the budget silently under-reports what the run is spending.
    from composer.llm.pricing import PriceTier

    tier = PriceTier(5.00, 25.00, 0.50, 6.25, 10.00)
    assert tier.cache_write_1h > tier.cache_write
    assert tier.cache_read < tier.cache_write
