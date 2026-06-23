"""
Sanity Rule Generator — generates the user-facing sanity spec.

This is the AutoSetup-native subset of rule generation. PreAudit's full
RuleGenerator extends this with checker-specific generators.
"""

import os
import shutil
from pathlib import Path
from typing import Callable, Dict, Optional

from certora_autosetup.utils.paths import (
    user_call_resolution_spec_path,
    user_erc7201_spec_path,
    user_sanity_spec_path,
)


class SanityRuleGenerator:
    """Generates the per-contract sanity spec that the user runs."""

    def __init__(self, certora_dir: Path, log_func: Callable[..., None] | None = None):
        self.certora_dir = certora_dir
        self.log = log_func if log_func else lambda msg, level="INFO": print(f"[{level}] {msg}")

    def generate_sanity_specs(self, per_contract_summaries_specs: Dict[str, Path]) -> None:
        """Create per-contract sanity specs at certora/specs/sanity-{C}.spec.

        Each spec is the bundled sanity.spec body with three imports prepended:
        summary aggregator, call-resolution, and (when present) erc7201. Skip-if-exists
        so user edits survive re-runs. Also touches an empty call-resolution
        spec so the import resolves before call_resolution.py runs.
        """
        sanity_template = self.certora_dir / "sanity.spec"
        if not sanity_template.exists():
            bundled = Path(__file__).parent.parent / "certora" / "sanity.spec"
            if bundled.exists():
                sanity_template = bundled

        if not sanity_template.exists():
            self.log("No sanity spec template found, skipping sanity spec generation.")
            return

        self.log("Creating per-contract sanity specs")
        project_root = self.certora_dir.parent
        for contract_name, summary_spec_path in per_contract_summaries_specs.items():
            target_spec = user_sanity_spec_path(project_root, contract_name)

            call_res_spec = user_call_resolution_spec_path(project_root, contract_name)
            call_res_spec.parent.mkdir(parents=True, exist_ok=True)
            if not call_res_spec.exists():
                call_res_spec.write_text("")

            if target_spec.exists():
                self.log(f"Sanity spec already exists, leaving untouched: {target_spec}")
                continue

            target_spec.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(sanity_template, target_spec)

            import_lines = [
                self._compute_import_statement(target_spec, summary_spec_path),
                self._compute_import_statement(target_spec, call_res_spec),
            ]
            erc7201_spec = user_erc7201_spec_path(project_root)
            if erc7201_spec.exists():
                import_lines.append(self._compute_import_statement(target_spec, erc7201_spec))

            self._prepend_imports_to_spec(target_spec, import_lines)

    def _compute_import_statement(self, spec_file_path: Path, summaries_spec_path: Path) -> str:
        """Compute the relative import statement from spec file to summaries spec."""
        try:
            import_path = os.path.relpath(summaries_spec_path, spec_file_path.parent)
        except Exception as e:
            self.log(f"Cannot create relative path for {summaries_spec_path} from {spec_file_path.parent}", "ERROR")
            raise Exception(f"Cannot compute relative import path: {e}")

        import_statement = f'import "{import_path}";\n'
        self.log(f"Computed import for {spec_file_path.name}: {import_statement.strip()}")
        return import_statement

    def _inject_import_to_spec(self, spec_file_path: Path, import_statement: str) -> None:
        """Inject import statement at the top of a spec file (idempotent)."""
        try:
            with open(spec_file_path, 'r') as f:
                content = f.read()

            if import_statement.strip() in content:
                self.log(f"Import already exists in {spec_file_path.name}")
                return

            new_content = import_statement + content

            with open(spec_file_path, 'w') as f:
                f.write(new_content)

            self.log(f"Injected summaries import into {spec_file_path.name}")

        except Exception as e:
            self.log(f"Error injecting import into {spec_file_path}: {e}", "ERROR")
            raise

    def _prepend_imports_to_spec(self, spec_file_path: Path, import_statements: list[str]) -> None:
        """Prepend a block of import statements to a spec file, skipping any already present."""
        with open(spec_file_path, 'r') as f:
            content = f.read()

        new_imports = [imp for imp in import_statements if imp.strip() not in content]
        if not new_imports:
            return

        with open(spec_file_path, 'w') as f:
            f.write("".join(new_imports) + content)

        self.log(f"Prepended {len(new_imports)} import(s) to {spec_file_path.name}")

    def create_spec_with_summary_import(
        self, source_spec: Path, target_spec: Path, summary_spec: Optional[Path] = None
    ) -> Path:
        """Create a spec file by copying a source and optionally injecting a summary import."""
        shutil.copy2(source_spec, target_spec)

        if summary_spec is not None and summary_spec.exists():
            import_statement = self._compute_import_statement(target_spec, summary_spec)
            self._inject_import_to_spec(target_spec, import_statement)

        return target_spec
