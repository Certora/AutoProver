"""Re-vendor the canonical Solana inlining and summaries files from the spec template.

These are **product data, not corpus data** — the difference matters because the CVLR knowledge
manifests all live in a separate repo now. A RAG manifest is content an agent retrieves; these are
prover tuning files the scaffold writes into a target project, so they have to ship in the wheel
and be readable off disk with no database and no network. They are vendored for the same reason
``certora_autosetup`` vendors its ``.spec`` files.

The upstream refresh mechanism is ``envs/justfile``'s ``curl`` from the template repo's ``main``,
which gives the newest file and no way to say which one it got. This clones instead, so the
vendored copy carries a revision — a tuning file that cannot say which upstream commit it came
from is one nobody can decide is stale.

``$CVLR_TEMPLATE_REPO`` (default ``~/src/solana-spec-template``) short-circuits the clone when a
checkout is already there, which is also how to vendor from an unmerged branch.

Run it as::

    uv run --no-sync python -m composer.scripts.refresh_cvlr_envs

and commit the result. Nothing in the product calls this: the scaffold reads what is committed.
"""

import argparse
import logging
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from composer.spec.cvlr.scaffold import CANONICAL_ENVS, ENV_DIR, PROVENANCE_FILE, TEMPLATE_REPO

logging.basicConfig(level=logging.INFO, format="%(message)s")
_log = logging.getLogger(__name__)

#: Where the template repo keeps them.
UPSTREAM_DIR = Path("envs")

CHECKOUT_ENV = "CVLR_TEMPLATE_REPO"
DEFAULT_CHECKOUT = Path("~/src/solana-spec-template")


def _revision(root: Path) -> str:
    def git(*args: str) -> str:
        return subprocess.run(
            ["git", "-C", str(root), *args], check=True, capture_output=True, text=True
        ).stdout.strip()

    return f"{git('rev-parse', 'HEAD')} ({git('log', '-1', '--format=%cI')})"


def _clone(dest: Path) -> Path:
    _log.info("cloning %s ...", TEMPLATE_REPO)
    subprocess.run(
        ["git", "clone", "--depth", "1", TEMPLATE_REPO, str(dest)],
        check=True, stdout=subprocess.DEVNULL,
    )
    return dest


def _source(checkout: Path | None) -> tuple[Path, str, bool]:
    """The tree to vendor from, its revision, and whether it was cloned fresh.

    A local checkout is reported as *possibly behind* rather than trusted silently: it is the
    convenient path and therefore the one that quietly vendors last month's tuning."""
    if checkout is not None and (checkout / UPSTREAM_DIR).is_dir():
        return checkout, _revision(checkout), False
    if checkout is not None:
        _log.warning("%s has no %s/ — cloning instead", checkout, UPSTREAM_DIR)
    tmp = Path(tempfile.mkdtemp(prefix="cvlr-template-"))
    root = _clone(tmp / "template")
    return root, _revision(root), True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--checkout", type=Path,
        default=Path(os.environ.get(CHECKOUT_ENV, DEFAULT_CHECKOUT)).expanduser(),
        help="Vendor from this checkout instead of cloning (default: %(default)s).",
    )
    parser.add_argument("--clone", action="store_true", help="Always clone, ignoring --checkout.")
    args = parser.parse_args()

    root, revision, cloned = _source(None if args.clone else args.checkout)
    try:
        missing = [n for n in CANONICAL_ENVS if not (root / UPSTREAM_DIR / n).is_file()]
        if missing:
            raise SystemExit(f"{root / UPSTREAM_DIR} is missing {', '.join(missing)}")
        ENV_DIR.mkdir(parents=True, exist_ok=True)
        for name in CANONICAL_ENVS:
            shutil.copy(root / UPSTREAM_DIR / name, ENV_DIR / name)
            _log.info("vendored %s", name)
        (ENV_DIR / PROVENANCE_FILE).write_text(f"{TEMPLATE_REPO} {revision}\n")
    finally:
        if cloned:
            shutil.rmtree(root.parent, ignore_errors=True)

    if not cloned:
        _log.warning(
            "vendored from a local checkout — run `git -C %s pull` first, or pass --clone, if you "
            "want the current upstream", root,
        )
    _log.info("%s: %s", PROVENANCE_FILE, revision)


if __name__ == "__main__":
    main()
