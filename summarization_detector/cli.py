"""Standalone CLI for the summarization-target detector — `python -m summarization_detector` or the
installed `detect-summaries` command. Reuses the same `detect()` orchestration the autosetup integration
calls, so a manual run and the in-pipeline run produce the same report."""
import argparse

from .detect import detect


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="detect-summaries",
        description="Rank the functions worth summarizing in a scene (and how: over-approx vs symbolic "
                    "model), from a prover run + the solc AST.")
    p.add_argument("--cut", required=True, help="the verified / main (parametric) contract name")
    p.add_argument("--job-url", default=None,
                   help="prover run URL — supplies the difficulty report (nonlinear + external signals). "
                        "Omit to run the AST-only hashing signal.")
    p.add_argument("--ast", default=None,
                   help="path to a solc AST dump (.asts.json). If omitted, --conf is compiled to produce one.")
    p.add_argument("--conf", default=None,
                   help="certoraRun .conf to generate the AST from (standalone mode: runs "
                        "`certoraRun --compilation_steps_only --dump_asts`).")
    p.add_argument("--external-call-graph", default=None,
                   help="the prover's externalCallGraph.json — enables the reachability-from-CUT gate on "
                        "the hashing signal.")
    p.add_argument("--solc-dir", default=None,
                   help="directory prepended to PATH so the conf's solcN.NN resolves (standalone mode).")
    p.add_argument("--include-dependencies", action="store_true",
                   help="also report hashing in lib/ dependency code (off by default).")
    a = p.parse_args(argv)

    report = detect(
        a.job_url,
        ast_path=a.ast,
        conf=a.conf,
        cut=a.cut,
        solc_dir=a.solc_dir,
        include_dependencies=a.include_dependencies,
        external_call_graph=a.external_call_graph,
    )
    print(report.format())
    return 0
