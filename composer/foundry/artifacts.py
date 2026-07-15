"""Foundry artifact writer: the ``certora/foundry/`` deliverable layout.

A subclass of the shared :class:`composer.spec.artifacts.ArtifactStore`. The
generated ``.t.sol`` tests live in the foundry project's own ``test/`` (so forge
finds them); everything else the AI tool produces — per-component property dumps,
property→test maps, commentary, per-test statuses, and the run report — lands
under ``certora/foundry/`` (diagnostics under ``.certora_internal/foundry/``) so a
co-located autoprove run shares the project without clobbering its outputs.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Literal, override

from pydantic import BaseModel, Field

from composer.foundry.author import GeneratedFoundryTest
from composer.foundry.runner import infer_test_dir
from composer.spec.artifacts import ArtifactStore
from composer.spec.context import SourceCode
from composer.spec.cvl_generation import SkippedProperty
from composer.spec.gen_types import (
    FOUNDRY_DELIVERABLE_DIR, FOUNDRY_INTERNAL_DIR, under_project,
)
from composer.spec.types import PropertyFormulation
from composer.spec.source.report.schema import AutoProverReport
from composer.spec.util import ensure_dir


@dataclass(frozen=True)
class FoundryTestArtifact:
    """A per-component generated foundry test. ``base`` is the (collision-
    disambiguated) component slug — the same one used for the ``.t.sol`` filename
    — so a component's metadata sits next to a predictably-named test file."""
    base: str

    @property
    def stem(self) -> str:
        return f"composer_{self.base}"

    @property
    def artifact_file(self) -> str:
        return f"{self.stem}.t.sol"


# ---------------------------------------------------------------------------
# Report schema (what the store serializes)
# ---------------------------------------------------------------------------


class PassedTest(BaseModel):
    status: Literal["passed"] = "passed"
    name: str


class ExpectedFailureTest(BaseModel):
    status: Literal["expected_failure"] = "expected_failure"
    name: str
    reason: str


#: A test forge ran: passed, or an author-declared expected failure (which alone
#: carries a reason). Discriminated on ``status``.
TestStatus = Annotated[PassedTest | ExpectedFailureTest, Field(discriminator="status")]


class ComponentTestStatus(BaseModel):
    """``{stem}.status.json`` — the forge-ground-truth status of one component's
    generated tests, plus any properties the author declared unformalizable."""
    tests: list[TestStatus]
    skipped: list[SkippedProperty]


# ---------------------------------------------------------------------------
# Store
# ---------------------------------------------------------------------------


class FoundryArtifactStore(ArtifactStore[FoundryTestArtifact, GeneratedFoundryTest]):
    """Persists the foundry pipeline's metadata under ``certora/foundry/`` (plus
    ``.certora_internal/foundry/`` diagnostics) and materializes the ``.t.sol``
    tests into the foundry project's own test dir."""

    def __init__(self, project_root: str):
        super().__init__(
            project_root,
            "property_tests",
            deliverable_dir=FOUNDRY_DELIVERABLE_DIR,
            internal_dir=FOUNDRY_INTERNAL_DIR,
            report_dir=FOUNDRY_DELIVERABLE_DIR / "reports"
        )

    @override
    def _artifact_dir(self) -> Path:
        """The foundry project's own test dir (``foundry.toml``'s
        ``[profile.default] test``, defaulting to ``test``) — where forge expects
        the generated ``.t.sol`` files, so they can't live under ``certora/``."""
        return ensure_dir(Path(self._project_root) / infer_test_dir(self._project_root))

    @override
    def write_artifact(
        self,
        i: FoundryTestArtifact,
        artifact: GeneratedFoundryTest,
    ) -> Path:
        """Materialize a generated test: write the ``.t.sol`` into the foundry
        project's test dir, plus its metadata bundle under ``certora/foundry/`` —
        ``{stem}.commentary.md``, ``{stem}.property_tests.json`` (the property→test
        map), and ``{stem}.status.json`` (each test's pass / expected-failure
        status and any declared skips). Returns the absolute path of the written
        ``.t.sol``."""
        test_path = super().write_artifact(i, artifact)

        tests: list[PassedTest | ExpectedFailureTest] = []
        
        for name in artifact.ran_tests:
            reason = artifact.expected_failures.get(name) or ""
            if reason:
                tests.append(ExpectedFailureTest(name=name, reason=reason))
            else:
                tests.append(PassedTest(name=name))
        status = ComponentTestStatus(tests=tests, skipped=artifact.skipped)
        (self._properties_dir() / f"{i.stem}.status.json").write_text(
            status.model_dump_json(indent=2)
        )
        return test_path
