"""
JSON reporting functionality for test automation and integration testing.
"""


import json
import time
from pathlib import Path
from typing import Dict, List, Any

from certora_autosetup.utils.llm_util import LlmUsageReport, UsageRow


class JsonReporter:
    """Generates JSON reports for automated testing and analysis."""

    def __init__(self, reports_dir: Path):
        self.reports_dir = Path(reports_dir)

    def generate_results_json(self, verification_results: List, report_stats: Dict[str, Any], get_report_name_fn, execution_info: Dict[str, Any] | None = None, llm_usage: list[UsageRow] | None = None) -> Dict[str, Any]:
        """Generate streamlined JSON summary focused on config results."""

        # Create a unified results list with all the info needed for testing
        config_results = []

        for result in verification_results:
            # Keep full relative path of config file
            config_file = str(result.job_handle.config_file) if hasattr(result.job_handle, 'config_file') else "unknown"

            # Get job URL from result
            job_url = result.job_handle.job_id if hasattr(result.job_handle, 'job_id') else None

            # Find corresponding report stats to get rule-level results
            rule_stats = {
                "verified": 0,
                "violations": 0,
                "timeout": 0,
                "unknown": 0
            }

            # Match with report stats using the provided report name function
            if hasattr(result, 'job_handle') and hasattr(result.job_handle, 'phase'):
                phase = result.job_handle.phase
                expected_report_key = get_report_name_fn(phase)

                if expected_report_key in report_stats:
                    stats = report_stats[expected_report_key]
                    rule_stats = {
                        "verified": stats.verified,
                        "violations": stats.violations,
                        "timeout": stats.timeout,
                        "unknown": stats.unknown
                    }

            config_data = {
                "config_file": config_file,
                "job_url": job_url,
                "success": result.success,
                "error_message": getattr(result, 'error_message', None),
                "return_code": result.output_data.get('return_code', 0) if result.output_data else 0,
                **rule_stats  # Add verified, violations, timeout, unknown counts
            }

            config_results.append(config_data)

        # Build streamlined JSON report
        json_data = {
            "timestamp": time.strftime('%Y-%m-%d %H:%M:%S'),
            "total_configs": len(verification_results),
            "config_results": config_results
        }

        # Add execution info if provided
        if execution_info:
            json_data["execution_info"] = execution_info

        # Add the LLM usage rows plus a derived rollup, if any usage was recorded.
        if llm_usage:
            json_data.update(LlmUsageReport.from_rows(llm_usage).to_dict())

        return json_data

    def save_results_json(self, json_data: Dict[str, Any], filename: str = "orchestrator_results.json") -> Path:
        """Save JSON results to file."""
        self.reports_dir.mkdir(parents=True, exist_ok=True)
        json_file = self.reports_dir / filename

        with open(json_file, 'w') as f:
            json.dump(json_data, f, indent=2, sort_keys=True)

        return json_file
