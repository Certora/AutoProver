from typing import Literal
from pydantic import BaseModel, TypeAdapter

type TemplateSort = Literal["TypedTemplate", "PartialTemplate"]

class TemplateDecl(BaseModel):
    module: str
    qualname: str
    template_name: str
    ty_sort: TemplateSort

Manifest = TypeAdapter(dict[str, TemplateDecl])
