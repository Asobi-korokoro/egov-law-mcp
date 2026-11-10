"""search_law の法令名一致優先ロジックと出力整形のテスト(ネットに出ない)。"""

import pytest

import server
from mcp_common import ToolError


def get_law_entry(law_id, title, law_type, abbrev="", law_num=""):
    return {
        "law_info": {"law_id": law_id, "law_type": law_type, "law_num": law_num},
        "revision_info": {"law_title": title, "abbrev": abbrev},
    }


def search_law(*args, **kwargs):
    """@mcp.tool() でラップされた関数から素の関数を呼ぶ。"""
    fn = server.search_law
    return (fn.fn if hasattr(fn, "fn") else fn)(*args, **kwargs)


# --------------------------------------------------------------------------
# 並び順: ①完全一致 ②法令種別 ③題名の短い順
# --------------------------------------------------------------------------
def test_exact_title_match_ranks_first_regardless_of_type(monkeypatch):
    """「刑法」実例を模した合成データ: 古い布告(Act型・非完全一致)より
    完全一致する法律名(同じくAct型)が先に来る。"""
    entries = [
        get_law_entry("117DF1000000032", "明治十七年太政官布告第三十二号（爆発物取締罰則）", "Act"),
        get_law_entry("140AC0000000045", "刑法", "Act"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("刑法", limit=10)
    lines = result.splitlines()
    assert lines[0].startswith("[法令名一致] 刑法（")
    assert "爆発物取締罰則" in lines[1]


def test_law_type_rank_orders_act_before_cabinet_order_before_ministerial(monkeypatch):
    entries = [
        get_law_entry("A2", "◯◯取締法施行規則", "MinisterialOrdinance"),
        get_law_entry("A3", "◯◯取締法施行令", "CabinetOrder"),
        get_law_entry("A1", "◯◯取締法", "Act"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("◯◯", limit=10)
    titles_in_order = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert "◯◯取締法（" in titles_in_order[0]
    assert "◯◯取締法施行令（" in titles_in_order[1]
    assert "◯◯取締法施行規則（" in titles_in_order[2]


def test_shorter_title_wins_within_same_type_tier(monkeypatch):
    entries = [
        get_law_entry("B2", "流通食品への毒物の混入等の防止等に関する特別措置法", "Act"),
        get_law_entry("B1", "毒物及び劇物取締法", "Act"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("毒物", limit=10)
    titles_in_order = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert titles_in_order[0].startswith("[法令名一致] 毒物及び劇物取締法（")


def test_combined_law_type_uses_primary_segment(monkeypatch):
    """law_typeが"Act,CabinetOrder"のようにカンマ区切りの実例に対応する。"""
    entries = [
        get_law_entry("C2", "◯◯令", "MinisterialOrdinance"),
        get_law_entry("C1", "◯◯", "Act,CabinetOrder"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("◯◯", limit=10)
    titles_in_order = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert titles_in_order[0].startswith("[法令名一致] ◯◯（")  # Act扱いでrank0、先頭に来る


def test_abbrev_exact_match_counts_as_exact(monkeypatch):
    """略称(abbrev)がカンマ区切りで複数格納されていても、検索語と完全一致すれば
    完全一致扱いになる(「安衛法」実例のabbrev="労安衛法,保安四法,安衛法")。"""
    entries = [
        get_law_entry("D2", "◯◯だけを含む長い法令名", "Act", abbrev="別の略称"),
        get_law_entry("D1", "本当の法令名", "MinisterialOrdinance", abbrev="他略称,安衛法,他略称2"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("安衛法", limit=10)
    titles_in_order = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert titles_in_order[0].startswith("[法令名一致] 本当の法令名（")


# --------------------------------------------------------------------------
# 本文一致での補完・重複除去
# --------------------------------------------------------------------------
def test_supplements_with_keyword_search_when_title_matches_insufficient(monkeypatch):
    title_entries = [get_law_entry("T1", "対象法", "Act")]
    keyword_entries = [
        get_law_entry("T1", "対象法", "Act"),  # 法令名一致と重複 → 除外されるはず
        get_law_entry("K1", "本文でだけ言及する法", "Act"),
    ]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": title_entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": keyword_entries})

    result = search_law("対象", limit=10)
    lines = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert len(lines) == 2
    assert lines[0].startswith("[法令名一致] 対象法（")
    assert lines[1].startswith("[本文一致] 本文でだけ言及する法（")


def test_does_not_call_keyword_search_when_title_matches_fill_limit(monkeypatch):
    """法令名一致だけでlimitを満たせば/keywordは呼ばない(不要なAPI呼び出しをしない)。"""
    title_entries = [get_law_entry(f"T{i}", f"法令{i}", "Act") for i in range(3)]
    called = {"n": 0}

    def fake_keyword(*a, **k):
        called["n"] += 1
        return {"items": []}

    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": title_entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", fake_keyword)

    search_law("法令", limit=3)
    assert called["n"] == 0


@pytest.mark.parametrize("bad_limit", [0, -1, 10_000])
def test_invalid_limit_is_rejected_without_hitting_network(monkeypatch, bad_limit):
    # limit=0は「該当あり」を「見つからなかった」に誤変換し、
    # limit=-1は負数スライスで末尾の一致を無言で落とし、上限無しだと
    # キーワード検索側の要求件数(remaining*10)が過大になる。
    def must_not_call(*a, **k):
        raise AssertionError("limitの検査はAPI呼び出しの前に行う")

    monkeypatch.setattr(server.egov_law, "search_by_title", must_not_call)
    monkeypatch.setattr(server.egov_law, "search_keyword", must_not_call)

    with pytest.raises(ToolError, match=str(bad_limit)):
        search_law("毒物", limit=bad_limit)


def test_no_hits_reports_not_found(monkeypatch):
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": []})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("存在しないはずの語", limit=10)
    assert "見つからなかった" in result


# --------------------------------------------------------------------------
# 出力整形: 1件1行、ラベル・法令名・法令ID・法令番号
# --------------------------------------------------------------------------
def test_output_is_one_line_per_entry_with_label_name_id_num(monkeypatch):
    entries = [get_law_entry("325AC0000000303", "毒物及び劇物取締法", "Act", law_num="昭和二十五年法律第三百三号")]
    monkeypatch.setattr(server.egov_law, "search_by_title", lambda title, limit: {"laws": entries})
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("毒物", limit=10)
    lines = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert lines == ["[法令名一致] 毒物及び劇物取締法（法令ID: 325AC0000000303 / 昭和二十五年法律第三百三号）"]
    assert server.egov_law.SOURCE_NOTE in result


# --------------------------------------------------------------------------
# limit を小さくしても完全一致が切り捨てられない(並べ替えの前に limit で切らない)
# --------------------------------------------------------------------------
def test_small_limit_keeps_exact(monkeypatch):
    """「所得税法」実例を模した合成データ: APIは自分の順で先頭 limit 件に切って返す。
    臨時特例法が先頭にあっても、limit=1 で完全一致の本体が返る。"""
    entries = [
        get_law_entry("X1", "東日本大震災の復興財源確保のための所得税法の臨時特例法", "Act"),
        get_law_entry("X2", "所得税法施行令", "CabinetOrder"),
        get_law_entry("140AC0000000033", "所得税法", "Act"),
    ]
    monkeypatch.setattr(
        server.egov_law, "search_by_title", lambda title, limit: {"laws": entries[:limit]}
    )
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law("所得税法", limit=1)
    lines = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert len(lines) == 1
    assert lines[0].startswith("[法令名一致] 所得税法（")


@pytest.mark.parametrize(
    "name,others",
    [
        ("労働基準法", ["労働基準法施行規則", "労働基準法の一部を改正する法律", "短時間労働者の雇用管理の改善等に関する労働基準法の特例"]),
        ("刑法", ["刑法施行法", "暴力行為等処罰ニ関スル刑法ノ特例ニ関スル法律", "刑法等の一部を改正する法律"]),
        ("民法", ["民法施行法", "民法の一部を改正する法律", "借地借家法に関する民法の特例"]),
    ],
)
def test_small_limit_keeps_exact_for_names_contained_in_others(monkeypatch, name, others):
    """名前が他の法令名に含まれる例。完全一致を末尾に置き、APIが先頭 limit 件で切っても limit=1 で返る。"""
    entries = [get_law_entry(f"O{i}", t, "Act") for i, t in enumerate(others)]
    entries.append(get_law_entry("EXACT", name, "Act"))
    monkeypatch.setattr(
        server.egov_law, "search_by_title", lambda title, limit: {"laws": entries[:limit]}
    )
    monkeypatch.setattr(server.egov_law, "search_keyword", lambda *a, **k: {"items": []})

    result = search_law(name, limit=1)
    lines = [ln for ln in result.splitlines() if ln.startswith("[")]
    assert len(lines) == 1
    assert lines[0].startswith(f"[法令名一致] {name}（")
