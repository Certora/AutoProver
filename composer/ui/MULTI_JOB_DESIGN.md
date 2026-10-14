# MultiJobApp Design

## Problem

Multi-agent pipelines run N concurrent LLM agents, each needing its
own event stream, tool display, HITL channel, and detail panel.  Every
multi-agent TUI is built on it: auto-prove (`AutoProveApp`), Foundry
(`FoundryApp`), NatSpec (`NatspecPipelineApp`), and the Rust
application host (`GenericRustApp`).
`MultiJobApp` factors out the generic parts so that subclasses only
provide phase definitions, tool configs, HITL schemas, and
completion handling.

## Type Parameters

`MultiJobApp[P, T]` is parameterized by:

- **`P`** — the phase type (typically an enum or literal union).
  Determines how tasks are grouped in the summary view.
- **`T`** — the task handler type (a `MultiJobTaskHandler` subclass).
  Determines per-task rendering and HITL behavior.

## Key Abstractions

### TaskInfo

A frozen descriptor: `(task_id, label, phase)`. Created by the
pipeline orchestrator for each unit of work. The phase determines
which summary section the task appears in and which tool display
config it gets.

### TaskHandle

The bundle returned by the handler factory to the pipeline. Contains
an `IOHandler`, an `EventHandler`, and lifecycle callbacks
(`on_start`, `on_done`, `on_error`). The pipeline creates these via
the factory; `run_task` manages the callbacks.

### TaskHost

A narrow protocol that handlers use to call back into the app.
Handlers hold a `TaskHost`, never a concrete app reference.
Dependency flows one direction: handler -> host.

| Method | Purpose |
|--------|---------|
| `on_task_status_change` | Handler reports status transitions; app updates summary row |
| `update_tokens` | Handler forwards AI messages; app aggregates token counts |
| `make_content_link` | Handler requests a clickable snapshot link; app manages storage and navigation |
| `hitl_input` | Handler mounts an Input widget; app routes submitted text to an asyncio.Queue |

### MultiJobTaskHandler

The per-task `IOHandler`. One per pipeline task. Parameterized by
`H`, the HITL schema type. Responsibilities:

- Renders AI/Human/Tool/System messages into its panel via
  `MessageRenderer`
- Creates collapsible nested containers for inner graph executions
- Manages HITL: mounts prompt + input, suspends via `hitl_input`,
  resumes on submit
- Reports status via `TaskHost`

Two subclass hooks:

- `format_hitl_prompt(ty: H)` — render the HITL payload into display
  text. Each domain defines its own schema.
- `on_node_state(path, node_name, values)` — process non-message
  graph state (e.g. NatSpec detects `curr_spec` updates and renders
  content links).

### HandlerFactory

The contract between the pipeline orchestrator and the app:

```python
class HandlerFactory[P: HasName, H](Protocol):
    def __call__(self, /, info: TaskInfo[P]) -> Awaitable[TaskHandle[H]]: ...
```

`MultiJobApp.make_handler` is the concrete implementation. It
creates the summary row, allocates the detail panel, calls the
subclass factory methods, and wires up lifecycle callbacks.

### run_task

Free function that consumes a `HandlerFactory`:

1. Calls the factory to get a `TaskHandle`
2. Optionally waits on a semaphore (concurrency limiting)
3. Enters `with_handler(handle.handler, handle.event_handler)`
4. Runs the task function
5. Calls `on_done` or `on_error`

## Layout

Three views in a `ContentSwitcher`:

1. **Summary** — collapsible sections grouped by phase, each
   containing status rows. Sections with active tasks sort to top;
   completed sections auto-collapse.
2. **Task detail** — per-task `VerticalScroll` panel populated by
   the handler. Click a summary row to drill in; ESC to go back.
3. **Content pane** — full-screen snapshot view (spec files, stubs).
   Reached via content links; ESC returns to previous view.

## EventHandler Patterns

The `EventHandler` is created separately from the `IOHandler`
because structural events (messages, start/end) and domain events
(prover output, spec updates) have different consumers. Two patterns
are in use:

- **Self-handling**: the task handler doubles as the event handler.
  It derives `NullEventHandler`, overrides `handle_event`, and
  `create_event_handler` returns the handler itself. This gives custom
  events direct access to the handler's panel (e.g. streaming prover
  output). Every app above does this.
- **Null** (default): `NullEventHandler` ignores all events. Used
  when a pipeline phase has no custom events. This is the default
  returned by `create_event_handler`.

## How Subclasses Specialize

A subclass provides:

1. Phase type, labels, and section order
2. `create_task_handler(panel, info)` — picks tool config from
   `info.phase`, constructs the handler
3. `create_event_handler(handler, info)` — constructs or returns
   the event handler
4. Completion behavior (called by the pipeline, not by the app)

### NatSpec (NatspecPipelineApp)

- `NatspecTaskHandler` detects `curr_spec` in state, renders content
  links. Formats HITL from `HumanQuestionSchema`.
  Its `handle_event` renders master spec and stub update events as
  clickable content links.
- On completion: writes every contract's interface, stub and specs
  under `natspec_output/` and lists them in the summary view.

## Event Flow

```
Pipeline task function
  -> run_to_completion(graph, ...)
    -> composer.io.context.run_graph(...)
      -> graphcore graph_runner.run_graph(...)
        -> events pushed to EventQueue
          -> _queue_drainer dispatches:
            - Start/End/StateUpdate/Checkpoint -> IOHandler (the task handler)
            - CustomUpdate -> EventHandler
```

Nesting is automatic: when `run_graph` is called while another is
active (e.g. a feedback sub-agent inside a CVL generation agent),
events are wrapped in `Nested(event, parent_id=outer_tid)`. The
drainer peels these layers to reconstruct the path, which the
handler uses to route widgets into the correct nested container.
