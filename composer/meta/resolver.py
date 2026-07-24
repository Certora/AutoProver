from typing import Iterable, get_args
import sys
import importlib
from .types import TemplateDecl

def resolve_params(s: TemplateDecl) -> Iterable[type]:
    importlib.import_module(s.module)
    t = sys.modules[s.module].__dict__[s.qualname]

    res = getattr(t, "__orig_class__")
    for i in get_args(res):
        assert isinstance(i, type), f"got {i} {type(i)}"
        yield i
