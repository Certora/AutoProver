# AutoProve Property Generators

A property generator replaces the pipeline's own system analysis and per-component
property inference. It lives in another package. A run uses the native analysis and
inference unless it names a generator:

```
console-autoprove … --property-generator <name>
AUTOPROVER_PROPERTY_GENERATOR=<name> console-autoprove …    # the flag wins
```

`native` (or no name) selects the built-in analysis and inference. A name that is not
installed fails the run before any service starts. A generator cannot be combined with
`--predefined-properties` or `--interactive`.

## What the driver does with a generator

| Step in `run_pipeline_inner` | native | generator |
|---|---|---|
| system analysis | `run_component_analysis` | `generate()`, which returns a `PropertySet` |
| `prepare_system` | on the analyzed model | on the set's `system_model` |
| property extraction | `_extract_all` (inference + plugin hooks) | the set's components, matched to the units, then the post-inference plugin hooks |
| ranking (prioritized), formalization, external-mode hand-off, report | yes | yes, unchanged |

The generator runs where system analysis runs, alongside the backend preflight, under the
`property_extraction` budget cap. The driver records the resulting batches in
`property_set.json`, exactly as it records the native extraction's.

Only the post-inference plugin hook (`post_process_property_inference`) runs on generated
properties. The pre-inference hook adds material to the native inference prompt, which a
generator does not use.

The generator's name suffixes the `final_props` record of each component and is recorded in
the run's cache tags (`AutoProveCacheTags.property_generator`). The formalization cache keys on
the properties themselves, so generated and native properties never share a formalization.

## Shipping a generator

Register a loader class under the entry-point group
`certora.autoprove.property_generators`. The entry-point name is the generator's name:

```toml
[project.entry-points."certora.autoprove.property_generators"]
my-generator = "my_pkg.generator:MyGeneratorLoader"
```

```python
class MyGeneratorLoader(PropertyGeneratorLoader[ContractComponentInstance]):
    @property
    def scope(self) -> PluginScope[ContractComponentInstance]:
        return ForEcosystem(ContractComponentInstance)

    @asynccontextmanager
    async def initialize(self) -> AsyncIterator[PropertyGeneratorImpl[ContractComponentInstance]]:
        yield MyGenerator()


class MyGenerator(PropertyGeneratorImpl[ContractComponentInstance]):
    async def generate(self, ctx: PropertyGenerationContext[Any, Any, ContractComponentInstance]) -> PropertySet:
        ...
```

The loader is constructed with no arguments, and only for a run that names it.
`initialize()` is entered before the pipeline body starts and exited when the run returns or
raises, as for a plugin or a run mode.

## `PropertyGenerationContext`

| Field | Meaning |
|---|---|
| `run` | The `PipelineRun`: the run's models (`env`), cache root (`ctx`), task runner, source (`source.content` is the design document) and `run_mode`. |
| `ecosystem` | The run's `Ecosystem`. The set's `ecosystem` must be its `name`, and its `system_model` must validate as its `system_model` type. |
| `threat_model`, `extra_context` | The run's guidance documents. |
| `existing_specs` | The project carries specifications written by its authors (an evaluate run). The generated properties must be the full set the contract should satisfy, not only the ones those specifications already encode. |
| `artifact_store` | Write anything delivered beyond the set (the generator's own reports) through `write_deliverable`. |
| `phase` | The backend's phase for the generator's tasks. |

## The returned `PropertySet`

* `ecosystem` and `contract_name` must be the run's, or the run fails.
* `system_model` is the analyzed model, dumped with `model_dump(mode="json")`. The driver
  rebuilds the main contract's units from it with `ecosystem.units`.
* Each `property_components` entry names one of those units by `display_name` and
  `unit_index`. An entry that matches no unit fails the run; a unit with no entry gets no
  properties. A generator that works on other units (inherited contracts, cross-contract
  interactions) maps each property onto a unit of the main contract.
* `run_mode` is not read; the driver records the set under the run's own mode.

The driver does not cache the generator's result. A generator caches its own work under
`ctx.run.ctx`, or re-runs on every run. Exceptions from `generate()` fail the run.

## The `property_generation` run mode

`--run-mode property_generation` stops once `property_set.json` is written, as
`--stop-after-properties` does. It creates no harness and keeps the main contract the user
named, even a library, like an external mode. It works with the native extraction and with
any generator.
