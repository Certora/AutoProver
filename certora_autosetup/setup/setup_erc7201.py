#!/usr/bin/env python3
"""
ERC-7201 Storage Pattern Detector

This script scans Solidity files for ERC-7201 storage location patterns
and generates the necessary configuration for Certora verification.
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Dict, List, Set, Tuple
from certora_autosetup.utils.progress_display import make_tqdm

from certora_autosetup.utils.logger import logger


class ERC7201Scanner:
    """Scanner for ERC-7201 storage location patterns."""
    
    def __init__(self, verbose: bool = False, log_func=None):
        self.verbose = verbose
        self.log = log_func if log_func else logger.log
        self.erc7201_pattern = re.compile(
            r'///\s*@custom:storage-location\s+erc7201:([^\s\n]+)',
            re.IGNORECASE
        )
        self.found_patterns: Dict[str, List[Tuple[str, int]]] = {}

    def should_exclude_path(self, path: Path) -> bool:
        """Check if a path should be excluded from scanning.

        Only excludes paths containing .certora_internal/.
        """
        return ".certora_internal" in str(path)

    def scan_file(self, file_path: Path) -> List[Tuple[str, int]]:
        """Scan a single Solidity file for ERC-7201 patterns."""
        patterns: list[tuple[str, int]] = []
        
        # Skip if this is actually a directory
        if not file_path.is_file():
            if self.verbose:
                self.log(f"Skipping {file_path}: not a regular file", "DEBUG")
            return patterns
        
        try:
            with open(file_path, 'r', encoding='utf-8') as f:
                for line_num, line in enumerate(f, 1):
                    match = self.erc7201_pattern.search(line)
                    if match:
                        namespace = match.group(1).strip()
                        patterns.append((namespace, line_num))
                        if self.verbose:
                            self.log(f"Found ERC-7201 pattern in {file_path}:{line_num} -> {namespace}", "DEBUG")
        
        except (UnicodeDecodeError, FileNotFoundError, IsADirectoryError) as e:
            if self.verbose:
                self.log(f"Warning: Could not read {file_path}: {e}", "WARNING")
        
        return patterns
    
    def scan_directory(self, directory: Path) -> None:
        """Scan all Solidity files in a directory recursively."""
        sol_files = []

        # Collect all .sol files, excluding hidden directories from traversal
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.endswith('.sol'):
                    sol_files.append(Path(root) / file)
        
        if not sol_files:
            self.log("No Solidity files found to scan.")
            return

        self.log(f"Scanning {len(sol_files)} Solidity files for ERC-7201 patterns...")
        
        # Scan files with progress bar
        for sol_file in make_tqdm(sol_files, desc="Scanning files", disable=logger.muted):
            patterns = self.scan_file(sol_file)
            if patterns:
                self.found_patterns[str(sol_file)] = patterns
    
    def get_unique_namespaces(self) -> Set[str]:
        """Get all unique namespaces found."""
        namespaces = set()
        for file_patterns in self.found_patterns.values():
            for namespace, _ in file_patterns:
                namespaces.add(namespace)
        return namespaces
    
    def generate_erc7201_spec(self, output_path: Path) -> bool:
        """Generate erc7201.spec file with namespace comments including source locations."""
        if not self.found_patterns:
            if self.verbose:
                self.log("No ERC-7201 patterns found, skipping spec generation.", "DEBUG")
            return False
        
        # Collect all namespaces with their source locations
        namespace_info: dict[str, list[tuple[str, int]]] = {}  # namespace -> list of (file, line) tuples
        
        for file_path, patterns in self.found_patterns.items():
            for namespace, line_num in patterns:
                if namespace not in namespace_info:
                    namespace_info[namespace] = []
                namespace_info[namespace].append((file_path, line_num))
        
        # Sort namespaces for consistent output
        sorted_namespaces = sorted(namespace_info.keys())
        
        spec_content = []
        for namespace in sorted_namespaces:
            # Add comment for this namespace with source locations
            spec_content.append(f"// ERC-7201 namespace: {namespace}")
            
            # Add source file and line information for each occurrence
            for file_path, line_num in sorted(namespace_info[namespace]):
                # Use relative path for cleaner output
                rel_path = Path(file_path).name  # Just filename, or could use relative path
                spec_content.append(f"//   Found in: {rel_path}:{line_num}")
            
            spec_content.append("")  # Empty line after each namespace block
        
        # Write the spec file
        output_path.parent.mkdir(parents=True, exist_ok=True)
        
        with open(output_path, 'w') as f:
            f.write('\n'.join(spec_content))
        
        self.log(f"Generated {output_path} with {len(sorted_namespaces)} ERC-7201 namespaces")
        return True
    
    def update_config_files(self, directory: Path) -> None:
        """Update configuration files to enable storage_extension_annotation."""
        config_files = []

        # Look for .conf files, excluding hidden directories from traversal
        for root, dirs, files in os.walk(directory):
            dirs[:] = [d for d in dirs if not d.startswith('.')]
            for file in files:
                if file.endswith('.conf'):
                    config_files.append(Path(root) / file)
        
        if not config_files:
            if self.verbose:
                self.log("No .conf files found to update.", "DEBUG")
            return
        
        updated_count = 0
        
        for conf_file in config_files:
            try:
                with open(conf_file, 'r') as f:
                    content = f.read()
                
                # Parse as JSON
                try:
                    config = json.loads(content)
                except json.JSONDecodeError:
                    if self.verbose:
                        self.log(f"Warning: {conf_file} is not valid JSON, skipping.", "WARNING")
                    continue
                
                # Check if already has storage_extension_annotation
                if config.get('storage_extension_annotation') is True:
                    if self.verbose:
                        self.log(f"{conf_file} already has storage_extension_annotation: true", "DEBUG")
                    continue
                
                # Add storage_extension_annotation
                config['storage_extension_annotation'] = True
                
                # Write back with nice formatting
                with open(conf_file, 'w') as f:
                    json.dump(config, f, indent=2)
                
                updated_count += 1
                if self.verbose:
                    self.log(f"Updated {conf_file} with storage_extension_annotation: true", "DEBUG")
            
            except Exception as e:
                if self.verbose:
                    self.log(f"Warning: Could not update {conf_file}: {e}", "WARNING")
        
        if updated_count > 0:
            self.log(f"Updated {updated_count} configuration files with storage_extension_annotation: true")
    
    def print_summary(self) -> None:
        """Print a summary of found patterns."""
        if not self.found_patterns:
            self.log("No ERC-7201 storage patterns found.")
            return

        namespaces = self.get_unique_namespaces()
        self.log(f"\nFound {len(namespaces)} unique ERC-7201 namespaces:")

        for namespace in sorted(namespaces):
            self.log(f"  - {namespace}")

        self.log(f"\nPatterns found in {len(self.found_patterns)} files:")
        for file_path, patterns in self.found_patterns.items():
            self.log(f"  {file_path}")
            for namespace, line_num in patterns:
                self.log(f"    Line {line_num}: {namespace}")


def run(directory=".", spec_output="certora/specs/erc7201.spec", verbose=False,
        no_config_update=False, summary_only=False) -> Tuple[int, bool]:
    """
    Run ERC-7201 detection and configuration as a library function.

    Args:
        directory: Directory to scan (default: current directory)
        spec_output: Output path for erc7201.spec file
        verbose: Enable verbose output
        no_config_update: Skip updating configuration files
        summary_only: Only print summary, do not generate files

    Returns:
        Tuple of (exit_code, namespaces_found):
        - exit_code: 0 for success, 1 for error
        - namespaces_found: True if ERC-7201 namespaces were detected
    """
    directory = Path(directory).resolve()
    if not directory.exists():
        logger.log(f"Error: Directory {directory} does not exist.", "ERROR")
        return 1, False

    if not directory.is_dir():
        logger.log(f"Error: {directory} is not a directory.", "ERROR")
        return 1, False

    # Create scanner and scan directory
    scanner = ERC7201Scanner(verbose=verbose)
    scanner.scan_directory(directory)

    # Print summary
    scanner.print_summary()

    if summary_only:
        return 0, bool(scanner.get_unique_namespaces())

    # Generate spec file if patterns found
    namespaces = scanner.get_unique_namespaces()
    if namespaces:
        spec_path = Path(spec_output)
        scanner.generate_erc7201_spec(spec_path)

        # Update existing config files on disk unless disabled
        if not no_config_update:
            scanner.update_config_files(directory)
    else:
        logger.log("No ERC-7201 patterns found, no files generated.")

    return 0, bool(namespaces)


def main():
    """Main entry point for command-line usage."""
    parser = argparse.ArgumentParser(
        description="Scan for ERC-7201 storage patterns and configure Certora verification"
    )
    parser.add_argument(
        'directory',
        nargs='?',
        default='.',
        help='Directory to scan (default: current directory)'
    )
    parser.add_argument(
        '--spec-output',
        default='certora/specs/erc7201.spec',
        help='Output path for erc7201.spec file (default: certora/specs/erc7201.spec)'
    )
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    parser.add_argument(
        '--no-config-update',
        action='store_true',
        help='Skip updating configuration files'
    )
    parser.add_argument(
        '--summary-only',
        action='store_true',
        help='Only print summary, do not generate files'
    )

    args = parser.parse_args()

    exit_code, _ = run(
        directory=args.directory,
        spec_output=args.spec_output,
        verbose=args.verbose,
        no_config_update=args.no_config_update,
        summary_only=args.summary_only
    )
    return exit_code


if __name__ == "__main__":
    exit(main())