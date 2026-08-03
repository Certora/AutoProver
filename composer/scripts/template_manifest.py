import importlib.util
import pathlib
import sys

from composer.meta.types import TemplateDecl, Manifest
from composer.meta.templates import build_manifest

# --------------------------------------------------------------------------
# STUBS -- fill these in for your repo
# --------------------------------------------------------------------------
PACKAGE = "composer"

_THIS_DIR = pathlib.Path(__file__).parent

def _package_root() -> pathlib.Path:
    """Locate PACKAGE on disk without importing it.
 
    find_spec on a *top-level* name only consults path finders; the module is
    never executed. (Dotted names would import parents -- don't pass those.)
    """
    spec = importlib.util.find_spec(PACKAGE)
    if spec is None or not spec.submodule_search_locations:
        raise SystemExit(f"cannot locate package {PACKAGE!r} on sys.path")
    to_ret = pathlib.Path(next(iter(spec.submodule_search_locations)))
    assert pathlib.Path(__file__).is_relative_to(to_ret)
    return to_ret

def _repo_root() -> pathlib.Path:
    return _package_root().parent

def render_manifest(manifest: dict[str, TemplateDecl]) -> str:
    """Canonical, diff-stable serialization (sorted keys, trailing newline)."""
    return Manifest.dump_json(manifest, indent=2).decode("utf-8")

def _main():
    (_repo_root() / "template_manifest.json").write_text(
        render_manifest(build_manifest(_package_root(), (_THIS_DIR,)))
    )

if __name__ == "__main__":
    sys.exit(_main())
