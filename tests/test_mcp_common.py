"""mcp_common の単体試験(TestClient。ネットワーク・実キーは使わない)。

キーは全てこの試験用のダミー(32文字以上)。
"""
import json

import pytest
from starlette.testclient import TestClient

import mcp_common
from mcp.server.mcpserver import MCPServer
from mcp_common import HeaderAuthMiddleware, ToolError, build_app, read_secret_env, serve

KEY = "test-key-" + "a" * 30  # 39文字
OLD_KEY = "old-key-" + "b" * 30
ACCEPT = "application/json, text/event-stream"


def make_server() -> MCPServer:
    mcp = MCPServer("test-server")

    @mcp.tool()
    def echo(text: str) -> str:
        """text をそのまま返す。"""
        return text

    @mcp.tool()
    def fail() -> str:
        """ToolError を送出する。"""
        raise ToolError("理由が見える")

    return mcp


@pytest.fixture
def app():
    return build_app(make_server(), keys=[KEY])


@pytest.fixture
def client(app):
    # with で lifespan(session manager)を走らせる。
    with TestClient(app) as c:
        yield c


def rpc(client, method, params=None, *, key=KEY, id_=1):
    headers = {"Accept": ACCEPT, "Content-Type": "application/json"}
    if key is not None:
        headers["x-api-key"] = key
    body = {"jsonrpc": "2.0", "id": id_, "method": method}
    if params is not None:
        body["params"] = params
    return client.post("/mcp", headers=headers, content=json.dumps(body))


def result_of(response):
    """JSON でも SSE でも、JSON-RPC の応答オブジェクトを返す。"""
    assert response.status_code == 200, response.text
    text = response.text
    if response.headers["content-type"].startswith("text/event-stream"):
        datas = [ln[len("data:"):].strip() for ln in text.splitlines() if ln.startswith("data:")]
        return json.loads(datas[-1])
    return json.loads(text)


INIT = {
    "protocolVersion": "2025-03-26",
    "capabilities": {},
    "clientInfo": {"name": "pytest", "version": "0"},
}


# --- 認証 -------------------------------------------------------------------

def test_no_key_is_401_without_www_authenticate(client):
    r = rpc(client, "initialize", INIT, key=None)
    assert r.status_code == 401
    assert r.json() == {"error": "unauthorized"}
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_wrong_key_is_401(client):
    r = rpc(client, "initialize", INIT, key="x" * 39)
    assert r.status_code == 401
    assert "www-authenticate" not in {k.lower() for k in r.headers}


def test_key_of_different_length_is_401(client):
    assert rpc(client, "initialize", INIT, key=KEY[:-1]).status_code == 401
    assert rpc(client, "initialize", INIT, key=KEY + "z").status_code == 401


def test_correct_key_passes(client):
    r = rpc(client, "initialize", INIT)
    assert result_of(r)["result"]["serverInfo"]["name"] == "test-server"


def test_previous_key_passes():
    app = build_app(make_server(), keys=[KEY, OLD_KEY])
    with TestClient(app) as c:
        assert rpc(c, "initialize", INIT, key=KEY).status_code == 200
        assert rpc(c, "initialize", INIT, key=OLD_KEY).status_code == 200
        assert rpc(c, "initialize", INIT, key="z" * 39).status_code == 401


def test_non_ascii_header_value_is_401_not_500(client):
    # ヘッダーは bytes のまま比較するので、latin-1 範囲の非 ASCII でも 401 になる。
    r = client.post(
        "/mcp",
        headers=[
            (b"accept", ACCEPT.encode()),
            (b"content-type", b"application/json"),
            (b"x-api-key", "鍵".encode("utf-8")),
            (b"x-api-key-dummy", b"\xff\xfe"),
        ],
        content=b"{}",
    )
    assert r.status_code == 401


def test_non_ascii_bytes_directly_via_asgi():
    """TestClient 経由では作れない生の bytes(0xFF 等)も、ミドルウェアは 401 にする。"""
    import asyncio

    sent = []

    async def inner(scope, receive, send):  # 呼ばれてはいけない
        raise AssertionError("認証を通ってしまった")

    mw = HeaderAuthMiddleware(inner, keys=[KEY])

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {"type": "http", "path": "/mcp", "headers": [(b"x-api-key", b"\xff\xfe\x80")]}
    asyncio.run(mw(scope, receive, send))
    assert sent[0]["status"] == 401


def test_only_first_header_is_used():
    """x-api-key が複数あっても、最初の1つだけで判定する(2つ目に正キーを置いても通らない)。"""
    import asyncio

    sent = []

    async def inner(scope, receive, send):
        raise AssertionError("認証を通ってしまった")

    mw = HeaderAuthMiddleware(inner, keys=[KEY])

    async def send(message):
        sent.append(message)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    scope = {
        "type": "http",
        "path": "/mcp",
        "headers": [(b"x-api-key", b"wrong"), (b"x-api-key", KEY.encode())],
    }
    asyncio.run(mw(scope, receive, send))
    assert sent[0]["status"] == 401


# --- 免除パス ---------------------------------------------------------------

@pytest.mark.parametrize(
    "path",
    [
        "/.well-known/oauth-authorization-server",
        "/.well-known/oauth-protected-resource",
        "/.well-known/oauth-protected-resource/mcp",
    ],
)
def test_well_known_is_404_not_401(client, path):
    assert client.get(path).status_code == 404


@pytest.mark.parametrize("path", ["/authorize", "/register", "/token"])
def test_oauth_endpoints_are_401_without_key(client, path):
    # 免除は /health と /.well-known/ だけ。他の経路は(存在しなくても)キーが要る。
    assert client.get(path).status_code == 401


def test_health_is_200_without_key(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json() == {"status": "ok"}


def test_exempt_is_prefix_only_for_well_known(client):
    # "/.well-known" 単体(末尾スラッシュなし)や "/healthz" は免除されない。
    assert client.get("/.well-known").status_code == 401
    assert client.get("/healthz").status_code == 401


# --- Host 検証 ---------------------------------------------------------------

def test_arbitrary_host_header_is_accepted(client):
    # streamable_http_app() の既定は localhost 以外の Host を全拒否(421)する。
    # build_app は DNS rebinding 保護を明示的に無効にしているので、Cloud Run の Host でも通る。
    headers = {"Accept": ACCEPT, "Content-Type": "application/json", "x-api-key": KEY,
               "Host": "example-abc123-an.a.run.app"}
    body = json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": INIT})
    r = client.post("/mcp", headers=headers, content=body)
    assert r.status_code == 200


# --- lifespan・ツール -------------------------------------------------------

def test_lifespan_runs_initialize_then_tools_list(client):
    assert rpc(client, "initialize", INIT).status_code == 200
    r = rpc(client, "tools/list", id_=2)
    names = sorted(t["name"] for t in result_of(r)["result"]["tools"])
    assert names == ["echo", "fail"]


def test_tool_call_and_toolerror_message(client):
    ok = result_of(rpc(client, "tools/call", {"name": "echo", "arguments": {"text": "hi"}}, id_=3))
    assert ok["result"]["content"][0]["text"] == "hi"
    err = result_of(rpc(client, "tools/call", {"name": "fail", "arguments": {}}, id_=4))
    assert err["result"]["isError"] is True
    assert "理由が見える" in err["result"]["content"][0]["text"]


def test_non_http_scope_passes_through():
    """lifespan など http 以外のスコープは、キー無しでもそのまま内側に渡る。"""
    import asyncio

    seen = []

    async def inner(scope, receive, send):
        seen.append(scope["type"])

    mw = HeaderAuthMiddleware(inner, keys=[KEY])
    asyncio.run(mw({"type": "lifespan"}, None, None))
    asyncio.run(mw({"type": "websocket", "path": "/mcp", "headers": []}, None, None))
    assert seen == ["lifespan", "websocket"]


def test_toolerror_is_reexported():
    from mcp.server.mcpserver.exceptions import ToolError as Official

    assert mcp_common.ToolError is Official


# --- 起動時の検査(フェイルクローズ) -----------------------------------------

def test_read_secret_env_strips_and_removes_bom(monkeypatch):
    monkeypatch.setenv("SOME_KEY", "﻿  " + KEY + "  \n")
    assert read_secret_env("SOME_KEY") == KEY


@pytest.mark.parametrize("value", [None, "", "   ", "﻿", "﻿ \n"])
def test_read_secret_env_empty_exits(monkeypatch, capsys, value):
    if value is None:
        monkeypatch.delenv("SOME_KEY", raising=False)
    else:
        monkeypatch.setenv("SOME_KEY", value)
    with pytest.raises(SystemExit) as exc:
        read_secret_env("SOME_KEY")
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert "SOME_KEY" in err and "未設定" in err


@pytest.fixture
def captured_uvicorn(monkeypatch):
    """uvicorn.run を差し替えて、渡されたアプリと引数を記録する(実際には起動しない)。"""
    import uvicorn

    calls = {}

    def fake_run(app, **kwargs):
        calls["app"] = app
        calls["kwargs"] = kwargs

    monkeypatch.setattr(uvicorn, "run", fake_run)
    monkeypatch.delenv("MCP_API_KEY", raising=False)
    monkeypatch.delenv("MCP_API_KEY_PREVIOUS", raising=False)
    monkeypatch.delenv("PORT", raising=False)
    return calls


def test_serve_starts_without_previous_key(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("MCP_API_KEY", KEY)
    serve(make_server())
    assert captured_uvicorn["kwargs"] == {"host": "0.0.0.0", "port": 8080}
    with TestClient(captured_uvicorn["app"]) as c:
        assert rpc(c, "initialize", INIT).status_code == 200
        assert rpc(c, "initialize", INIT, key=OLD_KEY).status_code == 401


def test_serve_accepts_previous_key_when_set(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("MCP_API_KEY", KEY)
    monkeypatch.setenv("MCP_API_KEY_PREVIOUS", OLD_KEY)
    monkeypatch.setenv("PORT", "9999")
    serve(make_server())
    assert captured_uvicorn["kwargs"]["port"] == 9999
    with TestClient(captured_uvicorn["app"]) as c:
        assert rpc(c, "initialize", INIT, key=KEY).status_code == 200
        assert rpc(c, "initialize", INIT, key=OLD_KEY).status_code == 200


def test_serve_ignores_blank_previous_key(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("MCP_API_KEY", KEY)
    monkeypatch.setenv("MCP_API_KEY_PREVIOUS", "  ")
    serve(make_server())
    assert "app" in captured_uvicorn


def test_serve_custom_env_names(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("MY_KEY", KEY)
    serve(make_server(), key_env="MY_KEY")
    with TestClient(captured_uvicorn["app"]) as c:
        assert rpc(c, "initialize", INIT).status_code == 200


def test_serve_empty_key_fails(monkeypatch, capsys, captured_uvicorn):
    monkeypatch.setenv("MCP_API_KEY", "")
    with pytest.raises(SystemExit) as exc:
        serve(make_server())
    assert exc.value.code == 1
    assert "app" not in captured_uvicorn
    assert "MCP_API_KEY" in capsys.readouterr().err


def test_serve_unset_key_fails(capsys, captured_uvicorn):
    with pytest.raises(SystemExit):
        serve(make_server())
    assert "app" not in captured_uvicorn


@pytest.mark.parametrize("env_name", ["MCP_API_KEY", "MCP_API_KEY_PREVIOUS"])
def test_serve_31_char_key_fails_without_leaking_value(monkeypatch, capsys, captured_uvicorn, env_name):
    short = "s3cr3t" + "x" * 25  # 31文字
    assert len(short) == 31
    monkeypatch.setenv("MCP_API_KEY", KEY)
    monkeypatch.setenv(env_name, short)
    with pytest.raises(SystemExit) as exc:
        serve(make_server())
    assert exc.value.code == 1
    assert "app" not in captured_uvicorn
    out = capsys.readouterr()
    assert env_name in out.err
    assert short not in out.out + out.err
    assert "31" not in out.out + out.err  # 長さも出さない
    assert "s3cr3t" not in out.out + out.err


def test_serve_32_char_key_is_accepted(monkeypatch, captured_uvicorn):
    monkeypatch.setenv("MCP_API_KEY", "k" * 32)
    serve(make_server())
    assert "app" in captured_uvicorn


def test_middleware_rejects_short_or_empty_keys(capsys):
    async def inner(scope, receive, send):
        pass

    for keys in ([], [""], ["k" * 31], [KEY, "k" * 31]):
        with pytest.raises(SystemExit) as exc:
            HeaderAuthMiddleware(inner, keys=keys)
        assert exc.value.code == 1
    out = capsys.readouterr()
    assert "kkkk" not in out.out + out.err
