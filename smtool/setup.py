"""Setup consumption — the minimal extraction from a setup `.conf`.

Our conformance spec `import`s the setup spec (inheriting its `using`, `methods{}`, imports, links)
and drops only the sanity rule via the conf's `rule` filter. And CUT calls are UNQUALIFIED — an
unqualified CVL call resolves to `currentContract`, which IS the CUT (the `verify` target). So we
need NO alias and NO spec parsing here; just three things from the conf:
  - cut               : CUT contract name        (conf.verify, before the ':')
  - setup_spec_import : the setup spec to import   (conf.verify, after the ':'; imported by basename)
  - conf              : the raw setup conf dict    (files/solc/... copied into the conformance conf)
"""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass
class SetupInfo:
    conf_path: Path
    sources_root: Path
    conf: dict            # raw setup .conf (files / solc / packages / … to copy)
    cut: str              # CUT contract name (== currentContract in the conformance spec)
    setup_spec: Path      # the setup spec (conf.verify target) — the spec we import
    setup_spec_import: str  # what the conformance spec writes in `import "..."` (co-located, basename)


def consume_setup(conf_path: str | Path, sources_root: str | Path | None = None) -> SetupInfo:
    conf_path = Path(conf_path).resolve()
    conf = json.loads(conf_path.read_text())
    # conf.verify is "CUT:relpath/to/spec", relative to the sources root (where certoraRun runs).
    # Default heuristic: conf sits at <root>/certora/conf/x.conf, so root = conf_path.parents[2].
    root = Path(sources_root).resolve() if sources_root else conf_path.parents[2]
    cut, spec_rel = conf["verify"].split(":", 1)
    setup_spec = (root / spec_rel).resolve()
    return SetupInfo(
        conf_path=conf_path, sources_root=root, conf=conf, cut=cut,
        setup_spec=setup_spec, setup_spec_import=setup_spec.name,
    )
