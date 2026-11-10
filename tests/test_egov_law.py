"""egov_law.py のキャッシュ動作のテスト（ネットに出ない）。"""

import urllib.error
import urllib.request

import pytest

import egov_law


def test_cache_avoids_second_network_call(tmp_path, monkeypatch):
    """同じURLへの2回目の fetch() はネットに出ず、キャッシュから返す。"""
    cache_path = tmp_path / "test_cache.sqlite3"
    monkeypatch.setattr(egov_law, "CACHE_PATH", cache_path)
    monkeypatch.setattr(egov_law, "_last_call", 0.0)
    monkeypatch.setattr(egov_law, "MIN_INTERVAL_SEC", 0.0)

    call_count = {"n": 0}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=30):
        call_count["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    url = "https://laws.e-gov.go.jp/api/2/laws?law_title=test"
    body1 = egov_law.fetch(url)
    body2 = egov_law.fetch(url)

    assert body1 == body2 == b'{"ok": true}'
    assert call_count["n"] == 1, "2回目はキャッシュヒットしてネットに出ないはず"


def test_cache_respects_ttl_expiry(tmp_path, monkeypatch):
    """TTLを過ぎたキャッシュは無効とみなされ、再度ネットに出る。"""
    from datetime import timedelta

    cache_path = tmp_path / "test_cache.sqlite3"
    monkeypatch.setattr(egov_law, "CACHE_PATH", cache_path)
    monkeypatch.setattr(egov_law, "_last_call", 0.0)
    monkeypatch.setattr(egov_law, "MIN_INTERVAL_SEC", 0.0)

    call_count = {"n": 0}

    class FakeResponse:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b"{}"

    def fake_urlopen(req, timeout=30):
        call_count["n"] += 1
        return FakeResponse()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    url = "https://laws.e-gov.go.jp/api/2/laws?law_title=ttl-test"
    egov_law.fetch(url, ttl=timedelta(seconds=0))
    egov_law.fetch(url, ttl=timedelta(seconds=0))

    assert call_count["n"] == 2, "TTL=0ならキャッシュは常に無効なので毎回ネットに出るはず"


def test_fetch_converts_url_error_to_runtime_error(monkeypatch):
    # HTTPError以外(接続失敗・DNS失敗等のURLError)は以前は素通りしていた。
    # _call_egov はRuntimeErrorしかToolErrorに変換しないため、素通りだと原因不明のまま
    # モデルに「Error executing tool」としか伝わらなかった。
    monkeypatch.setattr(egov_law, "_last_call", 0.0)
    monkeypatch.setattr(egov_law, "MIN_INTERVAL_SEC", 0.0)

    def fake_urlopen(req, timeout=30):
        raise urllib.error.URLError("[Errno -2] Name or service not known")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="Name or service not known"):
        egov_law.fetch("https://laws.e-gov.go.jp/api/2/laws?law_title=x", use_cache=False)


def test_fetch_converts_timeout_to_runtime_error(monkeypatch):
    monkeypatch.setattr(egov_law, "_last_call", 0.0)
    monkeypatch.setattr(egov_law, "MIN_INTERVAL_SEC", 0.0)

    def fake_urlopen(req, timeout=30):
        raise TimeoutError("timed out")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)

    with pytest.raises(RuntimeError, match="timeout"):
        egov_law.fetch("https://laws.e-gov.go.jp/api/2/laws?law_title=x", use_cache=False)
