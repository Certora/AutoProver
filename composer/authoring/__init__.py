"""The authoring workflow shared by every formalization backend.

An authoring session is one stateful agent turn-loop that produces a *spec* — a CVL file, a
foundry test file, a Rust harness — under the same protocol whichever backend it is for:

* a single ``curr_spec`` buffer the agent writes and edits (:mod:`composer.authoring.buffer`),
* declared skips for properties it will not formalize (:mod:`composer.authoring.state`),
* gate tools that *stamp* a digest of the current buffer into ``validations`` when they pass,
* a feedback judge the agent invokes and can file rebuttals against
  (:mod:`composer.authoring.judge`),
* and a publish gate that refuses to finalize until every required stamp matches the buffer
  **as it now stands** — so an edit after a green checker run invalidates that run.

What a backend supplies is the part that genuinely differs: what makes a spec valid at put time,
which tools gate it, and what ground truth the property→check mapping is checked against. Those are
parameters here, not subclasses; the per-backend assembly (which tools, which prompts, which cache)
stays in the backend's own entry point.

Nothing in this package knows what a CVL rule, a foundry test or a fuzz harness function *is* —
only that each is a *check* carrying a property.
"""
