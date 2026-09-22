"""TASK-JEV1 单测:tools/jev_poc 分块器 + 裁判(全 mock,不触网)。

覆盖:分块边界/行数守恒/stub 可逆性、裁判解析健壮性(坏 JSON/超时/限流/无 key → 保留)、
低置信 drop → 保留、成本统计函数。
"""

from __future__ import annotations

import json
import os
import sys

import pytest
import requests

BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(BACKEND_DIR)
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

from tools.jev_poc import judge as judge_mod  # noqa: E402
from tools.jev_poc.judge import (  # noqa: E402
    JudgeClient,
    build_user_prompt,
    estimate_screening_savings,
    parse_verdicts,
    resolve_llm,
)
from tools.jev_poc.splitter import (  # noqa: E402
    Chunk,
    StubStore,
    iter_stubbed,
    split_text,
    total_lines,
)

FAKE_ENV = {"ALIBABA_TOKEN_PLAN_API_KEY": "***"}


# ---------------------------------------------------------------- splitter

def _log(n: int, width: int = 40) -> str:
    return "\n".join(f"line-{i:04d} " + "x" * width for i in range(n)) + "\n"


class TestSplitter:
    def test_empty_text_zero_chunks(self):
        assert split_text("") == []

    def test_exact_multiple(self):
        chunks = split_text(_log(50))
        assert len(chunks) == 2
        assert [c.n_lines for c in chunks] == [25, 25]

    def test_remainder_tail_chunk(self):
        chunks = split_text(_log(51))
        assert len(chunks) == 3
        assert chunks[-1].n_lines == 1

    def test_line_count_conservation(self):
        text = _log(137)
        chunks = split_text(text)
        assert total_lines(chunks) == 137

    def test_char_perfect_roundtrip(self):
        text = _log(63)
        chunks = split_text(text)
        assert "".join(c.text for c in chunks) == text

    def test_no_trailing_newline_roundtrip_lenient(self):
        text = _log(30).rstrip("\n")
        chunks = split_text(text)
        assert "".join(c.text for c in chunks) == text + "\n"
        assert total_lines(chunks) == 30

    def test_custom_chunk_lines(self):
        chunks = split_text(_log(10), chunk_lines=4)
        assert [c.n_lines for c in chunks] == [4, 4, 2]

    def test_invalid_chunk_lines(self):
        with pytest.raises(ValueError):
            split_text("x", chunk_lines=0)

    def test_line_numbers_1indexed_closed_interval(self):
        chunks = split_text(_log(30), chunk_lines=25)
        assert (chunks[0].start_line, chunks[0].end_line) == (1, 25)
        assert (chunks[1].start_line, chunks[1].end_line) == (26, 30)

    def test_long_single_line_not_folded(self):
        text = "y" * 5000 + "\nshort\n"
        chunks = split_text(text)
        assert len(chunks) == 1
        assert chunks[0].lines[0] == "y" * 5000


class TestStubStore:
    def test_stub_restore_roundtrip(self):
        store = StubStore()
        chunks = split_text(_log(60), store=store)
        assert len(store) == 3
        for chunk in chunks:
            assert store.restore(chunk.key) == chunk.text

    def test_restore_unknown_key_none(self):
        assert StubStore().restore("c9999-deadbeef00") is None

    def test_stub_text_short_and_keyed(self):
        store = StubStore()
        chunk = split_text(_log(30), store=store)[0]
        stub = StubStore.stub_text(chunk)
        assert chunk.key in stub
        assert len(stub) < 140  # stub 必须远小于原块
        assert stub.count("\n") == 0

    def test_idempotent_put_same_content(self):
        store = StubStore()
        a, b = split_text(_log(25), store=store)[0], split_text(_log(25), store=store)[0]
        assert a.key == b.key  # 内容哈希稳定
        assert len(store) == 1

    def test_iter_stubbed_yields_stubs(self):
        stubs = list(iter_stubbed(_log(60)))
        assert len(stubs) == 3
        assert all(s.startswith("[jev-stub ") for s in stubs)

    def test_approx_tokens_positive_and_monotonic(self):
        small = split_text(_log(5))[0]
        big = split_text(_log(25))[0]
        assert small.approx_tokens > 0
        assert big.approx_tokens > small.approx_tokens


# ---------------------------------------------------------------- resolve_llm

class TestResolve:
    def test_picks_qwen_first(self):
        resolved = resolve_llm(FAKE_ENV)
        assert resolved["name"] == "qwen"
        assert "compatible-mode" in resolved["base_url"]

    def test_falls_back_to_deepseek(self):
        resolved = resolve_llm({"DEEPSEEK_API_KEY": "***"})
        assert resolved["name"] == "deepseek"

    def test_explicit_provider_override(self):
        env = dict(FAKE_ENV, DEEPSEEK_API_KEY="***", WHERE2GO_LLM_PROVIDER="deepseek")
        assert resolve_llm(env)["name"] == "deepseek"

    def test_no_key_returns_none(self):
        assert resolve_llm({}) is None

    def test_model_env_override(self):
        env = dict(FAKE_ENV, WHERE2GO_LLM_MODEL="qwen-turbo")
        assert resolve_llm(env)["model"] == "qwen-turbo"


# ---------------------------------------------------------------- parse_verdicts

def _ok_json(n: int, keep_flags=None) -> str:
    flags = keep_flags or [True] * n
    return json.dumps([{"id": i, "keep": k, "confidence": 0.95} for i, k in enumerate(flags)])


class TestParseVerdicts:
    def test_plain_json(self):
        verdicts = parse_verdicts(_ok_json(3), 3)
        assert [v.keep for v in verdicts] == [True, True, True]

    def test_code_fence_wrapped(self):
        verdicts = parse_verdicts("```json\n" + _ok_json(2) + "\n```", 2)
        assert len(verdicts) == 2

    def test_prose_around_json(self):
        verdicts = parse_verdicts("好的,判定如下:\n" + _ok_json(2) + "\n以上。", 2)
        assert len(verdicts) == 2

    def test_bad_json_returns_none(self):
        assert parse_verdicts("[{oops", 2) is None

    def test_empty_returns_none(self):
        assert parse_verdicts("", 2) is None
        assert parse_verdicts(None, 2) is None

    def test_missing_chunks_returns_none(self):
        assert parse_verdicts(_ok_json(2), 3) is None  # 少一块 -> 不猜

    def test_non_bool_keep_returns_none(self):
        assert parse_verdicts(json.dumps([{"id": 0, "keep": "yes"}]), 1) is None

    def test_low_confidence_drop_becomes_keep(self):
        raw = json.dumps([{"id": 0, "keep": False, "confidence": 0.3}])
        assert parse_verdicts(raw, 1)[0].keep is True

    def test_high_confidence_drop_survives(self):
        raw = json.dumps([{"id": 0, "keep": False, "confidence": 0.95}])
        assert parse_verdicts(raw, 1)[0].keep is False

    def test_confidence_clamped(self):
        raw = json.dumps([{"id": 0, "keep": True, "confidence": 9.9}])
        assert parse_verdicts(raw, 1)[0].confidence == 1.0

    def test_out_of_order_ids(self):
        raw = json.dumps([
            {"id": 1, "keep": False, "confidence": 0.99},
            {"id": 0, "keep": True, "confidence": 0.9},
        ])
        verdicts = parse_verdicts(raw, 2)
        assert verdicts[0].keep is True and verdicts[1].keep is False

    def test_nonzero_based_ids_for_batches(self):
        # 分批调用时裁判按 prompt 里的真实块号(id=12..13)回复,不能要求 0 基
        raw = json.dumps([
            {"id": 12, "keep": False, "confidence": 0.9},
            {"id": 13, "keep": True, "confidence": 0.9},
        ])
        assert parse_verdicts(raw, 2, ids=[12, 13]) is not None
        verdicts = parse_verdicts(raw, 2, ids=[12, 13])
        assert verdicts[0].keep is False and verdicts[1].keep is True
        # ids 不匹配 -> None 降级
        assert parse_verdicts(raw, 2, ids=[0, 1]) is None


# ---------------------------------------------------------------- JudgeClient(网络全 mock)

class _FakeResponse:
    def __init__(self, payload=None, status=200):
        self._payload = payload or {}
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"status {self.status_code}")

    def json(self):
        if not self._payload:
            raise ValueError("no json")
        return self._payload


class _FakeSession:
    def __init__(self, response=None, exc=None):
        self.response, self.exc = response, exc
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if self.exc:
            raise self.exc
        return self.response


def _payload(verdicts_json: str, pt=100, ct=20, model="qwen3.8-max"):
    return {
        "model": model,
        "choices": [{"message": {"content": verdicts_json}}],
        "usage": {"prompt_tokens": pt, "completion_tokens": ct},
    }


@pytest.fixture
def no_network(monkeypatch):
    """偷跑网络当场失败。"""
    def boom(*a, **k):
        raise AssertionError("测试不允许真实网络调用")
    monkeypatch.setattr(requests.Session, "request", boom)


CHUNKS3 = split_text(_log(75))


class TestJudgeClient:
    @pytest.mark.usefixtures("no_network")
    def test_happy_path(self):
        session = _FakeSession(response=_FakeResponse(_payload(_ok_json(3, [True, False, True]))))
        client = JudgeClient(session=session, environ=FAKE_ENV)
        result = client.judge("任务焦点", CHUNKS3)
        assert [v.keep for v in result.verdicts] == [True, False, True]
        assert result.kept == 2 and result.dropped == 1
        assert result.prompt_tokens == 100

    @pytest.mark.usefixtures("no_network")
    def test_no_key_degrades_to_keep_all(self):
        client = JudgeClient(session=_FakeSession(), environ={})
        result = client.judge("焦点", CHUNKS3)
        assert result.degraded_all and all(v.keep for v in result.verdicts)
        assert result.error and "key" in result.error

    @pytest.mark.usefixtures("no_network")
    def test_timeout_degrades_to_keep_all(self):
        session = _FakeSession(exc=requests.Timeout("timeout"))
        result = JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", CHUNKS3)
        assert result.degraded_all and len(result.verdicts) == 3

    @pytest.mark.usefixtures("no_network")
    def test_rate_limit_429_degrades(self):
        session = _FakeSession(response=_FakeResponse(status=429))
        result = JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", CHUNKS3)
        assert result.degraded_all

    @pytest.mark.usefixtures("no_network")
    def test_garbage_reply_degrades(self):
        session = _FakeSession(response=_FakeResponse(_payload("我不知道该说什么")))
        result = JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", CHUNKS3)
        assert result.degraded_all and result.model == "qwen3.8-max"

    @pytest.mark.usefixtures("no_network")
    def test_empty_chunks_no_call(self):
        session = _FakeSession()
        result = JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", [])
        assert result.verdicts == [] and session.calls == []

    @pytest.mark.usefixtures("no_network")
    def test_key_never_leaks_into_prompt_or_log(self):
        session = _FakeSession(response=_FakeResponse(_payload(_ok_json(3))))
        JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", CHUNKS3)
        url, kwargs = session.calls[0]
        assert url.endswith("/chat/completions")
        assert "sk-test" not in json.dumps(kwargs["json"])
        assert kwargs["headers"]["Authorization"].startswith("Bearer ")

    @pytest.mark.usefixtures("no_network")
    def test_usage_missing_defaults_zero(self):
        payload = {"choices": [{"message": {"content": _ok_json(3)}}]}
        session = _FakeSession(response=_FakeResponse(payload))
        result = JudgeClient(session=session, environ=FAKE_ENV).judge("焦点", CHUNKS3)
        assert result.prompt_tokens == 0 and result.cost_cny() == 0.0

    @pytest.mark.usefixtures("no_network")
    def test_extra_params_merged_into_payload(self):
        env = dict(FAKE_ENV)
        env["WHERE2GO_LLM_EXTRA_PARAMS"] = '{"enable_thinking": false}'
        session = _FakeSession(response=_FakeResponse(_payload(_ok_json(3))))
        JudgeClient(session=session, environ=env).judge("焦点", CHUNKS3)
        assert session.calls[0][1]["json"]["enable_thinking"] is False

    @pytest.mark.usefixtures("no_network")
    def test_bad_extra_params_json_ignored_not_degraded(self):
        env = dict(FAKE_ENV)
        env["WHERE2GO_LLM_EXTRA_PARAMS"] = "not-json{{"
        session = _FakeSession(response=_FakeResponse(_payload(_ok_json(3))))
        result = JudgeClient(session=session, environ=env).judge("焦点", CHUNKS3)
        assert not result.degraded_all and result.kept == 3
        assert "enable_thinking" not in session.calls[0][1]["json"]

    @pytest.mark.usefixtures("no_network")
    def test_extra_params_model_override(self):
        env = dict(FAKE_ENV)
        env["WHERE2GO_LLM_MODEL"] = "qwen3.8-flash"
        session = _FakeSession(response=_FakeResponse(_payload(_ok_json(3))))
        result = JudgeClient(session=session, environ=env).judge("焦点", CHUNKS3)
        assert session.calls[0][1]["json"]["model"] == "qwen3.8-flash"
        assert result.model == "qwen3.8-flash"


# ---------------------------------------------------------------- prompt & cost

class TestPromptAndCost:
    def test_prompt_contains_focus_and_ids(self):
        prompt = build_user_prompt("修复分段查询", CHUNKS3)
        assert "修复分段查询" in prompt
        for chunk in CHUNKS3:
            assert f"id={chunk.index}" in prompt

    def test_prompt_truncates_huge_chunk(self):
        huge = split_text("\n".join(["z" * 9000] * 25) + "\n")[0]
        prompt = build_user_prompt("焦点", [huge], max_chars_per_chunk=100)
        assert len(prompt) < 500 and "截断" in prompt

    def test_savings_all_keep_zero(self):
        verdicts = [judge_mod.Verdict(keep=True, confidence=1.0)] * len(CHUNKS3)
        stats = estimate_screening_savings(CHUNKS3, verdicts)
        assert stats["saved_tokens"] == 0 and stats["saved_ratio"] == 0.0
        assert stats["tokens_before"] == stats["tokens_after"]

    def test_savings_drop_shrinks(self):
        verdicts = [judge_mod.Verdict(keep=True, confidence=1.0)] * 2 + [
            judge_mod.Verdict(keep=False, confidence=0.9)
        ]
        stats = estimate_screening_savings(CHUNKS3, verdicts)
        assert stats["dropped"] == 1
        assert 0 < stats["saved_ratio"] < 1
        assert stats["tokens_after"] < stats["tokens_before"]

    def test_cost_cny_matches_price_table(self):
        result = judge_mod.JudgeResult(model="qwen3.8-max", prompt_tokens=1000, completion_tokens=1000)
        price = judge_mod.PRICE_PER_1K["qwen"]
        assert result.cost_cny() == pytest.approx(price["input"] + price["output"])

    def test_cost_cny_deepseek_table(self):
        result = judge_mod.JudgeResult(model="deepseek-chat", prompt_tokens=1000, completion_tokens=0)
        assert result.cost_cny() == pytest.approx(judge_mod.PRICE_PER_1K["deepseek"]["input"])
