from typing import Callable, Sequence, get_args
from functools import cache
from composer.meta.types import Manifest
from composer.meta.templates import build_manifest
import pathlib
import fnmatch
import traceback
import sys


repo_root = pathlib.Path(__file__).parent.parent

@cache
def _get_curr_manifest():
    package_root = repo_root / "composer"
    scripts_dir = package_root / "scripts"
    return build_manifest(package_root, (scripts_dir,))

def test_manifest_up_to_date():

    curr_manifest = repo_root / "template_manifest.json"

    assert curr_manifest.exists() and curr_manifest.is_file()

    assert Manifest.validate_json(curr_manifest.read_text()) == _get_curr_manifest()

PACKAGE = "composer"


def import_submodules(package_name: str, skips: Sequence[str] = ()) -> None:
    """Recursively import every module under a package (decorator/ctor side effects fire)."""
    import importlib
    import pkgutil
 
    package = importlib.import_module(package_name)
    for _finder, name, _ispkg in pkgutil.walk_packages(
        package.__path__, prefix=package.__name__ + "."
    ):
        if any(
            fnmatch.fnmatch(name, s) for s in skips
        ):
            continue
        importlib.import_module(name)


def test_every_runtime_instance_is_statically_discoverable(monkeypatch):
    # Purge cached package modules so imports actually re-execute under the
    # patched __new__ (other tests may have imported them already)...
    saved = {
        name: sys.modules.pop(name)
        for name in list(sys.modules)
        if name == PACKAGE or name.startswith(PACKAGE + ".")
    }
    try:
        from composer.spec.gen_types import TypedTemplate, PartialTemplate

        """Monkeypatch TemplateDecl.__new__ to record every instance, import the
        ENTIRE package tree, then assert each recorded instance is reachable via
        some (module, qualname) the static scan found.
    
        Catches everything the AST scanner is blind to: aliased type names,
        declarations built in loops/functions that run at import, multi-target
        assigns, etc. Costs a full package import -- that's the point.
        """
        recorded: list[tuple[object, str]] = []

        def mk_recorder[T](t: type[T], new: Callable[[type[T]], T]) -> Callable[..., T]:
            assert new is object.__new__
            def recording_new(cls: type[T], *args, **kwargs):
                inst = new(cls)
                frame = traceback.extract_stack(limit=2)[0]   # direct caller of __new__
                recorded.append((inst, f"{frame.filename}:{frame.lineno}"))
                return inst
            return recording_new

        tl = (TypedTemplate, PartialTemplate)
        for to_instr in tl:
            recorder = mk_recorder(to_instr, to_instr.__new__)
            monkeypatch.setattr(to_instr, "__new__", recorder)

        curr_manifest = _get_curr_manifest()
        import_submodules(
            PACKAGE,
            (
                "composer.scripts.*",
                "*.certoraRunWrapper",
                "*.certoraTypeCheck"

            )
        )
 
        statically_visible_ids = set()
        for entry in curr_manifest.values():
            obj = getattr(sys.modules[entry.module], entry.qualname)
            statically_visible_ids.add(id(obj))
 
        ghosts = [where for inst, where in recorded if id(inst) not in statically_visible_ids]
        assert recorded, (
            f"no template instances constructed while importing "
            f"{PACKAGE!r} -- wrong type, wrong package, or nothing registered?"
        )
        assert not ghosts, (
            f"{len(ghosts)} {TypedTemplate.__name__}/{PartialTemplate.__name__} instance(s) constructed at import "
            f"time but invisible to the static scan (aliased type name? built in a "
            f"loop? not bound to a top-level name?):\n  " + "\n  ".join(ghosts)
        )
    finally:
        # ...and restore the pristine module cache so this test's re-imports
        # (which ran under a patched __new__) don't leak into other tests.
        for name in list(sys.modules):
            if name == PACKAGE or name.startswith(PACKAGE + "."):
                del sys.modules[name]
        sys.modules.update(saved)

def test_template_params_recoverable():
    import importlib
    from composer.spec.gen_types import TypedTemplate, PartialTemplate
    for (_,v) in _get_curr_manifest().items():
        importlib.import_module(v.module)
        assert v.qualname in sys.modules[v.module].__dict__
        sym = sys.modules[v.module].__dict__[v.qualname]

        assert hasattr(sym, "__orig_class__")

        orig = sym.__orig_class__

        match v.ty_sort:
            case "PartialTemplate":
                assert type(sym) is PartialTemplate
                assert len(get_args(orig)) == 2
            case "TypedTemplate":
                assert type(sym) is TypedTemplate
                assert len(get_args(orig)) == 1
