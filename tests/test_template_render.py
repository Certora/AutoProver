"""
Render smoke tests for the prompt templates touched by the keccak/unstructured-storage
skip-gate work. These catch missing-variable regressions (a template referencing a
variable its callers don't supply) and pin the gating behavior of the judge system
prompt's `harness_augmentation` variable, which defaults to false in the template so
pipelines without harness augmentation can omit it entirely.
"""
import pytest

from composer.templates.loader import load_jinja_template
from composer.spec.prop import PropertyFormulation


def _props() -> list[PropertyFormulation]:
    return [
        PropertyFormulation(
            title="total_supply_preserved",
            methods="invariant",
            sort="invariant",
            description="the total supply is preserved by all operations",
        ),
        PropertyFormulation(
            title="no_free_mint",
            methods=["mint(address,uint256)"],
            sort="attack_vector",
            description="an attacker cannot mint tokens for free",
        ),
    ]


class TestJudgeSystemPrompt:
    def test_renders_without_harness_augmentation(self):
        # harness_augmentation omitted: the template's `default(false)` must render the
        # fallback wording (label as missing harness support), not the demand wording.
        out = load_jinja_template("property_judge_system_prompt.j2", sort="existing")
        assert "No Protocol Source Changes" in out
        assert "missing harness support, not CVL inexpressibility" in out
        assert "demand a harness augmentation" not in out

    def test_renders_with_harness_augmentation(self):
        out = load_jinja_template(
            "property_judge_system_prompt.j2", sort="existing", harness_augmentation=True
        )
        assert "demand a harness augmentation" in out
        assert "missing harness support, not CVL inexpressibility" not in out

    def test_explicit_false_matches_omitted(self):
        omitted = load_jinja_template("property_judge_system_prompt.j2", sort="existing")
        explicit = load_jinja_template(
            "property_judge_system_prompt.j2", sort="existing", harness_augmentation=False
        )
        assert omitted == explicit

    def test_greenfield_has_no_source_change_section(self):
        # The whole protocol-immutability section is gated on sort != greenfield
        # (greenfield has no pre-existing source to protect).
        out = load_jinja_template("property_judge_system_prompt.j2", sort="greenfield")
        assert "No Protocol Source Changes" not in out


class TestJudgePrompt:
    @pytest.mark.parametrize("sort", ["existing", "greenfield"])
    def test_criteria7_skip_checklist_renders(self, sort: str):
        out = load_jinja_template(
            "property_judge_prompt.j2", properties=_props(), sort=sort, context=None
        )
        # The capability checklist the judge must run before accepting a
        # "not expressible in CVL" skip lives inside Criteria 7.
        assert "Before accepting ANY skip" in out
        assert "precomputed keccak storage-slot constant" in out
        assert "harness getter or direct storage access" in out
        assert "ghost state mirroring" in out


class TestGenerationPrompt:
    def test_storage_access_block_renders(self):
        out = load_jinja_template(
            "property_generation_prompt.j2",
            contract_name="Widget",
            properties=_props(),
            resources=[],
            context=None,
        )
        assert "<storage_access>" in out
        assert "Sload" in out and "Sstore" in out
        assert "keccak storage slot" in out  # the KB search pointer
