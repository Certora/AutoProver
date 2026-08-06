"""Fetch a prover job's counterexamples WITHOUT downloading the whole zipOutput.

The merger the refine loop needs = POU's targeted fetch + composer's formatter:
- FETCH via ProverOutputUtility: `get_all_checks` enumerates the violated leaves (each carries its
  `output_files`), and `get_calltrace_for_violation` pulls just that leaf's callTrace JSON over the
  tree-view HTTP endpoint — no tarball, no lossy whole-report parse.
- FORMAT via composer: `calltrace_to_xml(CallTraceModel.model_validate(trace["callTrace"]))` renders the
  `<counterexample>` with CONCRETE argument values and noise nodes stripped (e.g.
  `glue(id='10001', amount='3948', to='0x2715')`, `mReader(id) ↪ '0x109c39...'`).

This is what turns "returns must agree" into an actionable CEX the fill agent can fix. Composer's own
path (`composer/prover/cloud.cloud_results`) downloads + extracts the full output tarball; we avoid that,
keeping smtool's zero-tarball footprint (verify.py already reads results treeView-only). Best-effort: any
failure (missing POU / auth / network / schema drift) returns {}, and the loop still refines on the
assert messages.
"""
from __future__ import annotations

_MAX_CEX_CHARS = 6000   # a call trace is small, but cap so one huge CEX can't blow up the refine prompt


def fetch_cex(job_url: str, rules: set[str] | None = None) -> dict[str, str]:
    """Return {label: counterexample_xml} for the VIOLATED leaves of `job_url` (all, or only those whose
    rule name is in `rules`). `label` is "<rule>: <assert message>". Fetches only the needed callTrace
    files via POU (no zipOutput) and formats via composer. Returns {} on any error."""
    if not job_url:
        return {}
    try:
        from prover_output_utility import ProverOutputAPI
        from composer.prover.results import calltrace_to_xml, CallTraceModel
    except Exception:
        return {}
    out: dict[str, str] = {}
    try:
        api = ProverOutputAPI(use_local=False)
        seen: set[tuple] = set()
        for c in api.get_all_checks(job_url):
            # only violated LEAVES carry a counterexample (output_files); parent nodes have none
            if not (c.is_violated and c.output_files):
                continue
            if rules is not None and c.rule_name not in rules:
                continue
            key = (c.rule_name, c.output_files[0])
            if key in seen:
                continue
            seen.add(key)
            try:
                trace = api.get_calltrace_for_violation(job_url, c).trace_data
                node = trace.get("callTrace") if isinstance(trace, dict) else None
                if node is None:
                    continue
                xml = "<counterexample>" + calltrace_to_xml(CallTraceModel.model_validate(node)) \
                      + "</counterexample>"
            except Exception:
                continue
            label = f"{c.rule_name}: {c.assert_message}" if c.assert_message else c.rule_name
            out[label] = xml if len(xml) <= _MAX_CEX_CHARS else xml[:_MAX_CEX_CHARS] + "…(truncated)"
    except Exception:
        return out
    return out
