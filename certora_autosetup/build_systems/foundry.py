#!/usr/bin/env python3
"""
Foundry Manager - Manages Foundry project configuration, compilation, and artifacts.

Based on Brain's foundry parsing logic but enhanced for better error handling,
multi-profile support, and integrated compilation management.
"""

import sys
from typing import Any, Dict, List, Optional
from pathlib import Path
from dataclasses import dataclass

from wcmatch import glob as wcglob

# Handle tomllib import for Python 3.11+ vs older versions
if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib

from certora_autosetup.build_systems.base import BuildSystemConfig
from certora_autosetup.build_systems.manager import BuildSystemManager
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.types import ContractHandle


@dataclass
class FoundryConfig(BuildSystemConfig):
    """Parsed foundry configuration with resolved settings."""

    # Foundry-specific fields (common fields inherited from BuildSystemConfig)
    out: Optional[str] = None
    libs: Optional[List[str]] = None

    # Remappings and dependencies
    remappings: Optional[List[str]] = None
    packages: Optional[List[str]] = None

    # Manager handling compiler restrictions from foundry.toml
    restriction_manager: Optional["FoundryRestrictionManager"] = None

    # Profile information
    profile: str = "default"

    def __post_init__(self):
        """Initialize default values for mutable fields."""
        # Call parent class initialization for common fields
        super().__post_init__()

        # Initialize Foundry-specific defaults
        if self.src is None:
            self.src = "src"
        if self.out is None:
            self.out = "out"
        if self.libs is None:
            self.libs = ["lib"]
        if self.remappings is None:
            self.remappings = []
        if self.packages is None:
            self.packages = []
        if self.restriction_manager is None:
            # No restrictions → apply_restrictions short-circuits, so the
            # defaults here are never consulted; we still pass optimizer_runs
            # for consistency. (evm_version isn't a FoundryConfig field.)
            self.restriction_manager = FoundryRestrictionManager(
                restrictions=[], # No restriction means no effect of apply_restrictions
                default_via_ir=bool(self.via_ir) if self.via_ir is not None else False,
                default_optimizer_runs=self.optimizer_runs,
            )

    def to_certora_dict(
        self,
        convert_solc_to_certora_format: bool = True,
        include_packages: bool = True
    ) -> Dict[str, Any]:
        """
        Convert Foundry config to Certora format.

        Args:
            convert_solc_to_certora_format: Whether to convert "0.8.19" to "solc8.19" format
            include_packages: Whether to include packages/remappings

        Returns:
            Dictionary with Certora config format
        """
        # Apply common settings (solc, optimizer, via_ir) using base class helper
        result = self._apply_common_solc_settings(convert_solc_to_certora_format)

        # Apply packages (Foundry-specific)
        if include_packages and self.packages:
            result["packages"] = self._relativize_packages(self.packages)

        return result

    def get_artifact_directory(self) -> str:
        """Return Foundry artifact directory."""
        return self.out or "out"

    def apply_per_contract_settings(
        self,
        contracts: List[ContractHandle],
        config_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """Apply Foundry `compilation_restrictions` to the conf.

        Overrides BuildSystemConfig.apply_per_contract_settings to delegate
        to the restriction_manager built at parse time. The manager handles
        the via_ir / optimizer_runs maps.
        """
        if self.restriction_manager is not None:
            return self.restriction_manager.apply_restrictions(contracts, config_dict)
        return config_dict


class FoundryManager(BuildSystemManager):
    """
    Foundry project manager with support for configuration, compilation, and artifacts.

    Based on Brain's foundry parsing approach but enhanced for better modularity,
    error handling, and integrated compilation management.
    """

    def __init__(self, project_root: Path, scope):
        """
        Initialize foundry manager.

        Args:
            project_root: Root directory of the project
            scope: Centralized scope for consistent filtering
        """
        super().__init__(project_root, scope, "FoundryManager")

    def get_config_filenames(self) -> List[str]:
        """Return list of config filenames to search for."""
        return ["foundry.toml"]

    def parse_config(self, config_file: Path, profile: str | None = None) -> FoundryConfig:
        """
        Parse foundry.toml file and extract configuration for specified profile.

        Based on Brain's parse_foundry function but enhanced for profile support.

        Args:
            config_file: Path to foundry.toml file
            profile: Foundry profile to use (default: "default")

        Returns:
            FoundryConfig with parsed settings
        """
        if profile is None:
            profile = "default"

        try:
            with config_file.open("rb") as f:
                foundry_data = tomllib.load(f)

            self.log(f"Parsing foundry config from {config_file} (profile: {profile})")

            # Start with default profile settings
            config = self._extract_profile_config(foundry_data, "default")

            # Override with specific profile if different from default
            if profile != "default":
                # Check if profile exists either at top level or under "profile" section
                profile_exists = profile in foundry_data or (
                    foundry_data.get("profile", {}) and profile in foundry_data.get("profile", {})
                )
                if profile_exists:
                    profile_config = self._extract_profile_config(foundry_data, profile)
                    config = self._merge_configs(config, profile_config)

            # Set the profile name
            config.profile = profile

            # Resolve paths relative to foundry.toml location
            config = self._resolve_paths(config, config_file.parent)

            # Parse remappings and convert to packages format
            if config.remappings:
                config.packages = self._convert_remappings_to_packages(
                    config.remappings, config_file.parent
                )

            self.log(
                f"Parsed foundry config: solc={config.solc_version}, optimizer={config.optimizer}"
            )
            return config

        except Exception as e:
            self.log(f"Failed to parse foundry config {config_file}: {e}", "ERROR")
            # Return minimal config as fallback
            return FoundryConfig(profile=profile)

    def _extract_profile_config(self, foundry_data: Dict[str, Any], profile: str) -> FoundryConfig:
        """Extract configuration from a specific profile section."""
        profile_data = foundry_data.get(profile, {})

        # Handle nested profile structure
        if "profile" in foundry_data and profile in foundry_data["profile"]:
            profile_data.update(foundry_data["profile"][profile])

        # Extract compiler settings
        solc_version = self._extract_solc_version(profile_data)
        optimizer_settings = self._extract_optimizer_settings(profile_data)

        via_ir = profile_data.get("via_ir")  # Will be None if not specified
        restriction_manager = FoundryRestrictionManager(
            restrictions=list(profile_data.get("compilation_restrictions", [])),
            default_via_ir=bool(via_ir) if via_ir is not None else False,
            default_optimizer_runs=optimizer_settings["runs"],
        )
        return FoundryConfig(
            solc_version=solc_version,
            optimizer=optimizer_settings["enabled"],
            optimizer_runs=optimizer_settings["runs"],
            via_ir=via_ir,
            src=profile_data.get("src"),  # Will be None if not specified
            out=profile_data.get("out"),  # Will be None if not specified
            libs=profile_data.get("libs"),  # Will be None if not specified
            remappings=profile_data.get("remappings", []),  # Always get remappings list
            restriction_manager=restriction_manager,
        )

    def _extract_solc_version(self, profile_data: Dict[str, Any]) -> Optional[str]:
        """Extract Solidity compiler version from various possible keys."""
        # Try different possible keys for solc version
        for key in ["solc", "solc_version", "solc-version"]:
            if key in profile_data:
                version = profile_data[key]
                if isinstance(version, str):
                    return version
        return None

    def _extract_optimizer_settings(self, profile_data: Dict[str, Any]) -> Dict[str, Any]:
        """Extract optimizer settings handling both boolean and object formats."""
        # Check if optimizer is explicitly set in this profile
        if "optimizer" not in profile_data:
            return {"enabled": None, "runs": profile_data.get("optimizer_runs", 200)}

        optimizer = profile_data["optimizer"]

        if isinstance(optimizer, bool):
            return {"enabled": optimizer, "runs": profile_data.get("optimizer_runs", 200)}
        elif isinstance(optimizer, dict):
            return {"enabled": optimizer.get("enabled", None), "runs": optimizer.get("runs", 200)}
        else:
            return {"enabled": None, "runs": 200}

    def _merge_configs(self, base: FoundryConfig, override: FoundryConfig) -> FoundryConfig:
        """Merge two configurations, with override taking precedence."""
        merged_via_ir = override.via_ir if override.via_ir is not None else base.via_ir
        merged_optimizer_runs = (
            override.optimizer_runs if override.optimizer_runs != 200 else base.optimizer_runs
        )
        # Restriction handling: override's restrictions REPLACE base's and rebuild the manager.
        base_mgr = base.restriction_manager
        override_mgr = override.restriction_manager

        base_restrictions = base_mgr.restrictions if base_mgr else []
        override_restrictions = override_mgr.restrictions if override_mgr else []
        merged_restrictions = list(override_restrictions if override_restrictions else base_restrictions)

        restriction_manager = FoundryRestrictionManager(
            restrictions=merged_restrictions,
            default_via_ir=bool(merged_via_ir) if merged_via_ir is not None else False,
            default_optimizer_runs=merged_optimizer_runs,
        )
        # For each field, use override value if it's not None, otherwise use base
        merged = FoundryConfig(
            solc_version=override.solc_version or base.solc_version,
            optimizer=override.optimizer if override.optimizer is not None else base.optimizer,
            optimizer_runs=merged_optimizer_runs,
            via_ir=merged_via_ir,
            src=override.src or base.src,
            out=override.out or base.out,
            libs=override.libs or base.libs,
            remappings=list(set((base.remappings or []) + (override.remappings or []))),  # Combine and dedupe
            restriction_manager=restriction_manager,
        )
        return merged

    def _resolve_paths(self, config: FoundryConfig, foundry_dir: Path) -> FoundryConfig:
        """Resolve relative paths in config to absolute paths."""
        # Convert relative paths to absolute
        if config.src and not Path(config.src).is_absolute():
            config.src = str(foundry_dir / config.src)
        if config.out and not Path(config.out).is_absolute():
            config.out = str(foundry_dir / config.out)

        # Resolve library paths
        resolved_libs = []
        if config.libs:
            for lib in config.libs:
                if not Path(lib).is_absolute():
                    resolved_libs.append(str(foundry_dir / lib))
                else:
                    resolved_libs.append(lib)
            config.libs = resolved_libs

        return config

    def _convert_remappings_to_packages(
        self, remappings: List[str], foundry_dir: Path
    ) -> List[str]:
        """
        Convert foundry remappings to Certora packages format.

        Based on Brain's parse_packages and parse_remappings_from_foundry functions.
        """
        packages = []

        for remapping in remappings:
            try:
                # Parse remapping format: "@openzeppelin/=lib/openzeppelin-contracts/"
                if "=" in remapping:
                    alias, path = remapping.split("=", 1)
                    alias = alias.strip()
                    path = path.strip()

                    # Resolve relative paths
                    if not Path(path).is_absolute():
                        path = str(foundry_dir / path)

                    # Convert to Certora package format
                    # if alias.startswith("@"):
                    #     # NPM-style package: @openzeppelin/contracts=lib/openzeppelin-contracts
                    #     package_name = alias[1:]  # Remove @
                    #     packages.append(f"{package_name}={path}")
                    # else:
                    #     # Simple alias: contracts=src/contracts
                    packages.append(f"{alias}={path}")

            except Exception as e:
                self.log(f"Failed to parse remapping '{remapping}': {e}", "WARNING")

        return packages

    def get_available_profiles(self, foundry_file: Path) -> List[str]:
        """Get list of available profiles in foundry.toml."""
        try:
            with foundry_file.open("rb") as f:
                foundry_data = tomllib.load(f)

            profiles = ["default"]  # Default profile always exists

            # Check for profile section
            if "profile" in foundry_data:
                profiles.extend(foundry_data["profile"].keys())

            # Check for top-level profile sections
            for key in foundry_data.keys():
                if key not in ["profile", "default"] and isinstance(foundry_data[key], dict):
                    # Could be a profile section
                    profiles.append(key)

            return list(set(profiles))  # Remove duplicates

        except Exception as e:
            self.log(f"Failed to get profiles from {foundry_file}: {e}", "ERROR")
            return ["default"]

    def auto_detect_config(self, profile: str | None = None) -> FoundryConfig:
        """
        Find foundry.toml, detect profile, and return parsed config.

        Overrides base class to add Foundry-specific profile handling.

        Args:
            profile: Optional profile name to override auto-detection

        Returns:
            FoundryConfig for the auto-detected foundry file and profile

        Raises:
            Exception: If no foundry.toml found or profile cannot be determined
        """
        config_file = self.find_config_file()
        if config_file is None:
            raise Exception("No foundry.toml found in project or parent directories")

        self.log(f"Auto-detected foundry file: {config_file}")

        # Determine profile - use explicit request or auto-detect
        if profile:
            available = self.get_available_profiles(config_file)
            if profile not in available:
                self.log(f"Profile '{profile}' not found in {config_file}", "ERROR")
                self.log(f"Available profiles: {', '.join(sorted(available))}", "ERROR")
                raise Exception(f"Invalid profile '{profile}'")
            self.log(f"Using explicitly requested profile: {profile}")
        else:
            profiles = self.get_available_profiles(config_file)

            if len(profiles) > 1:
                if "default" in profiles:
                    profile = "default"
                    self.log("Multiple profiles found, using 'default' profile")
                else:
                    error_msg = f"Found {len(profiles)} profiles with no 'default' profile:\n"
                    for p in profiles:
                        error_msg += f"  - {p}\n"
                    error_msg += "\nPlease specify which profile to use or create a 'default' profile."
                    raise Exception(error_msg)
            else:
                profile = profiles[0]

            self.log(f"Auto-detected profile: {profile}")

        config = self.parse_config(config_file, profile=profile)
        self.log("Successfully auto-detected Foundry configuration")
        return config

    def get_default_artifact_dir(self) -> str:
        """Return default Foundry artifact directory."""
        return "out"

    def get_build_command(self, profile: Optional[str] = None) -> str:
        """Return Foundry build command."""
        if profile and profile != "default":
            return f"FOUNDRY_PROFILE={profile} forge build"
        return "forge build"

    def filter_artifacts(self, artifacts_dir: Path) -> List[Path]:
        """
        Filter Foundry artifacts - all .json files except those in build-info/ directories.

        Args:
            artifacts_dir: Path to artifacts directory

        Returns:
            List of artifact file paths
        """
        return self._walk_and_filter_artifacts(
            artifacts_dir,
            skip_dirs={"build-info"},
            file_filter=lambda f: f.endswith(".json")
        )


class FoundryRestrictionManager:
    """ This manager keeps information about restrictions from
    foundry.toml (defined in the section [profile.<name>].compilation_restrictions).
    These restrictions modify the `solc_via_ir_map` and `solc_optimize_map`
    for specified contracts. (Per-path evm_version is intentionally not applied —
    see the note in apply_restrictions.)
    The manager also provides functionality for applying the restriction to a given certora conf file.
    """

    def __init__(
        self,
        restrictions: List[Dict[str, Any]],
        default_via_ir: bool = False,
        default_optimizer_runs: Optional[int] = None,
    ):
        self.restrictions = restrictions
        self.default_via_ir = default_via_ir
        self.default_optimizer_runs = default_optimizer_runs

    def apply_restrictions(
        self,
        contracts: List[ContractHandle],
        config_dict: Dict[str, Any],
    ) -> Dict[str, Any]:
        """
        Mutates `config_dict` in place according to restrictions AND returns it (so callers can chain).
        """
        # When foundry.toml has no compilation_restrictions, leave it as it is
        if not self.restrictions:
            return config_dict

        # ---------------- via_ir map ----------------
        restricted_via_ir = self._via_ir_map(contracts)
        if restricted_via_ir:
            existing_via_ir = config_dict.get("solc_via_ir_map", {})
            merged_via_ir = self._merge_via_ir_maps(existing_via_ir, restricted_via_ir)
            uniform = self._uniform_value(merged_via_ir)
            if uniform is not None:
                # All contracts agree — collapse to the global flag. (solc_via_ir
                # is only meaningful when True; all-False just means leave it off.)
                config_dict.pop("solc_via_ir_map", None)
                config_dict.pop("solc_via_ir", None)
                if uniform:
                    config_dict["solc_via_ir"] = True
            else:
                config_dict.pop("solc_via_ir", None)
                config_dict["solc_via_ir_map"] = merged_via_ir
            enabled = sorted(n for n, v in merged_via_ir.items() if v)
            logger.log(
                f"Applied Foundry compilation_restrictions: viaIR enabled "
                f"for {len(enabled)} contract(s): {enabled}",
                "INFO",
                "FoundryRestrictionManager",
            )

        # NOTE: per-path evm_version is intentionally NOT emitted. The prover
        # requires solc_evm_version_map to be total, so unmatched contracts
        # would have to be filled with the profile-level evm_version (e.g.
        # "prague") — which breaks older-solc files that don't support it
        # (solc 0.8.25 rejects "prague"). Leaving evm_version unset lets each
        # contract use its solc's own default, which is always compatible — the
        # behaviour that worked before per-path evm_version was introduced.

        # ---------------- solc_optimize map ----------------
        restricted_optimize = self._optimize_map(contracts)
        if restricted_optimize:
            existing_opt = config_dict.get("solc_optimize_map", {})
            merged_opt = {**existing_opt, **restricted_optimize}
            uniform = self._uniform_value(merged_opt)
            if uniform is not None:
                # All contracts share one runs value — we prefer the
                # scalar solc_optimize over a uniform map (and warns otherwise).
                config_dict.pop("solc_optimize_map", None)
                config_dict["solc_optimize"] = int(uniform)
            else:
                config_dict.pop("solc_optimize", None)
                # Certora's solc_optimize_map accepts numeric strings; prover_arg_names
                # treats it as an ordered dict of name -> value.
                config_dict["solc_optimize_map"] = {k: str(v) for k, v in merged_opt.items()}
            logger.log(
                f"Applied Foundry compilation_restrictions: optimizer_runs "
                f"set for {len(merged_opt)} contract(s) "
                f"({sorted(set(merged_opt.values()))})",
                "INFO",
                "FoundryRestrictionManager",
            )

        return config_dict

    @staticmethod
    def _uniform_value(mapping: Dict[str, Any]) -> Any:
        """Return the common value if every entry in `mapping` is identical,
        else None. Used to collapse a per-contract map to a single global key.
        """
        values = set(mapping.values())
        return next(iter(values)) if len(values) == 1 else None

    def _via_ir_map(self, contracts: List[ContractHandle]) -> Dict[str, bool]:
        """Restricts per-contract solc_via_ir map. Empty when no rule addresses via_ir.
        """
        matches = self._matching_rules(contracts)
        if not any("via_ir" in r for rules in matches.values() for r in rules):
            return {}
        result: Dict[str, bool] = {}
        for c in contracts:
            value = self._first_key(matches.get(c.contract_name, []), "via_ir")
            result[c.contract_name] = bool(value) if value is not None else self.default_via_ir
        return result

    def _optimize_map(self, contracts: List[ContractHandle]) -> Dict[str, int]:
        """Per-contract solc_optimize map. Empty when no rule sets a runs value.

        Reads `optimizer_runs`, falling back to `max_optimizer_runs` then
        `min_optimizer_runs` when only a range is given. When at least one rule
        sets a runs value the map contains all contracts in the scene.
        """
        matches = self._matching_rules(contracts)
        keys = ("optimizer_runs", "max_optimizer_runs", "min_optimizer_runs")
        if not any(k in r for rules in matches.values() for r in rules for k in keys):
            return {}
        result: Dict[str, int] = {}
        for c in contracts:
            value = self._first_key(matches.get(c.contract_name, []), *keys)
            runs: Optional[int] = None
            if value is not None:
                try:
                    runs = int(value)
                except (TypeError, ValueError):
                    runs = None
            if runs is None:
                runs = self.default_optimizer_runs
            if runs is not None:
                result[c.contract_name] = runs
        return result

    # =========================================================================
    # Private matching helpers
    # =========================================================================

    def _matching_rules(
        self, contracts: List[ContractHandle]
    ) -> Dict[str, List[Dict[str, Any]]]:
        """Return contract_name -> ordered list of matching restriction dicts.
        """
        valid_rules = [
            r for r in (self.restrictions or [])
            if isinstance(r, dict) and r.get("paths")
        ]
        if not valid_rules:
            return {}
        out: Dict[str, List[Dict[str, Any]]] = {}
        for c in contracts:
            src = str(c.source_file).replace("\\", "/")
            matches = [
                r for r in valid_rules
                if wcglob.globmatch(src, r["paths"], flags=wcglob.GLOBSTAR)
            ]
            if matches:
                out[c.contract_name] = matches
        return out

    @staticmethod
    def _first_key(rules: List[Dict[str, Any]], *keys: str) -> Any:
        """Return the value of the first key (across all keys, in priority order)
        that appears in any of the matching rules. None if no rule sets any."""
        for k in keys:
            for r in rules:
                if k in r:
                    return r[k]
        return None

    @staticmethod
    def _merge_via_ir_maps(
        existing: Dict[str, bool], restricted: Dict[str, bool]
    ) -> Dict[str, bool]:
        """Merge per-contract solc_via_ir maps with explicit priority.
        It marges existing and restricted dir. In the end, it sets
        items of existing that were false, to false again since these were
        disabled because of old socl version (those compilers literally cannot use viaIR)
        """
        merged = dict(existing)
        merged.update(restricted)
        for name, val in existing.items():
            if val is False:
                merged[name] = False
        return merged

