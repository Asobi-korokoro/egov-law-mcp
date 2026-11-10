"""MCPサーバー共通の認証・起動ヘルパー(x-api-key ヘッダー認証)。

方針:
    - 認証は URL パスではなくヘッダー `x-api-key`(Claude.ai コネクタの Request headers)。
    - アプリは `mcp.run(...)` ではなく `MCPServer.streamable_http_app()` で組み立て、
      純 ASGI のミドルウェア(HeaderAuthMiddleware)で包んで uvicorn に渡す。
      Starlette の `Mount` には入れない(Mount は中のアプリの lifespan を走らせず、
      session manager が起動しないので全リクエストが 500 になる)。
    - 起動時にキーが空・短い場合は起動失敗にする(フェイルクローズ)。値も長さも出力しない。

各サーバーの末尾は `if __name__ == "__main__": serve(mcp)` の1行になる。

このモジュールが持つのは認証と起動の配線だけ。ツールのロジックは一切持たない。
"""
from __future__ import annotations

import hmac
import json
import os
import sys
from collections.abc import Collection, Sequence
from typing import NoReturn

from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.server.transport_security import TransportSecuritySettings

__all__ = [
    "MIN_KEY_LENGTH",
    "ToolError",
    "HeaderAuthMiddleware",
    "build_app",
    "read_secret_env",
    "serve",
]

# キーの最短長。短いキーや手打ちの値を、起動時に止めるための下限。
MIN_KEY_LENGTH = 32


def _fail(message: str) -> NoReturn:
    print(message, file=sys.stderr)
    raise SystemExit(1)


def _clean_env(name: str) -> str:
    # 前後の空白と先頭の UTF-8 BOM を落とす(環境変数の貼り付けで混入しがち)。
    # Windows PowerShell 5.1 経由で Secret に BOM が混入した実例がある。
    return os.environ.get(name, "").strip().lstrip(chr(0xFEFF)).strip()


def read_secret_env(name: str) -> str:
    """環境変数を strip・BOM 除去して返す。空・未設定なら stderr に理由を出して終了する。

    値そのものは出力しない(名前だけ)。
    """
    value = _clean_env(name)
    if not value:
        _fail(f"{name} が未設定")
    return value


def _require_min_length(name: str, value: str) -> None:
    # 長さも値もエラーに出さない(下限だけ書く)。
    if len(value) < MIN_KEY_LENGTH:
        _fail(f"{name} が短すぎる({MIN_KEY_LENGTH}文字以上が必要)")


class HeaderAuthMiddleware:
    """`x-api-key` ヘッダーで認証する純 ASGI ミドルウェア。

    - http 以外のスコープ(lifespan・websocket)は素通し(lifespan を止めない)。
    - キーは bytes のまま hmac.compare_digest で比較する(非 ASCII のヘッダー値でも落ちない)。
    - キーは複数受け付ける(将来のキー入れ替えで、旧キーと新キーを重ねるため)。
      どのキーと一致したかで処理時間が変わらないよう、全キーと比較する。
    - 不一致・欠落は 401 の JSON。`WWW-Authenticate` は付けない(付けると Claude.ai が
      OAuth の探索を始める)。
    - 免除するのは `/health`(Cloud Run の確認用)と `/.well-known/` 前方一致だけ。
      後者は認証せずに素通しして、アプリ側の 404 をそのまま返す(401 にすると OAuth 探索を誘う)。
    """

    def __init__(
        self,
        app,
        *,
        keys: Sequence[str],
        header: str = "x-api-key",
        exempt_paths: Collection[str] = ("/health",),
        exempt_prefixes: Collection[str] = ("/.well-known/",),
    ) -> None:
        if not keys:
            _fail("認証キーが1つも指定されていない")
        for key in keys:
            if len(key) < MIN_KEY_LENGTH:
                _fail(f"認証キーが短すぎる({MIN_KEY_LENGTH}文字以上が必要)")
        self.app = app
        self._keys = [key.encode("utf-8") for key in keys]
        self._header = header.lower().encode("latin-1")
        self._exempt_paths = frozenset(exempt_paths)
        self._exempt_prefixes = tuple(exempt_prefixes)

    def _authorized(self, scope) -> bool:
        provided = None
        for name, value in scope.get("headers", []):
            if name == self._header:
                provided = value
                break
        if provided is None:
            return False
        matched = False
        for key in self._keys:
            if hmac.compare_digest(provided, key):
                matched = True
        return matched

    async def __call__(self, scope, receive, send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self._exempt_paths or path.startswith(self._exempt_prefixes):
            await self.app(scope, receive, send)
            return

        if self._authorized(scope):
            await self.app(scope, receive, send)
            return

        body = json.dumps({"error": "unauthorized"}).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": 401,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def build_app(
    mcp: MCPServer,
    *,
    keys: Sequence[str],
    path: str = "/mcp",
    json_response: bool = False,
):
    """`/health` を登録し、streamable_http_app をヘッダー認証で包んだ ASGI アプリを返す。

    TestClient でそのまま試験できる。同じ mcp で2回呼ぶと /health が二重に登録される
    (先に登録した方が使われるので害は無いが、1つの mcp につき1回にすること)。
    """
    from starlette.requests import Request
    from starlette.responses import JSONResponse

    @mcp.custom_route("/health", methods=["GET"])
    async def _health(request: Request) -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app = mcp.streamable_http_app(
        streamable_http_path=path,
        stateless_http=True,
        json_response=json_response,
        # DNS rebinding 保護は無効にする: 既定のままだと localhost 以外の Host を全拒否する。
        # Cloud Run の手前が Host を制御しており、守りはヘッダーキーで行う(公式もこの構成では無効が正直な設定)。
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )
    return HeaderAuthMiddleware(app, keys=keys)


def serve(
    mcp: MCPServer,
    *,
    key_env: str = "MCP_API_KEY",
    previous_key_env: str = "MCP_API_KEY_PREVIOUS",
    path: str = "/mcp",
    json_response: bool = False,
) -> None:
    """環境変数からキーを読み、uvicorn でサーバーを起動する(戻らない)。

    key_env は必須(空・31文字以下なら起動失敗)。previous_key_env は将来のキー入れ替え用で、
    設定されている(空でない)ときだけ読んで追加のキーとして受け付ける。
    """
    import uvicorn

    keys = [read_secret_env(key_env)]
    _require_min_length(key_env, keys[0])

    previous = _clean_env(previous_key_env)
    if previous:
        _require_min_length(previous_key_env, previous)
        keys.append(previous)

    app = build_app(mcp, keys=keys, path=path, json_response=json_response)
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8080")))
