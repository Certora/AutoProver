#!/usr/bin/env python3
"""
Foundry parser for extracting logic contracts from build artifacts.

This module parses Foundry's compilation output to identify which contracts
contain actual bytecode (logic contracts) vs interfaces/abstract contracts.
"""

import json
import sys
from pathlib import Path
from typing import Dict, List, Optional

from certora_autosetup.build_systems.foundry import FoundryManager
from certora_autosetup.parsers.base import ContractExtractor
from certora_autosetup.setup.solidity_utils import find_all_library_files_and_names
from certora_autosetup.utils import logger
from certora_autosetup.utils.types import ContractHandle


class FoundryContractExtractor(ContractExtractor):
    """Foundry-specific contract extractor."""

    def __init__(self, project_root: Path, profile: Optional[str] = None):
        """
        Initialize Foundry contract extractor.

        Args:
            project_root: Root directory of the project
            profile: Foundry profile to use for config resolution
        """
        super().__init__(project_root, FoundryManager, profile=profile)

    def build_source_path_to_contracts_map(self) -> Dict[str, List[tuple]]:
        """Build source_path -> [(contract_name, compiler_version)] mapping from Foundry artifacts.

        Walks out/{File}.sol/*.json, reads each artifact's compilationTarget and compiler version,
        and builds a dict mapping the full relative source path to (contract_name, version) tuples.
        Applies the same filters as extract_logic_contracts_impl: non-empty bytecode only,
        excludes libraries/deps/tests.
        """
        artifacts_dir = self.project_root / self.manager.get_default_artifact_dir()
        if not artifacts_dir.exists():
            fallback = self._try_read_artifact_dir_from_config()
            if fallback and fallback.exists():
                artifacts_dir = fallback
            else:
                return {}

        library_files = find_all_library_files_and_names()
        library_files = {Path(file).stem: names for file, names in library_files.items()}
        project_files = self.project_source_files()

        source_path_map: Dict[str, List[tuple]] = {}

        for subdir in sorted(artifacts_dir.iterdir()):
            if not subdir.is_dir() or not subdir.name.endswith(".sol"):
                continue
            if subdir.name.endswith(".t.sol") or subdir.name.endswith(".s.sol"):
                continue

            source_file_name = subdir.stem
            library_names = library_files.get(source_file_name, [])

            for json_file in sorted(subdir.glob("*.json")):
                contract_name = json_file.stem
                if contract_name in library_names:
                    continue

                try:
                    with open(json_file, "r") as f:
                        artifact = json.load(f)

                    bytecode = artifact.get("bytecode", {})
                    if not isinstance(bytecode, dict):
                        continue
                    bytecode_object = bytecode.get("object", "")
                    if not bytecode_object or bytecode_object == "0x":
                        continue

                    metadata = artifact.get("metadata")
                    if not metadata:
                        continue
                    settings = metadata.get("settings")
                    if not settings:
                        continue
                    compilation_target = settings.get("compilationTarget")
                    if not compilation_target or len(compilation_target) != 1:
                        continue

                    source_path, actual_contract_name = next(iter(compilation_target.items()))

                    # Extract compiler version (e.g. "0.8.33+commit..." -> "0.8.33")
                    compiler_version = metadata.get("compiler", {}).get("version", "")
                    compiler_version = compiler_version.split("+")[0]

                    if str(Path(source_path)) not in project_files:
                        continue

                    source_path_map.setdefault(source_path, []).append(
                        (actual_contract_name, compiler_version)
                    )

                except (json.JSONDecodeError, KeyError, Exception):
                    continue

        self.log(f"Built source-path contract map: {len(source_path_map)} source file(s)")
        for src_path, entries in source_path_map.items():
            stem = Path(src_path).stem
            if any(name != stem for name, _ver in entries):
                names = [name for name, _ver in entries]
                self.log(f"  {src_path} -> {names} (differs from filename stem '{stem}')")

        return source_path_map

    def extract_logic_contracts_impl(self, artifacts_dir: Path) -> List[ContractHandle]:
        """
        Extract logic contracts from Foundry artifacts.

        Assumes that `forge build` has already been run and the artifacts directory exists.
        Foundry's `out/` is keyed by source-file *basename* (not full path), so two source
        files with the same basename (e.g. `contracts/a/main.sol` and `contracts/b/main.sol`)
        share the same artifact directory. We disambiguate by reading the real source path
        from each artifact's `metadata.settings.compilationTarget`, then return one
        ContractHandle per unique (source_file, contract_name) pair. Multi-solc-version
        artifacts (e.g. `Foo.0.8.21.json` alongside `Foo.0.8.29.json`) are deduped.

        A contract is considered a "logic contract" if:
        - It has a bytecode.object field in the JSON artifact
        - The bytecode starts with "0x"
        - The bytecode is not empty ("0x")

        Args:
            artifacts_dir: Path to Foundry out directory

        Returns:
            List of ContractHandle, one per unique (source_file, contract_name) handle.
            source_file is the relative source path from the artifact's compilationTarget.
        """
        handles: List[ContractHandle] = []
        seen: set[tuple[str, str]] = set()
        library_files = find_all_library_files_and_names()
        library_files = {Path(file).stem: names for file, names in library_files.items()}
        project_files = self.project_source_files()

        # Iterate over all subdirectories in out/
        for subdir in sorted(artifacts_dir.iterdir()):
            if not subdir.is_dir():
                continue

            # Each subdirectory should be named {ContractName}.sol
            if not subdir.name.endswith(".sol"):
                continue

            # Skip test files (*.t.sol) and script files (*.s.sol)
            if subdir.name.endswith(".t.sol") or subdir.name.endswith(".s.sol"):
                continue

            # Extract source file name (without .sol extension)
            source_file_name = subdir.stem

            # If this source file is a library file, we need to skip the libraries in it
            library_names = []
            if source_file_name in library_files:
                library_names = library_files[source_file_name]

            # Iterate over all JSON files in the subdirectory
            for json_file in sorted(subdir.glob("*.json")):
                # Extract contract name from JSON filename (without .json extension)
                contract_name = json_file.stem

                # If this contract is known to be a library, skip it
                if contract_name in library_names:
                    continue

                try:
                    # Parse the JSON artifact
                    with open(json_file, 'r') as f:
                        artifact = json.load(f)

                    # Check if bytecode exists
                    if "bytecode" not in artifact:
                        continue

                    bytecode = artifact["bytecode"]
                    if not isinstance(bytecode, dict):
                        raise Exception(
                            f"Invalid bytecode format in {json_file}: expected dictionary, got {type(bytecode)}"
                        )

                    if "object" not in bytecode:
                        continue

                    bytecode_object = bytecode["object"]

                    # Validate bytecode format
                    if not isinstance(bytecode_object, str):
                        raise Exception(
                            f"Invalid bytecode.object in {json_file}: expected string, got {type(bytecode_object)}"
                        )

                    # Check if bytecode starts with 0x
                    if bytecode_object and not bytecode_object.startswith("0x"):
                        raise Exception(
                            f"Invalid bytecode in {json_file}: bytecode must start with '0x' but got "
                            f"'{bytecode_object[:10]}...'"
                        )

                    # Check if bytecode is not empty or just "0x"
                    if bytecode_object and bytecode_object != "" and bytecode_object != "0x":
                        # Extract actual contract name and source path from metadata.
                        # Foundry's subdir name is only the source basename, so
                        # compilationTarget is what disambiguates same-basename files in
                        # different directories.
                        actual_contract_name = None
                        source_path = None
                        try:
                            metadata = artifact.get("metadata")
                            if metadata:
                                settings = metadata.get("settings")
                                if settings:
                                    compilation_target = settings.get("compilationTarget")
                                    if compilation_target and len(compilation_target) == 1:
                                        # Take the single contract name value and source path
                                        for file_path, contract_name_from_metadata in compilation_target.items():
                                            actual_contract_name = contract_name_from_metadata
                                            source_path = file_path
                                            break
                        except Exception:
                            pass

                        # Without compilationTarget we can't safely attribute the artifact
                        # to a specific source file. Fall back to basename attribution
                        # (pre-metadata behavior). This is unsafe if another source file
                        # in a different directory shares this basename, but it's the
                        # behavior projects relied on before, so keep it as a fallback.
                        if not source_path or not actual_contract_name:
                            logger.log(
                                f"Artifact {json_file} lacks compilationTarget metadata; "
                                f"falling back to basename attribution",
                                "WARNING", "Foundry",
                            )
                            source_path = subdir.name
                            actual_contract_name = contract_name

                        # Only emit contracts whose source file is in scope (project-local,
                        # non-test, non-dependency).
                        if str(Path(source_path)) not in project_files:
                            continue

                        # Foundry may emit the same (source, contract) under multiple
                        # artifact filenames (compiler profiles, solc versions); deduplicate.
                        key = (source_path, actual_contract_name)
                        if key in seen:
                            continue
                        seen.add(key)
                        handles.append(ContractHandle(
                            contract_name=actual_contract_name,
                            source_file=source_path,
                        ))

                except json.JSONDecodeError as e:
                    raise Exception(f"Failed to parse JSON file {json_file}: {e}")
                except KeyError as e:
                    raise Exception(f"Missing required field in {json_file}: {e}")
                except Exception as e:
                    # Re-raise any other exceptions as fatal errors
                    raise Exception(f"Error processing {json_file}: {e}")

        return handles


def main():
    """Main entry point for command-line usage."""
    try:
        extractor = FoundryContractExtractor(Path.cwd())
        handles = extractor.extract_logic_contracts()

        if handles:
            logger.log(f"Found {len(handles)} logic contracts:", "INFO", "Foundry")
            for h in handles:
                logger.log(f"  - {h.source_file} (contract: {h.contract_name})", "INFO", "Foundry")
        else:
            logger.log("No logic contracts found in Foundry build output", "ERROR", "Foundry")
            sys.exit(1)

    except Exception as e:
        logger.log(f"Error: {e}", "ERROR", "Foundry")
        sys.exit(1)


if __name__ == "__main__":
    main()
