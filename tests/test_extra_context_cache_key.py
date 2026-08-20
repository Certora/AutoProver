"""The ``--extra-context`` documents must parameterize the bug-analysis cache key.

Two runs differing only in extra context must not share cached properties. The flag takes
a list, so adding a document, dropping one, or reordering them all change the prompt and
must all change the key. A run without extra context keeps the bare ``bug_analysis`` key,
so existing caches stay addressable.
"""

from composer.pipeline.run_tags import AutoProveCacheTags
from composer.spec.context import CacheKey
from composer.spec.prop_inference import BUG_ANALYSIS_KEY
from composer.spec.util import combine_digests


def _key(k: CacheKey) -> str:
    """``CacheKey`` is an opaque wrapper with no ``__eq__`` — compare the string."""
    return str(k)


# --- combine_digests ------------------------------------------------------------------

def test_combine_digests_is_fixed_width() -> None:
    # Folded, not concatenated — the key does not grow with the document count.
    one, two = combine_digests(["xc1"]), combine_digests(["xc1", "xc2"])
    assert one is not None and two is not None
    assert len(one) == len(two) == len(combine_digests(["a"] * 50) or "")


# --- key layout -----------------------------------------------------------------------

def test_no_extra_inputs_keeps_the_historical_key() -> None:
    assert _key(BUG_ANALYSIS_KEY(None, with_refinement=False)) == "bug_analysis"
    assert _key(BUG_ANALYSIS_KEY(None, with_refinement=True)) == "bug_analysis|refine"
    assert _key(BUG_ANALYSIS_KEY("tm1", with_refinement=False)) == "bug_analysis-tm-tm1"
    # ...and an empty document list folds to None, which must not perturb them.
    assert combine_digests([]) is None
    assert _key(BUG_ANALYSIS_KEY(
        "tm1", with_refinement=True, extra_context_digest=combine_digests([])
    )) == "bug_analysis|refine-tm-tm1"


def test_extra_context_composes_with_the_other_inputs() -> None:
    xc = combine_digests(["xc1"])
    assert _key(BUG_ANALYSIS_KEY(
        None, with_refinement=False, extra_context_digest=xc,
    )) == f"bug_analysis-xc-{xc}"
    assert _key(BUG_ANALYSIS_KEY(
        "tm1", with_refinement=True, extra_context_digest=xc,
    )) == f"bug_analysis|refine-tm-tm1-xc-{xc}"


def test_document_list_is_order_and_membership_sensitive() -> None:
    keys = [
        _key(BUG_ANALYSIS_KEY(None, False, combine_digests(docs)))
        for docs in ([], ["xc1"], ["xc2"], ["xc1", "xc2"], ["xc2", "xc1"], ["xc1", "xc1"])
    ]
    assert len(set(keys)) == len(keys), keys


# --- run tags -------------------------------------------------------------------------

def test_run_tags_carry_the_digests_in_order() -> None:
    tags = AutoProveCacheTags(
        cache_root=["ns"], contract_name="C", memory_ns=None,
        threat_model_digest="tm1", extra_context_digests=["xc1", "xc2"], interactive=False,
    )
    restored = AutoProveCacheTags.model_validate(tags.model_dump())
    assert restored.extra_context_digests == ["xc1", "xc2"]


def test_old_run_tags_default_to_no_extra_context() -> None:
    legacy = {"cache_root": ["ns"], "contract_name": "C", "memory_ns": None}
    tags = AutoProveCacheTags.model_validate(legacy)
    assert tags.extra_context_digests == []
    assert combine_digests(tags.extra_context_digests) is None
