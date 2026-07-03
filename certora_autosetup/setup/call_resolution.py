#!/usr/bin/env python3
"""
Call Resolution Phase - Iterative linking and dispatching until convergence.

This phase implements the core iterative loop for resolving contract dependencies
and function calls. It runs the prover with callResolutionOnly, then splits
unresolved calls into storage-path calls (handled by linking) and non-storage
calls (handled by dispatching).
"""

import asyncio
import enum
import sys
import time
import traceback
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Set, Tuple

from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.paths import user_call_resolution_spec_path

# Add project root to path for imports (like setup_prover.py)
project_root = Path(__file__).parent.parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from prover_output_utility.models import CallResolutionInfo  # type: ignore

from certora_autosetup.setup.proxy_detection import (
    ImplementationContract,
    ProxyDetector,
    ProxyMapping,
)
from certora_autosetup.setup.setup_summaries import SummarySetup
from certora_autosetup.setup.signature_types import extract_sighash_from_callee
from certora_autosetup.utils.llm_util import ledger_component
from certora_autosetup.utils.contract_dispatcher import (
    ContractDispatcher,
    DispatchingResult,
)
from certora_autosetup.utils.contract_linker import ContractLinker, LinkStatus
from certora_autosetup.utils.enhanced_config_manager import (
    ConfigManager,
    FileContent,
    ProverJobSpec,
)

# PreAudit imports
from certora_autosetup.utils.prover_runner import ProverRunner
from certora_autosetup.utils.scope import Scope
from certora_autosetup.utils.types import ContractHandle


class ContractSource(enum.StrEnum):
    """How a contract joined the verification scene during call resolution."""
    LINK = "link"
    DISPATCH = "dispatch"
    PROXY_IMPL = "proxy_impl"
    LIBRARY = "library"
    HARNESS = "harness"


@dataclass
class ContractProvenance:
    """Provenance of a contract that joined the verification scene during call resolution."""
    handle: ContractHandle
    source: ContractSource
    iteration: int
    # For harnesses: the contract that was harnessed. For libraries: the contract that pulled
    # this library in. ``None`` for sources where there is no back-reference (link, dispatch,
    # proxy_impl).
    parent: Optional[ContractHandle] = None


@dataclass
class ProxyScanRecord:
    """Result of a single proxy-detection scan over a contract during call resolution.

    Recorded for every scanned contract, not just proxy hits, so the report can show
    scan coverage. ``search_failed`` distinguishes detector errors from clean
    "not-a-proxy" verdicts; ``proxy_pattern`` is ``None`` in both of those cases.
    """
    contract: ContractHandle
    iteration: int
    is_proxy: bool
    proxy_pattern: Optional[str]
    implementations: List[ImplementationContract] = field(default_factory=list)
    search_failed: bool = False
    error_message: Optional[str] = None


@dataclass
class LinkingResult:
    """Result from a linking iteration."""

    new_contracts_found: Set[str]
    links: List[str]
    errors: List[str]
    success: bool


@dataclass
class IterationResult:
    """Data changes from a complete iteration (linking + dispatching)."""

    linking_result: Optional[LinkingResult]
    dispatching_result: Optional[DispatchingResult]

    @property
    def success(self) -> bool:
        linking_success = self.linking_result.success if self.linking_result else False
        dispatching_success = self.dispatching_result.success if self.dispatching_result else False
        return linking_success and dispatching_success

    @property
    def added_links(self) -> List[str]:
        return self.linking_result.links if self.linking_result else []

    @property
    def dispatchers_added(self) -> int:
        if self.dispatching_result:
            return self.dispatching_result.signatures_added
        return 0


class CallResolutionPhase:
    """
    Iterative call resolution:
    1. Run prover with callResolutionOnly to detect unresolved calls
    2. Split: storage-path calls -> linking (CVL links block), others -> dispatching
    3. Repeat until convergence
    """

    def __init__(
        self,
        scope: Scope,
        prover_runner: ProverRunner,
        config_manager: ConfigManager,
        config_file: Path,
        reports_dir: Path,
        extra_args: List[str],
        summary_setup: SummarySetup,
        library_resolver: Callable[[List[str]], List[ContractHandle]],
        is_library: Callable[[str], bool],
        max_prover_invocations: int = 10,
        verbose: bool = False,
        skip_proxy_detection: bool = False,
        skip_harnessing: bool = False,
    ):
        self.scope = scope
        self.prover_runner = prover_runner
        self.config_manager = config_manager
        self.config_file = config_file
        self.reports_dir = reports_dir
        self.extra_args = extra_args

        config_info = self.config_manager.extract_contract_and_spec_from_config(
            config_file, scope.project_root
        )
        if config_info is None:
            raise ValueError(f"Could not extract contract name from config: {config_file}")

        self.contract_name, _ = config_info

        self.contract_linker = ContractLinker(self.scope, skip_harnessing=skip_harnessing)
        self.contract_dispatcher = ContractDispatcher(self.scope, config_manager=config_manager)
        self.current_iteration = 0
        self._last_unresolved_calls: List[CallResolutionInfo] = []

        # Report state: tracks every contract added to the scene with its provenance,
        # every proxy-detection scan result (hits and misses), and the prover job URL
        # for each iteration (None for local runs that don't produce a cloud URL).
        self._contract_provenance: Dict[ContractHandle, ContractProvenance] = {}
        self._proxy_scan_results: List[ProxyScanRecord] = []
        self._prover_runs: List[Tuple[int, Optional[str]]] = []

        # Discovered implementations are added to config files alongside linker/dispatcher
        # additions; proxy_mappings is kept only to rebuild contract_extensions on each detection.
        self.proxy_detector: Optional[ProxyDetector] = (
            ProxyDetector(scope.project_root, verbose=verbose) if not skip_proxy_detection else None
        )
        self.proxy_mappings: List[ProxyMapping] = []
        self._already_scanned_for_proxy: Set[ContractHandle] = set()
        self._proxy_state_lock = asyncio.Lock()

        # Lazy-LLM analysis and library-on-demand attachment. ``is_library`` lets us
        # skip libraries in analyses that don't apply to them (e.g. proxy detection).
        self.summary_setup = summary_setup
        self.library_resolver = library_resolver
        self.is_library = is_library

    def _record_contract(
        self,
        handle: ContractHandle,
        source: ContractSource,
        parent: Optional[ContractHandle] = None,
    ) -> None:
        """Record provenance for a contract added to the scene. First source wins."""
        if handle in self._contract_provenance:
            return
        self._contract_provenance[handle] = ContractProvenance(
            handle=handle, source=source, iteration=self.current_iteration, parent=parent,
        )

    async def _on_contracts_added(
        self, handles: Iterable[ContractHandle], source: ContractSource
    ) -> None:
        """Hook called after contracts join the scene during call resolution.

        Records provenance, adds the library files the new contracts use to the config, then
        summarizes the batch via ``summary_setup.on_contracts_entered_scene`` (curated matching,
        LLM analysis, aggregator update, prune). Each step is best-effort: a failure is logged
        and doesn't block the others.

        ``source`` labels the provenance of the primary handles for the report
        (LINK, DISPATCH, PROXY_IMPL); harness wrappers are detected by
        consulting the linker's harness registry and labeled HARNESS.
        """
        handles_list = list(handles)
        names = [h.contract_name for h in handles_list]
        if not names:
            return

        # Record provenance for the primary handles (harnesses get their own source).
        for h in handles_list:
            if h.contract_name in self.contract_linker._harness_contracts:
                parent_handle, _ = self.contract_linker._harness_contracts[h.contract_name]
                self._record_contract(h, source=ContractSource.HARNESS, parent=parent_handle)
            else:
                self._record_contract(h, source=source)

        # 1. Library files used by the new contracts.
        try:
            extra_libs = self.library_resolver(names)
        except Exception as e:
            logger.warning(f"Library resolution failed for {names}: {e}")
            extra_libs = []
        if extra_libs:
            try:
                self.config_manager.add_files_to_config(
                    self.config_file, new_contract_files=extra_libs
                )
                logger.info(
                    f"Added {len(extra_libs)} library file(s) used by {names}: "
                    f"{[h.contract_name for h in extra_libs]}"
                )
                # Back-reference points only at handles_list[0]; if multiple contracts in
                # the batch pulled in this library, the additional parent attributions are
                # dropped from the report.
                for lib in extra_libs:
                    self._record_contract(lib, source=ContractSource.LIBRARY, parent=handles_list[0])
            except Exception as e:
                logger.warning(f"Failed to add library files for {names}: {e}")

        # 2. Summarize the new contracts: curated matching, LLM analysis, aggregator update, and
        # the scene-wide prune pass. Curated summaries resolve here because step 1 has already added
        # the libraries these contracts use to the conf.
        # TODO(perf): on_contracts_entered_scene's prune re-checks every emitted spec on each
        # contract-add. all_methods.json is built once and never regenerated, so an entry that
        # resolved before stays resolvable — only the newly-added specs need the existence check.
        # The exception is cross-spec dedup precedence (a newly-added curated spec can supersede a
        # (receiver,name,params) an existing LLM spec kept), which needs the global view. Consider
        # pruning only new specs for existence + handling dedup incrementally.
        try:
            await self.summary_setup.on_contracts_entered_scene(names, self.contract_name)
        except Exception as e:
            logger.warning(f"Summarizing {names} failed; continuing without their summaries: {e}")

    async def execute(
        self,
        max_iterations: int = 10,
    ) -> None:
        """Execute the iterative call resolution process."""
        logger.info(f"Starting call resolution for {self.contract_name}")

        resolution_failed = False
        try:
            # Add -callResolutionOnly flag to config for verification phases
            await self._add_skip_formula_checking_flag()

            iteration = 1

            while iteration <= max_iterations:
                logger.info(f"Call resolution iteration {iteration} for {self.contract_name}")

                # Scan any contracts not yet checked (initial files, then linker/dispatcher
                # additions on later iterations) for proxy patterns before the prover runs.
                files_before_proxy = set(self.config_manager.get_referenced_contracts(self.config_file))
                await self._scan_for_proxies()
                files_after_proxy = set(self.config_manager.get_referenced_contracts(self.config_file))
                proxy_added_files = bool(files_after_proxy - files_before_proxy)

                iteration_start = time.time()
                iteration_result: IterationResult = await self._execute_iteration(iteration)
                iteration_duration = time.time() - iteration_start

                logger.info(
                    f"Iteration {iteration} completed in "
                    f"{iteration_duration:.2f}s for {self.contract_name}"
                )

                if not iteration_result.success:
                    logger.error(f"Iteration {iteration} failed")
                    resolution_failed = True
                    break

                if (
                    iteration_result.dispatching_result
                    and iteration_result.dispatching_result.converged
                    and not proxy_added_files
                ):
                    logger.info(f"Call resolution converged after {iteration} iterations")
                    break

                iteration += 1
        finally:
            await self._remove_skip_formula_checking_flag()

        limit_reached = iteration > max_iterations
        if limit_reached:
            logger.warning(f"Call resolution reached max iterations ({max_iterations}) for {self.contract_name}")

        # Log remaining unresolved calls
        remaining = self._last_unresolved_calls
        if remaining:
            logger.info(f"Remaining unresolved calls for {self.contract_name} ({len(remaining)}):")
            for call in remaining:
                sighash = extract_sighash_from_callee(call.callee_name)
                if sighash:
                    sig = self.scope.signature_database.resolve_selector(sighash)
                    if sig:
                        logger.info(f"  {call.caller_name} -> {sig.signature} ({sighash})")
                    else:
                        logger.info(f"  {call.caller_name} -> {sighash} (not in signature database)")
                else:
                    logger.info(f"  {call.caller_name} -> {call.callee_name}")

        # An empty `remaining` only means "all resolved" when the loop finished cleanly.
        # If a prover run failed, the loop broke before refreshing `remaining`, so the
        # spec is incomplete regardless of how many calls earlier iterations resolved.
        if resolution_failed:
            logger.error(
                f"Call resolution did not complete for {self.contract_name}: a prover run failed — "
                f"the generated call-resolution spec may be incomplete"
            )
        elif not remaining:
            logger.info(f"All calls resolved for {self.contract_name}")

        # Generate final report
        report_file = self.reports_dir / f"{self.contract_name}_call_resolution_report.md"
        self._generate_report(report_file, limit_reached)

    async def _add_skip_formula_checking_flag(self) -> None:
        self.config_manager.update_config_with_prover_args(
            self.config_file,
            additional_args={"callResolutionOnly": "true"},
            _logger_context=self.contract_name,
        )

    async def _remove_skip_formula_checking_flag(self) -> None:
        self.config_manager.update_config_with_prover_args(
            self.config_file,
            remove_args=["-callResolutionOnly"],
            _logger_context=self.contract_name,
        )

    async def _scan_for_proxies(self) -> None:
        """Scan any contracts currently in the config that haven't been scanned yet.
        Runs detection in parallel across all candidates. Skips libraries — they're
        inlined at compile time and can't be proxies."""
        if self.proxy_detector is None:
            return
        current = set(self.config_manager.get_referenced_contracts(self.config_file))
        candidates = [
            h for h in current
            if h not in self._already_scanned_for_proxy and not self.is_library(h.contract_name)
        ]
        self._already_scanned_for_proxy.update(current)
        if not candidates:
            return
        logger.info(
            f"Scanning {len(candidates)} contract(s) for proxy patterns: "
            f"{[h.contract_name for h in candidates]}"
        )
        await asyncio.gather(*(self._detect_proxy_for(h) for h in candidates))

    async def _detect_proxy_for(self, handle: ContractHandle) -> None:
        """Per-contract proxy detection. The lock serializes the consolidated
        contract_extensions write since update_config_with_properties does top-level
        replace, not merge, so each writer must include all prior mappings."""
        if self.proxy_detector is None:
            return
        with ledger_component("proxy_detection"):
            result = await self.proxy_detector.analyze_contract(handle)

        if not result.search_completed:
            logger.warning(
                f"Proxy detection failed for {handle.contract_name}: {result.error_message}"
            )
            self._proxy_scan_results.append(ProxyScanRecord(
                contract=handle, iteration=self.current_iteration,
                is_proxy=False, proxy_pattern=None,
                search_failed=True, error_message=result.error_message,
            ))
            return

        self._proxy_scan_results.append(ProxyScanRecord(
            contract=handle, iteration=self.current_iteration,
            is_proxy=result.is_proxy, proxy_pattern=result.proxy_pattern,
            implementations=list(result.implementations),
        ))

        if not result.is_proxy:
            logger.debug(f"{handle.contract_name} is not a proxy")
            return

        logger.info(f"{handle.contract_name} detected as {result.proxy_pattern} proxy")

        impl_handles = [
            ContractHandle(impl.contract_name, impl.source_file)
            for impl in result.implementations
        ]

        async with self._proxy_state_lock:
            if impl_handles:
                self.config_manager.add_files_to_config(
                    self.config_file, new_contract_files=impl_handles
                )
                logger.info(
                    f"  Added {len(impl_handles)} implementation(s) to config files list: "
                    f"{[h.contract_name for h in impl_handles]}"
                )
                await self._on_contracts_added(impl_handles, source=ContractSource.PROXY_IMPL)

            self.proxy_mappings.append(ProxyMapping(
                proxy_contract_name=handle.contract_name,
                proxy_source_file=handle.source_file,
                implementations=result.implementations,
                proxy_pattern=result.proxy_pattern,
            ))

            merged_extensions: dict[str, list[dict[str, Any]]] = {
                mapping.proxy_contract_name: [
                    {"extension": impl.contract_name, "exclude": []}
                    for impl in mapping.implementations
                ]
                for mapping in self.proxy_mappings
            }
            logger.info(f"  Applied contract_extensions: {merged_extensions}")
            # Always set the `--contract_extensions_override` flag - if there are no overlapping functions
            # it's a no-op, and if there are then we want it.
            ext_props: dict[str, Any] = {
                "contract_extensions": merged_extensions,
                "contract_extensions_override": True
            }
            self.config_manager.update_config_with_properties(self.config_file, ext_props)

    async def _run_prover_and_parse_calls(self) -> Optional[List[CallResolutionInfo]]:
        """
        Run prover with callResolutionOnly and parse unresolved calls from output.

        Returns:
            List of unresolved calls if successful, or None if prover execution failed.
            An empty list means the prover ran successfully but found no unresolved calls.
        """
        logger.debug(f"[{self.contract_name}] Running prover for iteration {self.current_iteration}")

        config_content = FileContent.from_file(self.config_file)
        job_spec = ProverJobSpec(
            contract_name=self.contract_name,
            phase="call_resolution",
            config_file=config_content,
            extra_args=self.extra_args,
        )

        prover_result = await self.prover_runner.check_with_prover(job_spec)
        self._prover_runs.append((self.current_iteration, prover_result.job_url))

        if not prover_result.success:
            error_msg = prover_result.error_message or "Unknown prover error"
            logger.error(f"Prover execution failed: {error_msg}")
            return None

        job_id = prover_result.job_handle.job_id
        unresolved_calls = self.prover_runner.extract_unresolved_calls(job_id)
        logger.info(f"Found {len(unresolved_calls)} unresolved calls")
        return unresolved_calls

    def _write_call_resolution_spec_file(self) -> Optional[Path]:
        """
        Write the {contract_name}_call_resolution.spec file with the current
        links and dispatcher entries.

        The file is always emitted (possibly empty) so the static import in the
        user-facing base spec keeps resolving. Returns the spec path on success,
        or None when there is nothing to resolve and the file was left empty.
        """
        links_lines = self.contract_linker.generate_links_spec_lines()
        dispatcher_entries = self.contract_dispatcher.get_all_dispatcher_entries()

        project_root = self.scope.project_root
        call_resolution_spec_path = user_call_resolution_spec_path(project_root, self.contract_name)
        call_resolution_spec_path.parent.mkdir(parents=True, exist_ok=True)

        if not links_lines and not dispatcher_entries:
            call_resolution_spec_path.write_text("")
            return None

        spec_lines: List[str] = []
        spec_lines.extend(links_lines)

        if dispatcher_entries:
            spec_lines.append("methods {")
            for entry in dispatcher_entries:
                spec_lines.append(f"    {entry}")
            spec_lines.append("}")
            spec_lines.append("")

        call_resolution_spec_path.write_text("\n".join(spec_lines))
        logger.info(f"Generated call resolution spec file: {call_resolution_spec_path}")
        return call_resolution_spec_path

    def _generate_report(self, report_file: Path, limit_reached: bool) -> None:
        """Generate a unified call resolution report covering both linking and dispatching."""
        remaining_calls = self._last_unresolved_calls

        lines = [f"# Call Resolution Report: {self.contract_name}", ""]

        # Summary
        lines.append(f"- **Iterations:** {self.current_iteration}")
        if limit_reached:
            lines.append("- **Result:** Stopped - hit iteration limit")
        elif not remaining_calls:
            lines.append("- **Result:** Converged - all calls resolved")
        else:
            unique_remaining = len({
                (c.callee_name, c.call_site_snippet or "", c.source_location or "")
                for c in remaining_calls
            })
            lines.append(
                f"- **Result:** Converged with {len(remaining_calls)} unresolved call site(s) "
                f"({unique_remaining} unique)"
            )

        if self._prover_runs:
            _, last_url = self._prover_runs[-1]
            prior = self._prover_runs[:-1]
            headline = f"- **Last prover run:** {last_url}"
            if prior:
                # Plain number when no URL (local run) — avoids emitting broken markdown links.
                prior_parts = [f"[{n}]({u})" if u else f"{n}" for n, u in prior]
                headline += f"  (earlier iterations {', '.join(prior_parts)})"
            lines.append(headline)
        lines.append("")

        # Section B: Proxy Detection
        if self._proxy_scan_results:
            proxies = [r for r in self._proxy_scan_results if r.is_proxy]
            failed = [r for r in self._proxy_scan_results if r.search_failed]
            lines.append("## Proxy Detection")
            lines.append(f"- **Contracts scanned:** {len(self._proxy_scan_results)}")
            lines.append(f"- **Proxies detected:** {len(proxies)}")
            if failed:
                lines.append(f"- **Scans that failed:** {len(failed)}")
            lines.append("")
            for r in proxies:
                lines.append(f"### {r.contract.contract_name}")
                lines.append(f"- **Pattern:** {r.proxy_pattern}")
                lines.append(f"- **Detected in iteration:** {r.iteration}")
                if r.implementations:
                    impl_names = [impl.contract_name for impl in r.implementations]
                    lines.append(f"- **Implementations attached:** {', '.join(impl_names)}")
                else:
                    lines.append("- **Implementations attached:** (none found)")
                lines.append("")

        # Section A: Contracts added to the verification scene
        if self._contract_provenance:
            # Group harnesses under their parent so the parent row shows the wrappers.
            harnesses_by_parent: Dict[ContractHandle, List[ContractHandle]] = defaultdict(list)
            for prov in self._contract_provenance.values():
                if prov.source == ContractSource.HARNESS and prov.parent:
                    harnesses_by_parent[prov.parent].append(prov.handle)

            by_source: Dict[ContractSource, List[ContractProvenance]] = defaultdict(list)
            for prov in self._contract_provenance.values():
                if prov.source != ContractSource.HARNESS:
                    by_source[prov.source].append(prov)

            lines.append("## Contracts Added to the Verification Scene")
            total = sum(len(v) for v in by_source.values()) + sum(
                len(v) for v in harnesses_by_parent.values()
            )
            lines.append(f"- **Total contracts added:** {total}")
            lines.append("")

            def fmt_row(prov: ContractProvenance) -> str:
                harness_handles = sorted(
                    harnesses_by_parent.get(prov.handle, []),
                    key=lambda h: h.contract_name,
                )
                suffix = ""
                if harness_handles:
                    harness_names = [h.contract_name for h in harness_handles]
                    suffix = (
                        f" (+{len(harness_names)} harness wrapper(s): "
                        f"{', '.join(harness_names)})"
                    )
                parent_note = (
                    f" — pulled in by `{prov.parent.contract_name}`" if prov.parent else ""
                )
                return (
                    f"- `{prov.handle.contract_name}` "
                    f"({prov.handle.source_file}) — iteration {prov.iteration}"
                    f"{parent_note}{suffix}"
                )

            section_labels: List[Tuple[ContractSource, str]] = [
                (ContractSource.LINK, "Linked via storage variables"),
                (ContractSource.DISPATCH, "Added as DISPATCHER targets"),
                (ContractSource.PROXY_IMPL, "Proxy implementations"),
                (ContractSource.LIBRARY, "Libraries (compile-time dependencies)"),
            ]
            for src_key, label in section_labels:
                items = by_source.get(src_key) or []
                if not items:
                    continue
                lines.append(f"### {label}")
                for prov in sorted(items, key=lambda p: p.handle.contract_name):
                    lines.append(fmt_row(prov))
                lines.append("")

            # Parents that only landed in the scene via their harness wrappers — typical for
            # indexed-storage-path links, where ContractLinker replaces the original contract
            # with its harness contracts in the impl list before adding files to the config,
            # so the parent is never recorded directly in _contract_provenance.
            tracked_handles = {
                p.handle for items in by_source.values() for p in items
            }
            orphan_parents = sorted(
                set(harnesses_by_parent) - tracked_handles,
                key=lambda h: h.contract_name,
            )
            if orphan_parents:
                lines.append("### Harness wrappers")
                for parent in orphan_parents:
                    names = sorted(h.contract_name for h in harnesses_by_parent[parent])
                    lines.append(f"- Parent `{parent.contract_name}` → {', '.join(names)}")
                lines.append("")

        # Linking section
        linking_decisions = self.contract_linker._linking_decisions
        if linking_decisions:
            linked = [d for d in linking_decisions.values() if d.status == LinkStatus.LINKED]
            unresolved = [d for d in linking_decisions.values() if d.status == LinkStatus.UNRESOLVED]

            lines.append("## Linking")
            lines.append(f"- **Successful Links:** {len(linked)}")
            lines.append(f"- **Unresolved:** {len(unresolved)}")
            lines.append("")
            for decision in linking_decisions.values():
                status_icon = "LINKED" if decision.status == LinkStatus.LINKED else decision.status.value.upper()
                targets_str = ""
                if decision.link_targets:
                    targets_str = f" → [{', '.join(decision.link_targets)}]"
                lines.append(
                    f"- [{status_icon}] {decision.contract_name}.{decision.variable_name}"
                    f"{targets_str}: {decision.reason}"
                )
                if decision.missing_selectors:
                    lines.append(
                        "    - No implementer in scope for selector(s) called through this path:"
                    )
                    for sel in decision.missing_selectors:
                        sig = self.scope.signature_database.resolve_selector(sel)
                        if sig:
                            lines.append(f"        - `{sig.signature}` (`{sel}`)")
                        else:
                            lines.append(f"        - `{sel}` (not in signature database)")
            lines.append("")

        # Dispatching section
        resolved_sighashes = self.contract_dispatcher.resolved_sighashes
        if resolved_sighashes:
            lines.append("## Dispatching")
            lines.append(f"- **Dispatcher Entries Added:** {len(resolved_sighashes)}")
            lines.append("")
            for sig in sorted(resolved_sighashes.values(), key=lambda s: s.signature):
                lines.append(f"- `{sig.signature}` (`{sig.selector}`)")
            lines.append("")

        # Section C + D: Remaining unresolved calls, deduped and with resolved sighashes
        if remaining_calls:
            groups: Dict[Tuple[str, str, str], List[CallResolutionInfo]] = defaultdict(list)
            for call in remaining_calls:
                key = (
                    call.callee_name,
                    call.call_site_snippet or "",
                    call.source_location or "",
                )
                groups[key].append(call)

            # Sort: most-repeated groups first, then alphabetically for stability.
            sorted_groups = sorted(groups.items(), key=lambda kv: (-len(kv[1]), kv[0]))

            lines.append(
                f"## Remaining Unresolved Calls "
                f"({len(remaining_calls)} total, {len(groups)} unique)"
            )
            lines.append("**Legend.**")
            lines.append(
                "- *Call site*: the file + line where the call is written in source. "
                "`Snippet` is the Solidity expression at that line."
            )
            lines.append(
                "- *Callee*: the prover's name for the unresolved target. "
                "`[?].[?]` — neither contract nor selector known. "
                "`[?].[sighash=0x...]` — selector known but no implementer found in the scene."
            )
            lines.append(
                "- *Reachable from entry point(s)*: the public/external function(s) the prover "
                "analyzed that transitively reach this call site. Not the immediate caller — many "
                "entry points can share a single call site (e.g. through a common internal helper)."
            )
            lines.append("")

            for (callee, snippet, location), calls in sorted_groups:
                # Resolve sighash to function name (D).
                sighash = extract_sighash_from_callee(callee) or calls[0].selector
                sig_label = ""
                if sighash:
                    sig = self.scope.signature_database.resolve_selector(sighash)
                    if sig:
                        sig_label = f" — `{sig.signature}` (`{sighash}`)"
                    else:
                        sig_label = f" — sighash `{sighash}` (not in signature database)"

                header = snippet if snippet else callee
                lines.append(f"### `{header}`{sig_label}")
                if location:
                    lines.append(f"- **Call site:** {location}")
                if snippet and callee != snippet:
                    lines.append(f"- **Callee:** `{callee}`")
                lines.append(f"- **Occurrences:** {len(calls)}")

                distinct_callers = sorted({c.caller_name for c in calls})
                if len(distinct_callers) == 1:
                    lines.append(f"- **Reachable from entry point:** `{distinct_callers[0]}`")
                else:
                    lines.append(
                        f"- **Reachable from {len(distinct_callers)} analyzed entry point(s):**"
                    )
                    for caller in distinct_callers:
                        lines.append(f"  - `{caller}`")
                lines.append("")

        report_file.parent.mkdir(parents=True, exist_ok=True)
        report_file.write_text("\n".join(lines))
        logger.info(f"Generated call resolution report: {report_file}")

    async def _execute_iteration(self, iteration: int) -> IterationResult:
        """
        Execute one complete iteration:
        1. Run prover with callResolutionOnly to detect unresolved calls
        2. Split calls: storage-path calls -> linking, others -> dispatching
        3. Write links to spec, add dispatcher entries for remaining calls
        4. Add newly discovered contracts to config
        """
        try:
            self.current_iteration = iteration

            # Step 1: Run prover and get unresolved calls
            logger.info(f"Iteration {iteration} - running prover for {self.contract_name}")
            unresolved_calls = await self._run_prover_and_parse_calls()

            if unresolved_calls is None:
                logger.warning(f"Prover failed in iteration {iteration}")
                return IterationResult(
                    linking_result=LinkingResult(set(), [], ["Prover execution failed"], False),
                    dispatching_result=DispatchingResult(0, 0, False, False, "Prover execution failed"),
                )

            if not unresolved_calls:
                logger.info(f"No unresolved calls - converged at iteration {iteration}")
                return IterationResult(
                    linking_result=LinkingResult(set(), [], [], True),
                    dispatching_result=DispatchingResult(0, 0, True, True),
                )

            logger.info(f"Iteration {iteration} - {len(unresolved_calls)} unresolved calls")

            # Step 2: Split calls into storage-path (linking) vs non-storage (dispatching)
            links, remaining_for_dispatcher = self.contract_linker.generate_links_from_call_resolution(
                unresolved_calls, self.scope.signature_database
            )

            links_added = 0
            new_contracts: set[str] = set()

            # Step 3: Add new contract files to config for linked contracts
            if links:
                linked_contracts = self.contract_linker.get_linked_contracts(links)
                new_contracts.update(h.contract_name for h in linked_contracts)
                additional_files = self.contract_linker.get_additional_source_files(links)

                if additional_files:
                    self.config_manager.add_files_to_config(
                        self.config_file, new_contract_files=additional_files
                    )
                    logger.info(f"Added {len(additional_files)} contract files to config")
                    await self._on_contracts_added(additional_files, source=ContractSource.LINK)

                links_added = len(links)

            # Step 4: Generate dispatcher entries for remaining non-storage calls
            signatures_added = 0
            new_dispatcher_entries: list[str] = []
            if remaining_for_dispatcher:
                resolved_signatures = self.contract_dispatcher._resolve_calls_to_signatures(remaining_for_dispatcher)
                new_dispatcher_entries = self.contract_dispatcher.generate_new_dispatcher_entries(resolved_signatures)
                signatures_added = len(new_dispatcher_entries)
                added_dispatch_handles = self.contract_dispatcher.add_contract_files_to_config(
                    resolved_signatures, self.config_file,
                    exclude_contracts=self.contract_linker.harnessed_contracts,
                )
                await self._on_contracts_added(added_dispatch_handles, source=ContractSource.DISPATCH)

            # Step 5: Write call resolution spec file (links + dispatcher entries)
            if links_added > 0 or signatures_added > 0:
                call_resolution_spec = self._write_call_resolution_spec_file()
                if call_resolution_spec:
                    logger.info(f"Wrote call resolution spec: {call_resolution_spec.name}")

            # Track unresolved calls for reporting
            self._last_unresolved_calls = unresolved_calls

            converged = links_added == 0 and signatures_added == 0

            linking_result = LinkingResult(
                new_contracts_found=new_contracts,
                links=[f"{link.owner_contract}.{link.variable_path}" for link in links],
                errors=[],
                success=True,
            )
            dispatching_result = DispatchingResult(
                unresolved_calls_before=len(unresolved_calls),
                signatures_added=signatures_added,
                converged=converged,
                success=True,
            )

            return IterationResult(
                linking_result=linking_result,
                dispatching_result=dispatching_result,
            )

        except Exception as e:
            logger.error(f"Iteration {iteration} for {self.contract_name} failed: {e}")
            logger.error(traceback.format_exc())
            return IterationResult(linking_result=None, dispatching_result=None)
