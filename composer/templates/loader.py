from typing import Any, TypedDict, NotRequired

from jinja2 import Environment, FileSystemLoader, StrictUndefined, Undefined
from jinja2.loaders import BaseLoader
import pathlib
import os

from graphcore.graph import TemplateLoader

script_dir = pathlib.Path(__file__).parent

base_loader = FileSystemLoader(script_dir)


class _UndefinedParams(TypedDict):
    undefined: NotRequired[type[Undefined]]

_test_mode_undefined : _UndefinedParams = { "undefined": StrictUndefined } if os.environ.get("COMPOSER_STRICT_TEMPLATES") is not None else {}

def _autoescape(template_name: str | None) -> bool:
    # HTML templates (``*.html.j2``) must autoescape interpolated values; prompt templates
    # (plain ``.j2``) stay verbatim — escaping would corrupt their contents.
    return template_name is not None and template_name.endswith(".html.j2")


def make_loader(jinja_loader: BaseLoader) -> TemplateLoader:
    my_env = Environment(loader=jinja_loader, autoescape=_autoescape)
    def load(template_name: str, **kwargs: Any) -> str:
        template = my_env.get_template(template_name)
        return template.render(**kwargs)
    return load

env = Environment(loader=FileSystemLoader(script_dir), autoescape=_autoescape, **_test_mode_undefined)

def load_jinja_template(template_name: str, **kwargs: Any) -> str:
    """Load and render a Jinja template from the script directory"""
    template = env.get_template(template_name)
    return template.render(**kwargs)
