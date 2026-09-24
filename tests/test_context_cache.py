"""``WorkflowContext`` caching, and the one case where budget pressure must not suppress a write.

``cache_put`` drops writes made under budget pressure so a rushed result is not served to a later
run as finished. A *resume buffer* is the exception: it exists precisely to carry unfinished work
across the cut, so the run that most needs to write one is the run the guard would silence.
"""

import pytest
from langgraph.store.memory import InMemoryStore
from pydantic import BaseModel

from composer.diagnostics.budget import accumulate_cost, token_cost_budget
from composer.spec.context import CacheKey, WorkflowContext


class _Draft(BaseModel):
    text: str


RESULT = CacheKey[None, _Draft]("result")
BUFFER = CacheKey[None, _Draft]("buffer", survives_budget_pressure=True)

pytestmark = pytest.mark.asyncio


def _ctx(store: InMemoryStore) -> WorkflowContext[None]:
    return WorkflowContext.create(
        services=lambda _ns: None,  # type: ignore[arg-type]
        thread_id="t", store=store, recursion_limit=10, cache_namespace=("ns",),
    )


async def test_a_result_and_a_buffer_are_both_cached_when_there_is_no_pressure():
    store = InMemoryStore()
    root = _ctx(store)
    await root.child(RESULT).cache_put(_Draft(text="r"))
    await root.child(BUFFER).cache_put(_Draft(text="b"))

    assert await root.child(RESULT).cache_get(_Draft) == _Draft(text="r")
    assert await root.child(BUFFER).cache_get(_Draft) == _Draft(text="b")


async def test_pressure_drops_a_result_but_keeps_a_resume_buffer():
    # The shape of a budget-cut run: the draft is the only thing worth carrying forward, and
    # without the opt-in it is the one thing that would not be.
    store = InMemoryStore()
    root = _ctx(store)
    with token_cost_budget(10.0):
        accumulate_cost(10.0)
        await root.child(RESULT).cache_put(_Draft(text="r"))
        await root.child(BUFFER).cache_put(_Draft(text="b"))

    assert await root.child(RESULT).cache_get(_Draft) is None
    assert await root.child(BUFFER).cache_get(_Draft) == _Draft(text="b")


async def test_the_exemption_belongs_to_the_key_and_is_not_inherited_by_children():
    # It is a property of what is being stored, not of where in the tree it sits — a result
    # computed underneath a buffer is still a result.
    store = InMemoryStore()
    nested = _ctx(store).child(BUFFER).child(CacheKey[_Draft, _Draft]("inner"))
    with token_cost_budget(10.0):
        accumulate_cost(10.0)
        await nested.cache_put(_Draft(text="x"))

    assert await nested.cache_get(_Draft) is None
