"""
certora-fixconf: Fix compilation settings in an existing Certora .conf file.

Reads a .conf file, attempts compilation via certoraRun --compilation_steps_only,
and iteratively applies workarounds (solc version, packages, via-ir, EVM version, etc.)
until compilation succeeds. Optionally merges build system settings from Foundry/Hardhat.
"""

import argparse
import copy
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict

import json5

from certora_autosetup.fixconf_prechecks import (
    detect_solc_convention,
    fix_file_paths,
    fix_prover_args,
    fix_verify_contract,
)
from certora_autosetup.parsers.build_system_detector import BuildSystem, BuildSystemDetector
from certora_autosetup.setup.solidity_import_patch import apply_patch, create_patch, revert_patch
from certora_autosetup.utils.compilation_workarounds import CompilationWorkaroundManager
from certora_autosetup.utils.constants import DEFAULT_SOLC_VERSION
from certora_autosetup.utils.contract_utils import parse_contract_files
from certora_autosetup.utils.logger import logger


# Build system keys that should be stripped from the output if the user's original conf didn't have them.
# These are low-priority settings that can break the prover (e.g. solc_optimize with a huge value from foundry.toml).
# High-priority keys like solc and packages are kept since they're often needed for compilation.
_LOW_PRIORITY_BS_KEYS = {"solc_optimize", "solc_via_ir"}


class _AcceptAllScope:
    """Minimal scope that accepts all files, needed by BuildSystemManager."""

    def is_file_in_scope(self, file_path: str) -> bool:
        return True


def create_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="certora-fixconf",
        description="Fix compilation settings in a Certora .conf file.",
    )
    parser.add_argument("conf_file", type=Path, help="Path to the .conf file to fix")
    parser.add_argument("--no-build-system", action="store_true", help="Skip Foundry/Hardhat auto-detection")
    parser.add_argument("--profile", type=str, default=None, help="Build system profile (e.g., Foundry profile)")
    parser.add_argument("-v", "--verbose", action="count", default=0, help="Increase verbosity")
    return parser


def _merge_build_system_settings(conf: Dict[str, Any], profile: str | None) -> list[str]:
    """Detect build system and merge missing settings into conf as defaults.

    Returns the list of keys that were merged (not already in conf).
    """
    project_root = Path.cwd()
    detected = BuildSystemDetector.detect(project_root)

    if detected == BuildSystem.UNKNOWN:
        logger.log("No build system detected, skipping build system merge", "WARNING", "fixconf")
        return []

    logger.log(f"Detected build system: {detected.value}", "INFO", "fixconf")
    manager_class = BuildSystemDetector.get_manager_class(detected)
    manager = manager_class(project_root, _AcceptAllScope())  # type: ignore[call-arg]
    bs_config = manager.auto_detect_config(profile=profile)
    bs_dict = bs_config.to_certora_dict(convert_solc_to_certora_format=True, include_packages=True)

    merged_keys = []
    for key, value in bs_dict.items():
        if key not in conf:
            conf[key] = value
            merged_keys.append(key)

    if merged_keys:
        logger.log(f"Merged build system defaults: {', '.join(merged_keys)}", "INFO", "fixconf")

    return merged_keys


def _report_changes(original: Dict[str, Any], fixed: Dict[str, Any]) -> int:
    """Log differences between original and fixed conf. Returns number of changes."""
    changes = 0
    all_keys = set(original.keys()) | set(fixed.keys())

    for key in sorted(all_keys):
        if key not in original and key in fixed:
            logger.log(f"  Added: {key} = {json.dumps(fixed[key])}", "INFO", "fixconf")
            changes += 1
        elif key in original and key in fixed and original[key] != fixed[key]:
            logger.log(f"  Changed: {key}: {json.dumps(original[key])} -> {json.dumps(fixed[key])}", "INFO", "fixconf")
            changes += 1

    return changes


def fix_conf(
    conf_file: Path,
    no_build_system: bool,
    verbose: int,
    profile: str | None,
) -> bool:
    """Fix compilation settings in a .conf file.

    Returns True if compilation succeeds (with or without fixes).
    """
    project_root = Path.cwd()

    # Read conf
    with open(conf_file) as f:
        conf = json5.load(f)

    # Capture original before any fixes so _report_changes shows pre-compilation fixes too
    original_conf = copy.deepcopy(conf)

    logger.log(f"Fixing conf: {conf_file}", "INFO", "fixconf")

    # Pre-compilation fixes
    fix_prover_args(conf)
    fix_file_paths(conf, project_root)

    if "files" not in conf or not conf["files"]:
        logger.log("Conf file has no 'files' field or it is empty", "ERROR", "fixconf")
        sys.exit(1)

    # Parse contracts (after file path fixes)
    contracts = parse_contract_files(conf["files"], project_root=project_root, strict=False)
    if not contracts:
        logger.log("No valid contracts found in 'files' list", "ERROR", "fixconf")
        sys.exit(1)

    fix_verify_contract(conf, project_root)

    logger.log(f"  {len(contracts)} contracts in files list", "INFO", "fixconf")

    synthetic_verify = False

    # Merge build system settings
    merged_bs_keys: list[str] = []
    if not no_build_system:
        merged_bs_keys = _merge_build_system_settings(conf, profile)

    # Detect solc naming convention
    solc_convention = detect_solc_convention(project_root)

    # Ensure verify key exists (certoraRun requires it even for --compilation_steps_only)
    internal_dir = Path(".certora_internal")
    internal_dir.mkdir(exist_ok=True)
    dummy_spec = internal_dir / "fixconf_dummy.spec"
    dummy_spec.write_text("")

    if "verify" not in conf:
        conf["verify"] = f"{contracts[0].contract_name}:{dummy_spec}"
        synthetic_verify = True

    # Write working copy
    working_conf = internal_dir / "fixconf_working.conf"
    working_conf_dict = copy.deepcopy(conf)
    with open(working_conf, "w") as f:
        json.dump(working_conf_dict, f, indent=2)

    # Run compilation with workarounds
    cmd = ["certoraRun", str(working_conf), "--compilation_steps_only"]
    updated_config_dict = {k: v for k, v in conf.items() if k not in ("files", "verify", "msg")}
    workaround_mgr = CompilationWorkaroundManager(
        project_root, DEFAULT_SOLC_VERSION, verbose, solc_convention=solc_convention
    )
    success, output, updated_config_dict = workaround_mgr.run_compilation_with_workarounds(
        cmd, working_conf, working_conf_dict, contracts, updated_config_dict
    )

    # Import patcher fallback
    import_patcher_applied = False
    if not success:
        logger.log("Compilation failed, trying import patcher fallback...", "WARNING", "fixconf")
        try:
            create_patch(".")
            apply_patch()
            import_patcher_applied = True

            # Re-read working conf (may have been modified by workaround manager) and retry
            with open(working_conf) as f:
                working_conf_dict = json.load(f)
            updated_config_dict = {k: v for k, v in working_conf_dict.items() if k not in ("files", "verify", "msg")}
            success, output, updated_config_dict = workaround_mgr.run_compilation_with_workarounds(
                cmd, working_conf, working_conf_dict, contracts, updated_config_dict
            )

            if not success:
                logger.log("Compilation still fails after import patcher, reverting", "WARNING", "fixconf")
                revert_patch()
                import_patcher_applied = False
        except Exception as e:
            logger.log(f"Import patcher failed: {e}", "WARNING", "fixconf")

    # Read the final working conf (workaround manager modifies it on disk)
    with open(working_conf) as f:
        fixed_conf = json.load(f)

    # Strip low-priority build system keys that weren't in the user's original conf.
    # These were needed for compilation but can break the prover (e.g. solc_optimize with huge value).
    # After stripping, re-verify compilation to make sure they weren't actually needed.
    keys_to_strip = [k for k in merged_bs_keys if k in _LOW_PRIORITY_BS_KEYS and k not in original_conf]
    if keys_to_strip and success:
        stripped_conf = copy.deepcopy(fixed_conf)
        for key in keys_to_strip:
            stripped_conf.pop(key, None)

        # Write stripped conf and verify it still compiles
        with open(working_conf, "w") as f:
            json.dump(stripped_conf, f, indent=2)
        verify_result = subprocess.run(cmd, capture_output=True, text=True)

        if verify_result.returncode == 0:
            logger.log(
                f"Stripped unnecessary build system keys: {', '.join(keys_to_strip)}", "INFO", "fixconf"
            )
            fixed_conf = stripped_conf
        else:
            logger.log(
                f"Cannot strip build system keys ({', '.join(keys_to_strip)}) — compilation needs them",
                "WARNING",
                "fixconf",
            )

    # Strip synthetic verify key
    if synthetic_verify:
        fixed_conf.pop("verify", None)

    # Write output
    out_path = conf_file.with_suffix(".fixed.conf")
    with open(out_path, "w") as f:
        json.dump(fixed_conf, f, indent=2)
        f.write("\n")

    # Cleanup
    working_conf.unlink(missing_ok=True)
    dummy_spec.unlink(missing_ok=True)

    # Report
    changes = _report_changes(original_conf, fixed_conf)
    if import_patcher_applied:
        logger.log("  Import patcher was applied to source files", "INFO", "fixconf")

    if success and changes > 0:
        logger.log(f"Fixed {changes} setting(s) in {out_path}", "SUCCESS", "fixconf")
    elif success:
        logger.log(f"No changes needed — compilation already succeeds ({out_path})", "SUCCESS", "fixconf")
    else:
        logger.log(f"Compilation still fails after all workarounds ({out_path})", "ERROR", "fixconf")

    return success


def main():
    parser = create_parser()
    args = parser.parse_args()

    if not args.conf_file.exists():
        logger.log(f"Conf file not found: {args.conf_file}", "ERROR", "fixconf")
        sys.exit(1)

    logger.verbose = args.verbose
    success = fix_conf(
        conf_file=args.conf_file,
        no_build_system=args.no_build_system,
        verbose=args.verbose,
        profile=args.profile,
    )
    sys.exit(0 if success else 1)
