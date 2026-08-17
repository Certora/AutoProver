"""Standalone CLI for the summarization-target detector — `python -m summarization_detector` or the
installed `detect-summaries` command.

The one input is `--url` (a prover-run URL): it fetches the sources + conf, derives the main contract,
generates the AST, and pulls the difficulty report — getting as much signal as available. The lower-level
flags (`--ast`/`--conf`/`--cut`/…) are overrides / an offline path when you already have the artifacts."""
import argparse
import json

from .detect import detect
from .sources import detect_url


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="detect-summaries",
        description="Rank the functions worth summarizing in a scene (and how: over-approx vs symbolic "
                    "model). Give just --url; everything else is derived.")
    p.add_argument("--url", default=None,
                   help="prover-run URL — the only input needed: fetches sources + conf, derives the main "
                        "contract, generates the AST, and pulls the difficulty report.")
    p.add_argument("--cut", default=None,
                   help="override the main/parametric contract (else derived from the conf's `verify`).")
    p.add_argument("--work-dir", default=None,
                   help="where to fetch sources + generate the AST (default: a temp dir). --url mode only.")
    p.add_argument("--external-call-graph", default=None,
                   help="the prover's externalCallGraph.json — enables the reachability-from-CUT gate "
                        "(auto-found in the fetched tree when present).")
    p.add_argument("--solc-dir", default=None,
                   help="directory prepended to PATH so the conf's solcN.NN resolves.")
    p.add_argument("--include-dependencies", action="store_true",
                   help="also report hashing in lib/ dependency code (off by default).")
    p.add_argument("--json", action="store_true",
                   help="emit the report as JSON (for pipeline/tool consumption) instead of text.")
    # offline / override path (when you already have the artifacts instead of a URL):
    p.add_argument("--ast", default=None, help="path to a solc AST dump (.asts.json).")
    p.add_argument("--conf", default=None, help="a .conf to generate the AST from (offline, needs --cut).")
    p.add_argument("--job-url", default=None,
                   help="difficulty-report URL for the offline path (with --ast/--conf).")
    a = p.parse_args(argv)

    if a.url:
        report = detect_url(a.url, work_dir=a.work_dir, solc_dir=a.solc_dir, cut=a.cut,
                            external_call_graph=a.external_call_graph,
                            include_dependencies=a.include_dependencies)
    else:
        if not (a.ast or a.conf):
            p.error("give --url, or the offline path: --ast/--conf (+ --cut).")
        if not a.cut:
            p.error("--cut is required on the offline path (no --url to derive it from).")
        report = detect(a.job_url, ast_path=a.ast, conf=a.conf, cut=a.cut, solc_dir=a.solc_dir,
                        external_call_graph=a.external_call_graph,
                        include_dependencies=a.include_dependencies)
    print(json.dumps(report.to_dict(), indent=2) if a.json else report.format())
    return 0
