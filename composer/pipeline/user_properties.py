"""The ``user-properties`` property generator: the properties a person lists in JSON files, in
place of the native system analysis and inference.

A run takes one suite: the file ``AUTOPROVER_USER_PROPERTIES`` names, relative to the project
root, with the files it imports::

    {
      "component": {"name": "Accounting", "description": "Minting and burning shares."},
      "imports": ["common.property-suite.json"],
      "groups": [{"name": "Supply", "description": "The total supply."}],
      "properties": [
        {"id": "total_supply_matches_shares", "description": "totalSupply equals the sum of all shares.",
         "sort": "invariant", "group": "Supply"}
      ]
    }

An import is a path relative to the importing file, and brings in all of that file's properties.
Each property is defined in one file, so suites that share a property import the file that
defines it.

Each file that defines properties names its component, and its properties are that component's.
A file that only imports names none. Groups subgroup the properties of one file. A property's
``id`` is its title. No model is called: the system model is built from the suite, with the main
contract and its components and nothing else, so the components carry no entry points, state or
interactions.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, model_validator

from composer.pipeline.ecosystem import ChainTag
from composer.pipeline.plugin_api import ForEcosystem, PluginScope
from composer.pipeline.property_generators import (
    PropertyGenerationContext, PropertyGeneratorImpl, PropertyGeneratorLoader,
)
from composer.pipeline.property_set import PropertySet, PropertySetComponent
from composer.pipeline.run_mode import RunMode
from composer.spec.system_model import (
    ContractComponent, ContractComponentInstance, SourceApplication, SourceExplicitContract,
    SourceExternalActor,
)
from composer.spec.types import (
    ComponentName, ContractName, PropertyFormulation, PropertyTitle, PropertyType, SolidityIdentifier,
    SourceIdentifier,
)

#: The environment variable that names the file, relative to the project root.
USER_PROPERTIES_ENV = "AUTOPROVER_USER_PROPERTIES"

#: How a properties file's name ends. A tool that reads a repo's other suites finds them by it.
PROPERTY_FILE_SUFFIX = ".property-suite.json"

#: Where a property came from: a property generator (or a list given in its place), added by the
#: author of the suite while verifying, or written by a person.
type PropertySource = Literal["property_generator", "added", "user"]


class UserPropertyComponent(BaseModel):
    """The component of the main contract whose properties a file defines."""
    name: ComponentName = Field(min_length=1)
    description: str = Field(min_length=1)


class UserPropertyGroup(BaseModel):
    """A subgroup of the properties of one file."""
    name: str
    description: str = ""


class UserProperty(BaseModel):
    id: PropertyTitle
    description: str
    sort: PropertyType = Field(default="safety_property")
    #: A subgroup of the file's properties.
    group: str | None = None
    source: PropertySource = "user"


class UserPropertyFile(BaseModel):
    #: Required when the file defines properties. A file that only imports names none.
    component: UserPropertyComponent | None = None
    #: Other properties files whose properties the suite includes, relative to this file.
    imports: list[str] = Field(default_factory=list)
    groups: list[UserPropertyGroup] = Field(default_factory=list)
    properties: list[UserProperty] = Field(default_factory=list)

    @model_validator(mode="after")
    def _well_formed(self) -> "UserPropertyFile":
        if self.properties and self.component is None:
            raise ValueError("a file that defines properties names its component")
        for kind, names in (("property id", [p.id for p in self.properties]),
                            ("group", [g.name for g in self.groups])):
            seen: set[str] = set()
            for name in names:
                if name in seen:
                    raise ValueError(f"{kind} {name!r} appears more than once")
                seen.add(name)
        return self

    @staticmethod
    def load(path: Path) -> "UserPropertyFile":
        try:
            return UserPropertyFile.model_validate_json(path.read_text())
        except ValidationError as e:
            raise ValueError(f"invalid properties file {path}: {e}") from e



@dataclass(frozen=True)
class SuiteComponent:
    """A component of a suite: the file that names it, and the properties that file defines."""
    component: UserPropertyComponent
    file: Path
    properties: list[UserProperty]


@dataclass(frozen=True)
class PropertySuite:
    """A suite: its components, in import order."""
    components: list[SuiteComponent]

    @property
    def properties(self) -> list[UserProperty]:
        """Every property of the suite, component by component."""
        return [p for c in self.components for p in c.properties]

    @staticmethod
    def load(path: Path) -> "PropertySuite":
        """The suite ``path`` names: the components of that file and of every file it imports,
        transitively, in import order.

        A file reached along two import paths counts once. A cycle, an id two files define, and a
        component two files name are errors."""
        components: dict[str, SuiteComponent] = {}
        defined: dict[str, Path] = {}
        done: set[Path] = set()

        def visit(file: Path, stack: tuple[Path, ...]) -> None:
            if file in stack:
                cycle = " -> ".join(str(p) for p in (*stack[stack.index(file):], file))
                raise ValueError(f"properties files import each other in a cycle: {cycle}")
            if file in done:
                return
            done.add(file)
            loaded = UserPropertyFile.load(file)
            for p in loaded.properties:
                if (where := defined.get(p.id)) is not None:
                    raise ValueError(f"property {p.id!r} is defined in {where} and in {file}")
                defined[p.id] = file
            if loaded.component is not None:
                name = loaded.component.name
                if (seen := components.get(name)) is not None:
                    raise ValueError(f"component {name!r} is named in {seen.file} and in {file}")
                components[name] = SuiteComponent(loaded.component, file, list(loaded.properties))
            for name in loaded.imports:
                visit((file.parent / name).resolve(), (*stack, file))

        visit(path.resolve(), ())
        return PropertySuite(list(components.values()))


def user_property_set(
    suite: PropertySuite, *, ecosystem: ChainTag, contract_name: SourceIdentifier,
    contract_path: str, run_mode: RunMode,
) -> PropertySet:
    """The property set of ``suite`` for the main contract ``contract_name`` at
    ``contract_path``: one component of the main contract per component of the suite that has
    properties."""
    parts = [c for c in suite.components if c.properties]
    main: SourceExplicitContract | SourceExternalActor = SourceExplicitContract(
        sort="singleton", name=ContractName(contract_name),
        solidity_identifier=SolidityIdentifier(contract_name), description="", path=contract_path,
        components=[
            ContractComponent(
                name=c.component.name, description=c.component.description,
                external_entry_points=[], state_variables=[], interactions=[], requirements=[],
            )
            for c in parts
        ],
    )
    app = SourceApplication(application_type="", description="", components=[main])
    return PropertySet(
        ecosystem=ecosystem, contract_name=contract_name, run_mode=run_mode,
        system_model=app.model_dump(mode="json"),
        property_components=[
            PropertySetComponent(name=c.component.name, unit_index=i, properties=[
                PropertyFormulation(title=p.id, sort=p.sort, description=p.description)
                for p in c.properties
            ])
            for i, c in enumerate(parts)
        ],
    )


class UserPropertiesGenerator(PropertyGeneratorImpl[ContractComponentInstance]):
    async def generate(self, ctx: PropertyGenerationContext[Any, Any, ContractComponentInstance]) -> PropertySet:
        name = os.environ.get(USER_PROPERTIES_ENV)
        if not name:
            raise ValueError(f"the user-properties generator needs {USER_PROPERTIES_ENV}")
        source = ctx.run.source
        return user_property_set(
            PropertySuite.load(Path(source.project_root) / name),
            ecosystem=ctx.ecosystem.name, contract_name=source.contract_name,
            contract_path=source.relative_path, run_mode=ctx.run.run_mode,
        )


class UserPropertiesLoader(PropertyGeneratorLoader[ContractComponentInstance]):
    @property
    def scope(self) -> PluginScope[ContractComponentInstance]:
        return ForEcosystem(ContractComponentInstance)

    @asynccontextmanager
    async def initialize(self) -> AsyncIterator[PropertyGeneratorImpl[ContractComponentInstance]]:
        yield UserPropertiesGenerator()
