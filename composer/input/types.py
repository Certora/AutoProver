from typing import Optional, Protocol, Literal, Any, Annotated
from composer.rag.db import DEFAULT_CONNECTION as RAGDB_DEFAULT_CONNECTION
import pathlib
from dataclasses import dataclass

from composer.input.files import Document, TextDocument
DEFAULT_RECURSION_LIMIT = 1000

@dataclass
class BasicArg:
    help: str

@dataclass
class Arg(BasicArg):
    default: Any | None
    feature_flag: tuple[Any, Any] | None = None

@dataclass
class OptionalArg(BasicArg):
    pass


class InMemoryFile:
    def __init__(self, name: str, contents: str | bytes):
        self.basname = name
        self.bytes_contents = contents if isinstance(contents, bytes) else contents.encode("utf-8")

class NativeFS:
    """Filesystem-path-backed ``Uploadable``. Lazily reads bytes/text off disk;
    ``string_contents`` is ``None`` for non-UTF-8 (binary) files so a PDF system
    doc round-trips on resume instead of blowing up on ``read_text``."""

    def __init__(self, p: pathlib.Path):
        self.where = p

    @property
    def bytes_contents(self) -> bytes:
        return self.where.read_bytes()

    @property
    def basename(self) -> str:
        return self.where.name

    @property
    def string_contents(self) -> str | None:
        try:
            return self.where.read_text()
        except UnicodeDecodeError:
            return None


class TextNativeFS(NativeFS):
    """A ``NativeFS`` known to be text (spec / interface): ``string_contents``
    is guaranteed non-None, so it satisfies ``TextUploadable``."""

    @property
    def string_contents(self) -> str:
        return self.where.read_text()

class RAGDBOptions(Protocol):
    # database options
    rag_db: Annotated[str, Arg(
        help="Database connection string for CVL manual search",
        default=RAGDB_DEFAULT_CONNECTION
    )]

class LanggraphOptions(Protocol):
    checkpoint_id: Annotated[Optional[str], OptionalArg(help="The checkpoint id to resume a workflow from")]
    thread_id: Annotated[Optional[str], OptionalArg(help="The checkpoint id to resume a workflow from")]
    recursion_limit: Annotated[int, Arg(
        help="The number of iterations of the graph to allow (default: {default})",
        default=DEFAULT_RECURSION_LIMIT
    )]


class WorkflowOptions(RAGDBOptions, LanggraphOptions, Protocol):
    prover_capture_output: bool
    prover_keep_folders: bool
    local_prover: bool

    debug_prompt_override: Optional[str]

    skip_reqs: bool

class ModelConfiguration(Protocol):
    @property
    def tokens(self) -> int: ...
    @property
    def thinking_tokens(self) -> int | None: ...
    @property
    def memory_tool(self) -> bool: ...

    @property
    def interleaved_thinking(self) -> bool: ...

class TieredModelOptions(ModelConfiguration, Protocol):
    @property
    def lite_model(self) -> str:
        ...

    @property
    def heavy_model(self) -> str:
        ...

class ModelOptionsBase(ModelConfiguration, Protocol):
    """Read-only view of model options. thinking_tokens may be None to disable thinking."""
    @property
    def model(self) -> str: ...

_DEFAULT_TOKEN_COUNTS = 128_000

class _ModelOptionsCommon(Protocol):
    tokens: Annotated[int, Arg(
        help="Token budget for code generation (default: {default})",
        default=_DEFAULT_TOKEN_COUNTS
    )]
    thinking_tokens: Annotated[int, Arg(
        help="Token budget for thinking (default: {default})",
        default=2048
    )]
    memory_tool: Annotated[bool, Arg(
        help="Enable Anthropic's memory tool",
        default=None,
        feature_flag=("memory", True) # default to use if this is not exposed on command line
    )]
    interleaved_thinking: Annotated[bool, Arg(
        help="Enable interleaved thinking mode (default: {default})",
        default=False
    )]

class ModelOptions(_ModelOptionsCommon, Protocol):
    model: Annotated[str, Arg(
        help="Model to use for code generation (default: {default})", default="claude-opus-4-6"
        )]

class ExtendedModelOptions(_ModelOptionsCommon, Protocol):
    heavy_model: Annotated[str, Arg(
        help="Model to use for complex tasks (default: {default})", default="claude-opus-4-6"
    )]
    lite_model: Annotated[str, Arg(
        help="Model to use for simpler tasks(default: {default})", default="claude-sonnet-4-6"
    )]

class UploadPaths(Protocol):
    spec_file: str
    interface_file: str
    system_doc: str


class CommandLineArgs(WorkflowOptions, ModelOptions, UploadPaths, Protocol):
    debug_fs: str

    debug: bool

class ResumeArgs(WorkflowOptions, ModelOptions, Protocol):
    # common
    src_thread_id: str
    command: Literal["materialize", "resume-dir", "resume-id"]

    # materialize
    target: str

    # common resume
    commentary: Optional[str]
    updated_system: Optional[str]

    # resume-id
    new_spec: str

    # resume-fs
    working_dir: str


@dataclass
class SpecInput:
    """A single spec file paired with the VFS path at which it should be
    materialized inside the workflow's virtual filesystem."""
    file: TextDocument
    vfs_path: str


@dataclass
class InputData:
    """Normalized codegen workflow input for a single contract task.

    Carries one or more specs (all describing the same contract), the
    contract's interface, and the surrounding system document. Specs and
    interface are guaranteed text; system_doc may be PDF or text. Each spec's
    VFS path is resolved at load time so downstream code (executor, prover
    tool, audit) can key off a stable location.
    """
    specs: list[SpecInput]
    system_doc: Document
    intf: TextDocument

    @property
    def spec_vfs_paths(self) -> list[str]:
        return [s.vfs_path for s in self.specs]


class ResumeInput(Protocol):
    @property
    def comments(self) -> Optional[str]:
        ...

    @property
    def new_system(self) -> Optional[NativeFS]:
        ...

    @property
    def thread_id(self) -> str:
        ...

@dataclass
class ResumeIdData:
    thread_id: str
    # VFS path → new spec content. Only paths present here are updated on
    # resume; other specs keep their prior state. Single-spec CLI resumes
    # carry one entry.
    new_specs: dict[str, TextNativeFS]
    comments: Optional[str]
    new_system: Optional[NativeFS]

@dataclass
class ResumeFSData:
    thread_id: str
    file_path: str
    comments: Optional[str]
    new_system: Optional[NativeFS]
