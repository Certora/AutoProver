#!/usr/bin/env python3
"""
Solc Version Resolver - Centralized version resolution logic for pragma specifications.

This module provides utilities to:
- Fetch available solc versions from soliditylang.org
- Resolve pragma specifications to concrete versions
- Cache version data for performance
"""

import json
import re
import threading
import urllib.error
import urllib.request
from pathlib import Path
from typing import List, Optional

from packaging.specifiers import SpecifierSet
from packaging.version import Version

from certora_autosetup.utils.config_manager import certora_format_to_raw_version
from certora_autosetup.utils.logger import logger

# Minimum solc version that supports viaIR (introduced as settings.viaIR in 0.7.5, stabilized in 0.8.13)
VIA_IR_MIN_VERSION = Version("0.7.5")

# Module-level cache for solc versions
_solc_versions_cache: Optional[List[str]] = None
_cache_lock = threading.Lock()

# Fallback versions if network fetch fails
FALLBACK_VERSIONS = [
    "0.8.33",
    "0.8.30",
    "0.8.28",
    "0.8.26",
    "0.8.24",
    "0.8.20",
    "0.8.0",
    "0.7.6",
    "0.6.12",
    "0.5.17",
    "0.4.26",
]


def fetch_available_solc_versions() -> List[str]:
    """
    Fetch available solc versions from soliditylang.org binaries list.

    Uses module-level caching to avoid repeated network calls.
    Falls back to FALLBACK_VERSIONS if network fetch fails.

    Returns:
        List of version strings (e.g., ["0.8.33", "0.8.32", ...])
    """
    global _solc_versions_cache

    # Check cache first
    if _solc_versions_cache is not None:
        return _solc_versions_cache

    with _cache_lock:
        # Double-check after acquiring lock
        if _solc_versions_cache is not None:
            return _solc_versions_cache

        # Always use macOS URL (platform-independent version list)
        url = "https://binaries.soliditylang.org/macosx-amd64/list.json"

        try:
            logger.log(f"Fetching solc versions from {url}", "DEBUG", "SolcVersionResolver")
            req = urllib.request.Request(url, headers={"User-Agent": "PreAudit/1.0"})
            response = urllib.request.urlopen(req, timeout=10)
            data = json.loads(response.read().decode("utf-8"))

            # Extract version strings from releases dict
            versions = list(data.get("releases", {}).keys())

            if not versions:
                raise ValueError("No versions found in response")

            logger.log(f"Fetched {len(versions)} solc versions", "DEBUG", "SolcVersionResolver")
            _solc_versions_cache = versions
            return versions

        except (urllib.error.URLError, urllib.error.HTTPError, json.JSONDecodeError, KeyError, ValueError) as e:
            logger.log(f"Failed to fetch solc versions: {e}. Using fallback list.", "WARNING", "SolcVersionResolver")
            _solc_versions_cache = FALLBACK_VERSIONS
            return FALLBACK_VERSIONS


def parse_pragma_constraint(pragma_spec: str) -> Optional[SpecifierSet]:
    """
    Convert pragma solidity specification to packaging.SpecifierSet.

    Handles:
    - Exact version: "0.8.26" -> "==0.8.26"
    - Caret: "^0.8.0" -> ">=0.8.0,<0.9.0"
    - Range: ">=0.8.0 <0.8.6" -> ">=0.8.0,<0.8.6"
    - GTE/LTE: ">=0.8.0" -> ">=0.8.0"

    Args:
        pragma_spec: Raw pragma specification string

    Returns:
        SpecifierSet object or None if parsing fails
    """
    try:
        # Handle exact version: "0.8.26"
        if re.match(r"^\d+\.\d+\.\d+$", pragma_spec):
            return SpecifierSet(f"=={pragma_spec}")

        # Handle caret: "^0.8.0" -> ">=0.8.0,<0.9.0"
        caret_match = re.match(r"^\^(\d+)\.(\d+)\.(\d+)$", pragma_spec)
        if caret_match:
            major, minor, patch = caret_match.groups()
            if major == "0":
                # ^0.8.0 means >=0.8.0 <0.9.0
                next_minor = int(minor) + 1
                return SpecifierSet(f">={major}.{minor}.{patch},<{major}.{next_minor}.0")
            else:
                # ^1.2.3 means >=1.2.3 <2.0.0
                next_major = int(major) + 1
                return SpecifierSet(f">={major}.{minor}.{patch},<{next_major}.0.0")

        # Handle space-separated range with spaces anywhere
        # Examples: ">=0.8.0 <0.8.6", ">= 0.8.0 < 0.8.6", ">=0.8.0<0.8.6"
        # Step 1: Remove all spaces
        normalized = re.sub(r"\s+", "", pragma_spec)
        # Step 2: Add comma before operators that follow a digit (to separate constraints)
        normalized = re.sub(r"(\d)([><=!~^])", r"\1,\2", normalized)
        return SpecifierSet(normalized)

    except Exception as e:
        logger.log(f"Failed to parse pragma constraint '{pragma_spec}': {e}", "WARNING", "SolcVersionResolver")
        return None


def extract_pragma_spec(text: str) -> str | None:
    """
    Extract pragma solidity specification from source code or error output.

    Uses a robust regex pattern that handles variable whitespace.

    Args:
        text: Text containing pragma statement (source code, error message, etc.)

    Returns:
        Pragma specification string (e.g., ">=0.8.0 <0.8.6", "^0.8.0", "0.8.26")
        Returns None if no pragma found

    Examples:
        "pragma solidity ^0.8.0;" -> "^0.8.0"
        "pragma  solidity  >=0.8.0 <0.8.6;" -> ">=0.8.0 <0.8.6" (handles extra spaces)
    """
    # Use \s+ to handle variable whitespace between keywords
    pragma_match = re.search(r"pragma\s+solidity\s+([^;]+);", text)
    if pragma_match:
        return pragma_match.group(1).strip()
    return None


def read_pragma_from_source_file(source_file: Path, project_root: Optional[Path]) -> Optional[str]:
    """Read the raw ``pragma solidity`` spec from ``source_file``, or ``None``.

    A relative ``source_file`` is resolved against ``project_root``; a relative
    path with no ``project_root`` returns ``None``.
    Preserves the spec verbatim (e.g. ``"0.8.28"``, ``"^0.8.28"``,
    ``">=0.8.0 <0.9.0"``) so consumers can re-emit or constraint-match it
    without re-resolving. Returns ``None`` if the file is missing or
    unreadable, or if no ``pragma solidity`` directive is found.
    """
    if not source_file.is_absolute():
        if project_root is None:
            return None
        source_file = project_root / source_file
    try:
        return extract_pragma_spec(source_file.read_text())
    except OSError:
        return None


def resolve_pragma_to_version(
    pragma_spec: str, preferred_version: Optional[str] = None
) -> Optional[str]:
    """
    Resolve pragma specification to concrete solc version.

    If `preferred_version` is supplied and satisfies the pragma, it is returned
    unchanged. Otherwise fetches available versions from soliditylang.org and
    selects the highest version matching the constraint.

    Args:
        pragma_spec: Pragma version specification (e.g., "0.8.26", "^0.8.0", ">=0.8.0 <0.8.6")
        preferred_version: Project-wide solc, in raw ("0.8.30") or Certora
            ("solc8.30" / "solc-0.8.30") form. Normalized internally.

    Returns:
        Concrete version string like "0.8.5" (NOT in conf format like "solc-0.8.5")
        Returns None if no matching version found

    Examples:
        "0.8.26"           -> "0.8.26"
        "^0.8.0"           -> "0.8.33" (highest 0.8.x)
        ">=0.8.0 <0.8.6"   -> "0.8.5"  (highest below 0.8.6)
    """
    try:
        # Parse pragma into constraint
        constraint = parse_pragma_constraint(pragma_spec)
        if constraint is None:
            logger.log(f"Could not parse pragma spec: {pragma_spec}", "WARNING", "SolcVersionResolver")
            return None

        # If the project's solc satisfies the pragma, prefer it over the latest.
        # Accept either raw ("0.8.30") or Certora ("solc8.30" / "solc-0.8.30") forms.
        if preferred_version:
            normalized = certora_format_to_raw_version(preferred_version) or preferred_version
            try:
                if Version(normalized) in constraint:
                    logger.log(
                        f"Resolved pragma '{pragma_spec}' to preferred {normalized}",
                        "DEBUG",
                        "SolcVersionResolver",
                    )
                    return normalized
            except Exception as e:
                logger.log(
                    f"Ignoring invalid preferred_version '{preferred_version}': {e}",
                    "WARNING",
                    "SolcVersionResolver",
                )

        # Fetch available versions
        available_versions = fetch_available_solc_versions()

        # Filter versions that satisfy constraint
        matching_versions = [v for v in available_versions if Version(v) in constraint]

        if not matching_versions:
            logger.log(
                f"No solc version found matching pragma '{pragma_spec}' (constraint: {constraint})",
                "WARNING",
                "SolcVersionResolver",
            )
            return None

        # Select highest matching version
        highest_version = max(matching_versions, key=Version)

        logger.log(
            f"Resolved pragma '{pragma_spec}' to {highest_version} (from {len(matching_versions)} candidates)",
            "DEBUG",
            "SolcVersionResolver",
        )
        return highest_version

    except Exception as e:
        logger.log(
            f"Failed to resolve solc version for pragma {pragma_spec}: {e}", "WARNING", "SolcVersionResolver"
        )
        return None
