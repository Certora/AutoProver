# AutoProve Pipeline Plugins

Plugins let an out-of-tree package participate in the property-inference
phase of an autoprove pipeline run: contribute extra prompt material to the
per-component property-inference agent, post-process the property list it
produces, run their own (cached, task-tracked) agents to do either, and ship
their own Jinja templates. Plugins
are discovered from the installed environment — the host needs no
configuration to pick one up.

## Shipping a plugin

Register a **loader class** under the entry-point group
`certora.autoprove.plugins`:

```toml
[project.entry-points."certora.autoprove.plugins"]
my-plugin = "my_pkg.plugin:MyPluginLoader"
```

The entry-point **name** (`my-plugin`) is the plugin's identity: it appears in
cache keys, task ids, and the run's recorded plugin manifest. It must be
unique across the installed environment — a duplicate name fails the whole
pipeline at startup. Renaming it invalidates cached work (see
[Cache-key effects](#cache-key-effects)).

The loader:

```python
class MyPluginLoader(PipelinePluginLoader):
    @asynccontextmanager
    async def initialize(self) -> AsyncIterator[PipelinePlugin]:
        async with open_my_resources() as res:
            yield MyPlugin(res)
```

Contract:

* The loader class is instantiated with **no arguments** during entry-point
  scanning, before any event-loop work. Construction must be trivial; all
  real setup belongs in `initialize()`.
* `initialize()` returns an async context manager yielding the
  `PipelinePlugin` instance. Its scope **is** the pipeline run: it is entered
  before the pipeline body starts and
  exited when the pipeline returns *or raises*. Any resource the plugin's
  hooks or tools touch (DB pools, subprocesses, caches) must be acquired here
  and remain valid exactly while the scope is open.
* An exception from `initialize()` (either side of the `yield`) fails the run.

The plugin class:

```python
class MyPlugin(PipelinePlugin):
    NAME = "My Plugin"          # display name (task labels); the entry-point
                                # name, not NAME, is the identity
```

## Hooks

### `property_inference_input_hook(comp, run) -> AnyPropertyGenerationInput | None`

Called once per **functional component** of the main contract, per plugin,
*before* that component's property-inference agent runs. Return extra input
for the inference prompt, or `None` to contribute nothing for this component.

Invocation model:

* All plugins × all components run **concurrently**. Hook
  implementations and any state shared on the plugin instance must tolerate
  concurrent calls. Size shared resources accordingly (e.g. a DB pool with
  `max_size=1` serializes every component's probe).
* An exception from any hook **aborts the entire pipeline run**. Treat
  recoverable conditions ("nothing relevant found", "corpus unavailable") as
  `None`, not as raises.
* The host does **not** cache hook results. Long-running work must implement
  its own caching under `run.ctx` (see below) or it re-executes every run.

### `post_process_property_inference(comp, run, props) -> list[PropertyFormulation]`

Called once per **functional component**, per plugin, after that component's
property inference delivers a non-empty property list. Receives the current
list and returns its replacement — filter, rewrite, or extend. The base
implementation returns `props` unchanged; only override it to actually
transform the list.

Invocation model:

* Within a component, plugins run **sequentially, chained, in lexicographic
  entry-point-name order**: each plugin receives the previous plugin's
  output. If ordering relative to another plugin matters, that ordering is
  controlled by the entry-point names. Different components' chains still
  run concurrently with each other.
* Returning an **empty list drops the component** from the rest of the
  pipeline (no CVL generation for it), exactly as if inference had produced
  nothing.
* The returned list is what downstream CVL generation keys its cache on.
  Output must be **deterministic for a given input list** (stable order,
  stable text) — otherwise every run lands in a fresh CVL-generation cache
  entry even when nothing changed.
* The exception and caching rules from the pre hook apply unchanged: a raise
  aborts the run, and the host does not cache the hook's result. Note that
  `run.ctx`'s namespace incorporates a digest of the *incoming* property
  list (see below), so plugin-side caching is input-addressed — a change in
  what inference (or an earlier plugin in the chain) produced automatically
  lands you in a fresh namespace.

## `PluginContext` — what a hook receives

Both hooks receive `run: PluginContext[C]`, where `C` is the hook's marker
type (`PrePropertyInference` / `PostPropertyInference`). It carries four
things:

* **`run.ctx: WorkflowContext[C]`** — a cache/memory context rooted at a
  namespace private to this *(component, plugin)* pair; for the post hook
  the namespace additionally incorporates a digest of the incoming property
  list, so it moves whenever the input changes. Use it for plugin-side
  caching and agent memory.
  * `ctx.get_memory_tool()` — memory tool bound to this namespace, for
    handing to an agent.
  * `ctx.thread_id`, `ctx.recursion_limit` — pass through to
    `run_to_completion` when running your own graph.
  * `PrePropertyInference`/`PostPropertyInferece` is a generic "marker" context type, you should
    implement a `CacheKey[PrePropertyInference, MyModelType]` and navigate
    (via `child_ctx = ctx.child(...)`) to the `MyModelType` context
  * `await child_ctx.cache_get(MyModelType)` / `await child_ctx.cache_put(value)` -
    typed slot at the context's child key (values are pydantic models).
  * It is strongly recommended to use the caching layer. If you use the caching
    layer, it is recommended to use the same context object for the 
    memory tool/thread id as for caching.
    That is, instead of `child_ctx.cache_get(MyModelType); ... ctx.get_memory_tool()`
    do `child_ctx.cache_get(MyModelType); ... child_ctx.get_memory_tool()`

  Cached values persist across runs (same `--cache-ns`). The namespace is
  keyed by the component digest, the plugin's entry-point name, and (post
  hook only) the incoming property list — **not** by your plugin's version
  or logic. If your output format changes in a way that stale cache entries
  would poison, version your own child keys.

* **`run.env: ServiceHost`** — model builders (`env.models.builder_heavy()`,
  `builder_lite()`), analysis tools, sort. The template loader inside
  `env.models` is already the plugin's own (see [Templates](#templates)), so
  `with_sys_prompt_template(...)` / `TypedTemplate` resolve plugin templates.

* **`run.source: SourceCode`** — the target project (root, main contract,
  relative path).

* **`await run.runner(label, job)`** — run `job` (a nullary async callable)
  as a pipeline task. **All substantial work — anything that runs an agent,
  hits an LLM, or takes real wall-clock time — must go through this.** It
  registers the work with the pipeline's task display and concurrency
  semaphore; work done outside it is invisible and unthrottled. The label is
  shown as `(<sub-phase>) Plugin {NAME}: {label}` (sub-phase:
  `Property Pre-Inference` / `Property Post-Process`); task ids are
  namespaced per phase/plugin/component, so labels only need to be
  human-meaningful, not unique.

## Property-generation inputs

The hook's return value is spliced into the inference agent's prompt. Two
shapes, sharing `uid` / `sort` / `when`:

```python
PropertyGenerationInput(uid=..., sort=..., when=..., input=payload)
CacheablePropertyGenerationInput(uid=..., sort=..., when=..., provide=fn)
```

* **`uid`** — stable identifier (reverse-DNS style, e.g.
  `"com.certora.corpus"`). It is the sort key for deterministic placement:
  it must not vary across runs or components.
* **`sort`** —
  * `"generic"`: the payload is **byte-identical for every component** of the
    application (e.g. a threat model). Generic inputs are placed before all
    component-specific material so the prompt prefix is shared across
    components. Do not mark component-dependent content generic; it silently
    destroys that prefix sharing.
  * `"specific"`: component-dependent content.
* **`when`** —
  * `"initial"`: included only in the agent's first round.
  * `"always"`: included in every round's prompt.
* **payload** (`MessagePayloadType`): a string, a content-block dict, or a
  list of either.
* **`CacheablePropertyGenerationInput.provide(is_boundary: bool)`** — called
  at prompt-assembly time. When `is_boundary` is true, this input is the last
  block before a prompt-cache breakpoint and the final content block it
  returns should carry `cache_control` (e.g.
  `document.to_dict(with_cache=True)`). When false, return the same content
  without the marker.

Placement order in the assembled prompt:

```
[system prompt]
generic/always · generic/initial · specific/always · specific/initial
[host initial prompt (component, prior rounds)]
```

Within each group: non-cacheable inputs (sorted by `uid`), then cacheable
inputs (sorted by `uid`). Rounds after the first drop the `initial` groups.

## Cache-key effects

The host folds the **installed plugin manifest** (sorted entry-point names)
into every per-component cache key:

* Installing, removing, or renaming *any* plugin changes the digest and
  invalidates all per-component inference/CVL cache entries — for every
  plugin, not just the changed one.
* The manifest is recorded in the run's `cache_root` tags.
* The per-plugin namespace handed to the hook (`run.ctx`) is keyed by
  *(component digest, plugin name)* and is **not** part of the component key
  — its contents are the plugin's own to version and invalidate.

## Templates

`self.load_jinja_template(template_name, **params)` on the plugin renders
Jinja templates with this resolution:

* **Bare names** (`"my_prompt.j2"`) resolve from the plugin's own template
  directory first: `PackageLoader(<module of the plugin class>)`, i.e. a
  `templates/` directory in the package containing the plugin class. Override
  `plugin_loader()` to supply any other `jinja2.BaseLoader`; return `None`
  to use the host loader unchanged (this is also the silent fallback when
  `PackageLoader` can't locate a template directory).
* **`autoprover/`-prefixed names** (`"autoprover/tools/edit_prompting.j2"`)
  resolve from the host's template set. Host templates are resolved via
  namespacing magic, so you needn't worry about your template names colliding
  with host template names.

The same loader is spliced into `run.env.models`, so agents built from the
plugin's `PluginContext` resolve `with_sys_prompt_template(...)` /
`TypedTemplate` names by the same rules.

## Checklist

1. `pyproject.toml`: entry point in `certora.autoprove.plugins`.
2. Loader: trivial `__init__`, resources in `initialize()`, yield the plugin.
3. Plugin: set `NAME`; implement `property_inference_input_hook` and/or
   `post_process_property_inference`.
4. Cache expensive work under `run.ctx`; run it via `run.runner`.
5. Return `None` for "nothing to contribute"; never raise for recoverable
   conditions.
6. Choose `uid`/`sort`/`when` per the placement rules; use the cacheable
   variant for large payloads that deserve a prompt-cache breakpoint.
7. Templates in `templates/` next to the plugin class; reference host
   templates with the `autoprover/` prefix.
