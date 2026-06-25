"""Append-only summary aggregator for the base summaries spec.

The base summary aggregator at ``certora/specs/summaries/{main}_base_summaries.spec`` is
written once by ``SummarySetup.generate_base_aggregator`` with curated bundled
imports + per-contract LLM imports for the initial scene (main + additional
contracts). After that, only ``BaseSummariesAggregator.register`` may mutate it,
appending an ``import "./{C}_summaries.spec";`` line per newly summarized contract
brought into scene by call resolution. All mutations are idempotent so the same
contract can be registered repeatedly across iterations.
"""

from pathlib import Path
from typing import Iterable, List


class BaseSummariesAggregator:
    """Idempotent appender for the base summaries aggregator spec."""

    def __init__(self, summaries_dir: Path, main_contract: str):
        """Args:
            summaries_dir: ``certora/specs/summaries/`` directory in the user's project.
            main_contract: Main contract name; the summary aggregator file is
                ``{main_contract}_base_summaries.spec`` under ``summaries_dir``.
        """
        self.summaries_dir = summaries_dir
        self.aggregator_path = summaries_dir / f"{main_contract}_base_summaries.spec"

    def register(self, contract_names: Iterable[str]) -> List[str]:
        """Append one ``import "./{C}_summaries.spec";`` line per contract whose
        per-contract spec exists on disk and isn't already imported.

        Returns:
            The contract names that had imports added (for logging). Empty if all
            inputs were already-imported, no spec on disk, or the summary aggregator file
            doesn't exist yet.
        """
        if not self.aggregator_path.exists():
            return []

        existing = self.aggregator_path.read_text()
        new_lines: List[str] = []
        added: List[str] = []

        for name in contract_names:
            spec_path = self.summaries_dir / f"{name}_summaries.spec"
            if not spec_path.exists():
                continue
            import_line = f'import "./{name}_summaries.spec";'
            if import_line in existing or import_line in new_lines:
                continue
            new_lines.append(import_line)
            added.append(name)

        if not new_lines:
            return []

        with self.aggregator_path.open("a") as f:
            # Ensure the appended block starts on a new line even if the file
            # didn't end with one.
            if existing and not existing.endswith("\n"):
                f.write("\n")
            for line in new_lines:
                f.write(f"{line}\n")
        return added
