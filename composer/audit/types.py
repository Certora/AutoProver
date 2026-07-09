from typing import TypedDict

from composer.input.files import TextUploadable, Uploadable


class SpecRunEntry(TypedDict):
    """The spec file as surfaced to ``RunInput`` callers.

    ``vfs_path`` is the path at which the spec is materialized in the
    workflow's VFS (codegen's historical convention is ``rules.spec``)."""
    vfs_path: str
    basename: str
    contents: str


class RunInput(TypedDict):
    # Audit-restored file fields are ``Uploadable`` — not renderable content
    # blocks themselves; the executor rehydrates them through its
    # ``FileUploader`` into ``Document`` / ``TextDocument`` instances.
    spec: SpecRunEntry
    interface: TextUploadable
    system: Uploadable
    reqs: list[str] | None
