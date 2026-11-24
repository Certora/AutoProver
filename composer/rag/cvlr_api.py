"""The header layout of ``cvlr_api_kb``, which its producer writes and its tools read.

Kept apart from both so the producer (:mod:`composer.scripts.cvlr_api_docs`) does not import the
search tools, and with them the database and the embedding stack.
"""

from collections.abc import Sequence
from dataclasses import dataclass

#: The root of every header path in this corpus. Present in the path so a row read out of context
#: still says which corpus it came from.
API_ROOT = "CVLR API"

#: The leaf a crate's complete-surface section is filed under.
SURFACE_HEADING = "Complete public surface"


@dataclass(frozen=True)
class ApiPath:
    """``[API_ROOT, crate, version, leaf]``: one row's place in the corpus.

    ``crate`` is the *defining* crate in cargo's spelling, ``version`` the release the row was
    generated from, and ``leaf`` an item's name (qualified only where the bare name is not unique
    in its crate) or :data:`SURFACE_HEADING`.
    """

    crate: str
    version: str
    leaf: str

    def headers(self) -> list[str]:
        return [API_ROOT, self.crate, self.version, self.leaf]

    @staticmethod
    def parse(headers: Sequence[str]) -> "ApiPath | None":
        """``None`` for a header path that is not one of this corpus's rows."""
        if len(headers) != 4 or headers[0] != API_ROOT:
            return None
        _, crate, version, leaf = headers
        return ApiPath(crate, version, leaf)

    @property
    def is_surface(self) -> bool:
        return self.leaf == SURFACE_HEADING

    def names(self, identifier: str) -> bool:
        """Whether this row is the entry for ``identifier``.

        A leaf is the bare name, the name qualified by its kind (``cvlr_log (macro)``), or a path
        ending in the name (``cvlr_log::cvlr_log``, ``NativeIntU64::u64_max``).
        """
        return (
            self.leaf == identifier
            or self.leaf.startswith(f"{identifier} (")
            or self.leaf.endswith(f"::{identifier}")
        )
