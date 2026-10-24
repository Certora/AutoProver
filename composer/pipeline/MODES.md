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
| `prepare_system` | yes | yes |
| `prepare_formalization` (AutoSetup, summaries) | yes | **no** |
| `_extract_all` (property inference + plugins) | yes | yes |
| ranking, formalization, `build_report` | yes | **no** |
| `RunModeImpl.run`, then `write_report` | no | **yes** |

### Project build

For the prover backend, the driver does **not** build the project in an
external mode. Only AutoSetup installs dependencies, picks the solc versions
and writes the compilation config. AutoSetup runs inside
`prepare_formalization`, and an external mode skips that step. The backend's
`preflight` does none of this. So the mode gets the project as checked out:
a conf whose imports need installed dependencies, or a solc version that is
not on the machine, does not compile.

The build part of AutoSetup is dependency setup, solc selection and the
compilation config. To close this gap, move that part out of
`prepare_formalization`. Put it in `ProverBackend.preflight`, or in a separate
step that external modes also run. AutoSetup runs as one subprocess today, so this needs a build-only entry
point in `certora_autosetup`. Until that exists, a mode implementation must
prepare the build itself or report the confs that did not compile.

The extraction cache keys do not include the run mode. An external-mode run
at a commit that a `comprehensive` run already saw replays the inference at no
cost. The reverse is also true.

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
* A mode is not a plugin. It is not in the plugin manifest and does not enter
  any cache key.

## `ModeContext`

What `run` receives (`composer.pipeline.modes.ModeContext`):

| Field | What it holds |
|---|---|
| `run` | the `PipelineRun`: `env` (services, models), `ctx` (cache root), `runner` / `cpu_runner` (task runners), `source`, `run_mode` |
| `main` | the located main of the analyzed program (`prepared.main`) |
| `batches` | per component: `feat` (the unit), `props` (post-plugin properties), `feat_ctx` (the component's cache namespace) |
| `extraction_failures` | components whose inference raised: `unit`, `error` |
| `threat_model`, `extra_context` | the run's guidance documents |
| `artifact_store` | the backend's artifact store |
| `active_plugins` | the plugin manifest extraction ran with |

A component appears in at most one of `batches` and `extraction_failures`. A
component whose inference returned no properties is in neither. If `batches`
is empty, the driver still calls the mode. The project's specs exist either
way, and the mode decides what an empty property list means.

Run long steps through `ctx.run.runner(TaskInfo(...), job)` so they show in
the task UI, and cache agent work under `ctx.run.ctx` or a batch's
`feat_ctx`.

## `ModeResult`

| Field | Meaning |
|---|---|
| `report` | the `AutoProverReport`. The driver sets its `run_mode` and `active_plugins`, then writes it with `artifact_store.write_report`. |
| `succeeded` | the mode's verdict on the run. `False` makes the run exit non-zero. |
| `failures` | lines for the end-of-run summary. Extraction failures are added by the driver. |

An exception from `run` fails the run. The report is the mode's whole
deliverable, so unlike the report phase of a formalizing run it is not
best-effort.

The mode's spend is charged to the `formalization` budget cap, the phase it
takes the place of.

## Helpers an implementation can import

| Need | Where |
|---|---|
| Rule selection of a conf (`rule` / `exclude_rule`, globs) | `composer.prover.conf`: `InheritRules`, `SelectRules`, `ExcludeRules`, `RuleSelection`, `checked_rules` |
| Running certoraRun, listing a spec's declared rules | `composer.prover.core`: `run_prover`, `declared_rules_list`, `make_prover_options` |
| The overlay AutoProver forces on a conf | `composer.spec.source.prover`: `prover_config_overlay` |
| Per-rule verdicts of a finished run | `composer.spec.source.report_prover`: `fetch_run_verdicts`, `fetch_covering_verdicts` |
| Report assembly | `composer.spec.source.report.collect.collect` (pass `keep_orphans=True` to keep rules no property claims), `composer.spec.source.report.grouping`, `composer.spec.source.report.coverage.validate` |
| Findings for violated rules | `composer.spec.source.report.findings`: `build_findings` |
| Counterexample analyses captured during runs | `composer.spec.source.cex_capture`: `CexAnalysisStore` |
