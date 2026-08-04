import ast
import pathlib

from composer.meta.types import TemplateSort, TemplateDecl

PACKAGE = "composer"
FULL_TYPE_NAME = "TypedTemplate"
PARTIAL_NAME = "PartialTemplate"
TEMPLATE_ARG = 0

TYPE_NAMES = (FULL_TYPE_NAME, PARTIAL_NAME)

def _names_type(node: ast.expr) -> TemplateSort | None:
    """True if `node` textually refers to TYPE_NAME: bare, attribute, or subscripted."""
    if isinstance(node, ast.Name):
        if node.id in TYPE_NAMES:
            return node.id
    if isinstance(node, ast.Attribute):
        if node.attr in TYPE_NAMES:
            return node.attr
    if isinstance(node, ast.Subscript):  # TYPE_NAME[...] generic annotation
        return _names_type(node.value)
    return None

def _extract_template(value: ast.expr | None) -> str | None:
    """Pull the template-name argument (positional or kwarg per TEMPLATE_ARG)."""
    if not isinstance(value, ast.Call):
        return None
    arg = value.args[TEMPLATE_ARG] if len(value.args) > TEMPLATE_ARG else None
    if arg is None:
        return None
    if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
        return arg.value
    raise ValueError(
        f"template argument must be a string literal so the static "
        f"scanner can see it (got {ast.dump(arg)[:60]})"
    )

def _match_statement(stmt: ast.stmt) -> tuple[str, TemplateSort, ast.expr | None] | None:
    """Return (variable_name, value_expr_or_None) if `stmt` declares a TYPE_NAME."""
    if isinstance(stmt, ast.AnnAssign):
        if (ty_name := _names_type(stmt.annotation)) is not None and isinstance(stmt.target, ast.Name):
            return stmt.target.id, ty_name, stmt.value
        return None
    if isinstance(stmt, ast.Assign):
        if (isinstance(stmt.value, ast.Call) and (ty_sort := _names_type(stmt.value.func)) is not None
                and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name)):
            return stmt.targets[0].id, ty_sort, stmt.value
    return None

def iter_declarations(package_dir: pathlib.Path, skips: tuple[pathlib.Path, ...]):
    """Yield (module_name, var_name, template_or_None) for each top-level declaration.
 
    Purely textual/AST -- aliased type names (``from registry import
    TemplateDecl as TD``) or declarations built in loops will NOT be found.
    Keep declaration style boring.
    """
    base = package_dir.parent
    for py in sorted(package_dir.rglob("*.py")):
        if any(py.is_relative_to(skip) for skip in skips):
            continue
        source = py.read_text(encoding="utf-8")
        if FULL_TYPE_NAME not in source and PARTIAL_NAME not in source:  # cheap pre-filter before parsing
            continue
        tree = ast.parse(source, filename=str(py))
        module_name = ".".join(py.relative_to(base).with_suffix("").parts)

        if module_name.endswith(".__init__"):
            module_name = module_name.removesuffix(".__init__")
        for stmt in tree.body:  # top level ONLY -- no ast.walk on purpose
            match = _match_statement(stmt)
            if match is None:
                continue
            var_name, ty_sort, value = match
            try:
                template = _extract_template(value)
            except ValueError as exc:
                raise ValueError(f"{py}:{stmt.lineno}: {exc}") from None
            yield module_name, var_name, template, ty_sort

def build_manifest(root_package: pathlib.Path, skips: tuple[pathlib.Path, ...]) -> dict[str, TemplateDecl]:
    """Scan and return {"module:VAR": {"module", "qualname", "template"?}}."""
    manifest: dict[str, TemplateDecl] = {}
    templates_seen: dict[str, str] = {}
    for module_name, var_name, template, ty_sort in iter_declarations(root_package, skips):
        key = f"{module_name}:{var_name}"
        if key in manifest:
            raise ValueError(f"{key} declared twice at module top level")
        if template is None:
            raise ValueError(f"No template name extracted for {key}")
        if template in templates_seen:
            raise ValueError(
                f"template {template!r} declared by both "
                f"{templates_seen[template]} and {key}"
            )
        templates_seen[template] = key
        manifest[key] = TemplateDecl(
            qualname=var_name, module=module_name, ty_sort=ty_sort, template_name=template
        )
    return manifest
