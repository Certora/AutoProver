"""
Prover configuration parsing utilities.

This module provides utilities for parsing and extracting information
from Certora prover configuration files.
"""

import json5
from pathlib import Path
from typing import Optional


def extract_spec_name(config_file: Path, log_func=None) -> str:
    """
    Extract spec name from a Certora configuration file.
    
    The configuration file is expected to be a JSON file with a "verify" field
    in the format "ContractName:spec.spec".
    
    Args:
        config_file: Path to the configuration file
        log_func: Optional logging function to use for warnings
        
    Returns:
        The spec name if found, otherwise the config filename stem as fallback
    """
    log = log_func if log_func else lambda msg, level="INFO": print(f"[{level}] {msg}")
    
    try:
        with open(config_file, 'r') as f:
            config = json5.load(f)
        
        verify_field = config.get("verify", "")
        if ":" in verify_field:
            # Format is "ContractName:spec.spec"
            spec_name = verify_field.split(":")[1]
            return spec_name
        
        log(f"Could not extract spec name from {config_file}", "WARNING")
        return config_file.stem  # Use config filename as fallback
        
    except (ValueError, FileNotFoundError, KeyError) as e:
        log(f"Error reading config {config_file}: {e}", "WARNING")
        return config_file.stem  # Use config filename as fallback


def parse_prover_config(config_file: Path) -> dict:
    """
    Parse a Certora prover configuration file.
    
    Args:
        config_file: Path to the configuration file
        
    Returns:
        The parsed configuration dictionary, or empty dict if parsing fails
    """
    try:
        with open(config_file, 'r') as f:
            return json5.load(f)
    except (ValueError, FileNotFoundError) as e:
        print(f"Error parsing config file {config_file}: {e}")
        return {}


def get_contract_from_verify_field(verify_field: str) -> Optional[str]:
    """
    Extract the contract name from a verify field.
    
    Args:
        verify_field: The verify field value (e.g., "MyContract:spec.spec")
        
    Returns:
        The contract name if found, None otherwise
    """
    if ":" in verify_field:
        return verify_field.split(":")[0]
    return None


def get_spec_from_verify_field(verify_field: str) -> Optional[str]:
    """
    Extract the spec file path from a verify field.
    
    Args:
        verify_field: The verify field value (e.g., "MyContract:spec.spec")
        
    Returns:
        The spec file path if found, None otherwise
    """
    if ":" in verify_field:
        return verify_field.split(":")[1]
    return None