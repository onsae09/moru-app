"""BatchTranslator behavior and LM usage accounting with deterministic fakes."""

from __future__ import annotations

from types import SimpleNamespace

import dspy
from dspy.utils import DummyLM

from moru_engine.dspy_modules import BatchTranslator, render_style_directives
from moru_engine.dspy_modules.lm import token_usage
from moru_engine.dspy_modules.translator import check_protected

ENTRIES = {"a.b": "Hello {{PH1}} world", "c.d": "Iron Sword"}
GOOD = {"a.b": "안녕 {{PH1}} 세계", "c.d": "철 검"}


def _run(lm: DummyLM, max_refine: int = 2) -> dspy.Prediction:
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        return BatchTranslator(max_refine=max_refine)(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
        )


def test_check_protected() -> None:
    assert check_protected("x {{PH1}}", "y {{PH1}}") == []
    assert check_protected("x {{PH1}}", "y")  # dropped
    assert check_protected("x", "y {{PH1}}")  # invented
    assert check_protected("x", None)
    assert check_protected("x", "  ")


def test_happy_path_no_refine() -> None:
    lm = DummyLM([{"translations": GOOD}], adapter=dspy.JSONAdapter())
    pred = _run(lm)
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_refine_fixes_dropped_placeholder() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "안녕 {{PH1}} 세계"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm)
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_refine_exhaustion_surfaces_failure() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "여전히 깨짐"},
            {"fixed_translation": "여전히 깨짐 2"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm, max_refine=2)
    assert "a.b" in pred.failed
    assert any("{{PH1}}" in e for e in pred.failed["a.b"])
    # the good key is untouched
    assert pred.translations["c.d"] == GOOD["c.d"]


def test_hallucinated_keys_dropped_and_missing_retried() -> None:
    lm = DummyLM(
        [
            {"translations": {"c.d": "철 검", "made.up": "환각"}},
            {"translations": {"a.b": "안녕 {{PH1}} 세계"}},
        ],
        adapter=dspy.JSONAdapter(),
    )
    pred = _run(lm)
    assert "made.up" not in pred.translations
    # missing a.b was refined back in
    assert pred.translations["a.b"] == "안녕 {{PH1}} 세계"
    assert pred.failed == {}


async def test_multiple_missing_entries_are_retried_in_one_batch() -> None:
    entries = {"a": "Alpha", "b": "Beta", "c": "Gamma"}
    expected = {"a": "알파", "b": "베타", "c": "감마"}
    lm = DummyLM(
        [
            {"translations": {"a": expected["a"]}},
            {"translations": {"b": expected["b"], "c": expected["c"]}},
        ],
        adapter=dspy.JSONAdapter(),
    )
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        pred = await BatchTranslator(max_refine=2).acall(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=entries,
        )

    assert pred.translations == expected
    assert pred.failed == {}
    assert len(lm.history) == 2


async def test_acall_async_path_matches_forward() -> None:
    lm = DummyLM(
        [
            {"translations": {**GOOD, "a.b": "안녕 세계"}},
            {"fixed_translation": "안녕 {{PH1}} 세계"},
        ],
        adapter=dspy.JSONAdapter(),
    )
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        pred = await BatchTranslator(max_refine=2).acall(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
        )
    assert pred.translations == GOOD
    assert pred.failed == {}


def test_strip_invented_formatting() -> None:
    from moru_engine.dspy_modules.translator import strip_invented_formatting

    # invented RESET (not in source) is stripped
    assert (
        strip_invented_formatting("{{COLOR1}}Enchanted", "{{COLOR1}}마법 부여됨{{RESET2}}")
        == "{{COLOR1}}마법 부여됨"
    )
    # duplicated known COLOR keeps the first occurrence only
    assert (
        strip_invented_formatting("{{COLOR1}}x", "{{COLOR1}}가{{COLOR1}}나")
        == "{{COLOR1}}가나"
    )
    # invented ARG is NOT stripped (must fail loudly downstream)
    out = strip_invented_formatting("plain", "값 {{ARG9}}")
    assert "{{ARG9}}" in out
    assert check_protected("plain", out)
    # exact output untouched
    assert (
        strip_invented_formatting("{{COLOR1}}a{{RESET2}}", "{{COLOR1}}가{{RESET2}}")
        == "{{COLOR1}}가{{RESET2}}"
    )


def test_token_usage_sums_cached_tokens_across_provider_shapes() -> None:
    history = [
        # OpenAI/OpenRouter style: dict usage, nested details dict
        {
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 40,
                "prompt_tokens_details": {"cached_tokens": 60},
            }
        },
        # Anthropic style: pydantic-like object, attribute access only
        {
            "usage": SimpleNamespace(
                prompt_tokens=200,
                completion_tokens=50,
                cache_read_input_tokens=150,
            )
        },
        # object usage with object details; zero top-level fallback ignored
        {
            "usage": SimpleNamespace(
                prompt_tokens=80,
                completion_tokens=20,
                prompt_tokens_details=SimpleNamespace(cached_tokens=40),
                cache_read_input_tokens=0,
            )
        },
        # no cache info at all
        {"usage": {"prompt_tokens": 30, "completion_tokens": 10}},
        # provider over-reports the cache: clamped to that call's prompt
        {
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 5,
                "prompt_tokens_details": {"cached_tokens": 999},
            }
        },
    ]
    usage = token_usage(SimpleNamespace(history=history))
    assert usage["prompt_tokens"] == 430
    assert usage["completion_tokens"] == 125
    assert usage["total_tokens"] == 555
    assert usage["cached_tokens"] == 60 + 150 + 40 + 0 + 20
    assert usage["cached_tokens"] <= usage["prompt_tokens"]


def test_token_usage_empty_history() -> None:
    usage = token_usage(SimpleNamespace(history=[]))
    assert usage == {
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "total_tokens": 0,
        "cached_tokens": 0,
    }


# --- style directives ----------------------------------------------------


def test_default_directives_only_enforce_register_consistency() -> None:
    # Defaults must not add register or term-rendering mandates: the
    # compiled instructions' own style guidance has to survive untouched.
    rendered = render_style_directives("ko_kr")
    assert "ONE register per speaker" in rendered
    assert "하게체" not in rendered
    assert "음차" not in rendered


def test_each_speech_level_renders_its_own_endings() -> None:
    polite = render_style_directives("ko_kr", "polite")
    assert "존댓말" in polite
    assert "하게체" not in polite

    banmal = render_style_directives("ko_kr", "banmal")
    assert "반말 (해체)" in banmal
    assert "존댓말" not in banmal

    hage = render_style_directives("ko_kr", "hage")
    assert "하게체" in hage
    assert "자네" in hage

    # The consistency rule rides along with every explicit register.
    assert all("ONE register per speaker" in r for r in (polite, banmal, hage))


def test_term_style_picks_translation_or_transliteration() -> None:
    assert "prefer translating its meaning" in render_style_directives(
        "ko_kr", term_style="translate"
    )
    assert "prefer transliterating the source term" in render_style_directives(
        "ko_kr", term_style="transliterate"
    )


def test_speech_level_is_korean_only_while_term_style_is_not() -> None:
    # 하게체 has no meaning for a Japanese target; the term preference does.
    rendered = render_style_directives("ja_jp", "hage", "translate")
    assert "하게체" not in rendered
    assert "prefer translating its meaning" in rendered


def test_style_directives_reach_the_rendered_prompt() -> None:
    directives = render_style_directives("ko_kr", "hage", "transliterate")
    lm = DummyLM([{"translations": GOOD}], adapter=dspy.JSONAdapter())
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        BatchTranslator(max_refine=0)(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            style_directives=directives,
            entries=ENTRIES,
        )
    sent = "\n".join(
        message["content"]
        for message in lm.history[-1]["messages"]
        if message["role"] == "user"
    )
    assert "[[ ## style_directives ## ]]" in sent
    assert directives in sent


def test_omitted_style_directives_default_to_empty() -> None:
    # GEPA/evalset examples never supply the field; the module must not
    # require it, and the batch must still translate.
    lm = DummyLM([{"translations": GOOD}], adapter=dspy.JSONAdapter())
    pred = _run(lm)
    assert pred.translations == GOOD
    sent = "\n".join(
        message["content"]
        for message in lm.history[-1]["messages"]
        if message["role"] == "user"
    )
    assert "[[ ## style_directives ## ]]\n\n" in sent


async def test_a_failing_refine_keeps_the_rest_of_the_batch() -> None:
    translator = BatchTranslator(max_refine=1)

    async def broken_refine(**_: object) -> dspy.Prediction:
        raise RuntimeError("provider hiccup")

    translator.refine.acall = broken_refine  # type: ignore[method-assign]
    lm = DummyLM([{"translations": {**GOOD, "a.b": "안녕 세계"}}], adapter=dspy.JSONAdapter())
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        pred = await translator.acall(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
        )
    assert pred.translations["c.d"] == GOOD["c.d"]
    assert "a.b" in pred.failed


async def test_rollout_id_reaches_the_first_request() -> None:
    lm = DummyLM([{"translations": GOOD}], adapter=dspy.JSONAdapter())
    with dspy.context(lm=lm, adapter=dspy.JSONAdapter()):
        await BatchTranslator(max_refine=1).acall(
            source_lang="en_us",
            target_lang="ko_kr",
            context="test",
            glossary="",
            entries=ENTRIES,
            rollout_id=7,
        )
    assert lm.history[0]["kwargs"].get("rollout_id") == 7
