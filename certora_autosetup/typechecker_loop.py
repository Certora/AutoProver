"""
Typechecker Loop Handler

This module handles typechecker errors by creating copies of spec files and configs
instead of modifying files in-place. It manages rounds of typechecking with proper
file versioning and import management.
"""


import json
import os
import re
import subprocess
import uuid
from collections import defaultdict, deque
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from certora_autosetup.parsers.prover_config_parser import get_spec_from_verify_field
from certora_autosetup.parsers.spec_imports import parse_imports_from_spec
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.constants import SUMMARIES_SUBDIR
from certora_autosetup.utils.paths import internal_round_summaries_dir, internal_typechecker_round_dir

# Type alias for spec update callbacks
# Takes: (spec_path, rename_fn, reverse_rename_fn) -> returns new_spec_path
SpecUpdateCallback = Callable[[Path, Callable[[str], str], Callable[[str], str]], Path]


class TypecheckerLoop:
    """Manages typechecker error handling through spec/config copying."""

    def __init__(self, certora_dir: Path, verbose: bool = False, keep_intermediate_files: bool = False):
        """
        Initialize the TypecheckerLoop.

        Args:
            certora_dir: Path to the certora directory
            verbose: Enable verbose logging
            keep_intermediate_files: If True, create round files; if False, overwrite existing files
        """
        self.certora_dir = certora_dir
        self.verbose = verbose
        self.keep_intermediate_files = keep_intermediate_files
        self.round_number = 0
        self.current_random_string: str | None = None
        self.base_config_name: str | None = None  # Store original config basename
        self.base_spec_name: str | None = None  # Store original spec basename
        self.spec_base_names: dict[Path, str] = {}  # Map spec file paths to their base names (without ROUND suffixes)

    def log(self, message: str, level: str = "INFO"):
        """Log messages using centralized logger."""
        logger.log(message, level, "TypecheckerLoop")

    def _generate_round_suffix(self, round_num: int, random_string: str) -> str:
        """
        Generate the suffix for round-based file copies.

        Args:
            round_num: The current round number
            random_string: The random string for this round

        Returns:
            The suffix string (e.g., "-ROUND1-abc123")
        """
        return f"-ROUND{round_num}-{random_string}"

    def _parse_typechecker_errors(
        self, error_output: str
    ) -> List[Tuple[str, int, str, str]]:
        """
        Parse typechecker output for external method declaration errors.

        Args:
            error_output: Combined stderr and stdout from typechecker

        Returns:
            List of tuples: (spec_file, line_num, contract, method)
        """
        matches = []

        # Pattern 1: External method declaration errors
        # Error in spec file (extload.spec:6:5): External method declaration for CorkPool.exttload(bytes32 slot) returns (bytes32) does not correspond to any known declaration
        external_method_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): External method declaration for ([^\s.]+)\.([^\(]+)\([^\)]*\).*?does not correspond to any known declaration"
        external_matches = re.findall(external_method_pattern, error_output)
        matches.extend(external_matches)

        # Pattern 2: Math.Rounding enum type errors
        # CRITICAL: [main] ERROR ALWAYS - Error in spec file (OZ_Math.spec:12:21): could not type expression "Math.Rounding.Ceil", message: In enum constant Math.Rounding.Ceil, Math.Rounding is not a valid enum type
        rounding_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\):.*?Math\.Rounding is not a valid enum type"
        rounding_matches = re.findall(rounding_pattern, error_output)

        # For rounding errors, we use special markers to indicate this is a rounding error
        for spec_file, line_num in rounding_matches:
            matches.append((spec_file, line_num, "ROUNDING_ERROR", "Math.Rounding"))

        # Pattern 3: Contract not found errors
        # Error in spec file (OZ_Math-ERC20.spec:4:5): Contract `Math` not found. Receiver contracts must be `currentContract`, the name of a contract in the scene, or a name introduced by a `using` statement.
        contract_not_found_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): Contract `([^`]+)` not found\."
        contract_not_found_matches = re.findall(contract_not_found_pattern, error_output)

        # For contract not found errors, we use special markers
        for spec_file, line_num, contract_name in contract_not_found_matches:
            matches.append((spec_file, line_num, "CONTRACT_NOT_FOUND", contract_name))

        # Pattern 4: Incompatible return type errors
        # Error in spec file (extload-PoolManager.spec:5:5): Cannot merge "PoolManager.extsload(...) returns (bytes32[])" and "..." - they have incompatible return values
        incompatible_return_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): Cannot merge \"([^\.]+)\.([^\(]+)\([^\)]*\) returns \([^\)]+\)\".*?they have incompatible return values"
        incompatible_return_matches = re.findall(incompatible_return_pattern, error_output)

        # Mark these as incompatible return errors
        for spec_file, line_num, contract, method in incompatible_return_matches:
            matches.append((spec_file, line_num, "INCOMPATIBLE_RETURN", f"{contract}.{method}"))

        # Pattern 5: Internal method entry not found errors
        # CRITICAL: [main] ERROR ALWAYS - Error in spec file (FixedPointMathLib.spec:4:5): Internal method entry FixedPointMathLib.mulDivDown(uint256 x, uint256 y, uint256 denominator) returns (uint256) does not appear in code.
        internal_method_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): Internal method entry ([^\s.]+)\.([^\(]+)\([^\)]*\).*?does not appear in code"
        internal_matches = re.findall(internal_method_pattern, error_output)
        matches.extend(internal_matches)

        if self.verbose and matches:
            self.log(f"Found {len(matches)} typechecker errors to fix")
            for spec_file, line_num, contract, method in matches:
                if contract == "ROUNDING_ERROR":
                    self.log(f"  - {spec_file}:{line_num} - Math.Rounding enum error")
                elif contract == "CONTRACT_NOT_FOUND":
                    self.log(f"  - {spec_file}:{line_num} - Contract `{method}` not found")
                else:
                    self.log(f"  - {spec_file}:{line_num} - {contract}.{method}")

        return matches

    def generate_updates_to_specs_from_errors(
        self, errors: List[Tuple[str, int, str, str]]
    ) -> Dict[str, SpecUpdateCallback]:
        """
        Generate update callbacks for each erroneous spec file.

        This method analyzes typechecker errors and produces update functions that will fix
        each problematic spec file. The callbacks are keyed by the base spec name (without
        ROUND suffixes).

        Args:
            errors: List of tuples (spec_file, line_num, contract, method) from _parse_typechecker_errors

        Returns:
            Dictionary mapping spec base names to update callbacks.
            Each callback has signature: (spec_path, rename_fn, reverse_rename_fn) -> new_spec_path
        """
        updates = {}

        # Group errors by spec file to handle multiple errors in same spec
        errors_by_spec = defaultdict(list)
        for spec_file, line_num, contract, method in errors:
            errors_by_spec[spec_file].append((line_num, contract, method))

        for spec_file, spec_errors in errors_by_spec.items():
            spec_base = Path(spec_file).stem

            # Check error type - Math.Rounding, contract not found, or a summary line
            # referencing a method/declaration that does not exist in the compiled code
            # (external method declaration / incompatible return / internal method entry).
            has_rounding_error = any(
                contract == "ROUNDING_ERROR" for _, contract, _ in spec_errors
            )
            has_contract_not_found_error = any(
                contract == "CONTRACT_NOT_FOUND" for _, contract, _ in spec_errors
            )
            summary_line_for_nonexisting_method_error = any(
                contract not in ["ROUNDING_ERROR", "CONTRACT_NOT_FOUND"] for _, contract, _ in spec_errors
            )

            if has_rounding_error:
                # Create callback for Math.Rounding error fix
                def create_rounding_fix_callback(original_spec_file, keep_intermediate):
                    def fix_rounding(
                        spec_path: Path,
                        rename_fn: Callable[[str], str],
                        reverse_rename_fn: Callable[[str], str],
                    ) -> Path:
                        """Fix Math.Rounding enum errors by commenting out specific lines."""
                        # Create new spec name with round suffix (or overwrite if not keeping intermediate files)
                        if keep_intermediate:
                            # Get the base name without ROUND suffixes
                            base_name = reverse_rename_fn(spec_path.stem)
                            new_spec_name = rename_fn(base_name)
                            new_spec = spec_path.parent / f"{new_spec_name}.spec"
                        else:
                            new_spec = spec_path

                        # Read original spec
                        with open(spec_path, "r") as f:
                            lines = f.readlines()

                        # Comment out line 5 (index 4) and lines 10-17 (indices 9-16)
                        lines_to_comment = [
                            4,
                            9,
                            10,
                            11,
                            12,
                            13,
                            14,
                            15,
                            16,
                        ]  # 0-indexed

                        for line_idx in lines_to_comment:
                            if line_idx < len(lines):
                                original_line = lines[line_idx].rstrip()
                                lines[line_idx] = (
                                    f"// AUTO-DISABLED (Math.Rounding error): {original_line}\n"
                                )

                        # Write new spec
                        with open(new_spec, "w") as f:
                            f.writelines(lines)

                        action = "Overwrote" if not keep_intermediate else "Created"
                        self.log(
                            f"{action} {new_spec.name} with Math.Rounding error fixes (commented lines 5, 10-17)"
                        )
                        return new_spec

                    return fix_rounding

                updates[spec_base] = create_rounding_fix_callback(spec_file, self.keep_intermediate_files)

            elif has_contract_not_found_error:
                # Create callback for contract not found error fix (comment out the problematic line)
                def create_contract_not_found_fix_callback(original_spec_file, error_list, keep_intermediate):
                    def fix_contract_not_found(
                        spec_path: Path,
                        rename_fn: Callable[[str], str],
                        reverse_rename_fn: Callable[[str], str],
                    ) -> Path:
                        """Fix contract not found errors by commenting out the problematic lines."""
                        # Create new spec name with round suffix (or overwrite if not keeping intermediate files)
                        if keep_intermediate:
                            # Get the base name without ROUND suffixes
                            base_name = reverse_rename_fn(spec_path.stem)
                            new_spec_name = rename_fn(base_name)
                            new_spec = spec_path.parent / f"{new_spec_name}.spec"
                        else:
                            new_spec = spec_path

                        # Read original spec
                        with open(spec_path, "r") as f:
                            lines = f.readlines()

                        # Collect line numbers to comment (convert to 0-indexed)
                        lines_to_comment = set()
                        contract_names = []
                        for line_num, contract, contract_name in error_list:
                            if contract == "CONTRACT_NOT_FOUND":
                                lines_to_comment.add(
                                    int(line_num) - 1
                                )  # Convert to 0-indexed
                                contract_names.append(contract_name)

                        # Comment out the problematic lines
                        for line_idx in lines_to_comment:
                            if line_idx < len(lines):
                                original_line = lines[line_idx].rstrip()
                                lines[line_idx] = (
                                    f"// AUTO-DISABLED (contract not found): {original_line}\n"
                                )

                        # Write new spec
                        with open(new_spec, "w") as f:
                            f.writelines(lines)

                        action = "Overwrote" if not keep_intermediate else "Created"
                        self.log(
                            f"{action} {new_spec.name} with {len(lines_to_comment)} contract not found error fixes"
                        )
                        return new_spec

                    return fix_contract_not_found

                updates[spec_base] = create_contract_not_found_fix_callback(spec_file, spec_errors, self.keep_intermediate_files)

            elif summary_line_for_nonexisting_method_error and self._is_in_summaries_folder(spec_file):
                # Create callback to comment out summary lines whose summarized method/declaration
                # does not exist in the compiled code (external method declaration / incompatible
                # return / internal method entry). Gated to the generated summaries folder so we
                # never auto-edit the user's hand-written specs.
                def create_nonexisting_method_fix_callback(original_spec_file, error_list, keep_intermediate):
                    def fix_nonexisting_method(
                        spec_path: Path,
                        rename_fn: Callable[[str], str],
                        reverse_rename_fn: Callable[[str], str],
                    ) -> Path:
                        """Comment out summary lines whose summarized method/declaration does not exist in code."""
                        # Create new spec name with round suffix (or overwrite if not keeping intermediate files)
                        if keep_intermediate:
                            # Get the base name without ROUND suffixes
                            base_name = reverse_rename_fn(spec_path.stem)
                            new_spec_name = rename_fn(base_name)
                            new_spec = spec_path.parent / f"{new_spec_name}.spec"
                        else:
                            new_spec = spec_path

                        # Read original spec
                        with open(spec_path, "r") as f:
                            lines = f.readlines()

                        # Collect line numbers to comment (convert to 0-indexed)
                        lines_to_comment = set()
                        for line_num, contract, method in error_list:
                            if contract != "ROUNDING_ERROR":
                                lines_to_comment.add(
                                    int(line_num) - 1
                                )  # Convert to 0-indexed

                        # Comment out the problematic lines
                        for line_idx in lines_to_comment:
                            if line_idx < len(lines):
                                original_line = lines[line_idx].rstrip()
                                lines[line_idx] = (
                                    f"// AUTO-DISABLED (summarized method not in code): {original_line}\n"
                                )

                        # Write new spec
                        with open(new_spec, "w") as f:
                            f.writelines(lines)

                        action = "Overwrote" if not keep_intermediate else "Created"
                        self.log(
                            f"{action} {new_spec.name} with {len(lines_to_comment)} nonexisting-method summary fixes"
                        )
                        return new_spec

                    return fix_nonexisting_method

                updates[spec_base] = create_nonexisting_method_fix_callback(
                    spec_file, spec_errors, self.keep_intermediate_files
                )

        if self.verbose:
            self.log(f"Generated {len(updates)} update callbacks for erroneous specs")
            for spec_base in updates.keys():
                self.log(f"  - {spec_base}")

        return updates

    def _is_in_summaries_folder(self, spec_file: str) -> bool:
        """
        Check if a spec file lives anywhere under the bundled summaries folder
        (certora/specs/summaries/), including nested subdirectories such as
        certora/specs/summaries/OpenZeppelin/.

        Args:
            spec_file: The spec file path from error message. Typechecker errors carry
                only the basename, so we match by filename anywhere beneath the
                summaries tree (not just at its top level).

        Returns:
            True if the file is somewhere under certora/specs/summaries/
        """
        if str(SUMMARIES_SUBDIR) in spec_file:
            return True

        summaries_root = self.certora_dir / SUMMARIES_SUBDIR
        if not summaries_root.exists():
            return False

        # The file may live in a nested subdir under summaries/ (e.g. OpenZeppelin/),
        # and the error message carries only the basename — walk the summaries tree.
        target = Path(spec_file).name
        for _root, dirs, files in os.walk(summaries_root):
            # Skip .certora_internal directories (build artifacts)
            dirs[:] = [d for d in dirs if d != ".certora_internal"]
            if target in files:
                return True
        return False

    def _find_spec_file(self, spec_name: str) -> Optional[Path]:
        """
        Find a spec file in the certora directory.

        Args:
            spec_name: Name or partial path of the spec file

        Returns:
            Path to the spec file if found, None otherwise
        """
        # First try as-is
        if Path(spec_name).exists():
            return Path(spec_name)

        # Try in certora directory
        spec_path = self.certora_dir / spec_name
        if spec_path.exists():
            return spec_path

        # Try just the filename in certora tree
        spec_filename = Path(spec_name).name
        matches = [
            spec_file
            for spec_file in self.certora_dir.rglob(spec_filename)
            if ".certora_internal" not in str(spec_file)
        ]
        if len(matches) > 1:
            raise Exception(
                f"FATAL: Ambiguous spec lookup for {spec_name!r} under {self.certora_dir}: "
                f"found {len(matches)} matches: {[str(m) for m in matches]}"
            )
        if matches:
            return matches[0]

        return None

    def _build_spec_dependency_graph(
        self, root_spec: Path
    ) -> Tuple[Dict[Path, List[Path]], Dict[Path, List[Path]]]:
        """
        Build a dependency graph of spec files starting from a root spec.

        Args:
            root_spec: The root spec file to start from

        Returns:
            Tuple of (forward_graph, reverse_graph) where:
            - forward_graph[spec] = list of specs that 'spec' imports
            - reverse_graph[spec] = list of specs that import 'spec' (ancestors)
        """
        forward_graph: dict[Path, list[Path]] = defaultdict(list)
        reverse_graph: dict[Path, list[Path]] = defaultdict(list)
        visited = set()
        queue = deque([root_spec])

        while queue:
            current_spec = queue.popleft()
            if current_spec in visited:
                continue
            visited.add(current_spec)

            # Parse imports from current spec
            imports = parse_imports_from_spec(current_spec, recursive=False)

            for imported_spec in imports:
                # Normalize both paths for comparison
                current_normalized = current_spec.resolve()
                imported_normalized = imported_spec.resolve()

                # Add to forward graph
                if imported_normalized not in forward_graph[current_normalized]:
                    forward_graph[current_normalized].append(imported_normalized)

                # Add to reverse graph (imported_spec is imported by current_spec)
                if current_normalized not in reverse_graph[imported_normalized]:
                    reverse_graph[imported_normalized].append(current_normalized)

                # Add imported spec to queue for further processing
                if imported_normalized not in visited:
                    queue.append(imported_normalized)

        return dict(forward_graph), dict(reverse_graph)

    def _find_all_ancestors(
        self, spec_path: Path, reverse_graph: Dict[Path, List[Path]]
    ) -> List[Path]:
        """
        Find all ancestors (specs that import this spec, directly or indirectly) using BFS.

        Args:
            spec_path: The spec file to find ancestors for
            reverse_graph: The reverse dependency graph

        Returns:
            List of all ancestor spec paths
        """
        ancestors = []
        visited = set()
        queue = deque([spec_path.resolve()])

        while queue:
            current = queue.popleft()

            # Get all specs that import the current spec
            for parent in reverse_graph.get(current, []):
                if parent not in visited:
                    visited.add(parent)
                    ancestors.append(parent)
                    queue.append(parent)

        return ancestors

    def _copy_and_comment_summaries_spec(
        self,
        original_summaries: Path,
        problematic_specs: List[str],
        round_num: int,
        random_string: str,
        fixed_specs: Dict[str, str] | None = None,
    ) -> Path:
        """
        Create a copy of summaries.spec with problematic imports commented out and fixed spec imports updated.

        Args:
            problematic_specs: List of spec files to comment out
            round_num: Current round number
            random_string: Random string for this round
            fixed_specs: Dict mapping original spec names to fixed spec paths

        Returns:
            Path to the new summaries spec file

        Raises:
            Exception: If summaries.spec not found or file operations fail
        """

        if not original_summaries.exists():
            raise Exception(
                f"FATAL: current summaries.spec not found at {original_summaries}"
            )

        # Create new summaries spec name
        suffix = self._generate_round_suffix(round_num, random_string)
        round_summaries_dir = internal_round_summaries_dir(self.certora_dir.parent)
        round_summaries_dir.mkdir(parents=True, exist_ok=True)
        new_summaries = round_summaries_dir / f"summaries{suffix}.spec"

        # Read original summaries.spec
        with open(original_summaries, "r") as f:
            lines = f.readlines()

        # Process each line for commenting and updating imports
        modified_lines = []
        fixed_specs = fixed_specs or {}

        for line in lines:
            should_comment = False
            updated_line = line

            # Check if this line should be commented out
            for problematic_spec in problematic_specs:
                spec_name = Path(problematic_spec).stem  # Get name without .spec
                if "import" in line and spec_name in line:
                    should_comment = True
                    break

            # Check if this line should be updated with a fixed spec
            if not should_comment and "import" in line:
                for original_spec, fixed_spec_path in fixed_specs.items():
                    original_name = Path(original_spec).stem
                    if original_name in line:
                        # Update the import to point to the fixed spec
                        fixed_name = Path(fixed_spec_path).name
                        updated_line = line.replace(f"{original_name}.spec", fixed_name)
                        self.log(
                            f"Updated import: {line.strip()} -> {updated_line.strip()}"
                        )
                        break

            if should_comment:
                modified_lines.append(f"// AUTO-DISABLED (Round {round_num}): {line}")
                self.log(f"Commented out import: {line.strip()}")
            else:
                modified_lines.append(updated_line)

        # Write new summaries spec
        with open(new_summaries, "w") as f:
            f.writelines(modified_lines)

        self.log(
            f"Created {new_summaries.name} with {len(problematic_specs)} imports commented out"
        )
        return new_summaries

    def _copy_and_update_spec_imports(
        self,
        original_spec: str,
        rename_function: Callable[[str], str],
        base_name: str,
        updated_imports: Dict[str, str],
    ) -> Path:
        """
        Create a copy of a spec file with updated imports.

        Args:
            original_spec: Path to the original spec file
            round_num: Current round number
            random_string: Random string for this round
            base_name: Base name to use for the new spec file
            updated_imports: Dict mapping old import paths to new import paths

        Returns:
            Path to the new spec file

        Raises:
            Exception: If spec file not found or file operations fail
        """
        original_path = self._find_spec_file(original_spec)

        if not original_path:
            raise Exception(f"FATAL: Spec file not found: {original_spec}")

        # Create new spec name
        new_spec = original_path.parent / f"{rename_function(base_name)}.spec"

        # Read original spec
        with open(original_path, "r") as f:
            content = f.read()

        # Update all imports based on the updated_imports mapping
        for old_import, new_import in updated_imports.items():
            # Extract just the filename from old_import and new_import
            old_filename = Path(old_import).name
            new_filename = Path(new_import).name

            # Find all import statements and replace only those that end with the old filename
            # This handles cases like:
            #   import "summaries.spec" -> import "summaries-ROUND1.spec"
            #   import "../summaries.spec" -> import "../summaries-ROUND1.spec"
            #   import "../../certora/summaries.spec" -> import "../../certora/summaries-ROUND1.spec"

            def replace_import_filename(match):
                """Replace just the filename portion of an import path."""
                full_import_path = match.group(1)
                # Split the path and replace the last component (filename)
                if "/" in full_import_path:
                    path_parts = full_import_path.rsplit("/", 1)
                    if path_parts[1] == old_filename:
                        return f'import "{path_parts[0]}/{new_filename}"'
                else:
                    # No directory, just filename
                    if full_import_path == old_filename:
                        return f'import "{new_filename}"'
                # No match, return unchanged
                return match.group(0)

            # Match any import statement
            pattern = r'import\s+"([^"]+)"'
            content = re.sub(pattern, replace_import_filename, content)

        # Write new spec
        with open(new_spec, "w") as f:
            f.write(content)

        self.log(f"Created {new_spec.name} with updated imports")
        return new_spec

    def _copy_and_update_config(
        self,
        config_path: Path,
        new_spec_path: Path,
        round_num: int,
        random_string: str,
        base_name: str,
    ) -> Path:
        """
        Create a copy of the config file pointing to the new spec.

        Args:
            config_path: Path to the original config file
            new_spec_path: Path to the new spec file
            round_num: Current round number
            random_string: Random string for this round
            base_name: Base name to use for the new config file

        Returns:
            Path to the new config file

        Raises:
            Exception: If config file operations fail or verify field is invalid
        """
        # Create new config name
        suffix = self._generate_round_suffix(round_num, random_string)
        round_dir = internal_typechecker_round_dir(self.certora_dir.parent)
        round_dir.mkdir(parents=True, exist_ok=True)
        new_config = round_dir / f"{base_name}{suffix}.conf"

        # Read original config
        with open(config_path, "r") as f:
            config = json.load(f)

        # Update verify field
        if ":" in config.get("verify", ""):
            contract, _ = config["verify"].split(":", 1)
            project_root = self.certora_dir.parent
            new_spec_relative = str(new_spec_path.resolve().relative_to(project_root.resolve()))
            config["verify"] = f"{contract}:{new_spec_relative}"
        else:
            raise Exception(
                f"FATAL: Invalid verify field in config: {config.get('verify')}"
            )

        # Write new config
        with open(new_config, "w") as f:
            json.dump(config, f, indent=2)

        self.log(f"Created {new_config.name} pointing to {new_spec_path.name}")
        return new_config

    def perform_recursive_update(
        self,
        updates: Dict[str, SpecUpdateCallback],
        main_spec: str,
        rename_function: Callable[[str], str],
        reverse_rename_function: Callable[[str], str],
    ) -> str:
        """
        Apply spec updates and propagate changes through the dependency graph.

        This method:
        1. Builds dependency graph from main spec
        2. Finds all ancestor specs of the specs that need fixing
        3. Combines specs needing fixes and their ancestors
        4. Sorts all specs in topological order (dependencies before dependents)
        5. Processes specs in topological order:
           - For specs with fixes: applies update callbacks
           - For ancestor specs: updates imports to point to fixed specs
        6. Returns the path to the new main spec

        Args:
            updates: Dict mapping spec base names to update callbacks
            main_spec: Path to the main spec file
            rename_function: Function to generate versioned name from base name
            reverse_rename_function: Function to extract base name from versioned name

        Returns:
            Path to the new main spec file (str)

        Raises:
            Exception: If spec files cannot be found or operations fail
        """
        # Phase 1: Build dependency graph from main spec
        main_spec_path = self._find_spec_file(main_spec)
        if not main_spec_path:
            raise Exception(f"FATAL: Could not find main spec: {main_spec}")

        forward_graph, reverse_graph = self._build_spec_dependency_graph(main_spec_path)

        # Phase 2: Find all ancestors of the specs that need fixing
        all_ancestors = set()
        specs_needing_fix = []

        for spec_base in updates.keys():
            original_spec = self._find_spec_file(f"{spec_base}.spec")
            if not original_spec:
                self.log(
                    f"Warning: Could not find spec file for {spec_base}, skipping",
                    "WARNING",
                )
                continue

            original_spec_resolved = original_spec.resolve()
            specs_needing_fix.append(original_spec_resolved)

            # Find all ancestors of this spec that will need import updates
            ancestors = self._find_all_ancestors(original_spec_resolved, reverse_graph)
            all_ancestors.update(ancestors)
            self.log(f"Found {len(ancestors)} ancestor(s) of {spec_base}.spec")

        # Always include the main spec if it's an ancestor
        if (
            main_spec_path.resolve() not in all_ancestors
            and main_spec_path.resolve() not in specs_needing_fix
        ):
            all_ancestors.add(main_spec_path.resolve())

        self.log(
            f"Found {len(all_ancestors)} total ancestor spec(s) that need updating: {all_ancestors}"
        )

        # Phase 3: Combine specs needing fixes and their ancestors into one list for topological sort
        specs_to_update = list(specs_needing_fix) + list(all_ancestors)

        # Phase 4: Sort specs in topological order (dependencies before dependents)
        def topological_sort(specs, forward_graph):
            """Sort specs so dependencies come before dependents."""
            # Calculate in-degree (number of imports from other specs in our update list)
            in_degree = {spec: 0 for spec in specs}
            for spec in specs:
                for imported_spec in forward_graph.get(spec, []):
                    if imported_spec in in_degree:
                        in_degree[spec] += 1

            # Start with specs that have no dependencies (or all deps are external)
            queue = deque([spec for spec in specs if in_degree[spec] == 0])
            sorted_specs = []

            while queue:
                current = queue.popleft()
                sorted_specs.append(current)

                # For each spec that imports current, decrease its in-degree
                for spec in specs:
                    if current in forward_graph.get(spec, []):
                        in_degree[spec] -= 1
                        if in_degree[spec] == 0:
                            queue.append(spec)

            return sorted_specs

        specs_to_update = topological_sort(specs_to_update, forward_graph)

        # Phase 5: Track mapping of old import paths to new import paths
        import_updates: dict[str, str] = {}

        # Phase 6: Process all specs in topological order (specs needing fixes + ancestors)
        # Apply update callbacks where needed, and update imports for all
        new_main_spec = None

        # When not keeping intermediate files, only process specs with fixes, skip ancestors
        specs_to_process = specs_needing_fix if not self.keep_intermediate_files else specs_to_update

        for spec_path in specs_to_process:
            # Get or store the base name for this spec
            spec_path_resolved = spec_path.resolve()
            if spec_path_resolved not in self.spec_base_names:
                # First time seeing this spec - extract and store its base name
                self.spec_base_names[spec_path_resolved] = reverse_rename_function(
                    spec_path.stem
                )

            spec_basename = self.spec_base_names[spec_path_resolved]

            # Todo we'll need to combine updating with import updates later on by chaining callbacks.
            # Check if this spec has a fix to apply (it's in the updates dict)
            if spec_basename in updates:
                # This spec needs to be fixed - apply the update callback
                update_callback = updates[spec_basename]
                new_spec = update_callback(
                    spec_path, rename_function, reverse_rename_function
                )
                self.log(
                    f"Applied update callback: {spec_path.name} -> {new_spec.name}"
                )
            elif self.keep_intermediate_files:
                # This is an ancestor spec - just update its imports (only if keeping intermediate files)
                # Build the import updates for this specific spec
                spec_import_updates = {}
                for old_rel, new_rel in import_updates.items():
                    spec_import_updates[old_rel] = new_rel

                # Create updated version of this spec with updated imports
                new_spec = self._copy_and_update_spec_imports(
                    str(spec_path),
                    rename_function,
                    base_name=spec_basename,
                    updated_imports=spec_import_updates,
                )
                self.log(f"Updated imports: {spec_path.name} -> {new_spec.name}")
            else:
                # Not keeping intermediate files and this isn't a spec needing fixes - skip
                continue

            # Update the mapping to point to the new spec path (for next iteration)
            new_spec_resolved = new_spec.resolve()
            self.spec_base_names[new_spec_resolved] = spec_basename

            # Add this spec's mapping for specs that import it (only if keeping intermediate files)
            if self.keep_intermediate_files:
                old_spec_name = spec_path.name
                new_spec_name = new_spec.name
                import_updates[old_spec_name] = new_spec_name

            # Track the main spec
            if spec_path.resolve() == main_spec_path.resolve():
                new_main_spec = new_spec

        # When not keeping intermediate files, the main spec path doesn't change
        if not new_main_spec:
            if not self.keep_intermediate_files:
                # Main spec wasn't modified, return the original path
                new_main_spec = main_spec_path
            else:
                raise Exception("FATAL: Failed to create new main spec")

        return str(new_main_spec)

    def handle_typechecker_round(
        self, cmd: List[str], error_output: str
    ) -> Optional[List[str]]:
        """
        Handle one round of typechecker errors by creating spec/config copies.

        This method orchestrates the two-phase error fixing process:
        1. Generate update callbacks from errors
        2. Perform recursive update of all dependent specs

        Args:
            cmd: The original certoraRun command
            error_output: The error output from the typechecker

        Returns:
            New command with updated config path, or None if fatal error
        """
        # Increment round number
        self.round_number += 1
        self.current_random_string = str(uuid.uuid4())[:8]

        self.log(
            f"=== Starting Typechecker Round {self.round_number} (ID: {self.current_random_string}) ==="
        )

        # Parse errors
        errors = self._parse_typechecker_errors(error_output)
        if not errors:
            self.log("No fixable typechecker errors found", "WARNING")
            return None

        # Extract config path from command
        if len(cmd) < 2:
            self.log("FATAL: Invalid command - no config file specified", "ERROR")
            return None

        config_path = Path(cmd[1])
        if not config_path.exists():
            self.log(f"FATAL: Config file not found: {config_path}", "ERROR")
            return None

        # Store base config name on first round
        if self.base_config_name is None:
            self.base_config_name = config_path.stem

        # Read config to get main spec
        with open(config_path, "r") as f:
            config = json.load(f)
        main_spec = get_spec_from_verify_field(config.get("verify", ""))
        if not main_spec:
            raise Exception("FATAL: Could not extract spec from config verify field")

        # Store base spec name on first round
        if self.base_spec_name is None:
            self.base_spec_name = Path(main_spec).stem

        # Define rename and reverse rename functions
        def rename_function(base_name: str) -> str:
            """Generate versioned name from base name."""
            assert self.current_random_string is not None
            return f"{base_name}{self._generate_round_suffix(self.round_number, self.current_random_string)}"

        def reverse_rename_function(current_name: str) -> str:
            """Extract base name from versioned name."""
            # Check if this spec is tracked in spec_base_names
            for spec_path, base_name in self.spec_base_names.items():
                if (
                    spec_path.stem == current_name
                    or Path(current_name).stem == spec_path.stem
                ):
                    return base_name

            # Fallback: strip ROUND suffix using regex
            # Pattern: name-ROUNDN-randomstr -> name
            import re

            match = re.match(r"^(.+?)-ROUND\d+-[a-f0-9]+$", current_name)
            if match:
                return match.group(1)

            # No ROUND suffix found, return as-is
            return current_name

        # PHASE 1: Generate update callbacks from errors
        self.log("Phase 1: Generating update callbacks from errors...")
        updates = self.generate_updates_to_specs_from_errors(errors)

        if not updates:
            self.log("No fixable errors found", "WARNING")
            return None

        # PHASE 2: Perform recursive update
        self.log("Phase 2: Performing recursive update of all dependent specs...")
        try:
            new_main_spec_path = self.perform_recursive_update(
                updates, main_spec, rename_function, reverse_rename_function
            )
        except Exception as e:
            self.log(f"FATAL: Failed to perform recursive update: {e}", "ERROR")
            import traceback

            self.log(traceback.format_exc(), "ERROR")
            return None

        # Create new config pointing to the updated main spec (only if keeping intermediate files)
        new_main_spec = Path(new_main_spec_path)
        if self.keep_intermediate_files:
            assert self.current_random_string is not None
            assert self.base_config_name is not None
            new_config = self._copy_and_update_config(
                config_path,
                new_main_spec,
                self.round_number,
                self.current_random_string,
                base_name=self.base_config_name,
            )
            # Create new command with updated config
            new_cmd = cmd.copy()
            new_cmd[1] = str(new_config)
            self.log(
                f"Round {self.round_number} setup complete - will retry with {new_config.name}"
            )
        else:
            # No need to update config when overwriting files
            new_cmd = cmd.copy()
            self.log(
                f"Round {self.round_number} setup complete - overwrote spec files in place"
            )
        return new_cmd

    def _comment_out_rules_with_no_instantiations(self, output: str, cmd: List[str]) -> None:
        """Comment out `use rule` statements for rules that have no valid instantiations after filtering.

        Parses the rule names from the typechecker output, finds all spec files via the config's
        dependency graph, and comments out matching `use rule RULE_NAME;` lines.
        """
        rule_names = set(re.findall(r"for rule (\w+) remains with no valid instantiation", output))
        if not rule_names:
            return

        self.log(f"Rules with no valid instantiations: {', '.join(sorted(rule_names))}")

        # Find all spec files via config dependency graph
        config_path = Path(cmd[1])
        with open(config_path, "r") as f:
            config = json.load(f)
        main_spec = get_spec_from_verify_field(config.get("verify", ""))
        if not main_spec:
            self.log("Could not extract spec from config — skipping rule comment-out", "WARNING")
            return

        main_spec_path = self._find_spec_file(main_spec)
        if not main_spec_path:
            self.log(f"Could not find spec file '{main_spec}' — skipping rule comment-out", "WARNING")
            return
        main_spec_path = main_spec_path.resolve()
        forward_graph, _ = self._build_spec_dependency_graph(main_spec_path)
        all_specs: set[Path] = {main_spec_path}
        for deps in forward_graph.values():
            all_specs.update(deps)

        for spec_path in all_specs:
            if not spec_path.exists():
                continue
            lines = spec_path.read_text().splitlines(keepends=True)
            modified = False
            for i, line in enumerate(lines):
                stripped = line.lstrip()
                for rule_name in rule_names:
                    if stripped.startswith(f"use rule {rule_name}"):
                        lines[i] = f"// AUTO-DISABLED (no valid instantiations): {line}"
                        self.log(f"Commented out 'use rule {rule_name}' in {spec_path.name}")
                        modified = True
                        break
            if modified:
                spec_path.write_text("".join(lines))

    def _parse_rule_ranges(self, lines: list[str]) -> list[Tuple[str, int, int]]:
        """Parse all top-level rule definitions and return their (name, start_idx, end_idx) ranges.

        For each rule: finds the `rule` keyword line, then the first `{`, then uses brace
        counting to find the matching `}`. Handles `filtered { ... } { ... }` by continuing
        to the final closing brace.
        """
        rule_starts: list[Tuple[str, int]] = []
        for i, line in enumerate(lines):
            m = re.match(r"^\s*rule\s+(\w+)", line)
            if m:
                rule_starts.append((m.group(1), i))

        ranges: list[Tuple[str, int, int]] = []
        for name, start in rule_starts:
            brace_count = 0
            found_open = False
            end = start
            for j in range(start, len(lines)):
                for ch in lines[j]:
                    if ch == "{":
                        brace_count += 1
                        found_open = True
                    elif ch == "}":
                        brace_count -= 1
                if found_open and brace_count == 0:
                    # Check if next non-blank line starts a new block (filtered body)
                    k = j + 1
                    while k < len(lines) and lines[k].strip() == "":
                        k += 1
                    if k < len(lines) and lines[k].strip().startswith("{"):
                        # Continue counting through the body block
                        found_open = False
                        end = k
                        for j2 in range(k, len(lines)):
                            for ch in lines[j2]:
                                if ch == "{":
                                    brace_count += 1
                                    found_open = True
                                elif ch == "}":
                                    brace_count -= 1
                            if found_open and brace_count == 0:
                                end = j2
                                break
                        break
                    else:
                        end = j
                        break
            ranges.append((name, start, end))
        return ranges

    def _comment_out_autocvl_rules_with_missing_implementations(self, output: str, cmd: list[str]) -> bool:
        """Comment out rules in autocvl specs that have 'Did not find any implementations' errors.

        Returns True if any fixes were applied.
        """
        impl_pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): Did not find any implementations of"
        matches = re.findall(impl_pattern, output)
        if not matches:
            return False

        # Group by spec file, only autocvl specs
        errors_by_spec: dict[str, list[int]] = defaultdict(list)
        for spec_name, line_str in matches:
            if Path(spec_name).name.startswith("autocvl-"):
                errors_by_spec[spec_name].append(int(line_str) - 1)  # 0-based

        if not errors_by_spec:
            return False

        any_fixed = False
        for spec_name, error_lines in errors_by_spec.items():
            spec_path = self._find_spec_file(spec_name)
            if not spec_path or not spec_path.exists():
                self.log(f"Could not find autocvl spec file '{spec_name}' — skipping", "WARNING")
                continue

            lines = spec_path.read_text().splitlines(keepends=True)
            rule_ranges = self._parse_rule_ranges([l.rstrip("\n") for l in lines])

            # Find which rules contain the error lines (deduplicate)
            rules_to_disable: set[Tuple[str, int, int]] = set()
            for err_line in error_lines:
                for rule_name, start, end in rule_ranges:
                    if start <= err_line <= end:
                        rules_to_disable.add((rule_name, start, end))
                        break

            if not rules_to_disable:
                continue

            for rule_name, start, end in sorted(rules_to_disable, key=lambda x: x[1]):
                self.log(f"Commenting out rule '{rule_name}' (lines {start + 1}-{end + 1}) in {spec_path.name}")
                for i in range(start, end + 1):
                    if not lines[i].lstrip().startswith("//"):
                        lines[i] = "// AUTO-DISABLED (missing implementations): " + lines[i]

            spec_path.write_text("".join(lines))
            any_fixed = True

        return any_fixed

    def _comment_out_imports_with_missing_implementations(self, output: str, cmd: list[str]) -> None:
        """Comment out imports of bundled summary spec files that have 'Did not find any implementations' errors.

        Parses the spec filenames from the error output, finds the contract's summaries spec,
        and comments out matching import lines.
        """
        impl_pattern = r"Error in spec file \(([^:]+):.*?\): Did not find any implementations of"
        specs_to_disable = set(re.findall(impl_pattern, output))
        if not specs_to_disable:
            return

        self.log(f"Specs with missing implementations: {', '.join(sorted(specs_to_disable))}")

        # Find the summaries spec for this contract via the config's verify field
        config_path = Path(cmd[1])
        with open(config_path, "r") as f:
            config = json.load(f)
        verify = config.get("verify", "")
        contract_name = verify.split(":")[0] if ":" in verify else verify
        summaries_file = (
            self.certora_dir / SUMMARIES_SUBDIR / f"{contract_name}_base_summaries.spec"
        )
        if not summaries_file.exists():
            self.log(f"Summaries file not found: {summaries_file}", "WARNING")
            return

        lines = summaries_file.read_text().splitlines(keepends=True)
        modified = False
        for i, line in enumerate(lines):
            if not line.strip().startswith("import"):
                continue
            for spec_name in specs_to_disable:
                stem = Path(spec_name).stem
                if stem in line:
                    lines[i] = f"// AUTO-DISABLED (missing implementations): {line}"
                    self.log(f"Commented out import of {spec_name} in {summaries_file.name}")
                    modified = True
                    break
        if modified:
            summaries_file.write_text("".join(lines))

    def _comment_out_nondet_summaries_for_reference_types(self, output: str, cmd: list[str]) -> bool:
        """Comment out NONDET summary lines that fail because return types are reference types.

        Returns True if any fixes were applied.
        """
        pattern = r"Error in spec file \(([^:]+):(\d+):\d+\): Cannot use NONDET summary for function with return type"
        matches = re.findall(pattern, output)
        if not matches:
            return False

        # Group by spec file, deduplicate lines
        lines_by_spec: dict[str, set[int]] = defaultdict(set)
        for spec_name, line_str in matches:
            lines_by_spec[spec_name].add(int(line_str) - 1)  # 0-based

        any_fixed = False
        for spec_name, error_lines in lines_by_spec.items():
            spec_path = self._find_spec_file(spec_name)
            if not spec_path or not spec_path.exists():
                self.log(f"Could not find spec file '{spec_name}' — skipping", "WARNING")
                continue

            lines = spec_path.read_text().splitlines(keepends=True)
            for i in sorted(error_lines):
                if i < len(lines) and not lines[i].lstrip().startswith("//"):
                    lines[i] = "// AUTO-DISABLED (NONDET unsound for reference types): " + lines[i]
                    self.log(f"Commented out NONDET summary at line {i + 1} in {spec_path.name}")

            spec_path.write_text("".join(lines))
            any_fixed = True

        return any_fixed

    def run_typechecker_loop(
        self, initial_cmd: List[str], max_rounds: int = 10
    ) -> Tuple[bool, List[str]]:
        """
        Run the typechecker loop with automatic error fixing.
        Always runs with --compilation_steps_only to avoid submitting jobs during fixing.

        Args:
            initial_cmd: The initial certoraRun command
            max_rounds: Maximum number of rounds to attempt

        Returns:
            Tuple of (success, final_cmd) where:
            - success: True if typechecker passed (possibly after fixes)
            - final_cmd: The command to run for actual verification (without --compilation_steps_only)
        """
        # Check if compilation_steps_only is already in the config
        config_path = Path(initial_cmd[1]) if len(initial_cmd) > 1 else None
        has_compilation_only_in_config = False

        if config_path and config_path.exists():
            with open(config_path, "r") as f:
                config_data = json.load(f)
                has_compilation_only_in_config = config_data.get(
                    "compilation_steps_only", False
                )

        # Add --compilation_steps_only if not already in config
        current_cmd = initial_cmd.copy()
        if not has_compilation_only_in_config:
            current_cmd.append("--compilation_steps_only")

        for round_num in range(max_rounds):
            self.log(
                f"Running typechecker (attempt {round_num + 1}/{max_rounds}) with command: {' '.join(current_cmd)}"
            )

            # Run the command
            result = subprocess.run(
                current_cmd, capture_output=True, text=True, check=False
            )

            # Check for success or ignored filtering errors
            combined_output = (result.stderr or "") + (result.stdout or "")

            # Comment out rules with no valid instantiations and retry
            if "remains with no valid instantiations" in combined_output:
                self.log("Commenting out rules with no valid instantiations...", "WARNING")
                self._comment_out_rules_with_no_instantiations(combined_output, current_cmd)
                continue

            # Comment out rules/imports with missing implementations and retry
            if "Did not find any implementations of" in combined_output:
                autocvl_fixed = self._comment_out_autocvl_rules_with_missing_implementations(
                    combined_output, current_cmd
                )
                self._comment_out_imports_with_missing_implementations(combined_output, current_cmd)
                if autocvl_fixed:
                    self.log("Commented out autocvl rules with missing implementations", "WARNING")
                continue

            # Comment out NONDET summaries for reference return types and retry
            if "Cannot use NONDET summary for function with return type" in combined_output:
                if self._comment_out_nondet_summaries_for_reference_types(combined_output, current_cmd):
                    self.log("Commented out NONDET summaries for reference return types", "WARNING")
                continue

            if result.returncode == 0:
                self.log(f"✓ Typechecker passed after {self.round_number} rounds")
                # Return the final command for actual verification
                # If we had to fix things, use the updated config; otherwise use initial
                if self.round_number == 0:
                    final_cmd = initial_cmd.copy()
                else:
                    # Remove --compilation_steps_only unless it was in the original config
                    final_cmd = current_cmd.copy()
                    if (
                        not has_compilation_only_in_config
                        and "--compilation_steps_only" in final_cmd
                    ):
                        final_cmd.remove("--compilation_steps_only")
                return True, final_cmd

            # Try to fix errors
            error_output = (result.stderr or "") + (result.stdout or "")
            self.log(f"errors: {error_output}")
            new_cmd = self.handle_typechecker_round(current_cmd, error_output)

            if not new_cmd:
                self.log("No fixable errors or fatal error occurred", "ERROR")
                return False, []

            current_cmd = new_cmd

        self.log(f"Exceeded maximum rounds ({max_rounds})", "ERROR")
        return False, []
