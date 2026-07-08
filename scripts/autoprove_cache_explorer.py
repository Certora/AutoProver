"""
Cache & Memory Explorer for the Auto-Prove pipeline.

Usage:
    # by run id (recommended — works even when the design doc was auto-discovered):
    python scripts/autoprove_cache_explorer.py run <run_id>

    # by reconstructing the namespace from the original inputs (requires the doc):
    python scripts/autoprove_cache_explorer.py inputs <project_root> <main_contract> <system_doc> --cache-ns <ns> [--memory-ns <ns>]
"""

import argparse
import asyncio
import pathlib
import sys
from typing import AsyncGenerator

_repo_root = str(pathlib.Path(__file__).parent.parent.absolute())
if _repo_root not in sys.path:
    sys.path.append(_repo_root)

from composer.input.types import DEFAULT_RECURSION_LIMIT
from composer.ui.cache_explorer import (
    CacheNode, OrgNode, CacheTreeNode, CacheExplorerApp, DummyServices,
    node, node_for, leaf, memory, collect_tree,
)
from composer.spec.context import WorkflowContext, CVLGeneration, CVLJudge, CacheKey
from composer.spec.source.harness import (
    config_key,
    system_setup_key,
    harness_generation_key,
    HARNESS_ANALYSIS_KEY,
    ContractSetup,
    SystemDescriptionHarnessed,
    AgentSystemDescription,
    HarnessResult,
)
from composer.pipeline.cli import root_cache_key, user_ns
from composer.core.user import get_uid
from composer.workflow.services import get_async_store
from composer.io.run_index import get_run_data
from langgraph.store.base import BaseStore
from composer.spec.source.summarizer import _summary_key, _SummaryCache
from composer.spec.source.struct_invariant import STRUCTURAL_INV_KEY, Invariants
from composer.pipeline.core import (
    COMMON_SYSTEM_CACHE_KEY, PROPERTIES_KEY, _component_cache_key, _batch_cache_key,
)
from composer.spec.source.pipeline import INV_CVL_KEY
from composer.spec.prop_inference import (
    _BugAnalysisCache, _AgentResult, _AgentRoundWithHistory,
    bug_analysis_key, agent_round_key, AGENT_RESULT_KEY,
)
from composer.spec.cvl_generation import GeneratedCVL, _LastAttemptCache, LAST_ATTEMPT_KEY, CVL_JUDGE_KEY
from composer.spec.system_model import (
    SourceApplication, SourceExplicitContract, SourceExternalActor,
    HarnessedApplication, HarnessedExplicitContract, HarnessDefinition,
    ContractInstance, ContractComponentInstance,
)


# The driver writes the analyzed SourceApplication under CacheKey(COMMON_SYSTEM_CACHE_KEY)
# (pipeline.core.run_pipeline); mirror that key here to read it back.
SYSTEM_ANALYSIS_KEY = CacheKey[None, SourceApplication](COMMON_SYSTEM_CACHE_KEY)


# ---------------------------------------------------------------------------
# Cache value type
# ---------------------------------------------------------------------------

type AutoProveCachedValue = (
    SourceApplication
    | ContractSetup
    | SystemDescriptionHarnessed
    | AgentSystemDescription
    | HarnessResult
    | _SummaryCache
    | Invariants
    | GeneratedCVL
    | _LastAttemptCache
    | _BugAnalysisCache
    | CVLJudge
)


# ---------------------------------------------------------------------------
# Tree construction
# ---------------------------------------------------------------------------

def _build_harnessed_app(
    sa: SourceApplication,
    config_val: ContractSetup | None,
) -> HarnessedApplication:
    """Reconstruct the HarnessedApplication the same way pipeline.py does."""
    contract_to_harness: dict[str, list[HarnessDefinition]] = {}
    if config_val is not None:
        for c in config_val.system_description.transitive_closure:
            if not c.harness_definition:
                continue
            contract_to_harness.setdefault(c.harness_definition.harness_of, []).append(
                HarnessDefinition(name=c.solidity_identifier, path=c.path)
            )

    comp: list[SourceExternalActor | HarnessedExplicitContract] = []
    for c in sa.components:
        if not isinstance(c, SourceExplicitContract):
            comp.append(c)
            continue
        comp.append(HarnessedExplicitContract(
            sort=c.sort,
            name=c.name,
            solidity_identifier=c.solidity_identifier,
            components=c.components,
            description=c.description,
            path=c.path,
            harnesses=contract_to_harness.get(c.solidity_identifier, []),
        ))

    return HarnessedApplication(
        application_type=sa.application_type,
        description=sa.description,
        components=comp,
    )


async def _build_cvl_gen_nodes(
    ctx: WorkflowContext[CVLGeneration],
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    yield memory(ctx, child=CVL_JUDGE_KEY, label="Feedback")
    yield await leaf(ctx, LAST_ATTEMPT_KEY, "Last Attempt", _LastAttemptCache)


async def _build_component_nodes(
    prop_ctx: WorkflowContext,
    feat: ContractComponentInstance,
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    comp_key = _component_cache_key(feat)
    async with node_for(prop_ctx, comp_key, feat.component.name) as feat_ctx:
        # Bug analysis is layered: aggregate (_BugAnalysisCache) → agent result
        # (_AgentResult) → per-round (_AgentRoundWithHistory). The threat model
        # isn't recoverable from CLI args, so probe the no-threat-model key for
        # both refinement variants and use whichever was written.
        bug_key = bug_analysis_key(None, with_refinement=False)
        for refine in (False, True):
            candidate = bug_analysis_key(None, with_refinement=refine)
            if await feat_ctx.child(candidate).cache_get(_BugAnalysisCache) is not None:
                bug_key = candidate
                break

        async with node_for(feat_ctx, bug_key, "Bug Analysis", _BugAnalysisCache) as bug_ctx:
            async with node_for(bug_ctx, AGENT_RESULT_KEY, "Agent result", _AgentResult) as agent_ctx:
                # Round indices are dense; probe 0..N until the first miss.
                i = 0
                while True:
                    round_node = await leaf(
                        agent_ctx, agent_round_key(i),
                        f"Round {i + 1}", _AgentRoundWithHistory,
                    )
                    if round_node.value is None:
                        break
                    yield round_node
                    i += 1

        bug_cache = await feat_ctx.child(bug_key).cache_get(_BugAnalysisCache)
        if bug_cache is None:
            return
        batch_key = _batch_cache_key(bug_cache.items)
        async with node_for(feat_ctx, batch_key, "CVL Generation", GeneratedCVL) as cvl_ctx:
            async for n in _build_cvl_gen_nodes(cvl_ctx.abstract(CVLGeneration)):
                yield n


async def build_tree_inner(
    root_ctx: WorkflowContext[None],
    contract_name: str,
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    sa_leaf = await leaf(root_ctx, SYSTEM_ANALYSIS_KEY, "system-analysis", SourceApplication)
    yield sa_leaf

    # Read config value upfront so we can derive the summary key outside the with block
    config_val = await root_ctx.child(config_key).cache_get(ContractSetup)

    async with node_for(root_ctx, config_key, "config", ContractSetup) as config_ctx:
        if sa_leaf.value is not None:
            async with node_for(config_ctx, system_setup_key(sa_leaf.value), "setup", SystemDescriptionHarnessed) as setup_ctx:
                ha_leaf = await leaf(setup_ctx, HARNESS_ANALYSIS_KEY, "harness-analysis", AgentSystemDescription)
                yield ha_leaf
                if ha_leaf.value is not None and ha_leaf.value.needs_harnessing():
                    yield await leaf(
                        setup_ctx,
                        harness_generation_key(ha_leaf.value),
                        "harness-generation",
                        HarnessResult,
                    )

    # Summary — key derivable only once ContractSetup is cached
    if config_val is not None:
        yield await leaf(root_ctx, _summary_key(config_val), "summary", _SummaryCache)

    yield await leaf(root_ctx, STRUCTURAL_INV_KEY, "structural-inv", Invariants)
    async with node_for(root_ctx, INV_CVL_KEY, "invariant-cvl", GeneratedCVL) as inv_cvl_ctx:
        gen_ctx = inv_cvl_ctx.abstract(CVLGeneration)
        yield await leaf(
            gen_ctx, LAST_ATTEMPT_KEY, "Last Attempt", _LastAttemptCache
        )
        yield memory(
            gen_ctx, child=CVL_JUDGE_KEY, label="Feedback"
        )

    # Properties — per-component bug analysis + CVL generation
    if sa_leaf.value is None:
        with node(OrgNode(label="properties (no source analysis)")):
            pass
        return

    harnessed_app = _build_harnessed_app(sa_leaf.value, config_val)

    # Find the main contract. The pipeline matches the entry point by
    # solidity_identifier (pipeline.core.main_instance), so the explorer does too.
    contract_ind = -1
    for i, c in enumerate(harnessed_app.contract_components):
        if c.solidity_identifier == contract_name:
            contract_ind = i
            break

    if contract_ind == -1:
        with node(OrgNode(label=f"properties (contract '{contract_name}' not found)")):
            pass
        return

    contract_instance = ContractInstance(contract_ind, app=harnessed_app)
    prop_ctx = root_ctx.child(PROPERTIES_KEY)

    with node(OrgNode(label="properties")):
        for comp_idx in range(len(contract_instance.contract.components)):
            feat = ContractComponentInstance(_contract=contract_instance, ind=comp_idx)
            async for n in _build_component_nodes(prop_ctx, feat):
                yield n


async def build_tree(root_ctx: WorkflowContext[None], contract_name: str) -> CacheNode[AutoProveCachedValue]:
    return await collect_tree("root", root_ctx, build_tree_inner(root_ctx, contract_name))


# ---------------------------------------------------------------------------
# Value formatting
# ---------------------------------------------------------------------------

def format_value(val: AutoProveCachedValue) -> list[str]:
    lines: list[str] = []

    match val:
        case SourceApplication(application_type=app_type, description=desc, components=comps):
            lines.append(f"Type: {app_type}")
            lines.append(f"Description: {desc}")
            lines.append("")
            for c in comps:
                match c:
                    case SourceExplicitContract(name=name, sort=sort, path=path, description=cdesc):
                        lines.append(f"[{sort}] {name}  ({path})")
                        lines.append(f"  {cdesc}")
                    case SourceExternalActor(name=name, path=path, description=cdesc):
                        loc = f"  ({path})" if path else ""
                        lines.append(f"[external] {name}{loc}")
                        lines.append(f"  {cdesc}")

        case ContractSetup(system_description=sys_desc, config=cfg):
            lines.append("Pre-audit setup: OK")
            lines.append(f"Summaries path: {cfg.summaries_path}")
            lines.append(f"User types: {len(cfg.user_types)}")
            lines.append(f"Closure contracts: {len(sys_desc.transitive_closure)}")

        case AgentSystemDescription(
            non_trivial_state=nts,
            erc20_contracts=erc20s,
            external_interfaces=ext_ifaces,
            transitive_closure=closure,
        ):
            lines.append(f"Non-trivial state: {nts}")
            lines.append(f"ERC20 contracts: {', '.join(erc20s) if erc20s else 'none'}")
            lines.append(f"Needs harnessing: {val.needs_harnessing()}")
            lines.append("")
            lines.append(f"Transitive closure ({len(closure)}):")
            for c in closure:
                instances = f"  x{c.num_instances}" if c.num_instances else ""
                lines.append(f"  {c.name}{instances}")
                for lf in c.link_fields:
                    lines.append(f"    links → {', '.join(lf.target)}")
            if ext_ifaces:
                lines.append("")
                lines.append(f"External interfaces ({len(ext_ifaces)}):")
                for ei in ext_ifaces:
                    lines.append(f"  {ei.name}: {ei.behavioral_spec}")

        case SystemDescriptionHarnessed(
            non_trivial_state=nts,
            erc20_contracts=erc20s,
            external_interfaces=ext_ifaces,
            transitive_closure=closure,
        ):
            lines.append(f"Non-trivial state: {nts}")
            lines.append(f"ERC20 contracts: {', '.join(erc20s) if erc20s else 'none'}")
            lines.append("")
            lines.append(f"Transitive closure ({len(closure)}):")
            for c in closure:
                harnessed = " [harnessed]" if c.harness_definition else ""
                lines.append(f"  {c.name}  ({c.path}){harnessed}")
                if c.harness_definition:
                    lines.append(f"    harness of: {c.harness_definition.harness_of}")
                for lf in c.link_fields:
                    lines.append(f"    links → {', '.join(lf.target)} via {", ".join(lf.link_paths)}")
            if ext_ifaces:
                lines.append("")
                lines.append(f"External interfaces ({len(ext_ifaces)}):")
                for ei in ext_ifaces:
                    lines.append(f"  {ei.name}: {ei.behavioral_spec}")

        case HarnessResult(name_to_source=name_to_source):
            for contract, harnesses in name_to_source.items():
                lines.append(f"{contract}:")
                for h in harnesses:
                    lines.append(f"  {h.harness_name}  →  {h.path}")

        case _SummaryCache(content=content):
            for line in content.splitlines()[:40]:
                lines.append(line)
            if len(content.splitlines()) > 40:
                lines.append(f"... ({len(content.splitlines()) - 40} more lines)")

        case Invariants(inv=invs):
            lines.append(f"Invariants ({len(invs)}):")
            for inv in invs:
                lines.append(f"  {inv.description}")

        case GeneratedCVL(commentary=commentary, cvl=cvl, skipped=skipped):
            lines.append(f"Commentary: {commentary}")
            if skipped:
                lines.append(f"Skipped: {len(skipped)}")
            lines.append("")
            for line in cvl.splitlines()[:40]:
                lines.append(line)
            if len(cvl.splitlines()) > 40:
                lines.append(f"... ({len(cvl.splitlines()) - 40} more lines)")

        case _BugAnalysisCache(items=items):
            lines.append(f"Properties ({len(items)}):")
            for p in items:
                methods = p.methods
                lines.append(f"  - [{p.sort}] {p.description}")
                if isinstance(methods, list):
                    lines.append(f"    methods: {', '.join(methods)}")
                else:
                    lines.append(f"    methods: {methods}")

        case _LastAttemptCache(cvl=cvl):
            lines.append("LAST ATTEMPT")
            lines.append(cvl)

    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

Resolved = tuple[tuple[str, ...], str | None, str]
"""``(cache_root, memory_ns, contract_name)`` — everything the explorer needs to
open a run's cache/memory, however it was resolved."""


def _resolve_from_inputs(args: argparse.Namespace) -> Resolved | None:
    """Reconstruct the namespaces from the original CLI inputs. Requires the design
    doc (it feeds the byte-hash root key), so it does not work for auto-discovered
    runs — use the ``run`` subcommand for those. Returns ``None`` if the doc is unreadable."""
    project_root = pathlib.Path(args.project_root).resolve()
    main_contract_path, contract_name = args.main_contract.split(":", 1)
    full_contract_path = pathlib.Path(main_contract_path).resolve()
    relative_path = str(full_contract_path.relative_to(project_root))

    sys_path = pathlib.Path(args.system_doc)
    if not sys_path.is_file():
        print(f"Error: cannot read {sys_path}")
        return None

    root_ns = user_ns(
        args.cache_ns,
        root_cache_key(str(project_root), sys_path, relative_path, contract_name),
    )
    memory_ns = args.memory_ns
    if memory_ns:
        memory_ns = get_uid() + "/" + memory_ns
    return root_ns, memory_ns, contract_name


async def _resolve_from_run(store: BaseStore, run_id: str, uid: str | None) -> Resolved | None:
    """Look up the namespaces the pipeline recorded for ``run_id`` — the
    ``data_logger("cache_root", ...)`` record written from autoprove_common /
    foundry entry once the design doc (hence cache root) is resolved. Returns
    ``None`` if the run isn't found or ran without caching."""
    rec = await get_run_data(store, run_id, "cache_root", uid=uid)
    if rec is None:
        print(f"Error: no cache metadata for run {run_id!r} (looked under uid={uid!r}).")
        return None
    raw_ns = rec.get("cache_root")
    if raw_ns is None:
        print(f"Error: run {run_id!r} ran without caching — nothing to explore.")
        return None
    return tuple(raw_ns), rec.get("memory_ns"), rec["contract_name"]


async def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cache & Memory Explorer for the Auto-Prove pipeline"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_run = sub.add_parser(
        "run",
        help="Explore a run by id (recommended). Reads the cache namespace the run "
             "recorded in its metadata — works even when the design doc was auto-discovered.",
    )
    p_run.add_argument("run_id", help="Run id (from the autoprove logs / ap-trail).")
    p_run.add_argument(
        "--uid", default=None,
        help="User-id namespace the run was logged under (default: the run's default namespace).",
    )

    p_inputs = sub.add_parser(
        "inputs",
        help="Reconstruct the cache namespace from the original CLI inputs. Requires "
             "the supplied design doc, so it does NOT work for auto-discovered runs.",
    )
    p_inputs.add_argument("project_root", help="Root directory of the Solidity project")
    p_inputs.add_argument("main_contract", help="Main contract as path:ContractName")
    p_inputs.add_argument("system_doc", help="Path to the design document (text or PDF)")
    p_inputs.add_argument("--cache-ns", required=True, dest="cache_ns",
                          help="Cache namespace (same as passed to autoprove)")
    p_inputs.add_argument("--memory-ns", dest="memory_ns", default=None,
                          help="Memory namespace (enables memory browsing)")

    args = parser.parse_args()

    store = await get_async_store()

    resolved = (
        await _resolve_from_run(store, args.run_id, args.uid)
        if args.mode == "run"
        else _resolve_from_inputs(args)
    )
    if resolved is None:
        return 1
    root_ns, memory_ns, contract_name = resolved

    print(f"Root namespace: {root_ns}")

    root_ctx: WorkflowContext[None] = WorkflowContext.create(
        services=DummyServices(),  # type: ignore[arg-type]
        thread_id="explorer",
        store=store,
        recursion_limit=DEFAULT_RECURSION_LIMIT,
        memory_namespace=memory_ns,
        cache_namespace=root_ns,
    )

    status = f"Cache NS: {root_ns}"
    if memory_ns:
        status += f"  |  Memory NS: {memory_ns}"

    app = CacheExplorerApp(
        build_tree=lambda: build_tree(root_ctx, contract_name),
        format_value=format_value,
        store=store,
        status=status,
    )
    await app.run_async()
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
