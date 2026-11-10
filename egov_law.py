"""e-Gov 法令API Version 2 の薄いクライアント。

- APIキー不要 / 登録不要
- 取得結果は SQLite にキャッシュ（条文は滅多に変わらないので既定TTLは長め）
- 出典表示義務あり: e-Gov法令検索の利用規約(公共データ利用規約 第1.0版 = PDL1.0)

使い方:
    python egov_law.py --probe                 # 実機のエンドポイント一覧を確認
    python egov_law.py --search 毒物及び劇物取締法
    python egov_law.py --law 325AC0000000303   # 法令全文(生データ)
    python egov_law.py --types                 # 法令種別コード

標準ライブラリのみ。依存なし。
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

BASE = "https://laws.e-gov.go.jp/api/2"
SPEC_URL = f"{BASE}/swagger-ui/lawapi-v2.yaml"

SOURCE_NOTE = "出典：e-Gov法令検索（デジタル庁）（https://laws.e-gov.go.jp/）／本ツールによる整形・抜粋（加工）を含む"

# v2 のパスは --probe で実機確認してから確定させること。
# 下記は現時点の想定値。probe の出力と食い違ったらここを直す。
ENDPOINTS = {
    "laws": "/laws",            # 法令一覧（種別で絞る）
    "keyword": "/keyword",      # 条文横断キーワード検索
    "law_data": "/law_data",    # /law_data/{law_id} 全文
    "law_revisions": "/law_revisions",  # /law_revisions/{law_id} 改正履歴
}

CACHE_PATH = Path(__file__).with_name("egov_cache.sqlite3")
DEFAULT_TTL = timedelta(days=30)
MIN_INTERVAL_SEC = 0.5  # 連打しない

_last_call = 0.0


# --------------------------------------------------------------------------
# キャッシュ
# --------------------------------------------------------------------------
def _conn(path: Path | None = None) -> sqlite3.Connection:
    # path の既定値を CACHE_PATH にしない: デフォルト引数はこの関数の定義時に
    # 一度だけ評価されるため、モジュールの CACHE_PATH を後から差し替えても
    # (テストで monkeypatch した場合など)反映されない。ここで都度読む。
    con = sqlite3.connect(path or CACHE_PATH)
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS cache (
            url        TEXT PRIMARY KEY,
            fetched_at TEXT NOT NULL,
            status     INTEGER NOT NULL,
            body       BLOB NOT NULL
        )
        """
    )
    return con


def _cache_get(url: str, ttl: timedelta) -> bytes | None:
    with _conn() as con:
        row = con.execute(
            "SELECT fetched_at, body FROM cache WHERE url = ?", (url,)
        ).fetchone()
    if row is None:
        return None
    fetched_at = datetime.fromisoformat(row[0])
    if datetime.now(timezone.utc) - fetched_at > ttl:
        return None
    return row[1]


def _cache_put(url: str, status: int, body: bytes) -> None:
    with _conn() as con:
        con.execute(
            "INSERT OR REPLACE INTO cache (url, fetched_at, status, body) VALUES (?, ?, ?, ?)",
            (url, datetime.now(timezone.utc).isoformat(), status, body),
        )


# --------------------------------------------------------------------------
# HTTP
# --------------------------------------------------------------------------
def fetch(url: str, ttl: timedelta | None = None, use_cache: bool = True) -> bytes:
    """GET して bytes を返す。キャッシュヒット時はネットに出ない。"""
    global _last_call

    if use_cache:
        cached = _cache_get(url, ttl if ttl is not None else DEFAULT_TTL)
        if cached is not None:
            return cached

    wait = MIN_INTERVAL_SEC - (time.monotonic() - _last_call)
    if wait > 0:
        time.sleep(wait)

    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/json",
            "User-Agent": "egov-law-mcp/0.1 (unofficial client)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as res:
            body = res.read()
            status = res.status
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"HTTP {e.code} for {url}\n{e.read()[:400].decode(errors='replace')}") from e
    except TimeoutError as e:
        raise RuntimeError(f"timeout for {url}: {e}") from e
    except urllib.error.URLError as e:
        raise RuntimeError(f"network error for {url}: {e.reason}") from e
    finally:
        _last_call = time.monotonic()

    _cache_put(url, status, body)
    return body


def get_json(path: str, **params) -> dict:
    query = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}{path}" + (f"?{query}" if query else "")
    raw = fetch(url)
    return json.loads(raw.decode("utf-8"))


# --------------------------------------------------------------------------
# 各操作
# --------------------------------------------------------------------------
def probe() -> str:
    """OpenAPI 仕様(YAML)を取得し、パスらしき行だけ抜き出す。

    yaml パーサを入れずに済ませるための雑な方法。正確に読みたければ
    取得した仕様書をそのままエディタなり Claude なりに渡す。
    """
    raw = fetch(SPEC_URL, ttl=timedelta(days=7)).decode("utf-8")
    Path(__file__).with_name("lawapi-v2.yaml").write_text(raw, encoding="utf-8")
    lines = [
        line.rstrip()
        for line in raw.splitlines()
        if line.startswith("  /") and line.rstrip().endswith(":")
    ]
    return "\n".join(lines) if lines else "(パス行を抽出できず。lawapi-v2.yaml を直接見ること)"


def search_keyword(keyword: str, limit: int = 20, sentences_limit: int | None = None) -> dict:
    """条文横断のキーワード検索(法令本文の全文検索。/keyword)。

    注意: limit は「返る法令の件数」ではなく「sentences(本文中の
    ヒット箇所)の総数の上限」(公式仕様で確認済み)。1つの法令が本文中に
    何度もキーワードを含むと、それだけでlimitを消費し他の法令が
    結果に出てこないことがある。法令の異なり数を増やしたい場合は
    sentences_limit(1法令あたりのヒット件数の上限)を小さく
    (例: 1)指定し、その分 limit を大きめにすること。
    """
    return get_json(
        ENDPOINTS["keyword"], keyword=keyword, limit=limit, sentences_limit=sentences_limit
    )


def search_by_title(title: str, limit: int = 20) -> dict:
    """法令名・法令略称の部分一致で法令を検索する(/laws の law_title)。

    本文は見ず、法令名(law_title)・略称(abbrev)だけを対象に部分一致する
    (公式仕様で確認済み)。正式名称を知らなくても「毒物」「安衛法」
    「化審法」のような通称で目的の法令を見つけやすい。
    """
    return get_json(ENDPOINTS["laws"], law_title=title, limit=limit)


def list_laws(law_type: str | None = None, limit: int = 100) -> dict:
    """法令一覧。law_type 例: Act(法律) / CabinetOrder(政令) / MinisterialOrdinance(省令)"""
    return get_json(ENDPOINTS["laws"], law_type=law_type, limit=limit)


def get_law(law_id: str) -> dict:
    """法令全文。law_id は検索結果に含まれるもの（例: 民法 129AC0000000089）。"""
    return get_json(f"{ENDPOINTS['law_data']}/{urllib.parse.quote(law_id)}")


def get_revisions(law_id: str) -> dict:
    """改正履歴と施行日。「いつ時点の条文か」を確定させたい時に使う。"""
    return get_json(f"{ENDPOINTS['law_revisions']}/{urllib.parse.quote(law_id)}")


# --------------------------------------------------------------------------
def main() -> int:
    p = argparse.ArgumentParser(description="e-Gov 法令API v2 クライアント")
    p.add_argument("--probe", action="store_true", help="実機のエンドポイント一覧を表示")
    p.add_argument("--search", metavar="KEYWORD", help="キーワード検索")
    p.add_argument("--laws", action="store_true", help="法令一覧")
    p.add_argument("--type", metavar="LAW_TYPE", help="--laws の絞り込み種別")
    p.add_argument("--law", metavar="LAW_ID", help="法令全文を取得")
    p.add_argument("--revisions", metavar="LAW_ID", help="改正履歴を取得")
    p.add_argument("--limit", type=int, default=20)
    args = p.parse_args()

    try:
        if args.probe:
            print(probe())
        elif args.search:
            print(json.dumps(search_keyword(args.search, args.limit), ensure_ascii=False, indent=2)[:4000])
        elif args.laws:
            print(json.dumps(list_laws(args.type, args.limit), ensure_ascii=False, indent=2)[:4000])
        elif args.law:
            print(json.dumps(get_law(args.law), ensure_ascii=False, indent=2)[:4000])
        elif args.revisions:
            print(json.dumps(get_revisions(args.revisions), ensure_ascii=False, indent=2)[:4000])
        else:
            p.print_help()
            return 0
    except RuntimeError as e:
        print(e, file=sys.stderr)
        print("\n--probe で実際のパスを確認して ENDPOINTS を直すこと。", file=sys.stderr)
        return 1

    print(f"\n{SOURCE_NOTE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
