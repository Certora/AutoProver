import pathlib

from pydantic import BaseModel, Field

from langchain_core.messages import HumanMessage, SystemMessage
from langchain_core.language_models.chat_models import BaseChatModel

from .edit_store import EditStore
from .vfs_diff import compute_diff
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


def mk_oracle(
    llm: BaseChatModel,
    edit_store: EditStore,
    sc: SourceCode,
) -> MigrationOracle:
    """Build a :class:`MigrationOracle` that decides, in one LLM call, whether a
    recorded finding survives the edits between two source versions.

    The VFS is a union overlay over the base ``fs_layer``: a version snapshot holds
    only the files edited as of that version, and every other path reads through to
    ``sc.project_root`` on disk. So the ``new`` side is the ``end_version`` overlay,
    and ``old`` resolves each path as overlay-then-base — or, for V0
    (``start_version is None``), the base fs_layer alone."""

    async def oracle(
        *,
        start_version: str | None,
        end_version: str,
        question: str,
        answer: str,
    ) -> AnswerPortability:
        new = await edit_store.read(end_version)
        assert new is not None, f"end version {end_version!r} absent from edit store"

        root = pathlib.Path(sc.project_root)
        start = None if start_version is None else await edit_store.read(start_version)
        overlay = None if start is None else start.vfs

        def old(path: str) -> str | None:
            # Union FS: an edited path uses the overlay's content, everything else
            # reads through to the base fs_layer under project_root.
            if overlay is not None and path in overlay:
                return overlay[path]
            try:
                return (root / path).read_text(encoding="utf-8")
            except (OSError, UnicodeDecodeError):
                return None

        diff = compute_diff(old, new.vfs)
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
