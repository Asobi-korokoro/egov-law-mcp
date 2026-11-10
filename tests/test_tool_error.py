"""e-Gov API の HTTP エラーが ToolError としてモデルに伝わることのテスト(ネットに出ない)。

egov_law.fetch は HTTP エラーで RuntimeError を投げる。ToolError 以外の例外は、モデルには
「Error executing tool <name>」としか伝わらないため、server.py が境界で ToolError に変える。
"""

import asyncio

import pytest

import server
from mcp_common import ToolError

HTTP_404 = "HTTP 404 for https://laws.e-gov.go.jp/api/2/law_data/NOSUCHID\nnot found"


def _raise_runtime(*args, **kwargs):
    raise RuntimeError(HTTP_404)


def _call_tool(name, arguments):
    return asyncio.run(server.mcp.call_tool(name, arguments))


def test_get_law_article_http_error_becomes_tool_error(monkeypatch):
    monkeypatch.setattr(server.egov_law, "get_law", _raise_runtime)
    with pytest.raises(ToolError) as excinfo:
        _call_tool("get_law_article", {"law_id": "NOSUCHID"})
    assert "HTTP 404" in str(excinfo.value)
    assert "NOSUCHID" in str(excinfo.value)


def test_search_law_title_search_http_error_becomes_tool_error(monkeypatch):
    monkeypatch.setattr(server.egov_law, "search_by_title", _raise_runtime)
    with pytest.raises(ToolError) as excinfo:
        _call_tool("search_law", {"keyword": "毒物"})
    assert "HTTP 404" in str(excinfo.value)


def test_search_law_keyword_search_http_error_becomes_tool_error(monkeypatch):
    # 法令名一致が 0 件 -> 本文全文検索(search_keyword)に進み、そこで HTTP エラー
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": []})
    monkeypatch.setattr(server.egov_law, "search_keyword", _raise_runtime)
    with pytest.raises(ToolError) as excinfo:
        _call_tool("search_law", {"keyword": "毒物"})
    assert "HTTP 404" in str(excinfo.value)


def test_tool_error_message_has_no_credentials(monkeypatch):
    monkeypatch.setattr(server.egov_law, "get_law", _raise_runtime)
    with pytest.raises(ToolError) as excinfo:
        _call_tool("get_law_article", {"law_id": "NOSUCHID"})
    assert "x-api-key" not in str(excinfo.value).lower()


def test_other_exceptions_are_not_converted_by_server(monkeypatch):
    """RuntimeError 以外(想定外の不具合)は server.py では変換しない
    (境界で変換するのは、ツールから到達する raise=fetch の HTTP エラーだけ)。"""

    def boom(*args, **kwargs):
        raise KeyError("unexpected")

    monkeypatch.setattr(server.egov_law, "get_law", boom)
    with pytest.raises(KeyError):
        server._call_egov(server.egov_law.get_law, "X")
