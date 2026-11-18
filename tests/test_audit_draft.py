"""「直接问模型」快速起草的测试：全程用假模型，不访问网络、不需要密钥。"""

from __future__ import annotations

import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from langchain_core.messages import AIMessage

from data_agent import api, audit_draft
from data_agent.config import AgentSettings
from data_agent.storage import LocalSessionStorage

URL = "/api/sessions/{}/sales-audit/quick-draft"
GOOD = "销售额合计：17224.00 元\n销量合计：462 件\n订单数：462 笔"


class FakeLLM:
    """假模型：记录收到的消息与参数；绑定工具即视为违反「不调工具」。"""

    def __init__(self, control: FakeControl) -> None:
        self.control = control

    def invoke(self, messages, **kwargs):
        import langsmith.utils as ls_utils

        self.control.calls.append({"messages": messages, "kwargs": kwargs})
        self.control.tracing_seen.append(ls_utils.tracing_is_enabled())
        if self.control.block is not None:
            self.control.block.wait(5)
        reply = self.control.reply
        if isinstance(reply, Exception):
            raise reply
        return reply

    def bind_tools(self, *args, **kwargs):
        raise AssertionError("快速起草不得绑定任何工具")


class FakeControl:
    def __init__(self) -> None:
        self.reply: object = AIMessage(content=GOOD)
        self.calls: list[dict] = []
        self.settings: list[AgentSettings] = []
        self.tracing_seen: list[object] = []
        self.block: threading.Event | None = None


@pytest.fixture()
def control(monkeypatch) -> FakeControl:
    ctl = FakeControl()

    def fake_create(settings: AgentSettings):
        ctl.settings.append(settings)
        return FakeLLM(ctl)

    monkeypatch.setattr(audit_draft, "create_chat_model", fake_create)
    return ctl


@pytest.fixture()
def client(tmp_path, monkeypatch) -> TestClient:
    """隔离运行目录与限流桶，并清空进程内的起草计时。"""
    # _effective_settings() 每次都会 load_dotenv()；本机 .env 里若有 APP_ACCESS_TOKEN，
    # 会在第一次调用后被重新注入，使后续请求 401。屏蔽 .env 让测试与本机环境无关。
    monkeypatch.setattr("data_agent.config.load_dotenv", lambda *args, **kwargs: False)
    monkeypatch.delenv("APP_ACCESS_TOKEN", raising=False)
    monkeypatch.setenv("DEEPSEEK_API_KEY", "test-key")
    monkeypatch.setenv("DEEPSEEK_THINKING", "true")
    runs_dir = tmp_path / "runs"
    runs_dir.mkdir()
    settings = AgentSettings(
        api_key="not-used", runs_dir=runs_dir, rate_limit_per_minute=1000, session_ttl_hours=24
    )
    monkeypatch.setattr(api, "bootstrap_settings", settings)
    monkeypatch.setattr(
        api,
        "registry",
        api.SessionRegistry(runs_dir, 100, 24, storage=LocalSessionStorage()),
    )
    monkeypatch.setattr(api, "session_storage", LocalSessionStorage())
    api.request_buckets.clear()
    audit_draft._LAST_CALL.clear()
    yield TestClient(api.app)
    audit_draft._LAST_CALL.clear()
    api.request_buckets.clear()


def _upload(client: TestClient, name: str, content: bytes) -> str:
    response = client.post("/api/sessions", files={"file": (name, content, "text/csv")})
    assert response.status_code == 201, response.text
    return response.json()["id"]


def _sample(client: TestClient) -> str:
    response = client.post("/api/sessions/sample?audit=true")
    assert response.status_code == 201
    return response.json()["id"]


def test_returns_filtered_draft_with_honest_metadata(client, control):
    session_id = _sample(client)
    response = client.post(URL.format(session_id))
    assert response.status_code == 200
    body = response.json()
    assert body["draft"] == GOOD
    assert body["mode"] == "direct_no_tools"
    assert body["thinking"] is False
    assert body["model"] == control.settings[0].model
    assert body["rows_sent"] == 21
    assert body["masked"] is False
    assert body["dropped"] == 0
    assert body["elapsed_ms"] >= 0
    assert body["disclaimer"] == audit_draft.DISCLAIMER
    assert "未调用任何工具" in body["disclaimer"]
    assert "test-key" not in response.text


def test_thinking_is_off_per_request_without_touching_global_settings(client, control, monkeypatch):
    session_id = _sample(client)
    client.post(URL.format(session_id))
    sent = control.settings[0]
    assert sent.thinking_enabled is False
    assert sent.timeout_seconds == 60.0
    assert sent.api_key == "test-key"
    # 全局配置不受影响：同一进程里别的请求仍按环境变量得到开启思考。
    assert api._effective_settings().thinking_enabled is True
    assert control.calls[0]["kwargs"] == {"max_tokens": 1024}


def test_prompt_has_raw_table_and_no_hints(client, control):
    session_id = _sample(client)
    client.post(URL.format(session_id))
    prompt = control.calls[0]["messages"][0].content
    assert "order_id,region,category,product,sales,quantity" in prompt
    assert "1001,华东,电子产品,无线耳机,980.5,1" in prompt
    assert prompt.count("\n1001,") == 2
    for hint in ("去重", "DISTINCT", "distinct", "公式", "SUM", "COUNT", "同一订单"):
        assert hint not in prompt
    assert "销售额合计：<数> 元" in prompt
    assert len(control.calls) == 1


def test_tracing_is_disabled_while_the_table_is_sent(client, control, monkeypatch):
    import langsmith._internal._context as ls_context

    monkeypatch.setattr(ls_context, "_GLOBAL_TRACING_ENABLED", True)
    session_id = _sample(client)
    assert client.post(URL.format(session_id)).status_code == 200
    assert control.tracing_seen == [False]


def test_too_many_rows_is_422_and_model_is_not_called(client, control):
    rows = "".join(f"A{i},1.00,1\n" for i in range(301))
    session_id = _upload(client, "big.csv", ("order_id,sales,quantity\n" + rows).encode())
    response = client.post(URL.format(session_id))
    assert response.status_code == 422
    assert "300" in response.json()["detail"]
    assert control.calls == []


def test_exactly_300_rows_is_accepted(client, control):
    rows = "".join(f"A{i},1.00,1\n" for i in range(300))
    session_id = _upload(client, "edge.csv", ("order_id,sales,quantity\n" + rows).encode())
    response = client.post(URL.format(session_id))
    assert response.status_code == 200
    assert response.json()["rows_sent"] == 300


def test_non_csv_session_is_422(client, control):
    response = client.post(
        "/api/sessions",
        files={
            "file": ("data.json", b'[{"order_id": "A", "sales": 1, "quantity": 1}]', "text/json")
        },
    )
    assert response.status_code == 201
    result = client.post(URL.format(response.json()["id"]))
    assert result.status_code == 422
    assert "原始 CSV" in result.json()["detail"]
    assert control.calls == []


def test_unknown_session_is_404(client, control):
    assert client.post(URL.format("api_missing")).status_code == 404


def test_missing_api_key_is_409_with_actionable_hint(client, control, monkeypatch):
    monkeypatch.setattr(api, "_effective_settings", lambda: AgentSettings(api_key=""))
    session_id = _sample(client)
    response = client.post(URL.format(session_id))
    assert response.status_code == 409
    detail = response.json()["detail"]
    assert "API Key" in detail
    assert "DEEPSEEK_API_KEY" in detail
    assert control.calls == []
    # 409 没有真正调用模型，不占用 10 秒窗口。
    assert audit_draft._LAST_CALL == {}


def test_invalid_model_configuration_is_409(client, monkeypatch):
    def refuse(settings):
        raise ValueError("模型名称不能为空，key=sk-secret-123")

    monkeypatch.setattr(audit_draft, "create_chat_model", refuse)
    response = client.post(URL.format(_sample(client)))
    assert response.status_code == 409
    assert "sk-secret-123" not in response.text
    assert "模型配置" in response.json()["detail"]


def test_model_construction_failure_is_502_without_echo(client, monkeypatch):
    def boom(settings):
        raise RuntimeError("InvalidURL sk-secret-123")

    monkeypatch.setattr(audit_draft, "create_chat_model", boom)
    response = client.post(URL.format(_sample(client)))
    assert response.status_code == 502
    assert "sk-secret-123" not in response.text


def test_upstream_error_is_502_fixed_text_and_never_echoed(client, control, caplog):
    control.reply = RuntimeError("401 Unauthorized: invalid key sk-secret-123 at https://x.y")
    response = client.post(URL.format(_sample(client)))
    assert response.status_code == 502
    assert response.json()["detail"] == audit_draft._UPSTREAM_DETAIL
    assert "sk-secret-123" not in response.text
    assert "sk-secret-123" not in caplog.text
    assert "RuntimeError" in caplog.text


def test_timeout_is_504_fixed_text(client, control, monkeypatch):
    monkeypatch.setattr(audit_draft, "_TIMEOUT_SECONDS", 0.05)
    control.block = threading.Event()
    try:
        response = client.post(URL.format(_sample(client)))
    finally:
        control.block.set()
    assert response.status_code == 504
    assert response.json()["detail"] == audit_draft._TIMEOUT_DETAIL


def test_format_drift_lines_are_dropped_and_counted(client, control):
    control.reply = AIMessage(
        content=(
            "好的，我来算一下：\n"
            "销售额合计：17,224.00 元\n"
            "销售额合计：17224.00 元\n"
            "销量合计：约 462 件\n"
            "- 订单数：462 笔\n"
            "销量合计：462 件\n"
            "订单数：21 笔\n"
            "订单数：22 笔\n"
            "\n"
            "以上为结果。"
        )
    )
    body = client.post(URL.format(_sample(client))).json()
    assert body["draft"] == "销售额合计：17224.00 元\n销量合计：462 件\n订单数：21 笔"
    # 好的/千分位/约/列表符号/重复订单数/结尾说明 = 6 行被丢弃
    assert body["dropped"] == 6


def test_every_line_invalid_gives_empty_draft(client, control):
    control.reply = AIMessage(content="抱歉，我无法完成。")
    body = client.post(URL.format(_sample(client))).json()
    assert body["draft"] == ""
    assert body["dropped"] == 1


# dropped 的口径：被丢弃片段内的非空行数（含标签所在行），至少计 1。
@pytest.mark.parametrize(
    ("reply", "expected_dropped"),
    [
        # 开标签行 + 订单数行 + 闭标签行 = 3
        ("<think>先数一数\n订单数：999 笔\n</think>\n" + GOOD, 3),
        ("<thinking>订单数：999 笔</thinking>" + GOOD, 1),
        # 只有闭标签：其前的全部内容都当作思考丢弃（推理行 + 订单数行 + 闭标签行）
        ("推理过程……\n订单数：999 笔\n</think>\n" + GOOD, 3),
        # 只有开标签、没闭合：其后到结尾全部丢弃
        (GOOD + "\n<think>还没写完\n订单数：999 笔", 2),
        ("<THINK></THINK>" + GOOD, 1),
    ],
)
def test_think_tags_and_their_content_are_dropped(client, control, reply, expected_dropped):
    control.reply = AIMessage(content=reply)
    body = client.post(URL.format(_sample(client))).json()
    assert body["draft"] == GOOD
    assert "999" not in body["draft"]
    assert body["dropped"] == expected_dropped


def test_reasoning_content_makes_thinking_flag_true(client, control):
    control.reply = AIMessage(content=GOOD, additional_kwargs={"reasoning_content": "想了一下"})
    body = client.post(URL.format(_sample(client))).json()
    assert body["thinking"] is True
    assert body["draft"] == GOOD


def test_block_list_content_keeps_text_and_counts_other_blocks(client, control):
    control.reply = AIMessage(
        content=[
            {"type": "reasoning", "text": "不要这段"},
            {"type": "text", "text": "销售额合计：1.50 元"},
            "销量合计：3 件",
            {"type": "image_url"},
            {"text": "订单数：2 笔"},
        ]
    )
    body = client.post(URL.format(_sample(client))).json()
    assert body["draft"] == "销售额合计：1.50 元\n销量合计：3 件\n订单数：2 笔"
    assert body["dropped"] == 2


def test_non_text_content_shapes_do_not_crash():
    assert audit_draft.message_text(AIMessage(content="x")) == ("x", 0)
    message = type("Odd", (), {"content": 123})()
    assert audit_draft.message_text(message) == ("", 0)


def test_masking_hits_non_metric_columns_only(client, control):
    csv_text = (
        "order_id,sales,quantity,联系电话,email,note\n"
        "13800138000,10.00,1,13912345678,a@b.com,身份证 11010519491231002X\n"
        "13800138000,5.00,2,,c@d.org,来信 x@y.com\n"
        "13800138001,5.00,2,13912345679,,无\n"
    )
    session_id = _upload(client, "pii.csv", csv_text.encode("utf-8"))
    body = client.post(URL.format(session_id)).json()
    prompt = control.calls[0]["messages"][0].content
    assert body["masked"] is True
    # 联系电话 x2、email x2（按列名遮盖）、note x2（按内容识别）；空单元格不算。
    assert body["masked_cells"] == 6
    assert body["rows_sent"] == 3
    # 指标三列原样保留：订单号恰好长得像手机号也不能被改，否则去重语义变了。
    assert "13800138000,10.00,1," in prompt
    assert "13800138001,5.00,2," in prompt
    for secret in (
        "13912345678",
        "13912345679",
        "a@b.com",
        "c@d.org",
        "x@y.com",
        "11010519491231002X",
    ):
        assert secret not in prompt
    # 非敏感列名的单元格按内容识别，留下可读的脱敏标记。
    assert "[已脱敏-邮箱]" in prompt
    assert "[已脱敏-证件]" in prompt


def test_clean_table_reports_unmasked(client, control):
    body = client.post(URL.format(_sample(client))).json()
    assert body["masked"] is False
    assert body["masked_cells"] == 0


def test_repeat_within_ten_seconds_is_429_then_recovers(client, control, monkeypatch):
    clock = {"now": 1000.0}
    monkeypatch.setattr(audit_draft, "_now", lambda: clock["now"])
    first_id, second_id = _sample(client), _sample(client)
    assert client.post(URL.format(first_id)).status_code == 200

    clock["now"] += 3.2
    again = client.post(URL.format(first_id))
    assert again.status_code == 429
    assert "10 秒" in again.json()["detail"]
    assert again.headers["Retry-After"] == "7"
    # 其他会话不受影响。
    assert client.post(URL.format(second_id)).status_code == 200
    assert len(control.calls) == 2

    clock["now"] += 10.0
    assert client.post(URL.format(first_id)).status_code == 200
    assert len(control.calls) == 3


def test_failed_upstream_call_still_counts_toward_the_window(client, control):
    session_id = _sample(client)
    control.reply = RuntimeError("down")
    assert client.post(URL.format(session_id)).status_code == 502
    control.reply = AIMessage(content=GOOD)
    assert client.post(URL.format(session_id)).status_code == 429


def test_project_rate_limiter_still_applies(client, control, monkeypatch):
    monkeypatch.setattr(api.bootstrap_settings, "rate_limit_per_minute", 3)
    first_id, second_id = _sample(client), _sample(client)  # 占用 2 次写请求配额
    assert client.post(URL.format(first_id)).status_code == 200  # 第 3 次
    blocked = client.post(URL.format(second_id))
    assert blocked.status_code == 429
    assert blocked.json()["detail"] == "请求过于频繁，请稍后再试。"
    assert len(control.calls) == 1


# --- 纯函数与边界 -----------------------------------------------------------


def _write(tmp_path: Path, content: bytes, name: str = "t.csv") -> Path:
    path = tmp_path / name
    path.write_bytes(content)
    return path


def test_load_table_reads_gb18030_and_bom(tmp_path):
    text = "order_id,sales,quantity,备注\nA,1.00,1,华东\n"
    header, rows = audit_draft.load_table(_write(tmp_path, text.encode("gb18030")))
    assert header[-1] == "备注" and rows == [["A", "1.00", "1", "华东"]]
    header, _ = audit_draft.load_table(_write(tmp_path, text.encode("utf-8-sig"), "b.csv"))
    assert header[0] == "order_id"


def test_load_table_skips_blank_lines_and_keeps_cells_verbatim(tmp_path):
    content = b'order_id,sales,quantity\n\n"A ",1.00,1\n\n'
    _, rows = audit_draft.load_table(_write(tmp_path, content))
    assert rows == [["A ", "1.00", "1"]]


@pytest.mark.parametrize(
    ("content", "message"),
    [
        (b"\xff\xfe\x00\xd8garbage\x81", "编码"),
        (b"order_id,sales\nA,1\n", "order_id、sales、quantity"),
        (b"order_id,sales,quantity,sales\nA,1,1,1\n", "order_id、sales、quantity"),
        (b"order_id,sales,quantity\nA,1.00\n", "列数"),
        (b'order_id,sales,quantity\n"A,1.00,1\n', "引号"),
        (b"order_id,sales,quantity\n\n\n", "至少需要一条"),
        (b"", "order_id、sales、quantity"),
    ],
)
def test_load_table_rejects_bad_files(tmp_path, content, message):
    with pytest.raises(audit_draft.QuickDraftError, match=message) as info:
        audit_draft.load_table(_write(tmp_path, content))
    assert info.value.status_code == 422


def test_load_table_rejects_oversize_and_missing_files(tmp_path, monkeypatch):
    monkeypatch.setattr(audit_draft, "_MAX_BYTES", 50)
    big = _write(tmp_path, b"order_id,sales,quantity\n" + b"A,1.00,1\n" * 20)
    with pytest.raises(audit_draft.QuickDraftError, match="体积"):
        audit_draft.load_table(big)
    with pytest.raises(audit_draft.QuickDraftError, match="无法读取"):
        audit_draft.load_table(tmp_path / "gone.csv")


def test_resolve_source_requires_csv_inside_input_dir(tmp_path):
    inside = tmp_path / "input"
    inside.mkdir()
    outside = tmp_path / "other.csv"
    outside.write_text("x")
    good = inside / "a.csv"
    good.write_text("x")

    class Workspace:
        def __init__(self, source):
            self.source_path = source
            self.input_dir = inside

    assert audit_draft.resolve_source(Workspace(good)) == good
    for bad in (None, outside, inside / "a.xlsx"):
        with pytest.raises(audit_draft.QuickDraftError) as info:
            audit_draft.resolve_source(Workspace(bad))
        assert info.value.status_code == 422


def test_parse_draft_accepts_documented_shapes_only():
    ok = "销售额合计：-3.50 元\n销量合计：0 件\n订单数：12 笔"
    assert audit_draft.parse_draft(ok) == (ok, 0)
    for bad in (
        "销售额合计：3.555 元",
        "销售额合计：3. 元",
        "销量合计：1.5 件",
        "订单数：一二 笔",
        "销售额合计：1 万元",
        " 订单数：5 笔 额外",
        "订单数: 5 笔",
    ):
        assert audit_draft.parse_draft(bad) == ("", 1)
    # 行首尾空白无害，会被去掉，内容本身不改。
    assert audit_draft.parse_draft("  订单数：5 笔  ") == ("订单数：5 笔", 0)
