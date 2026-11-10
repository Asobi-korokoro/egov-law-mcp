"""pytest 共通設定。

- プロジェクトルート（server.py 等がある場所）を import パスに通す。
- server.py は `from mcp_common import ...` で同梱の共通パッケージを読む。
  認証キー(MCP_API_KEY)は serve() を呼ぶ起動時にしか読まないので、テストでは不要。
- テストが実ファイル（egov_cache.sqlite3）に書き込んでしまう事故を防ぐ
  ガードを autouse で仕込む（egov_law.py
  の _conn がデフォルト引数の遅延評価でCACHE_PATH monkeypatchを取りこぼす
  バグを踏んだ実績があるための再発防止）。
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))


@pytest.fixture(autouse=True)
def _guard_real_sqlite_writes(request, tmp_path, monkeypatch):
    """tmp_path の外への sqlite3.connect() を拒否する安全網。

    egov_law.CACHE_PATH をテストで monkeypatch し忘れると、プロジェクト
    直下の本物の egov_cache.sqlite3 にテストデータを書き込んでしまう事故が
    起き得る。":memory:" と tmp_path 配下以外への接続を例外にする。

    @pytest.mark.network の実機テストは対象外（意図的に本物のキャッシュへ
    触れるものなので、ここでは素通しする）。
    """
    if request.node.get_closest_marker("network"):
        yield
        return

    real_connect = sqlite3.connect
    tmp_resolved = tmp_path.resolve()

    def guarded_connect(database, *args, **kwargs):
        if database != ":memory:":
            resolved = Path(database).resolve()
            if not resolved.is_relative_to(tmp_resolved):
                raise AssertionError(
                    f"テストが tmp_path の外へ sqlite3.connect しようとした: {resolved}\n"
                    "CACHE_PATH を monkeypatch し忘れていないか確認すること"
                    "（tmp_path 配下のパスに差し替えるのが正しい書き方）。"
                )
        return real_connect(database, *args, **kwargs)

    monkeypatch.setattr(sqlite3, "connect", guarded_connect)
    yield
