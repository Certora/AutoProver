"""Turn a decided ``HarnessPlan`` into Solidity source.

Formatting only — every choice was made in ``plan.py``. The emitted contract holds the
library at arm's length rather than inheriting from it, because a library cannot be a
base contract.

The file carries a sentinel comment recording the plan hash. That, not the sidecar JSON,
is what makes regeneration idempotent: the file and its provenance cannot drift apart if
they travel together.
"""

import hashlib
import json
from typing import List, Sequence

from certora_autosetup.harnesser.model import (
    HarnessPlan,
    LibParam,
    StorageReader,
    Wrapper,
)
from certora_autosetup.utils.contract_linker import render_wrapper_contract

#: Marks a file as ours and records which plan produced it, so a re-run can tell an
#: up-to-date harness from a stale one without re-deriving the plan.
SENTINEL_PREFIX = "// certora-library-harness:"

#: The stub's placeholder function. A contract with no external functions is dropped by
#: contract discovery and by the signature database (both skip a contract with no
#: methods), so the probe build would not see the harness at all. It is removed once the
#: real wrappers are known.
STUB_FUNCTION_NAME = "certoraLibraryHarnessPlaceholder"


def _render_param(param: LibParam, include_name: bool) -> str:
    parts = [param.solidity_type]
    if param.location:
        parts.append(param.location)
    if include_name and param.name:
        parts.append(param.name)
    return " ".join(parts)


def _render_params(params: Sequence[LibParam]) -> str:
    return ", ".join(_render_param(p, include_name=True) for p in params)


def _render_returns(returns: Sequence[LibParam]) -> str:
    if not returns:
        return ""
    rendered = ", ".join(_render_param(r, include_name=False) for r in returns)
    return f" returns ({rendered})"


def _render_wrapper(wrapper: Wrapper, library_name: str) -> str:
    """Emit one public wrapper delegating to the library."""
    mutability = f" {wrapper.state_mutability}" if wrapper.state_mutability in ("view", "pure") else ""
    signature = (
        f"    function {wrapper.name}({_render_params(wrapper.params)}) "
        f"public{mutability}{_render_returns(wrapper.returns)} {{"
    )
    call = f"{library_name}.{wrapper.library_function}({', '.join(wrapper.call_args)});"

    if wrapper.returns_mutated_param:
        # The library mutates the argument in place and returns nothing; handing the
        # argument back is the only way the mutation is observable to a caller.
        body = [f"        {call}", f"        return {wrapper.returns_mutated_param};"]
    elif wrapper.returns:
        body = [f"        return {call}"]
    else:
        body = [f"        {call}"]

    return "\n".join([signature, *body, "    }"])


def _render_reader(reader: StorageReader) -> str:
    """Emit a getter over a member of an owned struct."""
    location = " memory" if reader.solidity_type.endswith("[]") else ""
    return "\n".join(
        [
            f"    function {reader.name}({_render_params(reader.params)}) "
            f"public view returns ({reader.solidity_type}{location}) {{",
            f"        return {reader.access_expression};",
            "    }",
        ]
    )


def plan_hash(plan: HarnessPlan) -> str:
    """Stable digest of everything that determines the emitted source."""
    payload = {
        "library": plan.library_name,
        "source": plan.library_source_file,
        "owned": [(v.var_name, v.solidity_type) for v in plan.owned_vars],
        "wrappers": [
            (
                w.name,
                w.library_function,
                [(p.solidity_type, p.location) for p in w.params],
                [(r.solidity_type, r.location) for r in w.returns],
                w.state_mutability,
                list(w.call_args),
                w.returns_mutated_param,
            )
            for w in plan.wrappers
        ],
        "readers": [(r.name, r.solidity_type, r.access_expression) for r in plan.readers],
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def sentinel_line(library_name: str, digest: str) -> str:
    return (
        f"{SENTINEL_PREFIX} "
        + json.dumps({"v": 1, "library": library_name, "plan_hash": digest}, separators=(",", ":"))
    )


def read_sentinel(source: str) -> dict | None:
    """Recover the provenance record from an existing harness file, if it is ours."""
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(SENTINEL_PREFIX):
            try:
                return json.loads(stripped[len(SENTINEL_PREFIX):].strip())
            except json.JSONDecodeError:
                return None
    return None


def render_stub(
    harness_name: str,
    library_name: str,
    pragma_line: str,
    import_lines: Sequence[str],
) -> str:
    """Emit the placeholder harness compiled by the probe build.

    It must import the library so the library lands in the same compilation unit and the
    build reports its full API, and it must declare one external function so contract
    discovery keeps it.
    """
    body = [
        f"    function {STUB_FUNCTION_NAME}() external pure returns (uint256) {{",
        "        return 42;",
        "    }",
    ]
    return render_wrapper_contract(
        harness_name=harness_name,
        parent_name=None,
        pragma_line=pragma_line,
        import_lines=list(import_lines),
        ctor_forward=None,
        body_blocks=body,
        header_comment_lines=[
            f"{SENTINEL_PREFIX} " + json.dumps({"v": 1, "library": library_name, "stub": True}, separators=(",", ":")),
            f"// Placeholder harness for library {library_name}; the probe build reports the",
            "// library's API and this file is then regenerated with one wrapper per function.",
        ],
    )


def render_harness(plan: HarnessPlan) -> str:
    """Emit the finished harness: owned state, wrappers, readers."""
    digest = plan_hash(plan)

    body: List[str] = []
    for var in plan.owned_vars:
        body.append(f"    {var.solidity_type} internal {var.var_name};")
    if plan.owned_vars:
        body.append("")

    for wrapper in plan.wrappers:
        body.append(_render_wrapper(wrapper, plan.library_name))
        body.append("")

    if plan.readers:
        body.append("    // Getters over the harness-owned storage, so a spec can relate the")
        body.append("    // library's API to the representation it maintains.")
        for reader in plan.readers:
            body.append(_render_reader(reader))
            body.append("")

    while body and body[-1] == "":
        body.pop()

    header = [
        sentinel_line(plan.library_name, digest),
        f"// Generated harness exposing library {plan.library_name} as a verifiable contract.",
        "// The Prover skips libraries when instantiating parametric rules, so the library's",
        "// functions are only reachable through a contract that calls them.",
    ]
    if plan.skipped:
        header.append(f"// {len(plan.skipped)} library function(s) could not be exposed; see the run report.")

    return render_wrapper_contract(
        harness_name=plan.harness_name,
        parent_name=None,
        pragma_line=plan.pragma_line,
        import_lines=list(plan.import_lines),
        ctor_forward=None,
        body_blocks=body,
        header_comment_lines=header,
        extra_pragma_lines=list(plan.extra_pragma_lines),
    )
