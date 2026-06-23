"""
Markdown report generator for Certora Prover results.
"""

import sys
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from certora_autosetup.utils.job_utils import extract_job_hash_from_url
from certora_autosetup.utils.logger import logger

# Import required dependencies
from prover_output_utility import ProverOutputAPI # type: ignore[import-untyped]
from prover_output_utility.models import CheckResult, BreadcrumbInfo # type: ignore[import-untyped]



class MarkdownReporter:
    """
    Generates markdown reports from Certora Prover job results.
    """
    
    def __init__(self, verbose: bool = False, skip_breadcrumbs: bool = False):
        """
        Initialize the markdown reporter.
        """
        self.verbose = verbose
        self.skip_breadcrumbs = skip_breadcrumbs
        self.api = ProverOutputAPI()
        
        # Configure centralized logger
        logger.set_verbosity(1 if verbose else 0)
        self.component = "MarkdownReporter"
        
        # Define breadcrumb filtering rules for specific rule names
        self.breadcrumb_filters = {
            # Rules that should only show function calls
            'function_calls_only': {
                'stable_extcall_targets_and_selectors',
                # Add more rules here as needed:
                # 'another_rule_name',
                # 'yet_another_rule_name',
            },
            # Future filter categories can be added here:
            # 'storage_only': {'rule1', 'rule2'},
            # 'branches_only': {'rule3', 'rule4'},
        }
    
    def log(self, message: str, level: str = "INFO"):
        """Log message using centralized logger."""
        logger.log(message, level, self.component)
    
    @staticmethod
    def get_validated_checks(api, job_url: str):
        """Get leaf checks from job URL and validate no METHOD_INSTANTIATION or ROOT nodeTypes."""
        all_checks = api.get_leaf_checks(job_url)
        
        # Validate that no unwanted nodeTypes are present
        if all_checks:
            for check in all_checks:
                if hasattr(check, 'nodeType') and check.nodeType in ['METHOD_INSTANTIATION', 'ROOT']:
                    assert False, f"Found {check.nodeType} nodeType in checks - these should be filtered by get_leaf_checks"
        
        return all_checks
    
    def generate_report(self, job_url: str, output_file: str = "prover_report.md") -> None:
        """
        Generate a comprehensive markdown report for a prover job.
        
        Args:
            job_url: Certora Prover job URL
            output_file: Output markdown file path
        """
        self.log(f"📊 Generating prover report for job: {job_url}")
        
        # Get job data
        import time
        start_time = time.time()
        job_data = self._get_job_data(job_url)
        fetch_time = time.time() - start_time
        self.log(f"Got job data in {fetch_time:.1f}s")
        
        if not job_data:
            self.log("❌ Failed to get job data", "ERROR")
            return
        
        # Generate markdown content
        start_time = time.time()
        markdown_content = self._generate_markdown_content(job_url, job_data)
        gen_time = time.time() - start_time
        self.log(f"Generated markdown in {gen_time:.1f}s")
        
        # Write to file
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        self.log(f"✅ Report generated: {output_file}")
    
    def generate_report_with_data(self, job_url: str, output_file: str, all_checks) -> None:
        """Generate a markdown report using pre-fetched job data."""
        self.log(f"📊 Generating prover report for job: {job_url} (using pre-fetched data)")
        
        # Convert pre-fetched data to the expected format
        job_data = self._format_job_data(job_url, all_checks)
        if not job_data:
            # Handle empty results gracefully - generate a minimal report
            self.log("⚠️ No checks/rules found in job - generating minimal report", "WARNING")
            job_data = self._create_empty_job_data(job_url)
        
        # Generate markdown content
        import time
        start_time = time.time()
        markdown_content = self._generate_markdown_content(job_url, job_data)
        gen_time = time.time() - start_time
        self.log(f"Generated markdown in {gen_time:.1f}s")
        
        # Write to file
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(markdown_content)
        
        self.log(f"✅ Report generated: {output_file}")
    
    def _format_job_data(self, job_url: str, all_checks) -> Optional[Dict[str, Any]]:
        """Format pre-fetched checks data into the expected job_data structure."""
        # Handle None or empty checks
        if all_checks is None:
            return None
        
        # Allow empty lists - they represent jobs with no rules/checks
        if isinstance(all_checks, list) and len(all_checks) == 0:
            # Return minimal data structure for empty results
            return self._create_empty_job_data(job_url)

        return {
            'job_id': extract_job_hash_from_url(job_url),
            'violations': all_checks,  # Store all checks
            'assert_nodes_count': len(all_checks)
        }

    def _create_empty_job_data(self, job_url: str) -> Dict[str, Any]:
        """Create minimal job data structure for jobs with no checks/rules."""
        return {
            'job_id': extract_job_hash_from_url(job_url),
            'violations': [],  # Empty list of checks
            'assert_nodes_count': 0
        }

    def _get_job_data(self, job_url: str) -> Optional[Dict[str, Any]]:
        """Get job data using ProverOutputAPI."""
        try:
            # Get ALL checks using the API - this includes both verified and violated
            all_checks = self.get_validated_checks(self.api, job_url)

            return {
                'job_id': extract_job_hash_from_url(job_url),
                'violations': all_checks or [],  # Store all checks, not just violations
                'assert_nodes_count': len(all_checks) if all_checks else 0
            }

        except Exception as e:
            self.log(f"❌ Error getting job data: {e}", "ERROR")
            return None
    
    def _get_breadcrumbs_data(self, job_url: str, dap_file: str) -> Optional[BreadcrumbInfo]:
        """Get breadcrumbs data for a specific DAP file."""
        if self.skip_breadcrumbs:
            return None
        try:
            return self.api.get_breadcrumbs(job_url, dap_file)
        except Exception as e:
            self.log(f"Failed to get breadcrumbs for {dap_file}: {e}", "WARNING")
            return None
    
    def _generate_markdown_content(self, job_url: str, job_data: Dict[str, Any]) -> str:
        """Generate the full markdown content."""
        lines = []
        
        # Header
        lines.append("# Certora Prover Report")
        lines.append("")
        lines.append(f"**Generated:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        lines.append(f"**Job URL:** {job_url}\n")
        lines.append(f"**Job ID:** {job_data.get('job_id', 'Unknown')}\n")
        lines.append("")
        
        # Summary - now using all checks instead of just violations
        all_checks = job_data.get('violations', [])  # Note: still using 'violations' key but now contains all checks
        
        # Count statuses using the same logic as orchestrator
        verified_count = 0
        violated_count = 0
        timeout_count = 0
        unknown_count = 0
        other_statuses: dict[str, int] = {}
        
        for check in all_checks:
            if hasattr(check, 'status'):
                status = check.status.upper() if hasattr(check.status, 'upper') else str(check.status).upper()
                
                if 'VERIFIED' in status or 'PASSED' in status:
                    verified_count += 1
                elif 'VIOLATED' in status or 'FAILED' in status:
                    violated_count += 1
                elif 'TIMEOUT' in status:
                    timeout_count += 1
                else:
                    unknown_count += 1
            # For backward compatibility, also check legacy properties if they exist
            elif hasattr(check, 'is_violated') and hasattr(check, 'is_verified'):
                if check.is_violated:
                    violated_count += 1
                elif check.is_verified:
                    verified_count += 1
                else:
                    unknown_count += 1
        
        # Table of Contents
        lines.append("## Table of Contents")
        lines.append("")
        lines.append("- [Summary](#summary)")
        if verified_count > 0:
            lines.append(f"- [Verified Rules ({verified_count})](#verified-rules)")
        if violated_count > 0:
            lines.append(f"- [Violated Rules ({violated_count})](#violated-rules)")
        if timeout_count > 0:
            lines.append(f"- [Timeout Rules ({timeout_count})](#timeout-rules)")
        if unknown_count > 0:
            lines.append(f"- [Unknown Status Rules ({unknown_count})](#unknown-status-rules)")
        for status, count in other_statuses.items():
            status_anchor = status.lower().replace(' ', '-').replace('_', '-')
            lines.append(f"- [{status} Rules ({count})](#{status_anchor}-rules)")
        lines.append("")
        
        lines.append("## Summary")
        lines.append("")
        lines.append(f"- **Total Assert Nodes:** {len(all_checks)}")
        lines.append(f"- **Verified:** {verified_count} ✅")
        lines.append(f"- **Violated:** {violated_count} ❌")
        if timeout_count > 0:
            lines.append(f"- **Timeout:** {timeout_count} ⏱️")
        if unknown_count > 0:
            lines.append(f"- **Unknown:** {unknown_count} ❓")
        for status, count in other_statuses.items():
            lines.append(f"- **{status}:** {count} ⚠️")
        lines.append("")
        
        if violated_count > 0:
            lines.append("⚠️ **Issues found that require attention**")
        elif timeout_count > 0 or unknown_count > 0 or other_statuses:
            lines.append("⚠️ **Some assertions could not be fully verified**")
        else:
            lines.append("✅ **All assertions verified successfully**")
        lines.append("")
        
        # Detailed Results - grouped by status
        if not all_checks:
            lines.append("## Detailed Results")
            lines.append("")
            lines.append("No checks found.")
            lines.append("")
        else:
            # Sort checks lexicographically by rule name for consistent ordering
            sorted_checks = sorted(all_checks, key=lambda x: getattr(x, 'rule_name', ''))
            
            # Group checks by status
            verified_checks = []
            violated_checks = []
            timeout_checks = []
            unknown_checks = []
            other_status_checks: dict[str, list] = {}
            
            for check in sorted_checks:
                if hasattr(check, 'status'):
                    status = check.status.upper() if hasattr(check.status, 'upper') else str(check.status).upper()
                    
                    if 'VERIFIED' in status or 'PASSED' in status:
                        verified_checks.append(check)
                    elif 'VIOLATED' in status or 'FAILED' in status:
                        violated_checks.append(check)
                    elif 'TIMEOUT' in status:
                        timeout_checks.append(check)
                    else:
                        if status not in other_status_checks:
                            other_status_checks[status] = []
                        other_status_checks[status].append(check)
                # For backward compatibility, also check legacy properties if they exist
                elif hasattr(check, 'is_violated') and hasattr(check, 'is_verified'):
                    if check.is_violated:
                        violated_checks.append(check)
                    elif check.is_verified:
                        verified_checks.append(check)
                    else:
                        unknown_checks.append(check)
            
            # Generate sections for each status type
            self._add_status_section(lines, "Verified Rules", "verified-rules", verified_checks, "✅", job_url)
            self._add_status_section(lines, "Violated Rules", "violated-rules", violated_checks, "❌", job_url)  
            self._add_status_section(lines, "Timeout Rules", "timeout-rules", timeout_checks, "⏱️", job_url)
            self._add_status_section(lines, "Unknown Status Rules", "unknown-status-rules", unknown_checks, "❓", job_url)
            
            # Add sections for other statuses
            for status, checks in other_status_checks.items():
                status_title = f"{status.title()} Rules"
                status_anchor = status.lower().replace(' ', '-').replace('_', '-') + "-rules"
                self._add_status_section(lines, status_title, status_anchor, checks, "⚠️", job_url)
        
        return "\n".join(lines)
    
    def _add_status_section(self, lines: List[str], title: str, anchor: str, checks: List, icon: str, job_url: str) -> None:
        """Add a section for a specific status type with checks."""
        if not checks:
            return
            
        lines.append(f"## {icon} {title} <a id=\"{anchor}\"></a>")
        lines.append("")
        
        # Parallelize _format_violation calls to speed up hierarchy API requests
        from concurrent.futures import ThreadPoolExecutor
        
        def format_violation_wrapper(args):
            """Wrapper for parallel execution of _format_violation"""
            i, check, job_url = args
            return self._format_violation(i, check, job_url)
        
        # Use ThreadPoolExecutor to parallelize the hierarchy API calls
        with ThreadPoolExecutor(max_workers=8) as executor:
            # Create arguments for parallel execution
            args_list = [(i, check, job_url) for i, check in enumerate(checks, 1)]
            
            # Execute in parallel and collect results
            violation_results = list(executor.map(format_violation_wrapper, args_list))
            
            # Flatten and add all results to lines
            for violation_lines in violation_results:
                lines.extend(violation_lines)
    
    def _get_rule_hierarchy_path(self, job_url: str, violation: CheckResult) -> Optional[str]:
        """Get the rule hierarchy path for better context about the assertion location."""
        try:
            # Try to get hierarchy from the first output file
            if violation.output_files:
                for output_file in violation.output_files:
                    try:
                        hierarchy = self.api.get_rule_hierarchy(job_url, output_file)
                        if hierarchy and len(hierarchy) > 1:
                            # Skip the first element as mentioned in the request
                            relevant_path = hierarchy[1:]
                            return " → ".join(relevant_path)
                    except Exception as e:
                        self.log(f"Could not get hierarchy for {output_file}: {e}", "DEBUG")
                        continue
            return None
        except Exception as e:
            self.log(f"Error getting rule hierarchy: {e}", "DEBUG")
            return None

    def _format_violation(self, index: int, violation: CheckResult, job_url: str) -> List[str]:
        """Format a single violation for markdown."""
        lines = []
        
        # Status icon and text based on status
        status_str = violation.status.value if hasattr(violation.status, 'value') else str(violation.status)
        
        if violation.is_violated:
            icon = "❌"
            status_class = "**VIOLATED**"
        elif violation.is_verified:
            icon = "✅"
            status_class = "**VERIFIED**"
        elif 'timeout' in status_str.lower():
            icon = "⏱️"
            status_class = "**TIMEOUT**"
        elif 'unknown' in status_str.lower():
            icon = "❓"
            status_class = "**UNKNOWN**"
        else:
            icon = "⚠️"
            status_class = f"**{status_str.upper()}**"
        
        # Header
        lines.append(f"### {index}. {icon} {violation.rule_name}")
        lines.append("")
        
        # Basic info
        lines.append(f"- **Status:** {status_class}")
        lines.append(f"- **Method:** `{violation.method_name}`")
        lines.append(f"- **Assert Message:** {violation.assert_message}")
        
        # Rule hierarchy path - shows the context of where the assertion is located
        hierarchy_path = self._get_rule_hierarchy_path(job_url, violation)
        if hierarchy_path:
            lines.append(f"- **Assertion Path:** {hierarchy_path}")
        
        # Source location
        if violation.source_location:
            lines.append(f"- **Location:** `{violation.source_location}`")
        
        # Extract and show method call chain from breadcrumbs
        method_chain = self._extract_method_chain(job_url, violation.debug_trace_file)
        if method_chain:
            lines.append(f"- **Call Chain:** {method_chain}")
        
        lines.append("")
        
        # Output files
        if violation.output_files:
            lines.append("**Available Traces:**")
            for output_file in violation.output_files:
                lines.append(f"- 📄 Raw trace: `{output_file}`")
        
        # Debug trace (breadcrumbs)
        if violation.debug_trace_file:
            lines.append(f"- 🔍 Debug trace: `{violation.debug_trace_file}`")
            
            # Only add breadcrumbs for violated rules
            if violation.is_violated:
                breadcrumb_section = self._generate_breadcrumb_section(job_url, violation.debug_trace_file, violation.rule_name)
                if breadcrumb_section:
                    lines.extend(breadcrumb_section)
        
        lines.append("")
        lines.append("---")
        lines.append("")
        
        return lines
    
    def _extract_method_chain(self, job_url: str, dap_file: Optional[str]) -> Optional[str]:
        """Extract a clean method call chain from breadcrumbs."""
        if not dap_file:
            return None
            
        breadcrumb_info = self._get_breadcrumbs_data(job_url, dap_file)
        if not breadcrumb_info or not breadcrumb_info.breadcrumbs:
            return None
        
        # Extract method calls from breadcrumbs
        method_calls = []
        seen_methods = set()
        
        for crumb in breadcrumb_info.breadcrumbs:
            frames = crumb.get('frames', [])
            if frames:
                # Get the deepest frame (current method)
                current_frame = frames[-1]
                method_name = current_frame.get('method')
                contract_name = current_frame.get('contract')
                
                if method_name and method_name not in seen_methods:
                    if contract_name:
                        method_call = f"{contract_name}.{method_name}()"
                    else:
                        method_call = f"{method_name}()"
                    
                    method_calls.append(method_call)
                    seen_methods.add(method_name)
        
        if method_calls:
            return " → ".join(method_calls)
        
        return None
    
    def _generate_breadcrumb_section(self, job_url: str, dap_file: str, rule_name: str) -> Optional[List[str]]:
        """Generate a collapsible breadcrumb section with optional filtering based on rule name."""
        import time
        start_time = time.time()
        breadcrumb_info = self._get_breadcrumbs_data(job_url, dap_file)
        if breadcrumb_info:
            fetch_time = time.time() - start_time
            if fetch_time > 1.0:  # Only log if it took more than 1 second
                self.log(f"Fetched breadcrumbs for {dap_file} in {fetch_time:.1f}s", "DEBUG")
        
        if not breadcrumb_info or not breadcrumb_info.breadcrumbs:
            return None
        
        lines = []
        
        # Determine if this rule needs filtering
        filter_type = self._get_filter_type_for_rule(rule_name)
        
        # Collapsible section
        lines.append("<details>")
        if filter_type:
            lines.append(f"<summary><strong>🔍 Execution Trace - {filter_type.replace('_', ' ').title()}</strong> (Click to expand)</summary>")
        else:
            lines.append("<summary><strong>🔍 Execution Trace</strong> (Click to expand)</summary>")
        lines.append("")
        
        # Summary
        lines.append("**Execution Summary:**")
        lines.append(f"- Total Steps: {breadcrumb_info.summary.get('total_steps', len(breadcrumb_info.breadcrumbs))}")
        lines.append(f"- Function Calls: {breadcrumb_info.summary.get('function_calls', 'N/A')}")
        lines.append(f"- Storage Operations: {breadcrumb_info.summary.get('storage_operations', 'N/A')}")
        lines.append(f"- Branches: {breadcrumb_info.summary.get('branches', 'N/A')}")
        lines.append(f"- Variable Assignments: {breadcrumb_info.summary.get('variable_assignments', 'N/A')}")
        lines.append("")
        
        # Filter and display trace steps with smart condensing
        filtered_breadcrumbs = self._filter_breadcrumbs(breadcrumb_info.breadcrumbs, filter_type)
        
        if filter_type:
            lines.append(f"**Execution Steps ({filter_type.replace('_', ' ').title()}):**")
        else:
            lines.append("**Execution Steps:**")
        lines.append("")
        
        # Process breadcrumbs with smart display (show leaf calls in detail, condense intermediate)
        processed_steps = self._process_breadcrumbs_smart(filtered_breadcrumbs)
        
        for i, (crumb, display_mode) in enumerate(processed_steps, 1):
            if display_mode == 'full':
                lines.extend(self._format_breadcrumb_step(i, crumb))
            elif display_mode == 'medium':
                lines.extend(self._format_breadcrumb_step_medium(i, crumb))
            else:
                lines.extend(self._format_breadcrumb_step_condensed(i, crumb))
        
        if filter_type and len(filtered_breadcrumbs) < len(breadcrumb_info.breadcrumbs):
            lines.append(f"*Note: Showing {len(filtered_breadcrumbs)} of {len(breadcrumb_info.breadcrumbs)} total steps (filtered for {filter_type.replace('_', ' ')})*")
            lines.append("")
        
        lines.append("</details>")
        lines.append("")
        
        return lines
    
    def _format_breadcrumb_step(self, index: int, crumb: Dict[str, Any]) -> List[str]:
        """Format a single breadcrumb step."""
        lines = []
        
        # Type icons
        type_icons = {
            'function_call': '🔵',
            'function_return': '🔴',
            'storage_operation': '💾',
            'variable_assignment': '📝',
            'branch': '🔀',
            'internal_function_call': '🟦',
            'source_step': '📍',
            'cvl_annotation': '🏷️'
        }
        
        crumb_type = crumb.get('type', 'unknown')
        icon = type_icons.get(crumb_type, '•')
        
        # Determine indentation from frames
        depth = len(crumb.get('frames', []))
        indent = "  " * depth
        
        lines.append(f"{indent}{index}. {icon} **{crumb_type.upper().replace('_', ' ')}**")
        
        # Add method/variable details
        details = crumb.get('details', {})
        if details.get('method'):
            lines.append(f"{indent}   - Method: `{details['method']}`")
        elif details.get('variable'):
            lines.append(f"{indent}   - Variable: `{details['variable']}`")
        elif details.get('operation'):
            lines.append(f"{indent}   - Operation: {details['operation']}")
        
        # Add source info
        source_info = crumb.get('source_info')
        if source_info:
            location = f"{source_info['file']}:{source_info['start_line']}"
            if source_info['start_line'] != source_info['end_line']:
                location += f"-{source_info['end_line']}"
            lines.append(f"{indent}   - 📁 `{location}`")
            
            if source_info.get('content'):
                lines.append(f"{indent}   - 💬 `{source_info['content']}`")
        
        lines.append("")
        
        return lines
    
    def _get_filter_type_for_rule(self, rule_name: str) -> Optional[str]:
        """Determine the filter type for a given rule name."""
        for filter_type, rule_set in self.breadcrumb_filters.items():
            if rule_name in rule_set:
                return filter_type
        return None
    
    def _process_breadcrumbs_smart(self, breadcrumbs: List[Dict[str, Any]]) -> List[tuple]:
        """
        Process breadcrumbs to determine which should be shown in full vs condensed.
        Returns list of (breadcrumb, display_mode) tuples.
        
        Strategy:
        - Show "leaf" calls (calls with no subcalls) in full detail
        - Show the LAST call at any depth level in more detail  
        - Condense intermediate calls that have subcalls AND are not the last at their level
        
        Example: A calls B then C, C calls D then E, E calls nothing
        - B: condensed (has subcalls and not last in A)
        - C: show details of its structure but not full (last in A but has subcalls)
        - D: condensed (has subcalls and not last in C)
        - E: full details (leaf call - no subcalls)
        """
        processed = []
        
        # First pass: identify which calls have subcalls
        has_subcalls = {}
        for i, crumb in enumerate(breadcrumbs):
            crumb_type = crumb.get('type', '')
            if 'function_call' in crumb_type or 'internal_function_call' in crumb_type:
                # Check if this call has subcalls
                current_depth = len(crumb.get('frames', []))
                has_subs = False
                
                # Look for any deeper calls before the matching return
                for j in range(i + 1, len(breadcrumbs)):
                    next_crumb = breadcrumbs[j]
                    next_type = next_crumb.get('type', '')
                    next_depth = len(next_crumb.get('frames', []))
                    
                    # If we find a deeper call, this has subcalls
                    if ('function_call' in next_type or 'internal_function_call' in next_type) and next_depth > current_depth:
                        has_subs = True
                        break
                    # If we're back at same or shallower depth, stop looking
                    elif next_depth <= current_depth and 'return' in next_type:
                        break
                
                has_subcalls[i] = has_subs
        
        # Second pass: identify last calls at each depth
        last_at_depth = {}
        for i, crumb in enumerate(breadcrumbs):
            crumb_type = crumb.get('type', '')
            if 'function_call' in crumb_type or 'internal_function_call' in crumb_type:
                depth = len(crumb.get('frames', []))
                # Keep track of the last call at this depth within the current context
                parent_depth = depth - 1
                context_key = f"{parent_depth}:{self._get_parent_context(breadcrumbs, i, parent_depth)}"
                last_at_depth[context_key] = i
        
        # Third pass: determine display mode
        for i, crumb in enumerate(breadcrumbs):
            crumb_type = crumb.get('type', '')
            
            if 'function_call' in crumb_type or 'internal_function_call' in crumb_type:
                depth = len(crumb.get('frames', []))
                parent_depth = depth - 1
                context_key = f"{parent_depth}:{self._get_parent_context(breadcrumbs, i, parent_depth)}"
                
                # Leaf calls (no subcalls) are always shown in full
                if not has_subcalls.get(i, False):
                    display_mode = 'full'
                # Last call at this depth gets medium detail
                elif last_at_depth.get(context_key) == i:
                    display_mode = 'medium'
                # Everything else is condensed
                else:
                    display_mode = 'condensed'
                    
                processed.append((crumb, display_mode))
                
            elif 'function_return' in crumb_type:
                # Always condense returns
                processed.append((crumb, 'condensed'))
            else:
                # For non-function operations, show based on whether they're leaf operations
                is_leaf = i == len(breadcrumbs) - 1
                if not is_leaf:
                    current_depth = len(crumb.get('frames', []))
                    next_depth = len(breadcrumbs[i + 1].get('frames', []))
                    is_leaf = next_depth < current_depth
                
                display_mode = 'full' if is_leaf else 'condensed'
                processed.append((crumb, display_mode))
        
        return processed
    
    def _get_parent_context(self, breadcrumbs: List[Dict[str, Any]], index: int, parent_depth: int) -> str:
        """Get a context identifier for the parent call at the given depth."""
        # Look backwards to find the parent call
        for i in range(index - 1, -1, -1):
            crumb = breadcrumbs[i]
            if 'function_call' in crumb.get('type', ''):
                depth = len(crumb.get('frames', []))
                if depth == parent_depth:
                    return crumb.get('details', {}).get('method', str(i))
        return 'root'

    def _format_breadcrumb_step_condensed(self, index: int, crumb: Dict[str, Any]) -> List[str]:
        """Format a breadcrumb step in condensed form (single line)."""
        lines = []
        
        # Type icons
        type_icons = {
            'function_call': '🔵',
            'function_return': '🔴',
            'storage_operation': '💾',
            'variable_assignment': '📝',
            'branch': '🔀',
            'internal_function_call': '🟦',
            'source_step': '📍',
            'cvl_annotation': '🏷️'
        }
        
        crumb_type = crumb.get('type', 'unknown')
        icon = type_icons.get(crumb_type, '•')
        
        # Determine indentation from frames
        depth = len(crumb.get('frames', []))
        indent = "  " * depth
        
        # Build condensed line
        details = crumb.get('details', {})
        method = details.get('method', '')
        variable = details.get('variable', '')
        operation = details.get('operation', '')
        
        # Create a one-line summary
        if method:
            summary = f"{method}"
        elif variable:
            summary = f"{variable}"
        elif operation:
            summary = operation[:50] + ('...' if len(operation) > 50 else '')
        else:
            summary = crumb_type.replace('_', ' ')
        
        lines.append(f"{indent}{index}. {icon} {summary}")
        lines.append("")
        
        return lines
    
    def _format_breadcrumb_step_medium(self, index: int, crumb: Dict[str, Any]) -> List[str]:
        """Format a breadcrumb step in medium detail (shows method and location)."""
        lines = []
        
        # Type icons
        type_icons = {
            'function_call': '🔵',
            'function_return': '🔴',
            'storage_operation': '💾',
            'variable_assignment': '📝',
            'branch': '🔀',
            'internal_function_call': '🟦',
            'source_step': '📍',
            'cvl_annotation': '🏷️'
        }
        
        crumb_type = crumb.get('type', 'unknown')
        icon = type_icons.get(crumb_type, '•')
        
        # Determine indentation from frames
        depth = len(crumb.get('frames', []))
        indent = "  " * depth
        
        lines.append(f"{indent}{index}. {icon} **{crumb_type.upper().replace('_', ' ')}**")
        
        # Add method/variable details
        details = crumb.get('details', {})
        if details.get('method'):
            lines.append(f"{indent}   - Method: `{details['method']}`")
        elif details.get('variable'):
            lines.append(f"{indent}   - Variable: `{details['variable']}`")
        
        # Add source location if available
        source_info = crumb.get('source_info')
        if source_info:
            location = f"{source_info['file']}:{source_info['start_line']}"
            lines.append(f"{indent}   - 📁 `{location}`")
        
        lines.append("")
        
        return lines
    
    def _filter_breadcrumbs(self, breadcrumbs: List[Dict[str, Any]], filter_type: Optional[str]) -> List[Dict[str, Any]]:
        """Filter breadcrumbs based on the filter type."""
        if not filter_type:
            return breadcrumbs
        
        if filter_type == 'function_calls_only':
            # Only keep function calls (regular and internal)
            return [
                crumb for crumb in breadcrumbs
                if crumb.get('type') in ['function_call', 'internal_function_call', 'function_return']
            ]
        
        # Add more filter types here as needed:
        # elif filter_type == 'storage_only':
        #     return [crumb for crumb in breadcrumbs if crumb.get('type') == 'storage_operation']
        # elif filter_type == 'branches_only':
        #     return [crumb for crumb in breadcrumbs if crumb.get('type') == 'branch']
        
        # Default: return all breadcrumbs
        return breadcrumbs


def generate_prover_report(job_url: str, output_file: str = "prover_report.md") -> None:
    """
    Convenience function to generate a prover report.
    
    Args:
        job_url: Certora Prover job URL
        output_file: Output markdown file path
    """
    reporter = MarkdownReporter()
    reporter.generate_report(job_url, output_file)


if __name__ == "__main__":
    import argparse
    
    parser = argparse.ArgumentParser(description="Generate Certora Prover markdown reports")
    parser.add_argument("job_url", help="Certora Prover job URL")
    parser.add_argument("--output", "-o", default="prover_report.md", help="Output markdown file")
    
    args = parser.parse_args()
    
    generate_prover_report(args.job_url, args.output)