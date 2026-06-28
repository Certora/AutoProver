"""
CLI entry point for standalone autosetup (certora-autosetup).

Runs the autosetup phase only — no checker generation or verification.
Outputs AutosetupResult to .certora_internal/autosetup_result.json.
"""

import json
import shlex
import sys
from datetime import datetime
from pathlib import Path

from certora_autosetup.autosetup.autosetup import Autosetup
from certora_autosetup.autosetup.cli_args import create_parser
from certora_autosetup.autosetup.types import AutosetupConfig
from certora_autosetup.cache.cache_fs import get_fs, init_cache_fs
from certora_autosetup.cache.content_cache import ContentCache
from certora_autosetup.reporting.reporter import Reporter
from certora_autosetup.setup.sanity_rule_generator import SanityRuleGenerator
from certora_autosetup.setup.setup_prover import SetupProver
from certora_autosetup.setup.signature_manager import SignatureManager
from certora_autosetup.utils.cloud_runner import CloudProverRunner
from certora_autosetup.utils.constants import (
    CERTORA_REPORTS_DIR,
    DIR_CERTORA_INTERNAL,
    FILE_AUTOSETUP_RESULT,
    FILE_LLM_USAGE,
)
from certora_autosetup.utils.contract_utils import auto_detect_contracts, deduplicate_contract_handles, parse_contract_files, resolve_contract_handles
from certora_autosetup.utils.enhanced_config_manager import ConfigManager
from certora_autosetup.utils.llm_util import LlmUsageReport, ledger_reset
from certora_autosetup.utils.logger import logger
from certora_autosetup.utils.scope import Scope


def main():
    parser = create_parser()
    args = parser.parse_args()

    init_cache_fs()
    # Start a clean per-process LLM usage ledger; every LLM response this process
    # makes is recorded into it (see llm_util).
    ledger_reset()

    # Validate args
    if args.min_loop_iter < 1:
        parser.error(f"--min-loop-iter ({args.min_loop_iter}) must be >= 1")
    if args.min_loop_iter > args.max_loop_iter:
        parser.error(f"--min-loop-iter ({args.min_loop_iter}) must be <= --max-loop-iter ({args.max_loop_iter})")

    # Parse contract handles
    contract_handles = []
    if args.contract_files_and_name:
        contract_handles = parse_contract_files(args.contract_files_and_name)
        contract_handles = resolve_contract_handles(
            contract_handles, Path.cwd(), profile=args.profile,
            requested_build_system=args.build_system,
        )

    if not contract_handles:
        contract_handles = auto_detect_contracts(
            Path.cwd(), profile=args.profile,
            requested_build_system=args.build_system,
        )

    contract_handles = deduplicate_contract_handles(contract_handles)

    # Parse main contract
    main_handles = parse_contract_files([args.main_contract])
    main_contract_handle = main_handles[0]

    # TODO: a bare `path.sol` spec drops only the contract whose name matches the file
    # stem. Expand to "drop every concrete contract in the file" for symmetry with
    # auto-detect's emit-all default. Mirror the same expansion for include specs
    # (parse_contract_files / resolve_contract_handles).
    if args.exclude_contracts:
        exclude_handles = set(parse_contract_files(args.exclude_contracts))
        if main_contract_handle in exclude_handles:
            parser.error(
                f"--exclude-contracts conflicts with --main-contract "
                f"{main_contract_handle.contract_name}@{main_contract_handle.source_file}"
            )
        before = len(contract_handles)
        contract_handles = [h for h in contract_handles if h not in exclude_handles]
        logger.log(
            f"Excluded {before - len(contract_handles)} contract(s) via --exclude-contracts",
            "INFO", "Autosetup",
        )

    # Parse extra args
    extra_args = shlex.split(args.extra_args) if args.extra_args else []

    project_root = Path.cwd()
    certora_dir = Path("certora")
    certora_dir.mkdir(exist_ok=True)

    logger.set_verbosity(args.verbose)
    orchestration_timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

    config_manager = ConfigManager(project_root)
    scope = Scope(project_root)
    signature_manager = SignatureManager(project_root)
    rule_generator = SanityRuleGenerator(certora_dir, lambda msg, level="INFO": logger.log(msg, level, "Autosetup"))

    if args.use_local_runner:
        from certora_autosetup.utils.local_runner import LocalProverRunner
        prover_runner = LocalProverRunner(
            project_root=project_root,
            config_manager=config_manager,
            certora_run_path=args.certora_run_command,
            disable_cache=args.no_cache,
            max_concurrent_jobs=args.max_local_jobs,
        )
    else:
        prover_runner = CloudProverRunner(
            project_root=project_root,
            config_manager=config_manager,
            certora_run_path=args.certora_run_command,
            disable_cache=args.no_cache,
            cancel_jobs_on_cleanup=not args.no_cancel_jobs_on_cleanup,
        )

    setup_prover = SetupProver(
        log=lambda msg, level="INFO": logger.log(msg, level, "Autosetup"),
        certora_dir=certora_dir,
        script_dir=Path(__file__).parent.parent,
        additional_contracts=args.additional_contracts or [],
        extra_args=extra_args,
        skip_llm=args.skip_llm,
        force_llm_regenerate=args.force_llm_regenerate,
        stop_after_summaries=args.stop_after_summaries,
        scope=scope,
        verbose=args.verbose,
        certora_run_command=args.certora_run_command,
        contract_names=[ch.contract_name for ch in contract_handles],
        get_build_system_config_dict=lambda: {},  # Will be set by autosetup
        solc_default_version=args.solc_default,
    )

    _content_cache = ContentCache("autosetup")
    if args.cache_status:
        fs = get_fs()
        base = _content_cache._base
        entries = fs.glob(base + "/*.json") if fs.exists(base) else []
        print(f"Cache dir: {base}")
        print(f"Cache entries: {len(entries)}")
        sys.exit(0)
    if args.clear_cache:
        _content_cache.clear()
        print("Cache cleared.")

    reports_dir = Path(args.reports_dir) if args.reports_dir else Path(CERTORA_REPORTS_DIR) / orchestration_timestamp

    reporter = Reporter(
        log=lambda msg, level="INFO": logger.log(msg, level, "Autosetup"),
        verbose=args.verbose,
        skip_breadcrumbs=True,
        reports_dir=reports_dir,
        prover_api=prover_runner.prover_api,
    )

    autosetup = Autosetup(
        config=AutosetupConfig(
            project_root=project_root,
            certora_dir=certora_dir,
            script_dir=Path(__file__).parent.parent,
            reports_dir=reports_dir,
            orchestration_timestamp=orchestration_timestamp,
            verbose=args.verbose,
            certora_run_command=args.certora_run_command,
            extra_args=extra_args,
            additional_contracts=args.additional_contracts or [],
            requested_build_system=args.build_system,
            requested_profile=args.profile,
            skip_sanity_setup=args.skip_sanity_setup,
            skip_call_resolution=args.skip_call_resolution,
            skip_proxy_detection=args.skip_proxy_detection,
            skip_harnessing=args.skip_harnessing,
            composer_output=getattr(args, 'composer_setup', None),
            dummy_erc20=args.dummy_erc20,
            keep_intermediate_typechecker_files=args.keep_intermediate_typechecker_files,
            skip_hashing_bound_detection=args.skip_hashing_bound_detection,
            min_loop_iter=args.min_loop_iter,
            max_loop_iter=args.max_loop_iter,
            no_strip_contracts=args.no_strip_contracts,
            include_foundry_packages=not args.exclude_foundry_packages,
            run_source=args.run_source,
        ),
        setup_prover=setup_prover,
        prover_runner=prover_runner,
        config_manager=config_manager,
        scope=scope,
        signature_manager=signature_manager,
        rule_generator=rule_generator,
        contract_handles=contract_handles,
    )

    # After creating autosetup, fix setup_prover's get_build_system_config_dict reference
    setup_prover.get_build_system_config_dict = autosetup.get_build_system_config_dict

    Autosetup.ensure_git_config_files()
    autosetup._load_signature_database_to_scope()
    autosetup.generate_dummy_erc20_files()

    result = autosetup.run(main_contract_handle, skip_warmup=args.skip_warmup)

    # Persist this run's token usage to the per-run reports dir (zero-row on a
    # cache hit). In parallel runs, collect_reports copies it into the aggregated
    # reports dir, where aggregate_llm_usage merges the per-contract files.
    ledger_rows = result.llm_usage
    reports_dir.mkdir(parents=True, exist_ok=True)
    (reports_dir / FILE_LLM_USAGE).write_text(
        json.dumps(LlmUsageReport.from_rows(ledger_rows).to_dict(), indent=2)
    )

    result_path = Path(DIR_CERTORA_INTERNAL) / FILE_AUTOSETUP_RESULT
    print(f"Autosetup complete. Result saved to: {result_path}")
    if args.composer_setup and result.composer_output is not None:
        Path(args.composer_setup).write_text(json.dumps(result.composer_output, indent=2))
        print(f"Composer output written to: {args.composer_setup}")

    # Submit test run jobs and generate reports via ConfRunner
    if result.test_run_specs:
        from certora_autosetup.reporting.json_reporter import JsonReporter
        from certora_autosetup.setup.setup_completeness_checker import SetupCompletenessChecker, SetupCompletenessReport
        from certora_autosetup.conf_runner import ConfRunner, ConfRunnerConfig

        json_reporter = JsonReporter(Path(DIR_CERTORA_INTERNAL))
        setup_checker = SetupCompletenessChecker()
        aggregated_setup_report = SetupCompletenessReport(Path(DIR_CERTORA_INTERNAL))

        conf_runner = ConfRunner(
            config=ConfRunnerConfig(
                extra_args=extra_args,
                verbose=args.verbose,
                certora_run_command=args.certora_run_command,
            ),
            prover_runner=prover_runner,
            config_manager=config_manager,
            reporter=reporter,
            json_reporter=json_reporter,
            reports_dir=reports_dir,
            setup_checker=setup_checker,
            aggregated_setup_report=aggregated_setup_report,
            orchestration_timestamp=orchestration_timestamp,
            project_root=project_root,
            certora_dir=certora_dir,
        )
        conf_runner.run_confs(
            config_files=[],
            test_run_specs=result.test_run_specs,
            sanity_analysis=result.sanity_analysis,
            bytes_mappings=result.bytes_mappings,
            llm_usage=ledger_rows,
        )

    sys.exit(0)
