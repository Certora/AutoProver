import pathlib

from .edit_store import EditStore
from .vfs_diff import changed_paths, fs_resolver
from composer.spec.source.versioned_index import (
    MigrationOracle,
    AnswerPortability,
    Stale,
    UpToDate,
)
from composer.spec.context import SourceCode


def mk_oracle(
    edit_store: EditStore,
    sc: SourceCode,
) -> MigrationOracle:
    """Build the conservative :class:`MigrationOracle`: a finding recorded on an
    older source version is stale whenever any file's content differs between the
    two views, with the changed files named in the reason. Whether a change
    actually invalidates the finding is deliberately left to the consumer — a
    diff alone cannot rule out behavior changes reaching a finding transitively
    (an edit to ``bar`` can change what ``foo`` does), and the consumer has both
    the source tools and the knowledge of which claims it is about to rely on.

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

        base = fs_resolver(pathlib.Path(sc.project_root))
        start = None if start_version is None else await edit_store.read(start_version)
        overlay = None if start is None else start.vfs

        def old(path: str) -> str | None:
            # Union FS: an edited path uses the overlay's content, everything else
            # reads through to the base fs_layer.
            if overlay is not None and path in overlay:
                return overlay[path]
            return base(path)

        changed = changed_paths(old, new.vfs)
        if not changed:
            return UpToDate(status="ok")
        return Stale(
            status="stale",
            reason=(
                "the source was edited after this answer was recorded "
                f"(changed files: {', '.join(changed)}); any claim that depends on "
                "their contents must be re-verified against the current source"
            ),
        )

    return oracle
