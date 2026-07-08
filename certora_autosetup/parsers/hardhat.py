#!/usr/bin/env python3
"""
Hardhat parser for extracting logic contracts from build artifacts.

This module parses Hardhat's compilation output to identify which contracts
contain actual bytecode (logic contracts) vs interfaces/abstract contracts.
"""

import json
import os
from pathlib import Path
from typing import List

from certora_autosetup.build_systems.hardhat import HardhatManager
from certora_autosetup.parsers.base import ContractExtractor
from certora_autosetup.setup.solidity_utils import find_all_library_files_and_names
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


class HardhatContractExtractor(ContractExtractor):
    """Hardhat-specific contract extractor."""

    def __init__(self, project_root: Path):
        """
        Initialize Hardhat contract extractor.

        Args:
            project_root: Root directory of the project
        """
        super().__init__(project_root, HardhatManager)

    def extract_logic_contracts_impl(self, artifacts_dir: Path) -> List[ContractHandle]:
        """
        Extract logic contracts from Hardhat artifacts.

        Assumes that `npx hardhat compile` has already been run and the artifacts directory exists
        with the standard Hardhat structure:

        artifacts/
        ├── build-info/           # SKIP - comprehensive build data
        │   └── <hash>.json
        └── contracts/
            ├── Lock.sol/
            │   ├── Lock.json        # PARSE - main artifact
            │   └── Lock.dbg.json    # SKIP - debug metadata
            └── subfolder/
                └── FooFile.sol/
                    ├── Foo.json     # PARSE
                    └── Foo.dbg.json # SKIP

        A contract is considered a "logic contract" if:
        - It has a bytecode field in the JSON artifact
        - The bytecode starts with "0x"
        - The bytecode is not empty ("0x")

        Args:
            artifacts_dir: Path to Hardhat artifacts/contracts directory

        Returns:
            List of ContractHandle, one per unique (source_file, contract_name) where
            source_file is the Hardhat-reported sourceName (project-relative POSIX).
        """
        handles: List[ContractHandle] = []
        seen: set[tuple[str, str]] = set()
        library_files = find_all_library_files_and_names()
        library_names_by_path = {str(file): names for file, names in library_files.items()}
        project_files = self.project_source_files()

        # Recursively find all JSON files in artifacts/contracts/ using os.walk for performance
        skip_dirs = {"build-info"}
        for root, dirs, files in os.walk(artifacts_dir):
            # Prune directories we don't want to traverse into
            dirs[:] = [d for d in dirs if d not in skip_dirs and not (d.endswith(".t.sol") or d.endswith(".s.sol"))]

            for file_name in files:
                # Skip .dbg.json files (Hardhat debug metadata)
                if file_name.endswith(".dbg.json"):
                    continue

                # Only process .json files
                if not file_name.endswith(".json"):
                    continue

                json_file = Path(root) / file_name

                try:
                    # Parse the JSON artifact
                    with open(json_file, 'r') as f:
                        artifact = json.load(f)

                    # Verify this is a Hardhat artifact (format check)
                    artifact_format = artifact.get("_format")
                    if artifact_format != "hh-sol-artifact-1":
                        # Not a Hardhat artifact or wrong format, skip
                        continue

                    # Extract fields from Hardhat artifact (different from Foundry!)
                    contract_name = artifact.get("contractName")
                    source_name = artifact.get("sourceName")  # e.g., "contracts/Lock.sol"
                    bytecode = artifact.get("bytecode")  # Direct string, not bytecode.object

                    # Validate required fields
                    if not contract_name:
                        logger.log(f"No contractName in {json_file}, skipping", "WARNING", "Hardhat")
                        continue

                    if not source_name:
                        logger.log(f"No sourceName in {json_file}, skipping", "WARNING", "Hardhat")
                        continue

                    # Check if bytecode exists and is valid
                    if not bytecode:
                        continue

                    # Validate bytecode format (must be string starting with 0x)
                    if not isinstance(bytecode, str):
                        raise Exception(f"Invalid bytecode in {json_file}: expected string, got {type(bytecode)}")

                    if not bytecode.startswith("0x"):
                        raise Exception(
                            f"Invalid bytecode in {json_file}: bytecode must start with '0x' but got "
                            f"'{bytecode[:10]}...'"
                        )

                    # Check if bytecode is not empty (more than just "0x")
                    if bytecode == "0x":
                        continue

                    # Skip libraries (matched by full source path).
                    if contract_name in library_names_by_path.get(source_name, []):
                        logger.log(f"Skipping library contract: {contract_name} in {source_name}", "DEBUG", "Hardhat")
                        continue

                    # Only emit contracts whose source file is in scope (project-local,
                    # non-test, non-dependency).
                    if str(Path(source_name)) not in project_files:
                        continue

                    key = (source_name, contract_name)
                    if key in seen:
                        continue
                    seen.add(key)
                    handles.append(ContractHandle(
                        contract_name=contract_name,
                        source_file=source_name,
                    ))

                except json.JSONDecodeError as e:
                    raise Exception(f"Failed to parse JSON file {json_file}: {e}")
                except KeyError as e:
                    raise Exception(f"Missing required field in {json_file}: {e}")
                except Exception as e:
                    # Re-raise any other exceptions as fatal errors
                    raise Exception(f"Error processing {json_file}: {e}")

        return handles
