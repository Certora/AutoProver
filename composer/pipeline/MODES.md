# AutoProve External Run Modes

A run mode decides what a run does with the properties it infers.
`comprehensive` and `prioritized` live in this package and formalize
properties. An **external** mode lives in another package: the pipeline runs
its shared front half, then hands the extracted properties to that package,
which returns the report.

Today there is one external mode, `evaluate`. It measures the specs and confs
already in the project against the inferred properties. Its implementation is
installed separately. This package only declares the mode and the hook.

## What the driver does for an external mode

| Step in `run_pipeline_inner` | `comprehensive` / `prioritized` | external mode |
|---|---|---|
| preflight and system analysis | yes | yes |
| `prepare_system` | yes, with harness creation | yes, **without** harness creation (prover backend) |
| `prepare_formalization` (AutoSetup, summaries) | yes | **no** |
| `prepare_external_mode` (project build) | no | **yes**, alongside extraction |
| `_extract_all` (property inference + plugins) | yes | yes |
| ranking, formalization, `build_report` | yes | **no** |
| `RunModeImpl.run`, then `write_report` | no | **yes** |

### Harness creation

For an external mode, the prover backend's `prepare_system` does not run the
harness agent. The project's confs already name the contracts they verify, and
the agent costs LLM calls and writes harness files into the project. `main` is
the located main contract of the analysis, lifted with no harnesses
(`lift_unharnessed`).

The CLI keeps the requested main contract for an external mode. A library named
as the main contract stays the library: the generated library harness that the
built-in modes verify through is neither created nor validated, and AutoSetup's
`--build-only` step skips that swap too.

### Project build

The driver builds the project for every external-mode run, before the
hand-off. It calls `PreparedSystem.prepare_external_mode`, which runs
alongside extraction, and passes the result to the mode as
`ModeContext.project_build`.

For the prover backend, the build is AutoSetup's build step
(`certora_autosetup --build-only`, `Autosetup.build`): build-system
detection, then the compilation analysis with its workarounds (solc selection
among the installed compilers, via-ir and optimizer settings, remappings and
packages, import fixes). It stops there: no summaries, sanity specs, base
conf, warmup or prover job. It runs over every contract the analysis found.
It shows in the task UI as "Project Build" (task id `project-build`), and its
output goes to `.certora_internal/autoProve/project_build.log`.

The build does not install dependencies or solc binaries. Like AutoSetup, it
expects them on the machine and picks among the installed compilers.

A failed build does not stop the run. The driver adds
`project build: <error>` to the run's failures, and the mode receives the
failed `ProjectBuild` and decides what it means. A backend with no build step
(the default `prepare_external_mode`) hands over `project_build=None`.

The build is not cached. Its import fixes edit project sources, so a cache
hit would leave the tree uncompilable.

### Caching

The extraction cache keys do not include the run mode. A component's key
includes the harnessed application, and an external-mode run lifts the
analysis with no harnesses. So an external-mode run replays a `comprehensive`
run's inference at no cost when that run's harness creation generated no
harnesses, and the reverse is also true. When the `comprehensive` run did
generate harnesses, the keys differ and inference runs again.

## Shipping a mode

Register a **loader class** under the entry-point group
`certora.autoprove.modes`. The entry-point **name** is the mode's wire form:

```toml
[project.entry-points."certora.autoprove.modes"]
evaluate = "my_pkg.mode:EvaluateLoader"
```

```python
class EvaluateLoader(RunModeLoader[ContractComponentInstance, ContractInstance]):
    name = RunMode.EVALUATE

    @property
    def scope(self) -> PluginScope[ContractComponentInstance]:
        return ForEcosystem(ContractComponentInstance)

    @asynccontextmanager
    async def initialize(self) -> AsyncIterator[RunModeImpl[ContractComponentInstance, ContractInstance]]:
        async with open_my_resources() as res:
            yield EvaluateMode(res)


class EvaluateMode(RunModeImpl[ContractComponentInstance, ContractInstance]):
    async def run(self, ctx: ModeContext[Any, Any, ContractComponentInstance, ContractInstance]) -> ModeResult:
        ...
        return ModeResult(report=report, succeeded=True)
```

Contract:

* `resolve_run_mode` checks that an entry point with the mode's name exists,
  before any service starts. A missing implementation stops the run there,
  with an error that names the mode and the entry-point group.
* The loader class is instantiated with **no arguments**, once, and only for a
  run in its mode. Construction must be trivial. Real setup belongs in
  `initialize()`.
* `name` must equal the mode the entry point is registered for, and there
  must be exactly one entry point per mode. Either mismatch fails the run.
* `scope` takes the same values as a plugin's scope (`AnyEcosystem`,
  `ForEcosystem(unit)`). A run over a unit type outside it fails at load.
* `initialize()` is entered before the pipeline body starts and exited when
  the run returns or raises. It is never entered for another mode, so an
  installed mode costs `comprehensive` and `prioritized` runs nothing.
* A mode is not a plugin. It is not in the active-plugin list and does not enter
  any cache key.

## `ModeContext`

What `run` receives (`composer.pipeline.modes.ModeContext`, a frozen
dataclass):

| Field | What it holds |
|---|---|
| `run` | the `PipelineRun`: `env` (services, models), `ctx` (cache root), `runner` / `cpu_runner` (task runners), `source`, `run_mode` |
| `main` | the located main of the analyzed program (`prepared.main`): for EVM a `ContractInstance` over the unharnessed lift |
| `batches` | per component (`ExtractedBatch`): `feat` (the unit), `props` (post-plugin properties), `feat_ctx` (the component's cache namespace) |
| `extraction_failures` | components whose inference raised (`ExtractionFailure`): `unit`, `error` |
| `threat_model`, `extra_context` | the run's guidance documents |
| `artifact_store` | the backend's artifact store. Write deliverables through `write_deliverable` |
| `phase` | the backend's phase for the mode's work. Tag the mode's tasks with it. The prover backend's is `AutoProvePhase.EVALUATION` ("Evaluation" in the UIs). A backend that names none gives its `formalization` phase |
| `project_build` | the `ProjectBuild` the driver ran, or `None` for a backend with no build step |
| `prover_options` | the backend's `ProverOptions` (`app`, `server`: set for a cloud run, `global_timeout`; `cloud`, `cli_args()`), or `None` for a backend that runs no prover |
| `active_plugins` | the active-plugin list extraction ran with |

`ProjectBuild` (`composer.pipeline.modes.ProjectBuild`, a pydantic model):

| Field | Meaning |
|---|---|
| `succeeded` | the project compiled |
| `compilation_config` | the build keys of a conf under which the project compiles: `solc` or `compiler_map`, `packages`, the via-ir, optimizer and evm-version keys and their per-contract maps. Never `files` or `verify`. Empty when the build failed |
| `solc_versions` | every solc the settings select, spelled as the conf spells it (`solc8.28`) |
| `sources_patched` | the build rewrote project sources (import fixes) to make the scene compile |
| `log_path` | absolute path of the build's full output |
| `error` | why the build failed, with AutoSetup's stderr. `None` when it succeeded |

`ArtifactStore.write_deliverable(relpath, content)` writes `content` (`str`
or `bytes`) to `<deliverable dir>/<relpath>` and returns the project-relative
path. For the prover backend the deliverable dir is `certora/`. `relpath`
must be relative and must not climb out with `..`, or it raises `ValueError`.
An existing file is replaced, with a warning. Choose a subdirectory of your
own (for example `evaluate/`) so the copies do not replace the project's own
specs and confs, which also live under `certora/`.

A component appears in at most one of `batches` and `extraction_failures`. A
component whose inference returned no properties is in neither. If `batches`
is empty, the driver still calls the mode. The project's specs exist either
way, and the mode decides what an empty property list means.

Run long steps through `ctx.run.runner(TaskInfo(task_id, label, ctx.phase), job)`
so they show in the task UI under the mode's phase, and cache agent work
under `ctx.run.ctx` or a batch's `feat_ctx`.

## `ModeResult`

| Field | Meaning |
|---|---|
| `report` | the `AutoProverReport`. The driver sets its `run_mode` and `active_plugins`, then writes it with `artifact_store.write_report`. |
| `succeeded` | the mode's verdict on the run. `False` makes the run exit non-zero. |
| `failures` | lines for the end-of-run summary. The driver adds a failed project build and the extraction failures ahead of them. |

When `succeeded` is `False`, the console, live and TUI entry points end with
"RUN FAILED: the evaluation produced no usable result." (for another external
mode: "RUN FAILED: the <mode> run reported failure."), not the line a
formalizing run prints.

The HTML report renders a property's `coverage` (`PropertyCoverage`: status
`covered` / `partial` / `uncovered`, the reason, and an "added by evaluator"
tag) in a "Property coverage" table under its group, when any property of the
group carries one. A report without coverage renders as before.

An exception from `run` fails the run. The report is the mode's whole
deliverable, so unlike the report phase of a formalizing run it is not
best-effort.

The mode's spend is charged to the `formalization` budget cap, the phase it
takes the place of.

## Helpers an implementation can import

| Need | Where |
|---|---|
| Rule selection of a conf (`rule` / `exclude_rule`, globs) | `composer.prover.conf`: `InheritRules`, `SelectRules`, `ExcludeRules`, `RuleSelection`, `checked_rules` |
| Running certoraRun, listing a spec's declared rules | `composer.prover.core`: `run_prover`, `declared_rules_list`. Take the options from `ctx.prover_options` rather than building them |
| The overlay AutoProver forces on a conf | `composer.spec.source.prover`: `prover_config_overlay` |
| Per-rule verdicts of a finished run | `composer.spec.source.report_prover`: `fetch_run_verdicts`, `fetch_covering_verdicts` |
| Report assembly | `composer.spec.source.report.collect.collect` (pass `keep_orphans=True` to keep rules no property claims), `composer.spec.source.report.grouping`, `composer.spec.source.report.coverage.validate` |
| Findings for violated rules | `composer.spec.source.report.findings`: `build_findings` |
| Counterexample analyses captured during runs | `composer.spec.source.cex_capture`: `CexAnalysisStore` |
