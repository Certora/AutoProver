# Solana AutoProver alpha: operator runbook

For the Certora engineer who runs the alpha for the customer. Deployment is in AIAutoProver's
`docs/AUTOPROVER_SOLANA_DEPLOY_DEV.md`; what the customer sends and receives is in
[solana-alpha-customer-guide.md](./solana-alpha-customer-guide.md).

## Before every image change

The cloud image is AIAutoProver `feature/solana`, pinned to an AutoProver `feature/solana` commit.
Before moving that pin:

1. Rebuild AutoProver's images and run `scripts/verify-solana-image.sh`. It checks the toolchain,
   the entrypoint guards and one Prover job, with no LLM, in about two minutes.
2. Pin the commit in AIAutoProver's `pyproject.toml` (full SHA: the `run-confined` stage reads it),
   `uv lock`, and push. CI pushes `autoprover-solana:latest-solana`.

## Starting a run

Zip the project as the customer sent it, from the workspace root, without `target/`:

```bash
git -C <checkout> archive --format=zip -o /tmp/project.zip <commit>
certora-cloud aiss workflow start-prebuild --kind AUTO_PROVER_SOLANA \
    --repo <org>/<repo> --git-commit <commit> \
    --contract-name <program> --contract-path programs/<pkg>/src/lib.rs \
    --token-budget <USD> /tmp/project.zip
```

Name a token budget. Without one the run gets the environment default ($150). For scale: a
four-component vault cost $42–65, and a nine-component stake program used a $600 budget in full
over 21 hours. The run proposes components itself, so ask how large the program is before choosing.

The customer starts runs the same way once their user id is in the environment's
`aiss_auto_prover_solana_allowed_user_ids` (cloud-infrastructure parameters). Until then the kind
answers 403 to them.

## Watching it

- `certora-cloud aiss workflow logs <workflow-id>`: the executor's log
  (`/aws/batch/security-suite/autoprover-solana`). The CVLR run logs each component's progress and
  every Prover job link.
- A Prover job that FAILED now reaches the author with its alert text. HALTED jobs (the 25-minute
  global timeout) are expected now and then; the author isolates the slow rule.
- For a run that loops or stalls, read its messages with the `inspect-run` skill against the AISS
  checkpoint database (`certora-cloud db shell` for the tunnel).

## Stopping it

`certora-cloud aiss workflow cancel <workflow-id>`. Batch stops the container with SIGTERM and
kills it 30 seconds later. The Solana executor turns the SIGTERM into an interrupt, so the run
unwinds and uploads `certora/`, including the harness patch, if it can do that within those 30
seconds. A run Batch kills at the 24-hour attempt limit uploads nothing.

## Reading the results

`certora-cloud aiss workflow download-all <workflow-id>`. Before sending results to the customer:

- Apply `cvlr/harness.patch` to a clean checkout at the commit you ran, and build with
  `cargo certora-sbf --features "certora unit_<module>"` for one component.
- For a component with program edits, apply `cvlr/components/<module>.patch` instead, on its own.
- Read every `source_edits` entry's argument. The report presents the edits as the author's claims.
- `report.json`'s `curtailed_components` lists anything the budget cut short; their verdicts are
  not claims.

The workflow's `/report` endpoint is empty for this kind: it is built from findings the executor
posts, and the Solana executor posts none. Use `report.json`.
