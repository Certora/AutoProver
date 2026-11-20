"""The ``user-properties`` property generator: the properties a person lists in JSON files, in
place of the native system analysis and inference.

A run takes one suite: the file ``AUTOPROVER_USER_PROPERTIES`` names, relative to the project
root, with the files it imports::

    {
      "imports": ["common.property-suite.json"],
      "groups": [{"name": "Accounting", "description": "Minting and burning shares."}],
      "properties": [
        {"id": "total_supply_matches_shares", "description": "totalSupply equals the sum of all shares.",
         "sort": "invariant", "group": "Accounting"}
      ]
    }

An import is a path relative to the importing file, and brings in all of that file's properties
and groups. Each property is defined in one file, so suites that share a property import the file
that defines it.

Each group becomes one component of the main contract, with the group's description; a property
without a group goes to the component named ``DEFAULT_GROUP``. A property's ``id`` is its title.
No model is called: the system model is built from the suite, with the main contract and its
components and nothing else, so the components carry no entry points, state or interactions.
"""

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

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

#: The component of a property that names no group.
DEFAULT_GROUP = ComponentName("Properties")

#: How a properties file's name ends. A tool that reads a repo's other suites finds them by it.
PROPERTY_FILE_SUFFIX = ".property-suite.json"


class UserPropertyGroup(BaseModel):
    name: ComponentName
    description: str = ""


class UserProperty(BaseModel):
    id: PropertyTitle
    description: str
    sort: PropertyType = Field(default="safety_property")
    group: ComponentName | None = None


class UserPropertyFile(BaseModel):
    #: Other properties files whose properties the suite includes, relative to this file.
    imports: list[str] = Field(default_factory=list)
    groups: list[UserPropertyGroup] = Field(default_factory=list)
    properties: list[UserProperty] = Field(default_factory=list)

    @model_validator(mode="after")
    def _ids_and_groups_are_unique(self) -> "UserPropertyFile":
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

    @staticmethod
    def load_suite(path: Path) -> "UserPropertyFile":
        """The suite ``path`` names: its own properties and groups, then those of every file it
        imports, transitively, in import order. The result imports nothing.

        A file reached along two import paths counts once. A cycle, an id two files define, and a
        group two files describe differently are errors."""
        properties: dict[str, tuple[UserProperty, Path]] = {}
        groups: dict[str, tuple[UserPropertyGroup, Path]] = {}
        done: set[Path] = set()

        def visit(file: Path, stack: tuple[Path, ...]) -> None:
            if file in stack:
                cycle = " -> ".join(str(p) for p in (*stack[stack.index(file):], file))
                raise ValueError(f"properties files import each other in a cycle: {cycle}")
            if file in done:
                return
            done.add(file)
            loaded = UserPropertyFile.load(file)
            for g in loaded.groups:
                seen = groups.get(g.name)
                if seen is None or not seen[0].description:
                    groups[g.name] = (g, file)
                elif g.description and g.description != seen[0].description:
                    raise ValueError(
                        f"group {g.name!r} has one description in {seen[1]} and another in {file}"
                    )
            for p in loaded.properties:
                if (seen_p := properties.get(p.id)) is not None:
                    raise ValueError(f"property {p.id!r} is defined in {seen_p[1]} and in {file}")
                properties[p.id] = (p, file)
            for name in loaded.imports:
                visit((file.parent / name).resolve(), (*stack, file))

        visit(path.resolve(), ())
        return UserPropertyFile(
            groups=[g for g, _ in groups.values()], properties=[p for p, _ in properties.values()],
        )

    def components(self) -> list[tuple[UserPropertyGroup, list[UserProperty]]]:
        """Each group with its properties: the declared groups in order, then a group a
        property names but the file does not declare, then ``DEFAULT_GROUP``. A group with no
        property is left out."""
        groups = {g.name: g for g in self.groups}
        for name in [*(p.group for p in self.properties if p.group), DEFAULT_GROUP]:
            groups.setdefault(name, UserPropertyGroup(name=name))
        members: dict[str, list[UserProperty]] = {name: [] for name in groups}
        for p in self.properties:
            members[p.group or DEFAULT_GROUP].append(p)
        return [(g, members[g.name]) for g in groups.values() if members[g.name]]


def user_property_set(
    file: UserPropertyFile, *, ecosystem: ChainTag, contract_name: SourceIdentifier, contract_path: str,
    run_mode: RunMode,
) -> PropertySet:
    """The property set of ``file`` for the main contract ``contract_name`` at
    ``contract_path``."""
    parts = file.components()
    main: SourceExplicitContract | SourceExternalActor = SourceExplicitContract(
        sort="singleton", name=ContractName(contract_name),
        solidity_identifier=SolidityIdentifier(contract_name), description="", path=contract_path,
        components=[
            ContractComponent(
                name=g.name, description=g.description, external_entry_points=[],
                state_variables=[], interactions=[], requirements=[],
            )
            for g, _ in parts
        ],
    )
    app = SourceApplication(application_type="", description="", components=[main])
    return PropertySet(
        ecosystem=ecosystem, contract_name=contract_name, run_mode=run_mode,
        system_model=app.model_dump(mode="json"),
        property_components=[
            PropertySetComponent(name=g.name, unit_index=i, properties=[
                PropertyFormulation(title=p.id, sort=p.sort, description=p.description) for p in props
            ])
            for i, (g, props) in enumerate(parts)
        ],
    )


class UserPropertiesGenerator(PropertyGeneratorImpl[ContractComponentInstance]):
    async def generate(self, ctx: PropertyGenerationContext[Any, Any, ContractComponentInstance]) -> PropertySet:
        name = os.environ.get(USER_PROPERTIES_ENV)
        if not name:
            raise ValueError(f"the user-properties generator needs {USER_PROPERTIES_ENV}")
        source = ctx.run.source
        return user_property_set(
            UserPropertyFile.load_suite(Path(source.project_root) / name),
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
