# egov-law-mcp

e-Gov 法令API（Version 2）を使って、**法令を探す**・**条文を取る**ための MCP サーバです。
Claude などの MCP クライアントから「毒物及び劇物取締法の第3条の2を見せて」のように聞けます。

*An unofficial MCP server for the Japanese e-Gov Law API (Version 2): search laws and fetch articles. Not affiliated with the Digital Agency of Japan.*

- ツールは2本だけ: `search_law` / `get_law_article`
- API キー・登録は不要（e-Gov 法令API は無登録で使えます）
- DB なし。取得結果は SQLite にキャッシュ（既定 30 日）
- 認証は `x-api-key` ヘッダー（`mcp_common/` に同梱。単体でも使えます）

> 非公式のツールです。返す内容は参考情報で、法的助言ではありません。正本は官報および e-Gov 法令検索で確認してください。

## ツール

| ツール | 用途 |
|---|---|
| `search_law(keyword, limit=10)` | 法令名・略称・本文キーワードで法令を探す（法令名の部分一致を先に、足りなければ本文検索で補う） |
| `get_law_article(law_id, article_num, paragraph_num, item_num, list_items, include_suppl, flat)` | 法令ID＋条番号で条文を取る。項・号まで絞れる。条番号を省くと冒頭だけ返す |

条番号は `2` / `二` / `11の2` / `十一の二` / `3-2` / `3_2` のどれでも指定できます。
応答は 8000 文字で切り、長い条は `paragraph_num`・`item_num`・`list_items` で絞る案内を付けます。

## 動かし方（ローカル）

Python 3.10 以上。

```bash
pip install -r requirements.txt
export MCP_API_KEY="$(python -c 'import secrets; print(secrets.token_urlsafe(32))')"   # 32文字以上が必須
python server.py        # http://localhost:8080/mcp （/health は認証なし）
```

試験（実ネットには出ません）:

```bash
pip install -r requirements-dev.txt
pytest                  # 実機テストだけ `pytest -m network`（e-Gov に実際にアクセスします。必要なときだけ）
```

## デプロイ例（Cloud Run）

```bash
gcloud run deploy egov-law --source . --region <REGION> \
  --max-instances 1 \
  --allow-unauthenticated \
  --set-secrets "MCP_API_KEY=<SECRET名>:<バージョン番号>"
```

- 認証はアプリ側のヘッダーキーで行うので、Cloud Run 側の IAM 認証は使わない前提です（`--allow-unauthenticated`）。キーは Secret Manager などに置き、コードや README に書かないでください。
- キャッシュはインスタンスごとのローカルファイルです。`--max-instances 1` を前提にしています。複数インスタンスでも動きますが、キャッシュは共有されません。
- キー入れ替え中だけ `MCP_API_KEY_PREVIOUS` に旧キーを入れると、新旧を重ねて受け付けます。
- Claude.ai のカスタムコネクタでは、認証は「サインインなし」にして、リクエストヘッダーに `x-api-key` を入れます（画面の名称は変わることがあります）。

## 実際に踏んだ失敗と設計判断

- **`/keyword` の `limit` は「法令の件数」ではない。** 本文中のヒット箇所の総数の上限です。1つの法令が何度もヒットすると枠を使い切り、目的の法令が結果から消えます。→ 1法令あたりのヒット数 `sentences_limit` を 1 に絞って、その分 `limit` を大きく取る。
- **API の並び順のまま `limit` で切ると完全一致が落ちる。** 「所得税法」を `limit=1` で引いたら臨時特例法が返りました。→ 広めに取って並べ替え（完全一致 → 法令種別 → 題名の短さ）、そのあとで `limit` に切る。
- **条番号は常にアラビア数字の `Num` 属性にある。** 枝番は `3_2`（`3の2` ではない）。人は「三の二」と書くので、入力側で漢数字・「の」・`-` を `3_2` に揃えて吸収する。
- **附則は既定で混ぜない。** 改正のたびに「第一条＝施行期日」が積み重なり、同じ条番号が何度も出ます。本則だけを返し、附則の件数だけ末尾に注記。必要なら `include_suppl=True`。
- **長い条は切って、続きの取り方を案内する。** 政令の1つの条が数百号・3万字超になることがあります。行の境界で切り、`list_items` で号の一覧を先に見るよう案内。
- **`ToolError` 以外の例外は、クライアントに「Error executing tool」としか伝わらない**（mcp 2.x）。HTTP エラーは境界で `ToolError`（ステータスと応答の先頭）に変換して、原因が見えるようにした。
- **`limit=0`・負数・極端に大きい値は弾く。** 0 は「該当あり」が「見つからなかった」に化け、負数は末尾の一致を無言で落とし、巨大な値は本文検索の要求件数を増幅する。
- **ヘッダー認証の落とし穴（`mcp_common`）。** `Mount` の中に入れると lifespan が走らず全リクエストが 500 になる → 純 ASGI ミドルウェアで包む。401 に `WWW-Authenticate` を付けると Claude.ai が OAuth の探索を始める → 付けない。キーは全件を `hmac.compare_digest` で比較し、32 文字未満なら起動失敗（フェイルクローズ）。
- **キャッシュのテストが本物のファイルを汚した。** 関数のデフォルト引数は定義時に一度しか評価されず、テストで差し替えたパスが効かなかった。→ パスは毎回読み、さらに `tmp_path` の外への `sqlite3.connect` をテストで拒否するガードを置いた（`conftest.py`）。

## 出典・利用条件

法令データは e-Gov 法令検索（デジタル庁）が提供しています。e-Gov 法令検索の[利用規約](https://laws.e-gov.go.jp/terms/)では、権利表記のないコンテンツに**公共データ利用規約（第1.0版、PDL1.0）**が適用されるとされています。要点は次のとおりです（原文を必ず確認してください）。

- **出典を書く。** 例: `出典：e-Gov法令検索（デジタル庁）（https://laws.e-gov.go.jp/）`（このツールの応答にも自動で付きます）。
- **編集・加工して使うときは、出典とは別に、加工したことを書く。** このツールは取得した法令データを整形・切り詰めて返すので、加工にあたります。応答を転載・再利用するときは、例えば「e-Gov法令検索（デジタル庁）（https://laws.e-gov.go.jp/）を加工して作成」のように書いてください。
- **加工した内容を、国が作成したかのように公表・利用しない。**
- 規約には、サイトの休止・廃止・重大な変更は原則 90 日前までに e-Gov のサイトで公表する、とあります。API の仕様や提供条件が変わることがあるので、e-Gov の案内を確認してください。
- `tests/fixtures/` は動作確認用に保存した法令API の応答（毒物及び劇物取締法・毒物及び劇物指定令・労働安全衛生法）です。出典は上と同じです。
- このリポジトリのコードは MIT ライセンスです（`LICENSE`）。法令データそのものには及びません。

## API と負荷について

- 使っているのは **法令API Version 2**（`https://laws.e-gov.go.jp/api/2`）だけです。Version 1 は使っていません。
- **注意:** e-Gov の仕様書は、法令本文取得API が返す **JSON 形式は試行版で、仕様変更が起こりうる**と案内しています。このツールはその JSON を読むので、API 側の変更で動かなくなる可能性があります（`tests/` の実機テストで早めに気づけます）。
- 負荷をかけない作りにしています: リクエスト間隔は最低 0.5 秒、取得結果は 30 日キャッシュ、`limit` は最大 100。
- 全法令を順番に取りに行くような使い方はしないでください。まとまったデータが要るときは、e-Gov が提供する XML 一括ダウンロードを使ってください。

## 作り方

設計と判断は人間(Asobi-korokoro)、実装は **Claude**(Anthropic の AI、Claude Code 経由)が担当しました。上の「失敗と設計判断」は、その作業で実際に起きたことの記録です。コミットにも Claude を共著者として残しています。
