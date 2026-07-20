"""Cache & Memory Explorer for the Auto-Prove pipeline.

Browses the cache + memory namespaces produced by the ``cli_pipeline``
drivers (``tui-autoprove`` / ``console-autoprove`` / the foundry
entries). Wired as ``cache-autoprove`` in ``pyproject.toml``.

Usage::

    # by run id (recommended — recovers the cache root, memory namespace,
    # threat-model digest and plugin manifest from the run's ``cache_root``
    # tags; works even when the design doc was auto-discovered):
    cache-autoprove run <run_id>

    # by reconstructing the namespace from the original inputs (requires
    # the design doc, so it does NOT work for auto-discovered runs):
    cache-autoprove inputs <project_root> <main_contract> <system_doc> \\
        --cache-ns <ns> [--memory-ns <ns>] [--threat-model <path>] \\
        [--plugins <name> ...]
"""

import argparse
import asyncio
import json
import pathlib
import sys
from dataclasses import dataclass
from typing import AsyncGenerator

from langgraph.store.base import BaseStore

from composer.input.types import DEFAULT_RECURSION_LIMIT
from composer.input.files import file_digest
from composer.ui.cache_explorer import (
    CacheNode, StoreNode, CacheTreeNode, CacheExplorerApp, DummyServices,
    node, section, node_for, leaf, memory, collect_tree,
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
from composer.pipeline.core import (
    COMMON_SYSTEM_CACHE_KEY, PROPERTIES_KEY,
    _component_cache_key, _batch_cache_key, _pre_property_cache_key,
)
from composer.pipeline.plugins import installed_plugin_manifest, manifest_digest
from composer.pipeline.run_tags import AutoProveCacheTags, CACHE_ROOT_RECORD
from composer.core.user import get_uid
from composer.workflow.services import store_context
from composer.io.run_index import get_run_data
from composer.spec.source.summarizer import _summary_key, _SummaryCache
from composer.spec.source.struct_invariant import STRUCTURAL_INV_KEY, Invariants
from composer.spec.source.pipeline import INV_CVL_KEY
from composer.spec.prop_inference import (
    _BugAnalysisCache, _AgentResult, _AgentRoundWithHistory,
    bug_analysis_key_from_digest, agent_round_key, AGENT_RESULT_KEY,
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

@dataclass
class PluginCacheRaw:
    """Wrapper for raw values pulled from a plugin's pre-inference cache
    subtree. Plugins cache under their own keys with their own value
    shapes, so these can't be rehydrated into typed models — display the
    stored payload as-is."""
    payload: dict


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
    | _AgentResult
    | _AgentRoundWithHistory
    | CVLJudge
    | PluginCacheRaw
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


async def _enumerate_raw_subtree(
    store: BaseStore,
    ctx: WorkflowContext,
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    """Surface every store slot under ``ctx``'s cache namespace as raw
    ``StoreNode`` entries. Used for plugin pre-inference subtrees, whose
    keys and value shapes are plugin-private."""
    ns = ctx.cache_namespace
    assert ns is not None
    items = await store.asearch(ns, limit=10_000)
    if not items:
        yield StoreNode[AutoProveCachedValue](
            label="(no cached entries)", slot=(ns, "<empty>"), value=None,
        )
        return
    for item in items:
        rel = "/".join((*item.namespace[len(ns):], item.key))
        yield StoreNode[AutoProveCachedValue](
            label=rel,
            slot=(tuple(item.namespace), item.key),
            value=PluginCacheRaw(payload=item.value),
        )


async def _resolve_bug_key(
    feat_ctx: WorkflowContext,
    tags: AutoProveCacheTags,
) -> CacheKey:
    """The bug-analysis key is parameterized on the threat-model digest and
    the refinement (interactive) flag. Both come from the run tags; records
    written before the ``interactive`` tag existed leave it ``None``, in
    which case probe both variants and use whichever was written."""
    if tags.interactive is not None:
        return bug_analysis_key_from_digest(
            tags.threat_model_digest, with_refinement=tags.interactive,
        )
    for refine in (False, True):
        candidate = bug_analysis_key_from_digest(tags.threat_model_digest, refine)
        if await feat_ctx.child(candidate).cache_get(_BugAnalysisCache) is not None:
            return candidate
    return bug_analysis_key_from_digest(tags.threat_model_digest, with_refinement=False)


async def _build_cvl_gen_nodes(
    ctx: WorkflowContext[CVLGeneration],
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    yield memory(ctx, child=CVL_JUDGE_KEY, label="Feedback")
    yield await leaf(ctx, LAST_ATTEMPT_KEY, "Last Attempt", _LastAttemptCache)


async def _build_component_nodes(
    prop_ctx: WorkflowContext,
    feat: ContractComponentInstance,
    store: BaseStore,
    tags: AutoProveCacheTags,
) -> AsyncGenerator[CacheTreeNode[AutoProveCachedValue], None]:
    comp_key = _component_cache_key(feat, manifest_digest(tags.plugins))
    async with node_for(prop_ctx, comp_key, feat.component.name) as feat_ctx:
        # Per-plugin pre-inference namespaces are siblings of the component
        # key under PROPERTIES_KEY, but they're per-component work — surface
        # them inside the component's subtree.
        for plugin in tags.plugins:
            pre_ctx = prop_ctx.child(_pre_property_cache_key(feat, plugin))
            with node(CacheNode(label=f"Plugin pre-inference: {plugin}", ctx=pre_ctx)):
                async for n in _enumerate_raw_subtree(store, pre_ctx):
                    yield n

        # Bug analysis is layered: aggregate (_BugAnalysisCache) → agent result
        # (_AgentResult) → per-round (_AgentRoundWithHistory).
        bug_key = await _resolve_bug_key(feat_ctx, tags)
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
    store: BaseStore,
    tags: AutoProveCacheTags,
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
        async for n in _build_cvl_gen_nodes(inv_cvl_ctx.abstract(CVLGeneration)):
            yield n

    # Properties — per-component plugin pre-inference + bug analysis + CVL generation
    if sa_leaf.value is None:
        with section("properties (no source analysis)"):
            pass
        return

    harnessed_app = _build_harnessed_app(sa_leaf.value, config_val)

    # Find the main contract. The pipeline matches the entry point by
    # solidity_identifier (pipeline.core.main_instance), so the explorer does too.
    contract_ind = -1
    for i, c in enumerate(harnessed_app.contract_components):
        if c.solidity_identifier == tags.contract_name:
            contract_ind = i
            break

    if contract_ind == -1:
        with section(f"properties (contract '{tags.contract_name}' not found)"):
            pass
        return

    contract_instance = ContractInstance(contract_ind, app=harnessed_app)
    prop_ctx = root_ctx.child(PROPERTIES_KEY)

    with section("properties"):
        for comp_idx in range(len(contract_instance.contract.components)):
            feat = ContractComponentInstance(_contract=contract_instance, ind=comp_idx)
            async for n in _build_component_nodes(prop_ctx, feat, store, tags):
                yield n


async def build_tree(
    root_ctx: WorkflowContext[None],
    store: BaseStore,
    tags: AutoProveCacheTags,
) -> CacheNode[AutoProveCachedValue]:
    return await collect_tree("root", root_ctx, build_tree_inner(root_ctx, store, tags))


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
                lines.append(f"  {c.solidity_identifier}{instances}")
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
                lines.append(f"  {c.solidity_identifier}  ({c.path}){harnessed}")
                if c.harness_definition:
                    lines.append(f"    harness of: {c.harness_definition.harness_of}")
                for lf in c.link_fields:
                    lines.append(f"    links → {', '.join(lf.target)} via {", ".join(lf.link_paths)}")
            if ext_ifaces:
                lines.append("")
                lines.append(f"External interfaces ({len(ext_ifaces)}):")
                for ei in ext_ifaces:
                    lines.append(f"  {ei.name}: {ei.behavioral_spec}")

        case HarnessResult(identifier_to_source=identifier_to_source):
            for target, harnesses in identifier_to_source.items():
                lines.append(f"{target}:")
                for h in harnesses:
                    lines.append(f"  {h.harness_name}  →  {h.path}")
            for harnesses in identifier_to_source.values():
                for h in harnesses:
                    lines.append("")
                    lines.append(f"--- {h.harness_name} ({h.path}) ---")
                    lines.extend(h.source.splitlines())

        case _SummaryCache(content=content):
            lines.extend(content.splitlines())

        case Invariants(inv=invs):
            lines.append(f"Invariants ({len(invs)}):")
            for inv in invs:
                lines.append(f"  {inv.description}")

        case GeneratedCVL(commentary=commentary, cvl=cvl, skipped=skipped):
            lines.append(f"Commentary: {commentary}")
            if skipped:
                lines.append(f"Skipped: {len(skipped)}")
            lines.append("")
            lines.extend(cvl.splitlines())

        # Subclass-of-_BugAnalysisCache cases first — match order matters.
        case _AgentRoundWithHistory(items=items, reasoning=reasoning, agent_conversation=history):
            lines.append(f"Properties this round ({len(items)}):")
            for p in items:
                lines.append(f"  - [{p.sort}] {p.description}")
            lines.append("")
            lines.append("--- Reasoning ---")
            lines.append(reasoning)
            lines.append("")
            lines.append(f"Agent history: {len(history)} message(s)")

        case _AgentResult(items=items, final_history=history):
            lines.append(f"Cumulative properties ({len(items)}):")
            for p in items:
                lines.append(f"  - [{p.sort}] {p.description}")
            lines.append("")
            lines.append(f"Final-round history: {len(history)} message(s)")

        case _BugAnalysisCache(items=items):
            lines.append(f"Properties ({len(items)}):")
            for p in items:
                lines.append(f"  - [{p.sort}] {p.description}")

        case _LastAttemptCache(cvl=cvl):
            lines.append("LAST ATTEMPT")
            lines.append(cvl)

        case PluginCacheRaw(payload=payload):
            lines.extend(json.dumps(payload, indent=2, default=str).splitlines())

        case CVLJudge():
            ...

    return lines


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _resolve_from_inputs(args: argparse.Namespace) -> AutoProveCacheTags | None:
    """Reconstruct the tags from the original CLI inputs. Requires the design
    doc (it feeds the byte-hash root key), so it does not work for auto-discovered
    runs — use the ``run`` subcommand for those. Returns ``None`` if the doc is
    unreadable."""
    project_root = pathlib.Path(args.project_root).resolve()
    main_contract_path, contract_name = args.main_contract.split(":", 1)
    full_contract_path = pathlib.Path(main_contract_path).resolve()
    relative_path = str(full_contract_path.relative_to(project_root))

    sys_path = pathlib.Path(args.system_doc)
    if not sys_path.is_file():
        print(f"Error: cannot read {sys_path}", file=sys.stderr)
        return None

    root_ns = user_ns(
        args.cache_ns,
        root_cache_key(str(project_root), sys_path, relative_path, contract_name),
    )
    memory_ns = args.memory_ns
    if memory_ns:
        memory_ns = get_uid() + "/" + memory_ns

    plugins: list[str] = (
        sorted(args.plugins) if args.plugins is not None else installed_plugin_manifest()
    )
    tm_digest = (
        file_digest(pathlib.Path(args.threat_model))
        if args.threat_model is not None else None
    )

    return AutoProveCacheTags(
        cache_root=list(root_ns),
        contract_name=contract_name,
        memory_ns=memory_ns,
        plugins=plugins,
        threat_model_digest=tm_digest,
        # Not recoverable from the inputs — the tree builder probes both
        # refinement variants of the bug-analysis key.
        interactive=None,
    )


async def _resolve_from_run(
    store: BaseStore, run_id: str, uid: str | None,
) -> AutoProveCacheTags | None:
    """Look up the tags the pipeline recorded for ``run_id`` — the
    ``cache_root`` run-data record written from ``cli_pipeline`` once the
    design doc (hence cache root) is resolved. Returns ``None`` if the run
    isn't found."""
    rec = await get_run_data(store, run_id, CACHE_ROOT_RECORD, uid=uid)
    if rec is None:
        print(
            f"Error: no cache metadata for run {run_id!r} (looked under uid={uid!r}).",
            file=sys.stderr,
        )
        return None
    return AutoProveCacheTags.model_validate(rec)


async def _async_main(args: argparse.Namespace) -> int:
    async with store_context() as store:
        tags = (
            await _resolve_from_run(store, args.run_id, args.uid)
            if args.mode == "run"
            else _resolve_from_inputs(args)
        )
        if tags is None:
            return 1
        if tags.cache_root is None:
            print(
                "Error: the run ran without caching — nothing to explore.",
                file=sys.stderr,
            )
            return 1
        root_ns = tuple(tags.cache_root)

        print(f"Root namespace: {root_ns}", file=sys.stderr)

        root_ctx: WorkflowContext[None] = WorkflowContext.create(
            services=DummyServices(),  # type: ignore[arg-type]
            thread_id="explorer",
            store=store,
            recursion_limit=DEFAULT_RECURSION_LIMIT,
            memory_namespace=tags.memory_ns,
            cache_namespace=root_ns,
        )

        status = f"Cache NS: {root_ns}"
        if tags.memory_ns:
            status += f"  |  Memory NS: {tags.memory_ns}"
        if tags.plugins:
            status += f"  |  Plugins: {', '.join(tags.plugins)}"
        if tags.threat_model_digest:
            status += f"  |  TM digest: {tags.threat_model_digest}"

        app = CacheExplorerApp(
            build_tree=lambda: build_tree(root_ctx, store, tags),
            format_value=format_value,
            store=store,
            status=status,
        )
        await app.run_async()
        return 0


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Cache & Memory Explorer for the Auto-Prove pipeline"
    )
    sub = parser.add_subparsers(dest="mode", required=True)

    p_run = sub.add_parser(
        "run",
        help="Explore a run by id (recommended). Reads the tags the run recorded "
             "in its metadata (cache root, memory ns, plugins, threat-model digest) "
             "— works even when the design doc was auto-discovered.",
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
    p_inputs.add_argument("--threat-model", dest="threat_model", default=None,
                          help="Path to the threat model used for the original run — its "
                               "digest parameterizes the bug-analysis cache key. Omit for "
                               "runs without one.")
    p_inputs.add_argument("--plugins", dest="plugins", nargs="*", default=None,
                          help="Plugin names active for the original run — the manifest "
                               "digest is suffixed onto per-component cache keys. Defaults "
                               "to the plugins installed in this environment; pass with no "
                               "names to force plugin-free keys.")

    args = parser.parse_args()
    return asyncio.run(_async_main(args))


if __name__ == "__main__":
    sys.exit(main())
