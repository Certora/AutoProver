from dataclasses import dataclass, field
import hashlib

from graphcore.tools.vfs import VFSAccessor

from composer.chassis.validation import completion_validations
from composer.core.state import AIComposerState
from composer.prover.core import DEFAULT_GLOBAL_TIMEOUT

@dataclass
class ProverOptions:
    capture_output: bool
    keep_folder: bool
    extra_args: list[str] = field(default_factory=list)

    @property
    def cloud(self) -> bool:
        return "--server" in self.extra_args

    @property
    def global_timeout(self) -> float:
        if "--global_timeout" not in self.extra_args:
            return DEFAULT_GLOBAL_TIMEOUT
        idx = self.extra_args.index("--global_timeout")
        return float(self.extra_args[idx + 1])

@dataclass
class AIComposerContext:
    # Genuinely graph-cross-cutting runtime state only. Prover-specific deps
    # (CEX handler, prover options) ride ``ProverDeps`` on the prover tool;
    # ``rag_db`` is injected directly into the CVL tools; the required
    # validations now live in the state — none belong here.
    vfs_materializer: VFSAccessor[AIComposerState]

def compute_state_digest(state: AIComposerState) -> str:
    # Digest the VFS overlay only — the agent-authored / dirty files. NOT the
    # materialized tree: a source-root run's fs_layer underlay (OZ deps, etc.)
    # is immutable for the run, so re-hashing it on every prover stamp is pure
    # waste. The state a validation stamp cares about lives in the VFS overlay.
    digester = hashlib.md5()
    for (_, cont) in sorted(state["vfs"].items(), key=lambda x: x[0]):
        digester.update(cont.encode("utf-8"))
    return digester.hexdigest()


# Codegen's completion-validation trio, wired from the chassis over the codegen
# state + digester. ``stamp`` is used by the prover / requirements judge to mark
# a gate satisfied; ``check_completion`` gates the result tool. The introspection
# tool is unused for now. ``refl`` is the identity (Python can't express the
# ``T: ValidationState[K]`` bound directly).
stamp, check_completion, _ = completion_validations(
    AIComposerState, compute_state_digest, lambda x: x
)
