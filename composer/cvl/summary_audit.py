"""
Audit for view-class summaries on payable methods.

A view-class summary (``NONDET``, ``CONSTANT``, ``PER_CALLEE_CONSTANT``,
``ALWAYS``) replaces the callee with an approximation that has no state
effects. On a payable method that erases the callee's ETH accounting, so a
caller that compensates after the call (e.g. checks its balance decreased by
the sent value) reverts on every path and all of its rules pass vacuously.

The audit walks the typed CVL AST — never the surface text — and checks the
summarized methods' mutability against the method inventory the AutoSetup
build already wrote (``.certora_internal/all_methods.json``). If the inventory
is missing the audit is a no-op: the prompt-level rule still applies, and a
missing artifact must not block spec generation.
"""
import json
import logging
from pathlib import Path
from typing import Mapping

from composer.cvl.schema import (
    AlwaysSummary,
    CallSummary,
    CatchAllSummary,
    CVLFile,
    HavocingSummary,
    ImportedFunction,
    KeywordSummary,
    MethodsBlock,
)

_logger = logging.getLogger(__name__)

# Domain aliases: method inventories key methods by (contract, method).
type ContractName = str
type MethodName = str

# Written by AutoSetup's compilation analysis, relative to the project root.
_ALL_METHODS_JSON = Path(".certora_internal/all_methods.json")

_PAYABLE = "payable"


def load_payable_methods(project_root: Path) -> Mapping[ContractName, frozenset[MethodName]] | None:
    """Payable external/public methods per contract, or None when the
    inventory artifact is absent or unreadable (audit then degrades to no-op)."""
    path = project_root / _ALL_METHODS_JSON
    try:
        entries = json.loads(path.read_text())
    except (OSError, ValueError):
        _logger.warning("Method inventory %s unavailable; payable-summary audit skipped", path)
        return None
    payable: dict[ContractName, set[MethodName]] = {}
    for entry in entries:
        if entry.get("stateMutability") == _PAYABLE and entry.get("visibility") in ("external", "public"):
            payable.setdefault(entry.get("contractName", ""), set()).add(entry.get("name", ""))
    return {contract: frozenset(names) for contract, names in payable.items()}


def _is_view_class(summary: CallSummary) -> bool:
    match summary:
        case HavocingSummary(havoc_keyword="nondet"):
            return True
        case KeywordSummary() | AlwaysSummary():
            return True
        case _:
            # Other havoc keywords stay conservative about state/ETH effects;
            # expression summaries model effects explicitly; dispatchers inline
            # real implementations. None of these erase ETH accounting.
            return False


def _payable_matches(
    payable: Mapping[ContractName, frozenset[MethodName]],
    contract: ContractName | None,
    method: MethodName,
    current_contract: ContractName,
) -> list[str]:
    """Qualified names of payable methods a summary on contract.method covers.
    ``contract`` follows MethodReference: None = current contract, "_" = wildcard."""
    if contract == "_":
        return [f"{c}.{method}" for c, names in payable.items() if method in names]
    host = current_contract if contract is None else contract
    return [f"{host}.{method}"] if method in payable.get(host, frozenset()) else []


def view_summary_violations(
    cvl: CVLFile,
    payable: Mapping[ContractName, frozenset[MethodName]],
    current_contract: ContractName,
) -> list[str]:
    """One message per view-class summary that covers a payable method."""
    violations: list[str] = []
    for block in cvl.blocks:
        if not isinstance(block, MethodsBlock):
            continue
        for entry in block.method_entries:
            match entry:
                case ImportedFunction(summary=summary, signature=sig) if summary is not None and _is_view_class(summary):
                    hits = _payable_matches(
                        payable, sig.method_ref.contract, sig.method_ref.method_name, current_contract
                    )
                    violations.extend(
                        f"View-class summary on payable method {hit}: it erases the callee's ETH "
                        "effects, so callers that account for the transferred value revert on every "
                        "path and their rules pass vacuously. Use an expression summary over ghost "
                        "state, or a havoc summary."
                        for hit in hits
                    )
                case CatchAllSummary(contract_name=contract, summary=summary) if _is_view_class(summary):
                    covered = sorted(payable.get(contract, frozenset()))
                    if covered:
                        violations.append(
                            f"Catch-all view-class summary on {contract}._ also covers its payable "
                            f"method(s) {', '.join(covered)}; summarize those separately with an "
                            "expression or havoc summary."
                        )
                case _:
                    pass
    return violations
