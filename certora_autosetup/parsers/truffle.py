#!/usr/bin/env python3
"""
Truffle parser for extracting logic contracts from build artifacts.

Parses Truffle's compilation output to identify which contracts contain actual bytecode
(logic contracts) vs interfaces/abstract contracts.
"""

import json
from pathlib import Path
from typing import List, Optional

from certora_autosetup.build_systems.truffle import TruffleManager
from certora_autosetup.parsers.base import ContractExtractor
from certora_autosetup.setup.solidity_utils import find_all_library_files_and_names
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


class TruffleContractExtractor(ContractExtractor):
    """Truffle-specific contract extractor."""

    def __init__(self, project_root: Path):
        """
        Initialize Truffle contract extractor.

        Args:
            project_root: Root directory of the project
        """
        super().__init__(project_root, TruffleManager)

    def extract_logic_contracts_impl(self, artifacts_dir: Path) -> List[ContractHandle]:
        """
        Extract logic contracts from Truffle artifacts.

        Assumes `truffle compile` has already been run. Truffle writes one flat artifact per
        compiled contract — no per-source directory, and no `sourceName`-style project-relative
        path; the source is identified by an absolute `sourcePath` recorded at compile time:

        build/contracts/
        ├── ACL.json
        ├── Kernel.json
        └── ERC20.json          # dependency from node_modules — dropped (outside the project)

        A contract is a "logic contract" if its `bytecode` is present, starts with "0x", and is
        more than just "0x".

        Args:
            artifacts_dir: Path to Truffle's contracts build directory

        Returns:
            List of ContractHandle, one per unique (source_file, contract_name), with
            source_file relative to the project root.
        """
        handles: List[ContractHandle] = []
        seen: set[tuple[str, str]] = set()
        library_files = find_all_library_files_and_names()
        library_names_by_path = {str(file): names for file, names in library_files.items()}
        project_files = self.project_source_files()

        for json_file in self.manager.filter_artifacts(artifacts_dir):
            try:
                with open(json_file, 'r') as f:
                    artifact = json.load(f)

                contract_name = artifact.get("contractName")
                source_path = artifact.get("sourcePath")
                bytecode = artifact.get("bytecode")

                # Not a Truffle contract artifact (e.g. a stray JSON in the build dir).
                if not contract_name or not source_path:
                    continue

                if not bytecode:
                    continue

                if not isinstance(bytecode, str):
                    raise Exception(f"Invalid bytecode in {json_file}: expected string, got {type(bytecode)}")

                if not bytecode.startswith("0x"):
                    raise Exception(
                        f"Invalid bytecode in {json_file}: bytecode must start with '0x' but got "
                        f"'{bytecode[:10]}...'"
                    )

                # Interfaces and abstract contracts compile to no bytecode.
                if bytecode == "0x":
                    continue

                source_file = self._project_relative_source(source_path)
                if source_file is None:
                    continue

                # Skip libraries (matched by full source path).
                if contract_name in library_names_by_path.get(source_file, []):
                    logger.log(f"Skipping library contract: {contract_name} in {source_file}", "DEBUG", "Truffle")
                    continue

                # Only emit contracts whose source file is in scope (project-local,
                # non-test, non-dependency).
                if source_file not in project_files:
                    continue

                key = (source_file, contract_name)
                if key in seen:
                    continue
                seen.add(key)
                handles.append(ContractHandle(
                    contract_name=contract_name,
                    source_file=source_file,
                ))

            except json.JSONDecodeError as e:
                raise Exception(f"Failed to parse JSON file {json_file}: {e}")
            except Exception as e:
                raise Exception(f"Error processing {json_file}: {e}")

        return handles

    def _project_relative_source(self, source_path: str) -> Optional[str]:
        """Normalize an artifact's `sourcePath` to a project-relative path.

        Truffle records the absolute path of the source as seen by the compiling process, so
        dependencies pulled from node_modules land outside the project root — those are not
        ours to verify and are dropped (None). A path that is already relative is returned
        as-is.
        """
        path = Path(source_path)
        if not path.is_absolute():
            return str(path)

        try:
            return str(path.resolve().relative_to(Path(self.project_root).resolve()))
        except ValueError:
            logger.log(f"Skipping contract compiled from outside the project: {source_path}", "DEBUG", "Truffle")
            return None
