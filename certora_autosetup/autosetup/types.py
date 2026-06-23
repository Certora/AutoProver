"""
Type definitions for the autosetup package.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


@dataclass
class AutosetupConfig:
    """Configuration for the Autosetup phase."""

    # Project paths
    project_root: Path
    certora_dir: Path
    script_dir: Path
    reports_dir: Path
    orchestration_timestamp: str
    verbose: int = 0

    # Prover settings
    certora_run_command: str = "certoraRun"
    extra_args: list[str] = field(default_factory=list)
    additional_contracts: list[str] = field(default_factory=list)

    # Build system
    requested_build_system: str | None = None
    requested_profile: str | None = None
    include_foundry_packages: bool = True

    # Feature flags
    skip_sanity_setup: bool = False
    # When True, skip the AIComposer-backed sanity coverage analysis (the per-method
    # coverage rerun jobs + sanity_analyzer vacuity analysis). Loop-iter and hashing-bound
    # detection still run. Set by PreAudit, which does not consume the advanced analysis.
    skip_sanity_coverage_analysis: bool = False
    skip_hashing_bound_detection: int | None = None
    min_loop_iter: int = 3
    max_loop_iter: int = 5
    skip_call_resolution: bool = False
    skip_proxy_detection: bool = False
    skip_harnessing: bool = False
    no_strip_contracts: bool = False
    keep_intermediate_typechecker_files: bool = False
    dummy_erc20: int | None = None
    composer_output: str | None = None

    # Prover metadata: identifies which upstream product asked for the run.
    # When set, stamped into every base conf as `"run_source": ...`.
    # When None (default), no run_source key is written — preserves behavior
    # for callers (e.g. direct CLI use) that don't want to attribute the run.
    run_source: str | None = None



@dataclass
class AutosetupResult:
    """Complete output from the autosetup phase.

    All paths are absolute. When serialized to JSON (for caching),
    paths are stored relative to project_root for portability.
    """

    # Artifact paths
    base_configs: dict[str, Path]            # contract_name -> certora/base-{name}.conf
    summary_specs: dict[str, Path]           # contract_name -> certora/specs/summaries/{name}_base_summaries.spec
    signature_database_path: Path | None     # .certora_internal/preaudit_state/signature_database.json
    asts_path: Path | None                   # .certora_internal/all_asts.json
    bytes_mappings_path: Path | None         # .certora_internal/bytes_mappings.json
    all_sources_path: Path | None = None     # .certora_internal/all_sources.json (compiler srclist)

    # Runtime state
    import_patcher_applied: bool = False
    compilation_config_updates: dict[str, Any] = field(default_factory=dict)
    sanity_analysis: dict = field(default_factory=dict)  # contract -> method -> SanityFailureResult
    bytes_mappings: list = field(default_factory=list)    # list[tuple[ContractHandle, list[str]]]

    # Deferred sanity test run jobs (created during warmup, submitted by ConfRunner)
    test_run_specs: list = field(default_factory=list)    # list[ProverJobSpec]

    # Build system info (needed by conf_runner for merging into checker confs)
    build_system_config_dict: dict[str, Any] = field(default_factory=dict)

    # Execution metadata
    orchestration_timestamp: str = ""
    llm_usage: list = field(default_factory=list)

    # Setup-only mode output (populated when composer_output is requested)
    composer_output: dict[str, Any] | None = None

    def all_referenced_paths(self) -> list[Path | None]:
        """File paths whose existence gates cache validity (consumed on a cache hit).

        Excludes ``asts_path``: the AST dump is a compute-only intermediate (used to
        build the sig DB / AST graph during a full run) and is never read on a cache
        hit, so it must not gate validity. It is also written to local disk only
        (shutil.copy2), so requiring it would always invalidate the cache in SaaS,
        where .certora_internal/ is not hydrated to local disk.
        """
        paths: list[Path | None] = [*self.base_configs.values(), *self.summary_specs.values()]
        paths.extend([self.signature_database_path, self.bytes_mappings_path, self.all_sources_path])
        return paths

    def to_json(self, project_root: Path) -> dict[str, Any]:
        """Serialize to JSON-compatible dict with paths relative to project_root."""
        def _rel(p: Path | None) -> str | None:
            if p is None:
                return None
            try:
                return str(p.relative_to(project_root))
            except ValueError:
                return str(p)

        return {
            "base_configs": {k: _rel(v) for k, v in self.base_configs.items()},
            "summary_specs": {k: _rel(v) for k, v in self.summary_specs.items()},
            "signature_database_path": _rel(self.signature_database_path),
            "asts_path": _rel(self.asts_path),
            "bytes_mappings_path": _rel(self.bytes_mappings_path),
            "all_sources_path": _rel(self.all_sources_path),
            "import_patcher_applied": self.import_patcher_applied,
            "compilation_config_updates": self.compilation_config_updates,
            "build_system_config_dict": self.build_system_config_dict,
            "orchestration_timestamp": self.orchestration_timestamp,
        }

    def save(self, path: Path, project_root: Path) -> None:
        """Persist AutosetupResult to a JSON file for caching."""
        path.parent.mkdir(parents=True, exist_ok=True)
        with open(path, "w") as f:
            json.dump(self.to_json(project_root), f, indent=2)

    @classmethod
    def from_json(cls, data: dict[str, Any], project_root: Path) -> "AutosetupResult":
        """Reconstruct from a JSON dict (as produced by to_json).

        Note: This restores only the serializable fields. Non-serializable fields
        (test_run_specs, sanity_analysis, bytes_mappings, llm_usage) are
        left at their defaults and must be reconstructed if needed.
        """
        def _abs(p: str | None) -> Path | None:
            if p is None:
                return None
            return project_root / p

        return cls(
            base_configs={k: _abs(v) for k, v in data["base_configs"].items()},  # type: ignore[arg-type]
            summary_specs={k: _abs(v) for k, v in data["summary_specs"].items()},  # type: ignore[arg-type]
            signature_database_path=_abs(data.get("signature_database_path")),
            asts_path=_abs(data.get("asts_path")),
            bytes_mappings_path=_abs(data.get("bytes_mappings_path")),
            all_sources_path=_abs(data.get("all_sources_path")),
            import_patcher_applied=data.get("import_patcher_applied", False),
            compilation_config_updates=data.get("compilation_config_updates", {}),
            build_system_config_dict=data.get("build_system_config_dict", {}),
            orchestration_timestamp=data.get("orchestration_timestamp", ""),
        )

    @staticmethod
    def load(path: Path, project_root: Path) -> "AutosetupResult":
        """Load a previously persisted AutosetupResult from JSON."""
        with open(path) as f:
            data = json.load(f)
        return AutosetupResult.from_json(data, project_root)
