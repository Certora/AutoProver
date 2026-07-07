from langchain_core.tools import BaseTool

from graphcore.tools.vfs import vfs_tools, VFSAccessor, VFSToolConfig, VFSState

from composer.core.state import AIComposerState


def get_memory_ns(thread_id: str, ns: str) -> str:
    return f"ai-composer-{thread_id}-{ns}"


def get_vfs_tools(
    fs_layer: str | None,
    immutable: bool
) -> tuple[list[BaseTool], VFSAccessor[VFSState]]:
    if immutable:
        return vfs_tools(VFSToolConfig(
            fs_layer=fs_layer,
            immutable=True
        ), VFSState)
    else:
        return vfs_tools(VFSToolConfig(
            fs_layer=fs_layer,
            immutable=False,
            forbidden_write="^rules.spec$",
            put_doc_extra= \
    """
    By convention, every Solidity file placed into the virtual filesystem should contain exactly one contract/interface/library definitions.
    Further, the name of the contract/interface/library defined in that file should name the name of the solidity source file sans extension.
    For example, src/MyContract.sol should contain an interface/library/contract called `MyContract`"

    IMPORTANT: You may not use this tool to update the specification, nor should you attempt to
    add new specification files.
    """
        ), AIComposerState)
