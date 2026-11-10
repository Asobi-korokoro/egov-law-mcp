"""実機テスト(@pytest.mark.network)。e-Gov 法令API に実際にアクセスする。

既定ではスキップされる(pytest.ini の addopts)。実行するときは `pytest -m network`。
負荷をかけないよう、必要なときだけ手元で回すこと。
"""

import pytest

import egov_law
import server


@pytest.mark.network
def test_probe_endpoint_paths_still_match_assumptions():
    """--probe の結果が ENDPOINTS の想定プレフィックスとまだ一致しているか。

    e-Gov 側の仕様変更を検知するための回帰テスト。
    """
    result = egov_law.probe()
    for path in egov_law.ENDPOINTS.values():
        assert path in result, f"{path} が --probe の結果に見当たらない(仕様変更の可能性)"


@pytest.mark.network
@pytest.mark.parametrize(
    "keyword, expected_top1",
    [
        ("毒物及び劇物", "毒物及び劇物取締法"),
        ("労働基準", "労働基準法"),
        ("刑法", "刑法"),
        ("化学物質", "化学物質の審査及び製造等の規制に関する法律"),
        ("化審法", "化学物質の審査及び製造等の規制に関する法律"),
        ("安衛法", "労働安全衛生法"),
    ],
)
def test_search_law_ranks_target_law_first(keyword, expected_top1):
    """search_law の法令名一致優先ロジックが実データでも効いているかの回帰テスト。

    2026-09-23時点で実機確認済み: 改修前は/keyword(本文全文検索)のみで
    法令ID昇順に並んでいたため、いずれのキーワードも目的の法令が
    上位5件に入らない(または全く出ない)ことがあった。
    """
    fn = server.search_law
    search_law = fn.fn if hasattr(fn, "fn") else fn

    result = search_law(keyword, limit=5)
    first_line = result.splitlines()[0]
    assert expected_top1 in first_line, f"1位に来るはずの法令名が見当たらない: {first_line!r}"
