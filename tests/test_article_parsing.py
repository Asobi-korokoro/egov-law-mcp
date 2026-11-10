"""server.py の条文抽出・整形のテスト。

毒物及び劇物取締法(dokugekiho)・労働安全衛生法(aneiho)の実APIレスポンスを
tests/fixtures/ に保存したものをフィクスチャとして使う。ネットには出ない。
毒劇法で 項/号 の2階層、安衛法(第46条)で 号の下位(イロハ)まで含む3階層を
それぞれ実データで網羅する。
"""

import json
from pathlib import Path

import pytest

import server

FIXTURES = Path(__file__).parent / "fixtures"
DOKUGEKIHO_ID = "325AC0000000303"
ANEIHO_ID = "347AC0000000057"


@pytest.fixture(scope="module")
def dokugekiho_law():
    return json.loads((FIXTURES / "dokugekiho_law.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def aneiho_law():
    return json.loads((FIXTURES / "aneiho_law.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_law_data(monkeypatch, dokugekiho_law, aneiho_law):
    """egov_law.get_law をフィクスチャ返却に差し替える(ネットに出さない)。"""
    by_id = {DOKUGEKIHO_ID: dokugekiho_law, ANEIHO_ID: aneiho_law}

    def fake_get_law(law_id):
        try:
            return by_id[law_id]
        except KeyError:
            raise AssertionError(f"フィクスチャに無い law_id が要求された: {law_id}")

    monkeypatch.setattr(server.egov_law, "get_law", fake_get_law)
    return by_id


def get_law_article(*args, **kwargs):
    """@mcp.tool() でラップされた関数から素の関数を呼ぶ。"""
    fn = server.get_law_article
    return (fn.fn if hasattr(fn, "fn") else fn)(*args, **kwargs)


# --------------------------------------------------------------------------
# 漢数字正規化
# --------------------------------------------------------------------------
@pytest.mark.parametrize(
    "raw, expected",
    [
        ("2", "2"),
        ("二", "2"),
        ("11の2", "11_2"),
        ("十一の二", "11_2"),
        ("二十三", "23"),
        ("百五十九", "159"),
        ("二百五十一", "251"),
        ("第三条", "3"),
        ("第三条の二", "3_2"),
        ("3-2", "3_2"),
        ("3_2", "3_2"),
        ("一", "1"),
        ("十", "10"),
    ],
)
def test_normalize_article_num(raw, expected):
    assert server._normalize_article_num(raw) == expected


# --------------------------------------------------------------------------
# 本則/附則の分離・件数注記・include_suppl ラベル
# --------------------------------------------------------------------------
def test_default_returns_main_only_with_suppl_count_note(fake_law_data):
    result = get_law_article(DOKUGEKIHO_ID, "1")

    assert "この法律は、毒物及び劇物について" in result  # 本則第一条の本文
    assert "--- 附則" not in result  # 附則の本文は既定では出さない
    assert "附則に同じ条番号が" in result and "件あります" in result


def test_include_suppl_labels_each_amending_law(fake_law_data):
    result = get_law_article(DOKUGEKIHO_ID, "1", include_suppl=True)

    assert "--- 附則（14件） ---" in result
    assert "[昭和三五年八月一〇日法律第一四五号]" in result
    # 制定時の附則(AmendLawNumが無い)は "制定時附則" ラベルになる
    assert "本則には該当条文なし" not in result


def test_branch_article_number_found(fake_law_data):
    """第3条の2(枝番号)が"3の2"表記でも見つかる。"""
    result = get_law_article(DOKUGEKIHO_ID, "3の2")
    assert "特定毒物研究者" in result


def test_unknown_article_number_reports_normalized_form(fake_law_data):
    result = get_law_article(DOKUGEKIHO_ID, "9999")
    assert "特定できなかった" in result
    assert "正規化後の条番号: 9999" in result


# --------------------------------------------------------------------------
# article_num省略時にlaw_info/revision_infoのメタ情報を
# 本文の前に混ぜない(以前はdata全体を平坦化しており、ラベル無しのメタ文字列
# が本文の前に20行以上入っていた)
# --------------------------------------------------------------------------
def test_omitted_article_num_excludes_revision_metadata_noise(fake_law_data):
    result = get_law_article(DOKUGEKIHO_ID)
    assert "この法律は、毒物及び劇物について" in result  # 本文(第一条)は残る
    # revision_info だけが持つメタ情報(読み仮名・改正元法令名・更新日時)は出ない
    assert "どくぶつおよびげきぶつとりしまりほう" not in result
    assert "刑法等の一部を改正する法律の施行に伴う関係法律の整理等に関する法律" not in result
    assert "CurrentEnforced" not in result


# --------------------------------------------------------------------------
# 本則内で同条番号が複数ヒットする異常系の警告
# --------------------------------------------------------------------------
def test_duplicate_main_article_triggers_warning(monkeypatch):
    fake = {
        "law_full_text": {
            "tag": "Law", "attr": {}, "children": [
                {"tag": "LawBody", "attr": {}, "children": [
                    {"tag": "MainProvision", "attr": {}, "children": [
                        {"tag": "Article", "attr": {"Num": "1"}, "children": []},
                        {"tag": "Article", "attr": {"Num": "1"}, "children": []},
                    ]}
                ]}
            ]
        }
    }
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: fake)

    result = get_law_article("DUMMY", "1")
    assert "同じ条番号が2件ヒット" in result
    assert "読み違いの可能性" in result


# --------------------------------------------------------------------------
# 字下げの階層(項2/号4/下位6スペース) — 実データで3階層すべて網羅
# --------------------------------------------------------------------------
def test_paragraph_indent_is_two_spaces_and_num_preserved(fake_law_data):
    """第19条: 項のみ(号なし)。第1項は番号なし、第2項以降は原文の全角数字。"""
    result = get_law_article(DOKUGEKIHO_ID, "19")
    lines = result.splitlines()

    # 第1項: 番号なしで2スペース字下げのみ
    assert any(
        line.startswith("  都道府県知事は、毒物劇物営業者の有する設備が")
        for line in lines
    )
    # 第2項: 全角「２」がそのまま行頭(字下げの直後)に残る
    assert any(line.startswith("  ２　前項の命令を受けた者が") for line in lines)
    assert any(line.startswith("  ５　厚生労働大臣は") for line in lines)


def test_item_indent_is_four_spaces_with_kanji_num(fake_law_data):
    """第4条の2: 号(一・二・三)。号は4スペース字下げ、漢数字がそのまま残る。"""
    result = get_law_article(DOKUGEKIHO_ID, "4の2")
    lines = result.splitlines()

    assert any(line.startswith("    一　一般販売業の登録") for line in lines)
    assert any(line.startswith("    二　農業用品目販売業の登録") for line in lines)
    assert any(line.startswith("    三　特定品目販売業の登録") for line in lines)


def test_subitem_indent_is_six_spaces(fake_law_data):
    """安衛法第46条: 号の下位(イロハ)。6スペース字下げ。"""
    result = get_law_article(ANEIHO_ID, "46")
    lines = result.splitlines()

    assert any(line.startswith("      イ　別表第一第一号又は第二号に掲げる機械等") for line in lines)
    assert any(line.startswith("      ロ　別表第一第四号に掲げる機械等") for line in lines)
    assert any(line.startswith("      ハ　別表第一第八号に掲げる機械等") for line in lines)
    # 号(4スペース)自体も一緒に確認しておく
    assert any(line.startswith("    一　次に掲げる機械等に係る特定機械等") for line in lines)


def test_paragraph_num_node_not_duplicated_in_body(fake_law_data):
    """ParagraphNumの値("２"等)が字下げ+番号の1回だけで、本文側に重複しない。"""
    result = get_law_article(DOKUGEKIHO_ID, "19")
    # 本文中に「２前項の命令」のような番号+本文の連結が2回出ないこと
    assert result.count("２　前項の命令を受けた者が") == 1
    assert "２\n" not in result  # flatten時と違い、単独行の番号だけが残ってはいけない


# --------------------------------------------------------------------------
# flat=True で平坦テキストが返る(退避オプション)
# --------------------------------------------------------------------------
def test_flat_true_returns_unindented_text(fake_law_data):
    structured = get_law_article(DOKUGEKIHO_ID, "19")
    flat = get_law_article(DOKUGEKIHO_ID, "19", flat=True)

    assert structured != flat
    # 平坦版は項番号が単独行になる(字下げも連結もしない)
    flat_lines = flat.splitlines()
    assert "２" in flat_lines
    # 構造化版のような字下げ("  ２　...")は無い
    assert not any(line.startswith("  ２　") for line in flat_lines)
