#!/usr/bin/env python3
"""
EmitMunger - Solidity Emit Statement Annotator

This script scans Solidity files and adds comments to emit statements:
- Single-line emits: /* EmitMunger */ before emit on the same line
- Multi-line emits: /* EmitMunger */ /* before emit, */ after the semicolon

Usage:
    python emit_munger.py [files...] [options]
    python emit_munger.py --all [options]
"""

import argparse
import os
import re
import sys
from pathlib import Path
from typing import List, Dict, Tuple

# Import shared solidity utilities
sys.path.insert(0, str(Path(__file__).parent.parent))
from certora_autosetup.setup.solidity_utils import find_all_solidity_files


class EmitMunger:
    def __init__(self, verbose: bool = False, dry_run: bool = False):
        self.verbose = verbose
        self.dry_run = dry_run
        self.processed_files = 0
        self.modified_files = 0
        self.total_emits = 0
        
    def log(self, message: str, level: str = "INFO"):
        """Log messages with optional verbosity."""
        if self.verbose or level in ["ERROR", "WARNING"]:
            prefix = {
                "ERROR": "❌",
                "WARNING": "⚠️", 
                "INFO": "ℹ️",
                "SUCCESS": "✅"
            }.get(level, "")
            print(f"{prefix} {message}")
    
    def find_all_solidity_files(self, include_test_files: bool = False, include_dependencies: bool = False) -> List[str]:
        """Find all Solidity files using shared utility function."""
        return find_all_solidity_files(
            include_test_files=include_test_files,
            include_dependencies=include_dependencies,
            verbose=self.verbose,
            log_func=self.log
        )
    
    def find_emit_statements(self, content: str) -> List[Tuple[int, int, str]]:
        """
        Find all emit statements in the content.
        
        Returns:
            List of tuples: (start_pos, end_pos, emit_statement)
        """
        emit_statements = []
        
        # Pattern to match emit statements (including multi-line)
        # This pattern looks for 'emit' followed by event call and ending with semicolon
        emit_pattern = r'\bemit\s+[^;]+;'
        
        for match in re.finditer(emit_pattern, content, re.DOTALL | re.MULTILINE):
            start_pos = match.start()
            end_pos = match.end()
            emit_text = match.group(0)
            
            emit_statements.append((start_pos, end_pos, emit_text))
        
        return emit_statements
    
    def is_already_munged(self, content: str, emit_start: int) -> bool:
        """Check if an emit statement is already munged."""
        # Look backwards from emit_start for EmitMunger comment
        lines_before = content[:emit_start].split('\n')
        if not lines_before:
            return False
            
        # Check the current line for /* EmitMunger */
        current_line_start = content.rfind('\n', 0, emit_start)
        if current_line_start == -1:
            current_line_start = 0
        else:
            current_line_start += 1
            
        line_before_emit = content[current_line_start:emit_start]
        
        return "/* EmitMunger */" in line_before_emit
    
    def munge_emit_statements(self, content: str) -> Tuple[str, int]:
        """
        Add EmitMunger comments to emit statements.
        
        Returns:
            Tuple of (modified_content, number_of_modifications)
        """
        emit_statements = self.find_emit_statements(content)
        
        if not emit_statements:
            return content, 0
        
        # Process in reverse order to avoid position shifting
        emit_statements.sort(key=lambda x: x[0], reverse=True)
        
        modified_content = content
        modifications = 0
        
        for start_pos, end_pos, emit_text in emit_statements:
            # Check if already munged
            if self.is_already_munged(modified_content, start_pos):
                if self.verbose:
                    self.log(f"Skipping already munged emit: {emit_text[:50]}...")
                continue
            
            # Check if emit spans multiple lines
            if '\n' in emit_text:
                # Multi-line emit: /* EmitMunger */ /* before emit, */ after semicolon
                before_comment = "/* EmitMunger */ /* "
                after_comment = " */"
                
                # Insert before emit
                modified_content = (
                    modified_content[:start_pos] + 
                    before_comment + 
                    modified_content[start_pos:end_pos] + 
                    after_comment + 
                    modified_content[end_pos:]
                )
            else:
                # Single-line emit: /* EmitMunger */ before emit on same line
                before_comment = "/* EmitMunger */ // "
                
                modified_content = (
                    modified_content[:start_pos] + 
                    before_comment + 
                    modified_content[start_pos:]
                )
            
            modifications += 1
            self.total_emits += 1
            
            if self.verbose:
                emit_preview = emit_text.replace('\n', ' ').strip()[:60]
                self.log(f"Munged emit: {emit_preview}...")
        
        return modified_content, modifications
    
    def process_file(self, file_path: str) -> bool:
        """
        Process a single Solidity file.
        
        Returns:
            True if file was modified, False otherwise
        """
        try:
            # Read file content
            with open(file_path, 'r', encoding='utf-8') as f:
                original_content = f.read()
            
            # Process emit statements
            modified_content, modifications = self.munge_emit_statements(original_content)
            
            # Write back if modified and not dry run
            if modifications > 0:
                if not self.dry_run:
                    with open(file_path, 'w', encoding='utf-8') as f:
                        f.write(modified_content)
                    self.log(f"Modified {file_path}: {modifications} emit(s) munged", "SUCCESS")
                else:
                    self.log(f"[DRY RUN] Would modify {file_path}: {modifications} emit(s)", "SUCCESS")
                
                self.modified_files += 1
                return True
            else:
                if self.verbose:
                    self.log(f"No changes needed for {file_path}")
                return False
                
        except Exception as e:
            self.log(f"Error processing {file_path}: {e}", "ERROR")
            return False
        finally:
            self.processed_files += 1
    
    def process_files(self, file_paths: List[str]) -> None:
        """Process a list of Solidity files."""
        self.log(f"=== EMITMUNGER: Processing {len(file_paths)} files ===")
        
        for file_path in file_paths:
            self.process_file(file_path)
        
        # Print summary
        self.log(f"\n=== SUMMARY ===")
        self.log(f"Files processed: {self.processed_files}")
        self.log(f"Files modified: {self.modified_files}")
        self.log(f"Total emits munged: {self.total_emits}")
        
        if self.dry_run:
            self.log("This was a dry run - no files were actually modified")


def main():
    parser = argparse.ArgumentParser(
        description="EmitMunger - Add comments to Solidity emit statements",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    python emit_munger.py contract1.sol contract2.sol
    python emit_munger.py --all --verbose
    python emit_munger.py --all --include-test-files --dry-run
        """
    )
    
    parser.add_argument(
        'files',
        nargs='*',
        help='Solidity files to process (if not provided with --all, processes all .sol files in project)'
    )
    
    parser.add_argument(
        '--all',
        action='store_true',
        help='Process all Solidity files in the project'
    )
    
    parser.add_argument(
        '-v', '--verbose',
        action='store_true',
        help='Enable verbose output'
    )
    
    parser.add_argument(
        '--dry-run',
        action='store_true',
        help='Show what would be modified without actually changing files'
    )
    
    parser.add_argument(
        '--include-test-files',
        action='store_true',
        help='Include test (.t.sol) and script (.s.sol) files (default: excluded)'
    )
    
    parser.add_argument(
        '--include-dependencies',
        action='store_true',
        help='Include files in dependency directories (node_modules, lib, forge-std) (default: excluded)'
    )
    
    args = parser.parse_args()
    
    # Validate arguments
    if not args.all and not args.files:
        parser.error("Must specify either files to process or use --all flag")
    
    if args.files and args.all:
        parser.error("Cannot specify both individual files and --all flag")
    
    # Validate individual files if provided
    if args.files:
        for file_path in args.files:
            if not file_path.endswith('.sol'):
                print(f"Error: {file_path} is not a Solidity file", file=sys.stderr)
                sys.exit(1)
            if not Path(file_path).exists():
                print(f"Error: {file_path} does not exist", file=sys.stderr)
                sys.exit(1)
    
    # Create EmitMunger instance
    munger = EmitMunger(verbose=args.verbose, dry_run=args.dry_run)
    
    # Determine files to process
    if args.all:
        files_to_process = munger.find_all_solidity_files(
            include_test_files=args.include_test_files,
            include_dependencies=args.include_dependencies
        )
        if not files_to_process:
            print("No Solidity files found to process")
            sys.exit(1)
    else:
        files_to_process = args.files
    
    # Process files
    munger.process_files(files_to_process)


if __name__ == "__main__":
    main()