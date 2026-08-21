"""The author-side surface of the run's :class:`TaskHost`: list the background
tasks plugin tools have launched, and collect their reports. Task results are
always strings — the lingua franca of tool results — so ``RetrieveTask`` is
the single retrieval tool regardless of which plugin launched the task.

Bind both over the host the author owns::

    TaskListTool.bind(host).as_tool(TASK_LIST)
    RetrieveTask.bind(host).as_tool(RETRIEVE_TASK)
"""

from typing import override

from pydantic import Field

from graphcore.tools.schemas import WithAsyncDependencies

from .task_host import NoSuchTaskError, TaskHost

TASK_LIST = "task_list"
RETRIEVE_TASK = "retrieve_task"


class TaskListTool(WithAsyncDependencies[str, TaskHost]):
    """
    List the background tasks launched in this session: each task's ID, what
    it is doing, and whether it is still running or has a report ready.
    """

    @override
    async def run(self) -> str:
        with self.tool_deps() as host:
            tasks = await host.list_tasks()
        if not tasks:
            return "No background tasks are outstanding (none launched, or every report already retrieved)."
        return "\n".join(
            f"{task_id}: {info.desc} [{info.status}]"
            for (task_id, info) in tasks.items()
        )


class RetrieveTask(WithAsyncDependencies[str, TaskHost]):
    """
    Collect a background task's report by ID, waiting for the task to finish
    if it is still running. Each report can be retrieved exactly once.
    """
    task_id: str = Field(description="The task ID reported when the task was launched")

    @override
    async def run(self) -> str:
        with self.tool_deps() as host:
            try:
                return await host.await_result(self.task_id, str)
            except NoSuchTaskError:
                return (
                    f"No task with ID {self.task_id}: it never existed, or its "
                    "report was already retrieved (reports are single-shot); "
                    f"see `{TASK_LIST}`."
                )
