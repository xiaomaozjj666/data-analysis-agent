"""联网搜索：解析、跳转展开、白名单重定向与工具注册。"""

from __future__ import annotations

import json

import pytest

from data_agent import web_search

_FIXTURE = """
<div class="result results_links results_links_deep web-result">
  <a rel="nofollow" class="result__a" href="//duckduckgo.com/l/?uddg=https%3A%2F%2Fexample.com%2Freport&amp;rut=x">行业&lt;基准&gt;报告</a>
  <a class="result__snippet" href="#">2026 年<strong>行业</strong>基准：中位数 12.3%</a>
</div>
<div class="result results_links results_links_deep web-result">
  <a rel="nofollow" class="result__a" href="https://stats.example.org/data">公开统计数据</a>
  <a class="result__snippet" href="#">季度口径说明</a>
</div>
"""


class _Response:
    def __init__(self, text: str, status_code: int = 200, headers: dict | None = None):
        self.text = text
        self.status_code = status_code
        self.headers = headers or {}


def test_search_parses_results_and_unwraps_redirects(monkeypatch):
    monkeypatch.setattr(web_search.httpx, "get", lambda *args, **kwargs: _Response(_FIXTURE))
    payload = web_search.search_web("行业 基准", limit=2)
    assert payload["source"] == "duckduckgo"
    assert payload["results"][0]["title"] == "行业<基准>报告"
    assert payload["results"][0]["url"] == "https://example.com/report"
    assert "中位数 12.3%" in payload["results"][0]["snippet"]
    assert payload["results"][1]["url"] == "https://stats.example.org/data"
    assert "外部数据" in payload["note"]


def test_search_rejects_bad_queries_and_http_errors(monkeypatch):
    with pytest.raises(ValueError, match="至少需要 2"):
        web_search.search_web("x")
    with pytest.raises(ValueError, match="不能超过 200"):
        web_search.search_web("x" * 201)

    monkeypatch.setattr(web_search.httpx, "get", lambda *args, **kwargs: _Response("", 503))
    with pytest.raises(ValueError, match="HTTP 503"):
        web_search.search_web("ok query")


def test_search_blocks_redirect_to_other_host(monkeypatch):
    monkeypatch.setattr(
        web_search.httpx,
        "get",
        lambda *args, **kwargs: _Response("", 302, {"location": "http://127.0.0.1/admin"}),
    )
    with pytest.raises(ValueError, match="非白名单主机"):
        web_search.search_web("ok query")


def test_search_without_parsed_results_is_explicit(monkeypatch):
    monkeypatch.setattr(web_search.httpx, "get", lambda *args, **kwargs: _Response("<html></html>"))
    payload = web_search.search_web("ok query")
    assert payload["results"] == []
    assert "不要" in payload["note"]


def test_web_search_tool_registered(workspace, monkeypatch):
    from data_agent.tools.builder import build_tools

    monkeypatch.setattr(web_search.httpx, "get", lambda *args, **kwargs: _Response(_FIXTURE))
    tools = {tool.name: tool for tool in build_tools(workspace)}
    assert "web_search" in tools
    assert "untrusted" in tools["web_search"].description.lower()
    payload = json.loads(tools["web_search"].invoke({"query": "行业 基准", "limit": 2}))
    assert len(payload["results"]) == 2


def test_parse_skips_unusable_blocks_and_follows_same_host_redirect(monkeypatch):
    page = (
        '<div class="result results_links">no link</div>'
        '<div class="result results_links"><a class="result__a" href="ftp://x">bad</a></div>'
    )
    assert web_search._parse_results(page, 5) == []

    responses = [
        _Response("", 302, {"location": "https://html.duckduckgo.com/html/?q=ok"}),
        _Response(_FIXTURE),
    ]
    monkeypatch.setattr(web_search.httpx, "get", lambda *args, **kwargs: responses.pop(0))
    payload = web_search.search_web("ok query", limit=1)
    assert len(payload["results"]) == 1
