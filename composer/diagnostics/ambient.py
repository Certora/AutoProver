"""Persistence for ambient run state: the accounting objects a process keeps in
contextvars for the life of a run (the run summary, the cost budget) and that a
later execution of the same run has to pick up where they were.

A state that wants this satisfies :class:`SaveableState`: it names itself, can
write itself into a dict and read itself back. :class:`AmbientStateSaver` is the
factory the entry point builds once per run, over the run's store namespace;
each context manager that installs a piece of ambient state asks it for
``persisted(state)``, which restores the state on entry, saves it periodically,
and flushes it on exit. What a state's dict holds is the state's own business:
the saver never looks inside.
"""

import asyncio
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, Protocol

from langgraph.store.base import BaseStore

_logger = logging.getLogger(__name__)


class SaveableState(Protocol):
    @property
    def id(self) -> str:
        """The record key within the saver's namespace; one per kind of state."""
        ...

    def save_to(self, out: dict[str, Any]) -> None:
        """Write this state's representation into ``out``."""
        ...

    def restore_from(self, data: dict[str, Any]) -> None:
        """Adopt a representation a previous execution saved. The state decides
        what to do with one it does not recognize, including nothing."""
        ...


class AmbientStateSaver:
    """Persists :class:`SaveableState`s under one namespace: restore on entry,
    a save every ``interval_s``, and a flush on exit. Store failures are logged
    and swallowed, the way the run's other records are; accounting must never
    take the run down."""

    def __init__(self, store: BaseStore, ns: tuple[str, ...], *, interval_s: float = 15.0) -> None:
        self._store = store
        self._ns = ns
        self._interval = interval_s

    @asynccontextmanager
    async def persisted(self, state: SaveableState) -> AsyncIterator[None]:
        await self._restore(state)
        ticker = asyncio.create_task(self._tick(state), name=f"ambient-save:{state.id}")
        try:
            yield
        finally:
            ticker.cancel()
            await asyncio.gather(ticker, return_exceptions=True)
            await self.save(state)

    async def save(self, state: SaveableState) -> None:
        out: dict[str, Any] = {}
        state.save_to(out)
        try:
            await self._store.aput(self._ns, state.id, out)
        except Exception:
            _logger.warning("could not save ambient state %r", state.id, exc_info=True)

    async def _restore(self, state: SaveableState) -> None:
        try:
            item = await self._store.aget(self._ns, state.id)
        except Exception:
            _logger.warning("could not read ambient state %r", state.id, exc_info=True)
            return
        if item is not None:
            state.restore_from(dict(item.value))

    async def _tick(self, state: SaveableState) -> None:
        while True:
            await asyncio.sleep(self._interval)
            await self.save(state)
