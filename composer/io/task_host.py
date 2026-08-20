from typing import Literal, Callable, Awaitable
from uuid import uuid4
import asyncio
from dataclasses import dataclass

@dataclass
class _TaskHandle[T]:
    desc: str
    handle: asyncio.Future[T]
    event: asyncio.Event
    ty: type[T]

@dataclass
class TaskStatus:
    desc: str
    status: Literal["running", "complete"]

class NoSuchTaskError(RuntimeError):
    ...

class TaskHost:
    def __init__(self):
        self._task_map : dict[str, _TaskHandle]= {}
        self._lock = asyncio.Lock()

    async def list_tasks(self) -> dict[str, TaskStatus]:
        return {
            k: TaskStatus(
                desc=j.desc,
                status="running" if not j.handle.done() else "complete"
            ) for (k, j) in self._task_map.items()
        }

    async def launch[T](self, description: str, t: type[T], task: Callable[[], Awaitable[T]]) -> str:
        # Callable[[], Awaitable[T]] isn't a "coroutine like" for the asyncio typing, so make that explicit
        async def task_wrap():
            return await task()
        async with self._lock:
            t_id = uuid4().hex
            compl = asyncio.Event()
            task_fut = asyncio.create_task(
                task_wrap()
            )
            task_fut.add_done_callback(lambda _: compl.set())
            self._task_map[t_id] = _TaskHandle(
                desc=description,
                handle=task_fut,
                event=compl,
                ty=t
            )
        return t_id

    async def await_result[T](self, task_id: str, t: type[T]) -> T:
        async with self._lock:
            handle = self._task_map.get(task_id)
        if handle is None:
            raise NoSuchTaskError()
        assert handle.ty is t, "Type mismatch"
        await handle.event.wait()
        res = handle.handle.result()
        async with self._lock:
            if task_id in self._task_map:
                del self._task_map[task_id]
        return res
