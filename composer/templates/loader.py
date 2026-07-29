from typing import Any
from jinja2 import Environment, FileSystemLoader
from markdown_it import MarkdownIt
from markupsafe import Markup, escape
import pathlib

script_dir = pathlib.Path(__file__).parent


def _autoescape(template_name: str | None) -> bool:
    # HTML templates (``*.html.j2``) must autoescape interpolated values; prompt templates
    # (plain ``.j2``) stay verbatim — escaping would corrupt their contents.
    return template_name is not None and template_name.endswith(".html.j2")


# Raw HTML in the source text is escaped (html=False), so LLM-authored prose
# rendered through this filter cannot inject markup; the result is Markup so
# autoescaping doesn't re-escape the tags markdown-it produced.
_md = MarkdownIt("commonmark", {"html": False})


def _markdown(text: str) -> Markup:
    return Markup(_md.render(text))


def _diff_line_class(line: str) -> str | None:
    if line.startswith(("+++", "---")):
        return "d-file"
    if line.startswith("@@"):
        return "d-hunk"
    if line.startswith("+"):
        return "d-add"
    if line.startswith("-"):
        return "d-del"
    return None


def _diff_html(diff: str) -> Markup:
    """Unified-diff text as one block-level span per line, classed by line kind
    (add/del/hunk/file header) for styling. Block spans stand in for newlines —
    joining with literal newlines inside a ``<pre>`` would double-space — so
    the lines are joined bare."""
    out = []
    for line in diff.splitlines():
        cls = _diff_line_class(line)
        out.append(f'<span class="d-line{f" {cls}" if cls else ""}">{escape(line)}</span>')
    return Markup("".join(out))


env = Environment(loader=FileSystemLoader(script_dir), autoescape=_autoescape)
env.filters["markdown"] = _markdown
env.filters["diff_html"] = _diff_html

def load_jinja_template(template_name: str, **kwargs: Any) -> str:
    """Load and render a Jinja template from the script directory"""
    template = env.get_template(template_name)
    return template.render(**kwargs)
