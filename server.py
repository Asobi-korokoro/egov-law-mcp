"""egov-law MCP サーバ。

公開するツール:
    search_law         キーワードで法令を探す
    get_law_article     法令ID + 条番号 で条文を取る

認証はヘッダー `x-api-key`(同梱の mcp_common)。

環境変数:
    MCP_API_KEY           認証キー（必須。32文字以上）
    MCP_API_KEY_PREVIOUS  キー入れ替え中だけ設定する旧キー（任意）
    PORT                  Cloud Run が渡す
"""

from __future__ import annotations

from typing import Any

from mcp.server.mcpserver import MCPServer

import egov_law
from mcp_common import ToolError, serve

MAX_TEXT = 8000  # 1レスポンスで返す本文の上限
MAX_SEARCH_LIMIT = 100  # search_law の limit の上限(大きすぎるとキーワード検索側の要求が過大になる)

mcp = MCPServer("egov-law")


def _call_egov(func, *args, **kwargs):
    """e-Gov API を呼ぶ。egov_law.fetch が HTTP エラーで投げる RuntimeError を ToolError にする。

    ToolError 以外の例外は、モデルには「Error executing tool <name>」としか伝わらない。
    法令IDの誤り(404)などの原因が伝わるよう、メッセージ(HTTP ステータスと e-Gov の
    URL・応答の先頭。秘匿値は含まない)をそのまま返す。egov_law.py は単体 CLI(--probe 等)
    でも使うモジュールなので、mcp_common には依存させず、変換はこの境界で行う。
    """
    try:
        return func(*args, **kwargs)
    except RuntimeError as e:
        raise ToolError(str(e)) from e


# --------------------------------------------------------------------------
# 条文の抽出
#
# --probe / --law で実機確認した e-Gov JSON の構造:
#   law_full_text は {"tag": ..., "attr": {...}, "children": [...]} という
#   XML→JSON変換ツリー。条文は tag=="Article" のノードで、条番号は
#   トップレベルの "Num" ではなく attr["Num"] にある（旧実装のバグ）。
#   枝番号(第3条の2)は "3_2" のようなアンダースコア表記（漢数字ではない）。
#   例: 毒物及び劇物取締法の Article Num は
#       ['1', '2', '3', '3_2', '3_3', '4', '4_2', ...]
# --------------------------------------------------------------------------
_KANJI_DIGITS = {"〇": 0, "一": 1, "二": 2, "三": 3, "四": 4,
                 "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
_KANJI_UNITS = {"十": 10, "百": 100, "千": 1000}


def _kanji_to_int(s: str) -> int | None:
    """条番号でよく使う漢数字(一〜数百程度)を整数に変換する。

    渡された文字列が漢数字だけで構成されていない場合は None を返す。
    """
    if not s or not all(c in _KANJI_DIGITS or c in _KANJI_UNITS for c in s):
        return None
    total = 0
    num = 0
    for ch in s:
        if ch in _KANJI_DIGITS:
            num = _KANJI_DIGITS[ch]
        else:
            total += (num or 1) * _KANJI_UNITS[ch]
            num = 0
    return total + num


def _normalize_num_part(s: str) -> str:
    """条番号1個ぶん(枝番の片側)をアラビア数字の文字列に正規化する。"""
    s = s.strip()
    if not s or s.isdigit():
        return s
    n = _kanji_to_int(s)
    return str(n) if n is not None else s


def _normalize_article_num(article_num: str) -> str:
    """ユーザー入力の条番号表記を e-Gov の Num 表記へ正規化する。

    受け付ける形: "3" / "三" / "3の2" / "三の二" / "3-2" / "3_2"。
    実機確認では Article の attr.Num 自体は常にアラビア数字
    （枝番は "3_2" のようなアンダースコア表記）だったが、
    人が入力するときは漢数字表記が自然なので、ここで両対応する。
    """
    s = article_num.strip().replace("第", "").replace("条", "")
    s = s.replace("-", "_").replace("の", "_")
    return "_".join(_normalize_num_part(p) for p in s.split("_") if p != "")


def _find_articles(
    node: Any,
    want: str,
    section: str,
    suppl_label: str,
    found: list[tuple[str, str, dict]],
) -> None:
    """(区分, 附則ラベル, Articleノード) のタプルを集める。

    区分は "main"(本則) または "suppl"(附則)。附則配下では、
    SupplProvision の attr.AmendLawNum からどの改正法によるものかを
    ラベルにする(制定時の附則には AmendLawNum が無いことがある)。
    ノードそのものを返す(整形前)のは、構造化整形と平坦テキストの
    両方を呼び出し側で選べるようにするため。
    """
    if isinstance(node, dict):
        tag = node.get("tag")
        if tag == "MainProvision":
            section, suppl_label = "main", ""
        elif tag == "SupplProvision":
            attr = node.get("attr", {})
            section = "suppl"
            suppl_label = attr.get("AmendLawNum") or "制定時附則"

        if tag == "Article" and node.get("attr", {}).get("Num") == want:
            found.append((section, suppl_label, node))
            return

        children = node.get("children") if "tag" in node else node.values()
        for v in children:
            _find_articles(v, want, section, suppl_label, found)
    elif isinstance(node, list):
        for v in node:
            _find_articles(v, want, section, suppl_label, found)


# Ruby以外で地の文に割り込むインライン要素。Sup/Subは中身に前置する記号。
_INLINE_MARKUP_PREFIX = {"Sup": "^", "Sub": "_"}
_INLINE_TAGS = {"Ruby", *_INLINE_MARKUP_PREFIX}


def _flatten_text(node: Any) -> str:
    """テキストへ平坦化する。

    tag/attr/children 形式のノードは children だけを辿る
    （tag名やattr値をテキストに混ぜない）。それ以外の素朴な dict/list
    （law_info・revision_info・keyword検索結果など）は全値を辿る。

    Ruby（ルビ）は親文字だけを残し、読み（Rt）は捨てる。Sup（上付き）・Sub
    （下付き）はそれぞれ"^"・"_"を中身の前に付けて残す（例: κ^二、化学物質名の
    錯体表記などで使われる）。これらはいずれも地の文の途中に割り込む形で
    出現する（例: "一水素二" + Ruby("弗") + "化アンモニウム"、
    "κ" + Sup("二") + "Ｓ・Ｓ′"）ため、中身は前後の文字列と改行を挟まず
    連結する。一方、Paragraph/Item/Sentence 等のタグ付きノードが並ぶ場合は、
    それぞれ別行として扱う（項番号が単独行になる、Sentenceが複数あれば
    行を分ける、といった従来の挙動はここで維持される）。
    """
    if isinstance(node, str):
        return node.strip()
    if isinstance(node, dict):
        tag = node.get("tag")
        if tag == "Ruby":
            base = next((c for c in node.get("children", []) if isinstance(c, str)), "")
            return base.strip()
        if tag in _INLINE_MARKUP_PREFIX:
            return _INLINE_MARKUP_PREFIX[tag] + _flatten_text(node.get("children"))
        if "tag" in node and "children" in node:
            return _flatten_text(node.get("children"))
        return "\n".join(t for t in (_flatten_text(v) for v in node.values()) if t)
    if isinstance(node, list):
        lines: list[str] = []
        buf = ""
        for child in node:
            if isinstance(child, str) or (isinstance(child, dict) and child.get("tag") in _INLINE_TAGS):
                buf += _flatten_text(child)
                continue
            if buf:
                lines.append(buf)
                buf = ""
            rendered = _flatten_text(child)
            if rendered:
                lines.append(rendered)
        if buf:
            lines.append(buf)
        return "\n".join(lines)
    return ""


# --------------------------------------------------------------------------
# 条文の階層整形（項/号/号の下位を字下げで表現）
#
# 実機確認した e-Gov の入れ子構造（毒物及び劇物取締法・労働安全衛生法で確認）:
#   Paragraph(項, attr.Num) -> ParagraphNum(番号) + ParagraphSentence(本文)
#                              + Item(号)...
#   Item(号, attr.Num)      -> ItemTitle(番号)    + ItemSentence(本文)
#                              + Subitem1(号の下位)...
#   Subitem1(号の下位)      -> Subitem1Title(番号) + Subitem1Sentence(本文)
#                              (Subitem2以降も同型で続く可能性があるため
#                               タグ名を機械的に生成して汎用対応する)
#
# 第1項はParagraphNumが空(原文に番号が無い)。号・号の下位の番号は
# ItemTitle/Subitem*Title の原文表記(漢数字「一」)をそのまま使う。
# --------------------------------------------------------------------------
_HIERARCHY_LEVELS: list[tuple[str, str, str]] = [
    ("Paragraph", "ParagraphNum", "ParagraphSentence"),
    ("Item", "ItemTitle", "ItemSentence"),
] + [
    (f"Subitem{n}", f"Subitem{n}Title", f"Subitem{n}Sentence") for n in range(1, 4)
]
_HIERARCHY_TAGS = {tag: (num_tag, sentence_tag) for tag, num_tag, sentence_tag in _HIERARCHY_LEVELS}


def _render_children(children: list, depth: int, lines: list[str]) -> None:
    """Paragraph/Item/Subitem* を再帰的に字下げ整形する。"""
    indent = "  " * depth
    for child in children:
        if not isinstance(child, dict):
            continue
        hier = _HIERARCHY_TAGS.get(child.get("tag"))
        if hier is None:
            continue
        num_tag, sentence_tag = hier

        num_text = ""
        caption_text = ""
        sentence_text = ""
        nested: list[dict] = []
        for gc in child.get("children", []):
            if not isinstance(gc, dict):
                continue
            gtag = gc.get("tag")
            if gtag == num_tag:
                num_text = _flatten_text(gc)
            elif gtag == sentence_tag:
                sentence_text = _flatten_text(gc)
            elif gtag and gtag.endswith("Caption"):
                caption_text = _flatten_text(gc)
            elif gtag in _HIERARCHY_TAGS:
                nested.append(gc)

        body_lines = [ln for ln in ([caption_text] if caption_text else []) + sentence_text.split("\n") if ln]
        prefix = f"{num_text}　" if num_text else ""
        for line in body_lines:
            lines.append(f"{indent}{prefix}{line}")
            prefix = ""  # 2文目以降(同じ項/号内)は番号を付けない

        if nested:
            _render_children(nested, depth + 1, lines)


def render_article_structured(article_node: dict) -> str:
    """Article ノードを 項/号/号の下位 の階層を字下げで保って整形する。

    項は2スペース、号は4スペース、号の下位は6スペース(以降も再帰的に+2)。
    項番号・号番号は原文表記(全角数字・漢数字)をそのまま行頭に残す。
    第1項のように原文に番号が無い場合は、番号なしで字下げだけ行う。
    ParagraphNum/ItemTitle などの番号ノードは番号として消費し、
    本文側(Sentence)には重複して出さない。
    """
    lines: list[str] = []
    caption = title = ""
    top_children: list[dict] = []
    for c in article_node.get("children", []):
        if not isinstance(c, dict):
            continue
        tag = c.get("tag")
        if tag == "ArticleCaption":
            caption = _flatten_text(c)
        elif tag == "ArticleTitle":
            title = _flatten_text(c)
        else:
            top_children.append(c)

    header = f"{title}　{caption}" if title and caption else (title or caption)
    if header:
        lines.append(header)

    _render_children(top_children, 1, lines)
    return "\n".join(lines)


# --------------------------------------------------------------------------
# 項・号による絞り込み（get_law_article の paragraph_num / item_num / list_items）
#
# Paragraph・Item は render_article_structured と同じノード形状なので、
# 単一ノードの整形は _render_children([node], depth, lines) に委譲する
# （depth=1で項相当の2スペース字下げ、depth=2で号相当の4スペース字下げになる。
# render_article_structured内でParagraph→Item→Subitem*と再帰的にdepth+1
# しているのと同じ深さになるよう合わせてある）。
# --------------------------------------------------------------------------
def _truncate_at_line_boundary(text: str, limit: int) -> tuple[str, int]:
    """limit文字を超える場合、直前の改行位置で切る（行の途中で千切れないようにする）。

    戻り値は (切った後のテキスト, 削った文字数)。削っていなければ削った文字数は0。
    1行がlimitを超えるなど改行が見つからない場合は、素の文字数スライスにフォールバックする。
    """
    if len(text) <= limit:
        return text, 0
    cut = text.rfind("\n", 0, limit)
    if cut <= 0:
        cut = limit
    return text[:cut], len(text) - cut


def _finish_with_truncation(body: str, guidance: str) -> str:
    """MAX_TEXTで打ち切る時は行境界で切り、打ち切った旨と残り文字数を明示する。"""
    truncated, remaining = _truncate_at_line_boundary(body, MAX_TEXT)
    if remaining:
        truncated += f"\n\n※ 応答が長いため途中で打ち切った(残り約{remaining}文字)。{guidance}"
    return f"{truncated}\n\n{egov_law.SOURCE_NOTE}"


def _paragraphs_of(article_node: dict) -> list[dict]:
    """Article直下のParagraphノードを原文の並び順で返す。"""
    return [c for c in article_node.get("children", []) if isinstance(c, dict) and c.get("tag") == "Paragraph"]


def _items_of(paragraph_node: dict) -> list[dict]:
    """Paragraph直下のItemノードを原文の並び順で返す(号の下位Subitem*は含まない)。"""
    return [c for c in paragraph_node.get("children", []) if isinstance(c, dict) and c.get("tag") == "Item"]


def _render_single(node: dict, depth: int) -> str:
    """Paragraph/Itemノード1件を、render_article_structuredと同じ字下げ規則で整形する。"""
    lines: list[str] = []
    _render_children([node], depth, lines)
    return "\n".join(lines)


def _item_preview(item_node: dict, width: int = 10) -> str:
    """号1件を「番号　本文の先頭width字」の1行に要約する(list_items用)。"""
    title = ""
    body = ""
    for gc in item_node.get("children", []):
        if not isinstance(gc, dict):
            continue
        tag = gc.get("tag")
        if tag == "ItemTitle":
            title = _flatten_text(gc)
        elif tag == "ItemSentence":
            body = _flatten_text(gc).replace("\n", "")
    if len(body) > width:
        body = body[:width] + "…"
    return f"{title}　{body}" if title else body


# --------------------------------------------------------------------------
# search_law: 法令名一致優先 → 本文キーワード検索で補完
#
# e-Gov API v2 の /laws は law_title(法令名・略称の部分一致)で絞れるが、
# /keyword(本文全文検索)にはその種のフィルタが無い(公式OpenAPI仕様で確認済み)。
# 法令名を知らないユーザーが「毒物及び劇物」のような語で探すと、/keywordだけでは
# 本文にその語を含む無関係な法令(児童福祉法など)が先に来てしまうため、まず
# /lawsで法令名一致を探し、件数が足りない分だけ/keywordの本文一致で補う。
# --------------------------------------------------------------------------
_LAW_TYPE_RANK = {
    "Constitution": 0,  # 憲法
    "Act": 0,  # 法律
    "CabinetOrder": 1,  # 政令
    "MinisterialOrdinance": 2,  # 府省令
    "Rule": 2,  # 規則
    "ImperialOrder": 3,  # 勅令(布告等)
    "Misc": 3,  # その他(布告等)
}


# 法令名一致は、並べ替えの前に切られないよう limit より広く取る(APIの最大値に収まる範囲)
_TITLE_FETCH_LIMIT = 100


def _law_type_rank(law_type: str) -> int:
    """法令種別を並び順のグループ番号にする(憲法・法律=0 < 政令=1 < 府省令・規則=2 < 布告等=3)。

    law_typeはカンマ区切りで複数格納されることがある(実データで確認: 例
    "Act,CabinetOrder"、明治期の政令が後に法律とみなされた等の経緯によるもの)。
    先頭の種別を代表として使う。schemaに無い未知の値は最下位(布告等)扱い。
    """
    primary = (law_type or "").split(",", 1)[0]
    return _LAW_TYPE_RANK.get(primary, 3)


def _law_title_sort_key(entry: dict, query: str) -> tuple:
    """法令名一致の並び順キー: ①完全一致 ②法令種別 ③題名の短い順。"""
    law_info = entry.get("law_info", {})
    rev_info = entry.get("revision_info", {})
    title = rev_info.get("law_title") or ""
    abbrevs = [a for a in (rev_info.get("abbrev") or "").split(",") if a]
    is_exact = title == query or query in abbrevs
    return (0 if is_exact else 1, _law_type_rank(law_info.get("law_type", "")), len(title))


def _format_law_entry(entry: dict, label: str) -> str:
    """検索結果1件を「[ラベル] 法令名（法令ID: ... / 法令番号）」の1行にする。"""
    law_info = entry.get("law_info", {})
    rev_info = entry.get("revision_info", {})
    title = rev_info.get("law_title") or "(法令名不明)"
    law_id = law_info.get("law_id") or "?"
    law_num = law_info.get("law_num") or ""
    return f"[{label}] {title}（法令ID: {law_id} / {law_num}）"


# --------------------------------------------------------------------------
# ツール
# --------------------------------------------------------------------------
@mcp.tool()
def search_law(keyword: str, limit: int = 10) -> str:
    """法令名・略称・本文キーワードで法令を検索する。

    正式名称を覚えていなくても、法令名の一部や通称(「毒物」「安衛法」
    「化審法」等)から目的の法令を見つけられるようにするため、まず
    法令名・略称の部分一致(e-Gov /laws の law_title)で探す。件数が
    limit に満たない場合だけ、本文全文検索(/keyword)で不足分を補う
    (法令名一致で既に出た法令は重複させない)。

    法令名一致内の並び順: ①検索語と法令名・略称が完全一致するもの
    ②法令種別(憲法・法律 → 政令 → 府省令・規則 → 布告等の順)
    ③題名が短いもの、の順。本文一致で補った分はAPIの既定順（法令ID順）
    のまま追加する(法令名一致に比べれば付随的な結果のため)。

    Args:
        keyword: 検索語（法令名の一部・略称・本文キーワードなど。
            例: 毒物及び劇物、危険物、化審法、安衛法）
        limit: 最大件数
    """
    # limit=0はtitle_entries[:0]で「該当あり」が「見つからなかった」に
    # なる。limit<0は負数スライスで末尾の一致を無言で落とす。上限が無いと大きいlimitが
    # そのままキーワード検索側の要求件数(remaining*10)に増幅される。
    if not (1 <= limit <= MAX_SEARCH_LIMIT):
        raise ToolError(f"limit は1〜{MAX_SEARCH_LIMIT}の範囲で指定すること(渡された値: {limit})")

    # APIは自分の並び(法令ID順など)で limit 件に切って返すため、limit をそのまま渡すと
    # 完全一致が切り捨てられる(keyword="所得税法", limit=1 で臨時特例法が返った)。
    # 並べ替えの前に切らないよう、広めに取ってから並べ替え、その後で limit に切る。
    by_title = _call_egov(egov_law.search_by_title, keyword, limit=max(limit, _TITLE_FETCH_LIMIT))
    title_entries = list(by_title.get("laws") or [])
    title_entries.sort(key=lambda e: _law_title_sort_key(e, keyword))

    seen_ids = {e.get("law_info", {}).get("law_id") for e in title_entries}
    lines = [_format_law_entry(e, "法令名一致") for e in title_entries[:limit]]

    remaining = limit - len(lines)
    if remaining > 0:
        # sentences_limit=1 で1法令あたりの消費を1件に抑え、limitという
        # 本来「sentences総数」の予算を、法令の異なり数の確保に近づける。
        by_keyword = _call_egov(
            egov_law.search_keyword, keyword, limit=max(remaining * 10, 50), sentences_limit=1
        )
        for item in by_keyword.get("items") or []:
            law_id = item.get("law_info", {}).get("law_id")
            if law_id is None or law_id in seen_ids:
                continue
            seen_ids.add(law_id)
            lines.append(_format_law_entry(item, "本文一致"))
            if len(lines) >= limit:
                break

    if not lines:
        return f"「{keyword}」に該当する法令が見つからなかった。\n\n{egov_law.SOURCE_NOTE}"

    return "\n".join(lines) + f"\n\n{egov_law.SOURCE_NOTE}"


@mcp.tool()
def get_law_article(
    law_id: str,
    article_num: str = "",
    paragraph_num: str = "",
    item_num: str = "",
    list_items: bool = False,
    include_suppl: bool = False,
    flat: bool = False,
) -> str:
    """法令の条文を取得する。条番号を省略すると冒頭のみ返す。

    既定では本則(MainProvision)の条文だけを返す。附則(SupplProvision)には
    改正のたびに同じ条番号（第一条＝施行期日、など）が積み重なっているため、
    既定で混ぜると読みにくくなる。附則の内容が要るときは include_suppl=True
    を指定する（経過措置などを見落とさないよう、附則の件数だけは既定でも
    末尾に注記する）。

    既定では項・号・号の下位（イロハ）を字下げで整形して返す
    （項は2スペース、号は4スペース、以降+2スペースずつ）。
    項番号・号番号は原文表記（全角数字「２」・漢数字「一」）をそのまま
    行頭に残す。第1項のように原文に番号が無い場合は番号なしで字下げのみ。

    条文が長く(毒物及び劇物指定令 第2条など、数百号ある条は本文だけで
    3万字を超えることがある)、応答が自前の文字数上限(MAX_TEXT)で
    途中打ち切りになる場合は、paragraph_num・item_num で項・号まで
    絞り込むか、list_items=True でまず号の一覧(号番号+本文冒頭)を
    確認してから該当する号だけを取得すること。

    Args:
        law_id: e-Govの法令ID（例: 325AC0000000303）
        article_num: 条番号（例: 2、二、11の2、十一の二）
        paragraph_num: 項番号（例: 1、一）。省略時は条内の全項が対象。
            第1項もParagraphのNum自体は"1"なので、"1"を指定すれば
            原文に項番号の印字が無い第1項も確実に取れる。
        item_num: 号番号（例: 76、76の2、76-2、七十六の二）。枝番号は
            article_num と同じ書式(「の」「-」いずれも可)で指定できる。
            paragraph_num を省略した場合は条内の全項を横断して探し、
            同じ号番号が複数の項にヒットしたら該当する項をすべて列挙して
            知らせる(article_num の重複ヒットと同じ扱い)。
        list_items: True なら本文ではなく、項ごとの号番号と本文冒頭
            (約10字)だけの一覧を返す。物質名などで該当する号を
            探してから item_num で本文を取りに行く用途を想定している。
        include_suppl: True なら附則にある同じ条番号もすべて返す
            （どの改正法によるものかをラベルとして付ける）。
            paragraph_num・item_num・list_items 指定時は非対応
            （本則のみが対象。附則の有無は件数だけ注記する）。
        flat: True なら階層整形せず平坦なテキストで返す
            （デバッグ用・階層が想定外だった場合の退避用）
    """
    data = _call_egov(egov_law.get_law, law_id)

    if not article_num:
        # data は {law_info, revision_info, law_full_text, ...} の形。
        # data全体を平坦化すると law_info・revision_info のメタ情報(法令番号・公布日など)が
        # 本文の前にラベル無しで20行以上混ざり、MAX_TEXTの枠を無駄に消費する。
        # 本文は law_full_text だけなので、そこだけを平坦化する。
        return _finish_with_truncation(
            _flatten_text(data.get("law_full_text", data)),
            "article_num を指定して該当する条文だけを取得すること。",
        )

    want = _normalize_article_num(article_num)
    matches: list[tuple[str, str, dict]] = []
    _find_articles(data, want, "", "", matches)

    main_hits = [m for m in matches if m[0] == "main"]
    suppl_hits = [m for m in matches if m[0] == "suppl"]

    if not main_hits and not suppl_hits:
        return (
            f"第{article_num}条を特定できなかった（正規化後の条番号: {want}）。\n"
            f"条番号の書式が違う可能性がある。article_num を省略して"
            f"全文の冒頭を確認すること。"
        )

    want_paragraph = _normalize_article_num(paragraph_num) if paragraph_num else ""
    want_item = _normalize_article_num(item_num) if item_num else ""

    if want_paragraph or want_item or list_items:
        if not main_hits:
            return (
                f"第{article_num}条は本則に見つからず、附則にのみ{len(suppl_hits)}件ある。"
                "paragraph_num・item_num・list_items は本則のみ対応のため使えない。"
                "article_num のみで include_suppl=True を指定して確認すること。"
            )
        if len(main_hits) > 1:
            return (
                f"第{article_num}条が本則内に{len(main_hits)}件ヒットした。"
                "条文構造の読み違いの可能性があるため、"
                "まず paragraph_num・item_num を指定せずに結果を確認すること。"
            )

        _, _, article_node = main_hits[0]
        all_paragraphs = _paragraphs_of(article_node)
        paragraphs = all_paragraphs
        if want_paragraph:
            paragraphs = [p for p in paragraphs if p.get("attr", {}).get("Num") == want_paragraph]
            if not paragraphs:
                return (
                    f"第{article_num}条 第{paragraph_num}項 を特定できなかった"
                    f"（正規化後の項番号: {want_paragraph}）。\n"
                    f"この条には項が{len(all_paragraphs)}件ある。"
                )

        suppl_note = (
            f"※ このほか附則に同じ条番号が{len(suppl_hits)}件あります"
            "（項・号指定では対象外。article_num のみで include_suppl=True を指定すると見られる）。"
            if suppl_hits
            else ""
        )

        if list_items:
            lines = [f"【{law_id} 第{article_num}条 号一覧】"]
            multi = len(paragraphs) > 1
            for p in paragraphs:
                items = _items_of(p)
                if not items:
                    continue
                if multi:
                    lines.append(f"[第{p.get('attr', {}).get('Num')}項]")
                lines.extend(_item_preview(it) for it in items)
            if len(lines) == 1:
                lines.append("(号なし)")
            if suppl_note:
                lines.append(suppl_note)
            return _finish_with_truncation(
                "\n".join(lines),
                "item_num が分かっている号があれば直接指定すること。",
            )

        if not want_item:
            render_one = _flatten_text if flat else (lambda node: _render_single(node, 1))
            label = f"第{paragraph_num}項" if paragraph_num else "全項"
            lines = [f"【{law_id} 第{article_num}条 {label}】"]
            lines.extend(render_one(p) for p in paragraphs)
            if suppl_note:
                lines.append(suppl_note)
            return _finish_with_truncation(
                "\n\n".join(lines),
                "item_num で号を指定するか、list_items=True で号の一覧を確認すること。",
            )

        hits: list[tuple[str, dict]] = []
        for p in paragraphs:
            for it in _items_of(p):
                if it.get("attr", {}).get("Num") == want_item:
                    hits.append((p.get("attr", {}).get("Num"), it))

        if not hits:
            total_items = sum(len(_items_of(p)) for p in paragraphs)
            scope = f"第{paragraph_num}項" if paragraph_num else f"全{len(paragraphs)}項"
            return (
                f"第{article_num}条 "
                f"{(f'第{paragraph_num}項 ' if paragraph_num else '')}"
                f"第{item_num}号 を特定できなかった（正規化後の号番号: {want_item}）。\n"
                f"{scope}には号が合計{total_items}件ある。"
                "item_num の書式が違う可能性がある。"
                "list_items=True で号の一覧を確認すること。"
            )

        render_item = _flatten_text if flat else (lambda node: _render_single(node, 2))
        lines = [f"【{law_id} 第{article_num}条 第{item_num}号】"]
        if len(hits) > 1:
            where = "、".join(f"第{pn}項" for pn, _ in hits)
            lines.append(
                f"※ 第{item_num}号は複数の項（{where}）にヒットした。"
                "paragraph_num も指定して絞り込むこと。"
            )
        for pn, node in hits:
            prefix = f"[第{pn}項]\n" if len(hits) > 1 else ""
            lines.append(prefix + render_item(node))
        if suppl_note:
            lines.append(suppl_note)
        if want_paragraph:
            # 項・号とも指定済みでこれ以上絞り込めないため、案内は
            # 「絞り込め」ではなく全文の確認方法にする。
            guidance = (
                "項・号まで指定しているためこれ以上は絞り込めない。"
                "全文は e-Gov法令検索で確認すること。"
            )
        else:
            guidance = "paragraph_num で項を絞り込むこと。"
        return _finish_with_truncation("\n\n".join(lines), guidance)

    render = _flatten_text if flat else render_article_structured

    lines = [f"【{law_id} 第{article_num}条】"]

    if len(main_hits) > 1:
        lines.append(
            f"※ 本則内に同じ条番号が{len(main_hits)}件ヒットしました。"
            f"条文構造の読み違いの可能性があるため、結果を確認してください。"
        )
    if not main_hits:
        lines.append("（本則には該当条文なし）")

    for _, _, article_node in main_hits:
        lines.append(render(article_node))

    if suppl_hits:
        if include_suppl:
            lines.append(f"--- 附則（{len(suppl_hits)}件） ---")
            for _, label, article_node in suppl_hits:
                lines.append(f"[{label}]\n{render(article_node)}")
        else:
            lines.append(
                f"※ このほか附則に同じ条番号が{len(suppl_hits)}件あります"
                f"（経過措置の可能性）。include_suppl=True で取得できます。"
            )

    body = "\n\n".join(lines)
    return _finish_with_truncation(
        body,
        "paragraph_num・item_num で項・号を指定するか、list_items=True で号の一覧を確認すること。",
    )


# --------------------------------------------------------------------------
if __name__ == "__main__":
    serve(mcp)
