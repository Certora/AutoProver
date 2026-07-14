"""
Argument parser for the certora-autosetup CLI.
"""

import argparse

from certora_autosetup.utils.constants import DEFAULT_SOLC_VERSION


def create_parser():
    """Create and return the argument parser for the autosetup CLI."""
    parser = argparse.ArgumentParser(
        description="PreAudit Autosetup - Run Certora verification tools with intelligent caching",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Usage Examples:
  # Run all checkers (default behavior)
  certora-autosetup Contract.sol

  # Run with verbose output
  certora-autosetup -v Contract1.sol Contract2.sol

  # Advanced ExternalCallChecker with non-zero target assertions
  certora-autosetup --extcall-mode advanced --extcall-check-non-zero Contract.sol

  # Disable specific checkers
  certora-autosetup--disable-extcall Contract.sol
  certora-autosetup--disable-privileged Contract.sol

  # Skip warmup phase
  certora-autosetup--skip-warmup Contract.sol

  # Pass additional certoraRun arguments
  certora-autosetup Contract.sol --server prover --prover_version master
  certora-autosetup Contract.sol --timeout 300 --settings mySettings

  # Pass additional certoraRun arguments using --extra-args (as quoted string)
  certora-autosetup--extra-args "--server prover --prover_version master" Contract.sol
  certora-autosetup--extra-args "--timeout 300 --settings mySettings" Contract.sol

  # All options combined
  certora-autosetup-v --extcall-mode advanced --disable-privileged Contract.sol --server prover --timeout 300

  # Contract are always split - run each verification separately for each contract
  certora-autosetupToken.sol Vault.sol

  # Include additional contracts in scene without verification (e.g., harness files)
  certora-autosetup--additional-contracts harness/Helper.sol Contract.sol

  # Skip breadcrumb fetching for faster debugging
  certora-autosetup--skip-breadcrumbs Contract.sol

  # Run only sanity setup (no rule generation or verification)
  certora-autosetup--skip-checkers Contract.sol

  # Debug summaries generation by stopping after that phase
  certora-autosetup--stop-after-summaries Contract.sol

  # Skip LLM analysis for faster debugging (static analysis only)
  certora-autosetup--skip-llm Contract.sol

  # Skip generating harness wrappers for contracts behind indexed storage paths
  certora-autosetup --skip-harnessing Contract.sol

  # Cache management
  certora-autosetup--cache-status                  # Show cache status
  certora-autosetup--clear-cache Contract.sol  # Clear cache before running
  certora-autosetup--no-cache Contract.sol     # Disable caching

Generator Options:
  --disable-extcall         Disable ExternalCallChecker (enabled by default)
  --extcall-mode {simple,advanced}  ExternalCallChecker complexity mode
  --extcall-check-non-zero  Use assertions instead of requires for target > 0
  --disable-privileged      Disable PrivilegedOperations checker (enabled by default)
  --disable-delegatecall    Disable Delegatecall Target Stability Checker (enabled by default)
  --disable-cei             Disable CEI Violation checker (enabled by default)
  --disable-pausability     Disable PausabilityChecker (enabled by default)

Cache Options:
  --no-cache               Disable caching (always run fresh verifications)
  --clear-cache            Clear all cached results before running
  --cache-status           Show cache status and exit

Submission Options:
  --require-all-submissions  Require all configurations to be successfully submitted (default: True)
  --allow-submission-failures Allow to continue even if some configurations fail to submit

Retry Options:
  --retry-difficult         Retry rules with timeout/unknown methods when verification shows promise (experimental)
                           Retries occur only when at least one rule (excluding envfreeFuncsStaticCheck)
                           has a real result (VERIFIED or VIOLATED), indicating there is "hope" for success.
                           Creates individual retry jobs for each rule with timeout/unknown methods.

Prerequisites: NONE! All generators run automatically as needed.
Caching: Results are cached based on file hashes to avoid redundant runs.
Default Behavior: By default, all configurations must be successfully submitted. Use --allow-submission-failures to override.
        """
    )

    parser.add_argument(
        'contract_files_and_name',
        nargs='*',  # Changed from '+' to '*' to allow zero arguments
        help='List of Solidity contract files (.sol) optionally with contract names (file.sol:ContractName)'
    )

    parser.add_argument(
        '-v', '--verbose',
        action='count',
        default=0,
        help='Increase verbosity level (can be used multiple times: -v, -vv, -vvv)'
    )

    parser.add_argument(
        '--build-system',
        choices=['auto', 'foundry', 'hardhat'],
        default='auto',
        help='Specify build system to use (default: auto - detect from project structure)'
    )

    parser.add_argument(
        '--profile',
        type=str,
        default=None,
        help='Build system profile to use (e.g. Foundry profile name). Default: auto-detect'
    )

    parser.add_argument(
        '--skip-warmup',
        action='store_true',
        help='Skip cache warmup phase'
    )

    # Generator options (all enabled by default)
    parser.add_argument(
        '--disable-extcall',
        action='store_true',
        help='Disable ExternalCallChecker generator (enabled by default)'
    )

    parser.add_argument(
        '--extcall-mode',
        choices=['simple', 'advanced'],
        default='simple',
        help='ExternalCallChecker mode (default: simple)'
    )

    parser.add_argument(
        '--extcall-check-non-zero',
        action='store_true',
        help='Assert target > 0 in ExternalCallChecker specs (default: use require)'
    )

    parser.add_argument(
        '--disable-privileged',
        action='store_true',
        help='Disable PrivilegedOperations generator (enabled by default)'
    )

    parser.add_argument(
        '--disable-delegatecall',
        action='store_true',
        help='Disable Delegatecall Target Stability Checker (enabled by default)'
    )

    parser.add_argument(
        '--disable-storage-collision',
        action='store_true',
        help='Disable StorageCollisionChecker generator (enabled by default)'
    )

    parser.add_argument(
        '--disable-erc4626',
        action='store_true',
        help='Disable ERC4626 Tokenized Vault checker (enabled by default)'
    )

    parser.add_argument(
        '--disable-erc20',
        action='store_true',
        help='Disable ERC20 Core Invariant checker (enabled by default)'
    )
    parser.add_argument(
        '--disable-cei',
        action='store_true',
        help='Disable CEI Violation checker (enabled by default)'
    )

    parser.add_argument(
        '--disable-pausability',
        action='store_true',
        help='Disable PausabilityChecker generator (enabled by default)'
    )

    parser.add_argument(
        '--disable-input-validation',
        action='store_true',
        help='Disable InputValidationChecker generator (enabled by default)'
    )

    parser.add_argument(
        '--disable-generic',
        action='store_true',
        help='Disable GenericRules checker (enabled by default)'
    )


    # Cache management options
    parser.add_argument(
        '--no-cache',
        action='store_true',
        help='Disable caching (always run fresh verifications)'
    )

    parser.add_argument(
        '--clear-cache',
        action='store_true',
        help='Clear all cached results before running'
    )

    parser.add_argument(
        '--cache-status',
        action='store_true',
        help='Show cache status and exit'
    )

    parser.add_argument(
        '--max-configs',
        type=int,
        metavar='N',
        help='Limit the number of config files to run (for testing purposes)'
    )

    parser.add_argument(
        '--force-llm-regenerate',
        action='store_true',
        help='Force regeneration of LLM summaries even if they already exist'
    )

    parser.add_argument(
        '--skip-breadcrumbs',
        action='store_true',
        help='Skip breadcrumb fetching for faster debugging'
    )

    parser.add_argument(
        '--stop-after-summaries',
        action='store_true',
        help='Stop execution after function summaries generation (for debugging summaries setup)'
    )

    parser.add_argument(
        '--stop-after-compilation-analysis',
        action='store_true',
        help='Stop right after the compilation-analysis phase succeeds '
             '(for compile-only sweeps that never reach the prover)'
    )

    parser.add_argument(
        "--composer-setup",
        type=str,
        default=None
    )

    parser.add_argument(
        '--skip-llm',
        action='store_true',
        help='Skip LLM analysis entirely for faster debugging (only use static analysis)'
    )


    parser.add_argument(
        '--skip-checkers',
        action='store_true',
        help='Run only sanity phases (summaries, call resolution, loop/hashing optimization) then stop before rule generation and verification'
    )

    parser.add_argument(
        '--skip-sanity-setup',
        action='store_true',
        help='Skip sanity bounds optimization (loop_iter and hashing_length_bound)'
    )

    parser.add_argument(
        '--skip-hashing-bound-detection',
        type=int,
        metavar='BOUND',
        default=None,
        help='Skip hashing bound detection and use the given value as hashing_length_bound (e.g. 1024)'
    )

    parser.add_argument(
        '--min-loop-iter',
        type=int,
        metavar='N',
        default=3,
        help='Lower bound (inclusive) of the loop_iter search range used by the sanity phase (default: 3)'
    )

    parser.add_argument(
        '--max-loop-iter',
        type=int,
        metavar='N',
        default=5,
        help='Upper bound (inclusive) of the loop_iter search range used by the sanity phase (default: 5)'
    )

    parser.add_argument(
        '--skip-call-resolution',
        action='store_true',
        help='Skip call resolution phase (linking and dispatching) - enabled by default'
    )

    parser.add_argument(
        '--no-strip-contracts',
        action='store_true',
        help='Keep all explicitly passed contracts in the base config files list instead of stripping them before call resolution'
    )

    parser.add_argument(
        '--run-source',
        type=str,
        default=None,
        help='Upstream product that initiated this run (e.g. AUTO_PROVER, '
             'STATIC_ANALYZER). When set, stamped into every base conf as "run_source".'
    )

    parser.add_argument(
        '--skip-proxy-detection',
        action='store_true',
        help='Skip proxy pattern detection and implementation contract discovery'
    )

    parser.add_argument(
        '--skip-harnessing',
        action='store_true',
        help='Skip generating per-instance harness wrappers for contracts reached through '
             'indexed storage paths (arrays/mappings); link the implementing contracts directly'
    )

    parser.add_argument(
        '--skip-setup-check',
        action='store_true',
        help='Skip setup completeness checking after prover runs'
    )

    parser.add_argument(
        '--certora-run-command',
        default='certoraRun',
        help='Command to use for running Certora verification (default: certoraRun)'
    )

    parser.add_argument(
        '--extra-args',
        type=str,
        help='Extra arguments to pass to certoraRun as a quoted string (e.g., "--server prover --timeout 300")'
    )

    parser.add_argument(
        '--main-contract',
        type=str,
        required=True,
        help='Main contract file to verify (e.g., Contract.sol:MainContract)'
    )

    parser.add_argument(
        '--additional-contracts',
        type=str,
        nargs='*',
        default=[],
        help='Additional contract files to include in the scene without verification (e.g., harness files)'
    )

    parser.add_argument(
        '--exclude-contracts',
        type=str,
        nargs='*',
        default=[],
        metavar='PATH.sol[:CONTRACT]',
        help=(
            'Drop contracts from the auto-detected scene. Same spec format as '
            '--additional-contracts: a project-relative .sol path, optionally suffixed '
            'with :ContractName. Without the suffix the contract name is inferred from '
            'the file stem. If a file contains multiple contracts, enumerate them '
            'explicitly: file.sol:Foo file.sol:Bar.'
        )
    )

    parser.add_argument(
        '--require-all-submissions',
        action='store_true',
        default=True,  # Default to True - require all submissions to succeed
        help='Require all configurations to be successfully submitted (default: True)'
    )

    parser.add_argument(
        '--allow-submission-failures',
        action='store_false',
        dest='require_all_submissions',
        help='Allow to continue even if some configurations fail to submit'
    )

    parser.add_argument(
        '--retry-difficult',
        action='store_true',
        default=False,
        help='Enable retry of rules with timeout/unknown methods when verification shows promise (experimental)'
    )

    parser.add_argument(
        '--use-local-runner',
        action='store_true',
        default=False,
        help='Use local prover runner instead of cloud runner (runs certoraRun locally)'
    )

    parser.add_argument(
        '--max-local-jobs',
        type=int,
        metavar='N',
        default=1,
        help='Maximum number of local prover jobs to run concurrently (default: 1). '
             'Each certoraRun spawns many z3 processes, so keeping this low prevents CPU contention.'
    )

    parser.add_argument(
        '--dummy-erc20',
        type=int,
        metavar='N',
        default=1,
        help='Specify an integer value for the number of dummy ERC20 contracts to include (default: 1)'
    )

    parser.add_argument(
        '--keep-intermediate-typechecker-files',
        action='store_true',
        help='Keep intermediate typechecker files instead of overwriting them'
    )

    parser.add_argument(
        '--solc-default',
        type=str,
        default=DEFAULT_SOLC_VERSION,
        metavar='VERSION',
        help=f'Default Solidity compiler version for compiler_map (default: {DEFAULT_SOLC_VERSION}). Example: solc8.26'
    )

    parser.add_argument(
        '--no-cancel-jobs-on-cleanup',
        action='store_true',
        help='Do not cancel cloud jobs when cleaning up (jobs continue running on Certora servers)'
    )

    parser.add_argument(
        '--exclude-foundry-packages',
        action='store_true',
        help='Exclude packages from foundry config in generated configurations (default: packages are included)'
    )

    parser.add_argument(
        '--reports-dir',
        type=str,
        default=None,
        help='Directory for report output (default: .CertoraProverLiteReports/<timestamp>)',
    )

    return parser
