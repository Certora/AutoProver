"""The summarization-target detector, split across submodules; this package IS `summarization_detector.detect`.

From a prover run it ranks the functions worth summarizing (a `Candidate` list with WHY each is
prover-hostile, WHERE it can be summarized, and — for a curated public-library match — a suggested summary).
The public surface is re-exported here so `summarization_detector.detect` stays a stable import path:

  model      runtime dataclasses + scoring constants/helpers + procId string helpers (leaf)
  ast_scan   signal 2 — the AST hashing/encoding scan + source-location resolution
  catalog    signal 4 — the prover-hostile-op catalog (+ curated overlay) + surviving aggregation
  callgraph  the AST call graph: reachability gate, cone weights, fn-facts, expressibility, boundaries
  fuse       `detect_from` — folds the signals into the ranked candidate list (scoring + caps)
  core       `ensure_ast` + `detect` — AST acquisition and orchestration
"""
from ..difficulty import DifficultyReport, Hotspot, fetch_difficulty   # re-exported for back-compat
from .ast_scan import _function_locations, _project_relative, scan_ast
from .callgraph import (
    FnFacts,
    _by_qual_or_bare,
    _caller_boundaries,
    _descend_to_prims,
    _entrypoint_in_edges,
    _expressible_typename,
    _fn_facts,
    _is_nonlinear_prim,
    _is_reference_typename,
    _shallowest_view_boundary,
    _survives,
    cone_weights,
    reachable_from_main,
)
from .catalog import (
    CuratedEntry,
    HostileCategory,
    HostileMatch,
    _entry_of_rule,
    classify_hostile,
    surviving_hostile,
)
from .core import _touch_missing_imported_specs, detect, ensure_ast
from .fuse import detect_from
from .model import (
    ALREADY_SUMMARISED_MIN_PCT,
    BRANCHING_MIN_PCT,
    Boundary,
    Candidate,
    DetectionReport,
    HashSignal,
    MAX_PER_CATEGORY,
    NONLINEAR_MIN_PCT,
    SURVIVING_SCORE,
    _UNSERIALIZED_FIELDS,
)
