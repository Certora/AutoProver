import difflib
import pathlib
from typing import Callable

from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel

from .edit_store import EditStore
from composer.spec.source.versioned_index import (
    MigrationOracle,
    AnswerPortability,
    Stale,
    UpToDate,
)
from composer.spec.context import SourceCode


_ORACLE_SYSTEM = """\
You judge whether a previously recorded finding about smart-contract source code is still accurate after the source was edited.

You are given the finding (the original question and the answer that was recorded) and a unified diff from the source version the finding was written against to the current version.

Decide whether anything in the diff contradicts or outdates the answer. If every claim the answer makes still holds against the changed source, it is still valid. If the diff changes something the answer depends on — a moved or renamed symbol, altered control flow, a removed branch, a changed signature — it is no longer valid, and you say in one sentence what changed.

Edits are usually small and surgical, so most findings are unaffected; mark a finding invalid only when the diff actually touches what the answer relies on."""


class _PortabilityVerdict(BaseModel):
    still_holds: bool = Field(
        description="True if every claim in the prior answer is still accurate against "
        "the changed source; False if the diff makes any part of it wrong, stale, or misleading."
    )
    reason: str = Field(
        default="",
        description="When still_holds is False, one sentence naming the change that invalidates "
        "the answer. Left empty when it still holds.",
    )


def _file_diff(path: str, old: str | None, new: str) -> str:
    """Unified diff for a single file. A path absent from the old view (``old`` is
    ``None``) reads as an addition; identical content produces the empty string."""
    if old == new:
        return ""
    old_lines = (old or "").splitlines(keepends=True)
    new_lines = new.splitlines(keepends=True)
    from_label = f"a/{path}" if old is not None else "/dev/null"
    return "".join(
        difflib.unified_diff(old_lines, new_lines, fromfile=from_label, tofile=f"b/{path}")
    )


def _compute_diff(old: Callable[[str], str | None], new: dict[str, str]) -> str:
    """Diff the ``old`` view against the complete ``new`` VFS. There is no
    delete-file tool, so every path of interest is a key of ``new`` and iterating
    it is sufficient — a file missing from ``old`` is an addition, never a deletion."""
    chunks = (_file_diff(path, old(path), content) for path, content in new.items())
    return "".join(c for c in chunks if c)


def mk_oracle(
    llm: BaseChatModel,
    edit_store: EditStore,
    sc: SourceCode,
) -> MigrationOracle:
    """Build a :class:`MigrationOracle` that decides, in one LLM call, whether a
    recorded finding survives the edits between two source versions.

    The ``new`` side of the diff is always the complete VFS snapshot at
    ``end_version``. The ``old`` side is resolved lazily per path: from the
    ``start_version`` snapshot, or — for V0 (``start_version is None``) — from the
    base ``fs_layer`` on disk under ``sc.project_root``."""

    async def oracle(
        *,
        start_version: str | None,
        end_version: str,
        question: str,
        answer: str,
    ) -> AnswerPortability:
        new = await edit_store.read(end_version)
        assert new is not None, f"end version {end_version!r} absent from edit store"

        if start_version is None:
            root = pathlib.Path(sc.project_root)

            def old(path: str) -> str | None:
                try:
                    return (root / path).read_text(encoding="utf-8")
                except (OSError, UnicodeDecodeError):
                    return None
        else:
            start = await edit_store.read(start_version)

            def old(path: str) -> str | None:
                return start.get(path) if start is not None else None

        diff = _compute_diff(old, new)
        if not diff:
            # Nothing observable changed between the two views, so no edit we can
            # see could have invalidated the finding.
            return UpToDate(status="ok")

        user = (
            f"Prior finding recorded about the source code.\n\n"
            f"Question: {question}\n\n"
            f"Answer:\n{answer}\n\n"
            f"Unified diff from the version this finding was written against to the "
            f"current version:\n\n{diff}"
        )
        verdict = await llm.with_structured_output(_PortabilityVerdict).ainvoke(
            [SystemMessage(_ORACLE_SYSTEM), HumanMessage(user)]
        )
        assert isinstance(verdict, _PortabilityVerdict)

        if verdict.still_holds:
            return UpToDate(status="ok")
        return Stale(status="stale", reason=verdict.reason)

    return oracle
