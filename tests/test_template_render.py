"""
Render smoke tests for the prompt templates touched by the keccak/unstructured-storage
skip-gate work. These catch missing-variable regressions (a template referencing a
variable its callers don't supply) and pin the gating behavior of the judge system
prompt's `harness_augmentation` variable, which defaults to false in the template so
pipelines without harness augmentation can omit it entirely.
"""
import pytest

from composer.templates.loader import load_jinja_template
from composer.spec.feedback import (
    FeedbackSystemTemplate,
    FeedbackTemplate,
    Properties,
    _bind_harness_augmentation,
)
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

    def test_criteria7_immutability_is_protocol_scoped(self):
        # The immutability carve-out must not contradict checklist item (b): only
        # *protocol* code changes validate a skip; harness getters/wrappers do not.
        out = load_jinja_template(
            "property_judge_prompt.j2", properties=_props(), sort="existing", context=None
        )
        assert "only with *protocol* Solidity code changes" in out
        assert "NOT protocol code" in out
        # The old blanket doctrine ("the Solidity code is IMMUTABLE", unqualified) is gone.
        assert "The Solidity code is *IMMUTABLE*" not in out

    def test_criteria7_greenfield_rejects_getter_skips(self):
        # In greenfield the author defines the contract API through the stub, so
        # getter-shaped skips stay rejectable and the "missing harness support"
        # fallback labeling must not render.
        out = load_jinja_template(
            "property_judge_prompt.j2", properties=_props(), sort="greenfield", context=None
        )
        assert "If any of these mechanisms applies, reject the skip" in out
        assert "exposing the state through a getter" in out
        assert "missing harness support, not CVL inexpressibility" not in out

    def test_criteria7_fallback_labels_harness_skips(self):
        # harness_augmentation omitted (default false): Criteria 7 must align with the
        # system prompt's fallback — harness-shaped skips get the "missing harness
        # support" label rather than a rejection the author has no tool to satisfy.
        out = load_jinja_template(
            "property_judge_prompt.j2", properties=_props(), sort="existing", context=None
        )
        assert "missing harness support, not CVL inexpressibility" in out
        assert "If any of these mechanisms applies, reject the skip" not in out
        assert "does NOT make a skip valid under this rule" not in out

    def test_criteria7_augmentation_rejects_harness_skips(self):
        # With harness_augmentation the judge may demand harness getters/wrappers:
        # all three mechanisms trigger rejection and harness need does not validate
        # a skip under the protocol-immutability carve-out.
        out = load_jinja_template(
            "property_judge_prompt.j2",
            properties=_props(),
            sort="existing",
            context=None,
            harness_augmentation=True,
        )
        assert "If any of these mechanisms applies, reject the skip" in out
        assert "does NOT make a skip valid under this rule" in out
        assert "missing harness support, not CVL inexpressibility" not in out

    @pytest.mark.parametrize("flag", [True, False])
    def test_judge_templates_agree_on_harness_wording(self, flag: bool):
        # The system prompt and Criteria 7 encode the same harness-skip policy; for a
        # given flag value the harness wording must be present/absent in BOTH templates
        # together, or the judge receives self-contradictory instructions.
        sys_out = load_jinja_template(
            "property_judge_system_prompt.j2", sort="existing", harness_augmentation=flag
        )
        task_out = load_jinja_template(
            "property_judge_prompt.j2",
            properties=_props(),
            sort="existing",
            context=None,
            harness_augmentation=flag,
        )
        assert ("demand a harness augmentation" in sys_out) == flag
        assert ("If any of these mechanisms applies, reject the skip" in task_out) == flag
        assert ("missing harness support, not CVL inexpressibility" in sys_out) == (not flag)
        assert ("missing harness support, not CVL inexpressibility" in task_out) == (not flag)

    @pytest.mark.parametrize("flag", [True, False])
    def test_property_feedback_judge_injection_binds_both_templates(self, flag: bool):
        # property_feedback_judge threads its harness_augmentation parameter into both
        # binds via _bind_harness_augmentation; render both through the same helper and
        # assert the resulting prompts agree for both flag values.
        task = _bind_harness_augmentation(
            FeedbackTemplate.bind({"context": None, "sort": "existing"})
            .depends(Properties)
            .inject({"properties": _props()}),
            flag,
        )
        sys_p = _bind_harness_augmentation(
            FeedbackSystemTemplate.bind({"sort": "existing"}), flag
        )
        task_out = task.render_to(load_jinja_template)
        sys_out = sys_p.render_to(load_jinja_template)
        assert ("demand a harness augmentation" in sys_out) == flag
        assert ("If any of these mechanisms applies, reject the skip" in task_out) == flag
        assert ("missing harness support, not CVL inexpressibility" in sys_out) == (not flag)
        assert ("missing harness support, not CVL inexpressibility" in task_out) == (not flag)

    def test_criteria7_explicit_false_matches_omitted(self):
        omitted = load_jinja_template(
            "property_judge_prompt.j2", properties=_props(), sort="existing", context=None
        )
        explicit = load_jinja_template(
            "property_judge_prompt.j2",
            properties=_props(),
            sort="existing",
            context=None,
            harness_augmentation=False,
        )
        assert omitted == explicit


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
