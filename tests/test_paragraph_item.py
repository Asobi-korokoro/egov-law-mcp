"""get_law_article の paragraph_num / item_num / list_items のテスト。

毒物及び劇物指定令(340CO0000000002)第2条は、本文だけで3万字を超える
(号が340件超・うち第32号は117個のSubitem1を持つ)実例。この条の
第1項第76号の2を単独で取れることを実データのフィクスチャで確認する。
"""

import json
from pathlib import Path

import pytest

import server

FIXTURES = Path(__file__).parent / "fixtures"
SHITEIREI_ID = "340CO0000000002"
DOKUGEKIHO_ID = "325AC0000000303"


@pytest.fixture(scope="module")
def shiteirei_law():
    return json.loads((FIXTURES / "shiteirei_law.json").read_text(encoding="utf-8"))


@pytest.fixture(scope="module")
def dokugekiho_law():
    return json.loads((FIXTURES / "dokugekiho_law.json").read_text(encoding="utf-8"))


@pytest.fixture
def fake_shiteirei(monkeypatch, shiteirei_law):
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: shiteirei_law)


@pytest.fixture
def fake_dokugekiho(monkeypatch, dokugekiho_law):
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: dokugekiho_law)


def get_law_article(*args, **kwargs):
    """@mcp.tool() でラップされた関数から素の関数を呼ぶ。"""
    fn = server.get_law_article
    return (fn.fn if hasattr(fn, "fn") else fn)(*args, **kwargs)


# --------------------------------------------------------------------------
# 受け入れ確認: 第76号の2 が単独で取れる。表記ゆれ("76の2"/"76-2"/"七十六の二")
# のいずれでも同じ結果になる。
# --------------------------------------------------------------------------
@pytest.mark.parametrize("item_spelling", ["76の2", "76-2", "七十六の二"])
def test_item_num_extracts_single_item_regardless_of_spelling(fake_shiteirei, item_spelling):
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num=item_spelling)

    # 第76号の2はトルエン(実データで確認済み)。号番号(原文表記)と本文が両方出る。
    assert "七十六の二" in result
    assert "トルエン" in result
    # 号が1件だけなので複数項ヒットの警告は出ない
    assert "複数の項" not in result
    assert server.egov_law.SOURCE_NOTE in result


def test_item_num_without_paragraph_num_searches_all_paragraphs(fake_shiteirei):
    """paragraph_num を省略しても item_num だけで見つかる(条内の全項を横断探索)。"""
    result = get_law_article(SHITEIREI_ID, "2", item_num="76の2")
    assert "七十六の二" in result
    assert "トルエン" in result


def test_item_num_result_excludes_other_items(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num="1")
    lines = result.splitlines()
    # 第1号の本文行はあるが、第2号以降の見出し的な語("三十の五"等)は含まれない
    assert not any("三十の五" in ln for ln in lines)


# --------------------------------------------------------------------------
# 第1項: ParagraphのNum自体は"1"(原文の項番号印字が無いだけ)。
# --------------------------------------------------------------------------
def test_paragraph_num_1_matches_first_paragraph_even_without_visible_number(fake_dokugekiho):
    """第19条第1項はParagraphNumが空(原文に番号なし)だが、paragraph_num="1"で取れる。"""
    result = get_law_article(DOKUGEKIHO_ID, "19", paragraph_num="1")
    assert "都道府県知事は、毒物劇物営業者の有する設備が" in result
    # 第2項の文言は含まれない
    assert "前項の命令を受けた者が" not in result


def test_paragraph_num_selects_only_that_paragraph(fake_dokugekiho):
    result = get_law_article(DOKUGEKIHO_ID, "19", paragraph_num="2")
    assert "前項の命令を受けた者が" in result
    assert "都道府県知事は、毒物劇物営業者の有する設備が" not in result


# --------------------------------------------------------------------------
# 存在しない項・号の指定
# --------------------------------------------------------------------------
def test_nonexistent_item_reports_count_in_scope(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num="9999")
    assert "特定できなかった" in result
    assert "正規化後の号番号: 9999" in result
    assert "には号が合計" in result and "件ある" in result
    assert "list_items=True" in result


def test_nonexistent_paragraph_reports_count(fake_dokugekiho):
    result = get_law_article(DOKUGEKIHO_ID, "19", paragraph_num="99")
    assert "特定できなかった" in result
    assert "正規化後の項番号: 99" in result
    assert "には項が" in result and "件ある" in result


# --------------------------------------------------------------------------
# list_items: 号番号+本文冒頭の一覧
# --------------------------------------------------------------------------
def test_list_items_returns_compact_listing_for_one_paragraph(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", list_items=True)
    assert "号一覧" in result
    assert "七十六の二" in result  # 号番号(原文表記)が一覧に出る
    # 一覧は本文全部ではなく先頭付近だけの短い行のはず
    lines = [ln for ln in result.splitlines() if ln.startswith("七十六の二")]
    assert lines and len(lines[0]) < 40


def test_list_items_labels_paragraphs_when_multiple(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2", list_items=True)
    assert "[第1項]" in result or "[第2項]" in result


# --------------------------------------------------------------------------
# MAX_TEXTでの打ち切り: 行境界で切り、残り文字数と対処法を明示する
# --------------------------------------------------------------------------
def test_whole_article_truncation_breaks_at_line_boundary_with_guidance(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2")
    assert "途中で打ち切った" in result
    assert "残り約" in result and "文字" in result
    assert "list_items=True" in result
    # 打ち切りメッセージより前の本文部分は、途中の行で千切れていない
    # (打ち切りメッセージ自体を含む末尾ブロックを除いた本文が改行で終わっている)
    body_before_note = result.split("※ 応答が長いため途中で打ち切った", 1)[0]
    assert body_before_note.endswith("\n\n") or body_before_note.endswith("\n")


def test_list_items_truncation_also_breaks_at_line_boundary(fake_shiteirei):
    result = get_law_article(SHITEIREI_ID, "2", list_items=True)
    if "途中で打ち切った" in result:
        assert "item_num が分かっている号があれば直接指定すること" in result


# --------------------------------------------------------------------------
# 複数項で同じ号番号がヒットする場合の警告(合成データで確定的に再現)
# --------------------------------------------------------------------------
def test_item_hit_in_multiple_paragraphs_warns(monkeypatch):
    fake = {
        "law_full_text": {
            "tag": "Law", "attr": {}, "children": [
                {"tag": "LawBody", "attr": {}, "children": [
                    {"tag": "MainProvision", "attr": {}, "children": [
                        {"tag": "Article", "attr": {"Num": "1"}, "children": [
                            {"tag": "Paragraph", "attr": {"Num": "1"}, "children": [
                                {"tag": "Item", "attr": {"Num": "1"}, "children": [
                                    {"tag": "ItemTitle", "attr": {}, "children": ["一"]},
                                    {"tag": "ItemSentence", "attr": {}, "children": ["項1号1の本文"]},
                                ]},
                            ]},
                            {"tag": "Paragraph", "attr": {"Num": "2"}, "children": [
                                {"tag": "Item", "attr": {"Num": "1"}, "children": [
                                    {"tag": "ItemTitle", "attr": {}, "children": ["一"]},
                                    {"tag": "ItemSentence", "attr": {}, "children": ["項2号1の本文"]},
                                ]},
                            ]},
                        ]},
                    ]}
                ]}
            ]
        }
    }
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: fake)

    result = get_law_article("DUMMY", "1", item_num="1")
    assert "複数の項" in result
    assert "第1項" in result and "第2項" in result
    assert "項1号1の本文" in result
    assert "項2号1の本文" in result


def test_paragraph_num_narrows_multi_paragraph_item_hit(monkeypatch):
    fake = {
        "law_full_text": {
            "tag": "Law", "attr": {}, "children": [
                {"tag": "LawBody", "attr": {}, "children": [
                    {"tag": "MainProvision", "attr": {}, "children": [
                        {"tag": "Article", "attr": {"Num": "1"}, "children": [
                            {"tag": "Paragraph", "attr": {"Num": "1"}, "children": [
                                {"tag": "Item", "attr": {"Num": "1"}, "children": [
                                    {"tag": "ItemTitle", "attr": {}, "children": ["一"]},
                                    {"tag": "ItemSentence", "attr": {}, "children": ["項1号1の本文"]},
                                ]},
                            ]},
                            {"tag": "Paragraph", "attr": {"Num": "2"}, "children": [
                                {"tag": "Item", "attr": {"Num": "1"}, "children": [
                                    {"tag": "ItemTitle", "attr": {}, "children": ["一"]},
                                    {"tag": "ItemSentence", "attr": {}, "children": ["項2号1の本文"]},
                                ]},
                            ]},
                        ]},
                    ]}
                ]}
            ]
        }
    }
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: fake)

    result = get_law_article("DUMMY", "1", paragraph_num="2", item_num="1")
    assert "項2号1の本文" in result
    assert "項1号1の本文" not in result
    assert "複数の項" not in result


# --------------------------------------------------------------------------
# Ruby・Sup・Sub: 地の文に割り込むインライン要素が改行で分断されないこと
#
# 340CO0000000002 第2条 第1項第7号の下位「ヘ」は実データで
# Sup（上付き文字）が本文中に現れる実例(κ の右肩に「二」)。Subは
# フィクスチャに実例が無いため合成データで確認する。
# --------------------------------------------------------------------------
def test_sup_element_merges_inline_instead_of_breaking_line(fake_shiteirei):
    """Sup要素("二")が改行で分断されず、"^"を前置して前後の文字列と1行に連結される。"""
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num="7")
    lines = result.splitlines()
    he_lines = [ln for ln in lines if ln.strip().startswith("ヘ")]
    assert len(he_lines) == 1
    assert "κ^二Ｓ・Ｓ′" in he_lines[0]
    assert "トリス（ジペンチルジチオカルバマト―κ^二Ｓ・Ｓ′）アンチモン五％以下を含有する製剤" in he_lines[0]


# --------------------------------------------------------------------------
# 打ち切り注記の出し分け: 指定済みの引数に合ったものになっているか
#
# 340CO0000000002 第2条 第32号(第1項)は実データでSubitem1が117個あり、
# paragraph_num・item_num とも指定していても単独でMAX_TEXTを超える実例。
# --------------------------------------------------------------------------
def test_truncation_note_when_paragraph_and_item_both_specified(fake_shiteirei):
    """項・号とも指定済みでの打ち切りは、絞り込みを促さず全文の確認方法を案内する。"""
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num="32")
    assert "途中で打ち切った" in result
    note = result.split("※ 応答が長いため途中で打ち切った", 1)[1]
    assert "これ以上は絞り込めない" in note
    assert "e-Gov法令検索" in note
    # 既に指定済みの引数を重ねて指定するよう促す文言にはならない
    assert "paragraph_num で" not in note
    assert "item_num で" not in note


def test_truncation_note_when_only_paragraph_specified(fake_shiteirei):
    """項までしか指定していない打ち切りは、item_num での絞り込みを案内する。"""
    result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1")
    assert "途中で打ち切った" in result
    note = result.split("※ 応答が長いため途中で打ち切った", 1)[1]
    assert "item_num で号を指定する" in note
    assert "list_items=True" in note
    # 既に指定済みの paragraph_num を重ねて指定するよう促す文言は出ない
    assert "paragraph_num" not in note


def test_truncation_note_when_article_only(fake_shiteirei):
    """条のみ指定の打ち切りは、従来どおり paragraph_num・item_num 両方を案内する。"""
    result = get_law_article(SHITEIREI_ID, "2")
    assert "途中で打ち切った" in result
    note = result.split("※ 応答が長いため途中で打ち切った", 1)[1]
    assert "paragraph_num・item_num で項・号を指定する" in note
    assert "list_items=True" in note


def test_truncation_note_when_article_only_flat(fake_shiteirei):
    """flat=True でも条のみ指定時の案内は同じ。"""
    result = get_law_article(SHITEIREI_ID, "2", flat=True)
    assert "途中で打ち切った" in result
    note = result.split("※ 応答が長いため途中で打ち切った", 1)[1]
    assert "paragraph_num・item_num で項・号を指定する" in note


def test_truncation_note_when_item_specified_without_paragraph(monkeypatch):
    """paragraph_num 未指定で item_num だけ指定した打ち切りは、
    paragraph_num での絞り込みを案内する(絞り込む余地が実際に残っているため)。"""
    big_sentence = "あ" * 9000
    fake = {
        "law_full_text": {
            "tag": "Law", "attr": {}, "children": [
                {"tag": "LawBody", "attr": {}, "children": [
                    {"tag": "MainProvision", "attr": {}, "children": [
                        {"tag": "Article", "attr": {"Num": "1"}, "children": [
                            {"tag": "Paragraph", "attr": {"Num": "1"}, "children": [
                                {"tag": "Item", "attr": {"Num": "1"}, "children": [
                                    {"tag": "ItemTitle", "attr": {}, "children": ["一"]},
                                    {"tag": "ItemSentence", "attr": {}, "children": [big_sentence]},
                                ]},
                            ]},
                        ]},
                    ]},
                ]}
            ]
        }
    }
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: fake)

    result = get_law_article("DUMMY", "1", item_num="1")
    assert "途中で打ち切った" in result
    note = result.split("※ 応答が長いため途中で打ち切った", 1)[1]
    assert "paragraph_num で項を絞り込むこと" in note


def test_non_truncating_item_lookups_unaffected(fake_shiteirei):
    """打ち切られない号の取得結果は今回の変更で変わらない(出し分け対象外)。"""
    for item_spelling in ["7", "10の2", "76の2"]:
        result = get_law_article(SHITEIREI_ID, "2", paragraph_num="1", item_num=item_spelling)
        assert "途中で打ち切った" not in result


def test_sub_element_merges_inline_with_underscore_prefix(monkeypatch):
    """Sub(下付き文字)は実データが無いため合成データで確認する。"_"を前置して連結する。"""
    fake = {
        "law_full_text": {
            "tag": "Law", "attr": {}, "children": [
                {"tag": "LawBody", "attr": {}, "children": [
                    {"tag": "MainProvision", "attr": {}, "children": [
                        {"tag": "Article", "attr": {"Num": "1"}, "children": [
                            {"tag": "Paragraph", "attr": {"Num": "1"}, "children": [
                                {"tag": "ParagraphSentence", "attr": {}, "children": [
                                    {"tag": "Sentence", "attr": {"Num": "1"}, "children": [
                                        "化学式Ｘ",
                                        {"tag": "Sub", "attr": {}, "children": ["二"]},
                                        "Ｙを含有する製剤",
                                    ]},
                                ]},
                            ]},
                        ]},
                    ]}
                ]}
            ]
        }
    }
    monkeypatch.setattr(server.egov_law, "get_law", lambda law_id: fake)

    result = get_law_article("DUMMY", "1")
    assert "化学式Ｘ_二Ｙを含有する製剤" in result
    assert "化学式Ｘ\n" not in result
