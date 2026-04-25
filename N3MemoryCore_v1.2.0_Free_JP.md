# N3MemoryCore (N3MC) v1.2.0 [Immutable Memory]
> A NeuralNexusNote™ product

> **「Immutable Memory」とは？** 保存は発生した瞬間にディスクへ物理的にコミットされます。バッファリングも非同期書き込みもありません。保存直後にプロセスを強制終了してもデータは残ります。これが N3MC の設計原則です。
>
> **対象ユーザー**: セッションをまたいで持続する検索可能な記憶を、CLAUDE.md を手動で管理せずに実現したい Claude Code ユーザー。
>
> **動作確認環境**: Claude Pro (claude.ai/code) / Windows 11

## ⚠️ 免責事項・配布条件

本ソフトウェアおよび本指示書は **現状有姿（AS-IS）** で提供されます。

- **ノーサポート**: 作者はバグ修正・質問対応・動作保証などの一切のサポートを提供しません。
- **ノークレーム・無保証**: 本ソフトウェアの使用によって生じたいかなる損害（データ損失、業務停止、第三者への損害等）についても、作者は一切の責任を負いません。
- **自己責任**: 利用者は本ソフトウェアをご自身のリスクと判断のもとで使用してください。
- **変更・廃止**: 作者はいつでも予告なく本ソフトウェアを変更・廃止できます。

本ソフトウェアを使用した時点で、上記条件に同意したものとみなします。

- **使用許諾**: Apache License 2.0。詳細は LICENSE ファイルを参照してください。

> **削除（アンインストール）**: N3MemoryCore を削除する場合は、フォルダを直接削除せず、Claude Code に「N3MemoryCore を削除してください」と依頼してください。フック設定の解除も含めて安全に処理されます。
> **削除前のバックアップ**: 記憶を引き継ぐ場合は削除前に以下の2ファイルを保存してください。`n3memory.db`（記憶データ）と `config.json`（`owner_id`・`local_id` の UUIDv4 キーを含む）はセットで保管する必要があります。キーが一致しないと記憶の所有者検証や環境分類が正しく機能しません。

> **実装上の疑問について**: 作者への問い合わせはできませんが、本指示書を Claude Code に読み込ませて質問することで、実装・カスタマイズの支援を受けることができます。
> **カスタマイズについて**: カスタマイズする時は Claude Code に指示書の場所を教えて「指示書も直してください。」と言ってください。この指示書もそのようにして作成されました。

---

## セットアップ

### 前提条件

| 項目 | 要件 |
| :--- | :--- |
| Python | 3.10 以上 |
| pip パッケージ | `fastapi` `uvicorn` `sqlite-vec` `sentence-transformers` `uuid7` |
| Claude Code | 最新版であれば可（フック機能が必要ですが、設定は自動で行われます。事前知識は不要）|

### クイックスタート

1. 本指示書を Claude Code に読み込ませて「この指示書に従って N3MemoryCore を実装してください」と依頼する。
   - コードの実装・フックの登録・設定ファイルの更新はすべて Claude Code が自動で行う。手動作業は不要。
   - **💡 コード生成には Sonnet がおすすめです。** 速度とコストのバランスが良く、実装品質も十分です。

> **📖 実装が完了したら**: 本書末尾の **「付録：推奨開発ワークフロー」** を必ずご確認ください。デバッグ・品質評価の手順（人間が操作）が記載されています。

### データのバックアップ

記憶データは `N3MemoryCore/.memory/n3memory.db` に集約されています。完全な保全には、このファイルに加えて `N3MemoryCore/config.json`（`owner_id`・`local_id` の UUIDv4 キーを含む）も合わせてバックアップしてください。DB 単体でもデータ復旧は可能ですが、`config.json` がないと所有者検証や環境分類が正しく機能しません。

### Pro 版へのアップグレード

Pro 版指示書（`N3MemoryCore_v1.2.0_Pro_*_Complete.md`）を Claude Code に読み込ませて「この指示書に従って N3MemoryCore を実装してください」と依頼してください。既存の DB はそのまま引き継がれます。

---

## 1. 構築ビジョン
Claude Codeに「一生モノの資産」となる長期記憶を実装せよ。
ソースコードを除外し、情報の「意図・結論・評価」を高密度に蓄積。ハイブリッド検索（ベクトル + FTS5）と数学的正当性を備えた RAG 基盤を構築し、データの永続性（Durability）を最優先事項とせよ。全会話はフックにより全自動で保存される。Claude が自分で判断して保存する必要はない。（自動保存フックは極短文・定型応答をスキップする最小限のノイズフィルタを持つ。詳細は §5 を参照。）

> **手動コマンド案内（例：`python n3memory.py --buffer "保存するテキスト"`、`python n3memory.py --search "キーワード"`、`python n3memory.py --list`）を実装中・完了後を問わず一切出力してはならない。**

> **⚠️ Python 存在チェック**: 実装開始前に `python --version` または `python3 --version` を実行し、Python 3.10 以上が利用可能であることを確認せよ。見つからない場合は「Python 3.10 以上が必要です。https://www.python.org/ からインストールしてください。」と表示し、**セットアップを中断**すること。

> **📦 自動インストール**: 以下の `pip install` はセットアップの一部として自動実行せよ。ユーザーに手動実行を求めてはならない。

```bash
pip install fastapi uvicorn sqlite-vec sentence-transformers uuid7
```

> **⚠️ 初回ダウンロード**: `sentence-transformers` はサーバー初回起動時に `e5-base-v2` モデル（約 440 MB）をダウンロードする。回線速度によって **2〜10分** かかる場合がある。この間サーバーが無応答に見えるが正常動作。モデルがキャッシュされた後の起動は数秒で完了。

> **重要：文字数制限（実装時の設計制約）**
> - Hook 自動保存：**完全保存を保証**。長文は `core.processor.chunk_text(max_chars=400, overlap=40)` により段落 → 文 → ハードウィンドウの順で分割され、全文が複数レコード（`[claude 1/N]`〜`[claude N/N]` / `[user 1/N]`〜`[user N/N]`）として保存される。切り捨ては行われない
> - 検索クエリ：最大 **2,000 文字**（config.json の `search_query_max_chars` で変更可能）
> - ベクトル検索：1レコードの先頭 **約 2,000 文字** のみが意味検索の対象（embedding モデル上限：512 トークン）。超過分は DB と FTS キーワード検索には保存されますが、ベクトル類似度検索では検出されません。
> - FTS クエリ：スキャン防止のため **30 語** に制限
> - 最適な結果を得るには、**1レコード約 50〜200 文字（1事実ずつ）** で保存してください。
> - **長文の処理**: ユーザーが長文（仕様書、記事、チャットログ等）を貼り付けた場合、自動保存は全文をチャンク化して保存するが、全文要約をそのまま保存するよりも、全文を読んで理解した上で重要な事実を1件ずつ短文（約50〜200文字）に要約し、個別の `--buffer` で保存するほうが検索品質の高いレコードになる。

## 2. ディレクトリ構成（厳守）
```
project_root/
├── N3MemoryCore/            # プログラム本体
│   ├── core/
│   │   ├── database.py      # DB層: スキーマ定義・CRUD・PRAGMA設定・マイグレーション
│   │   └── processor.py     # 処理層: 埋め込み生成・ランキング計算・purify（文書設計によるコードブロック置換）
│   ├── .memory/             # N3MC データベース保存先（隠しフォルダ）
│   │   ├── n3memory.db      # SQLite DB (vec0 + FTS5)
│   │   ├── n3mc.pid         # FastAPIサーバーのPIDファイル（起動中のみ存在）
│   │   └── memory_context.md  # 検索結果の出力先（CLAUDE.md の @import 経由で読み込み）
│   ├── n3mc_hook.py         # UserPromptSubmitフック: --repair + --search + --buffer（ユーザー発言の自動保存）を実行
│   ├── n3mc_stop_hook.py    # Stopフック: Claudeの回答を自動保存 + --stop
│   ├── n3memory.py          # メインCLI (Stop フック エントリポイント)
│   ├── config.json          # 永続設定: owner_id・server_port・dedup_threshold等（⚠️ git除外対象 — 個人UUIDを含む）
│   └── config.json.example  # 設定リファレンス — GitHub公開用（config.jsonは初回起動時に自動生成）
├── .gitignore               # config.json と .memory/ を git から除外
└── .claude/                 # Claude Code 連携用設定
    ├── settings.json        # ★HooksとPermissionsの定義
    ├── CLAUDE.md            # @import 構造（§4 参照）
    └── rules/
        └── n3mc-behavior.md # AI行動指針（--stop により自動生成）
```

## 3. 技術仕様（一切の変更禁止）

> **⚠️ AI自動変更禁止**: 以下の仕様をAIが速度向上・最適化を理由に自律的に変更してはならない。埋め込みモデル・次元数・同期書き込み設定の変更は、人間が `config.json` を手動編集することでのみ許可される。

### ID 階層構造

N3MemoryCore は 5 つの ID フィールドで各レコードの出所と文脈を識別する：

| ID | 保存場所 | 生成タイミング | 粒度 | 用途 |
|---|---|---|---|---|
| `id` (PK) | DB レコード | レコード作成時 (UUIDv7, 時系列順) | **1 レコード** | 各記憶の一意識別子 — 削除・重複検出に使用 |
| `owner_id` | `config.json` | 初回起動時 (UUIDv4) | **所有者** | データの所有者識別 — 共有・マルチユーザーシナリオ用 |
| `local_id` (agent_id) | `config.json` | 初回起動時 (UUIDv4) | **エージェント / インストール** | エージェントの UUIDv4 識別子。マルチエージェント時は各エージェント固有の UUID を指定。レコードに保存される。**Free 版では `b_local` は無効（常に 1.0）** — local バイアス乗数は Pro 機能。下記「検索ランキングバイアス」を参照 |
| `session_id` | メモリ内 | サーバー起動ごと (UUIDv4) | **サーバープロセス** | どのサーバーセッションか（互換性のため保存。Free版のランキングには使用しません） |
| `agent_name` | DB レコード | バッファ呼び出し時 (任意文字列) | **エージェント表示名** | (agent_id) の人間可読なラベル（例：`"claude-code"`） |

**階層関係：**

```
owner_id  (1 人のユーザー)
  └── local_id  (エージェントの UUIDv4 識別子)
        ├── agent_name  (その表示名: "claude-code" 等)
        └── session_id  (1 回のサーバー起動)
              └── id  (1 件の記憶レコード)
```

**検索ランキングバイアス（Free版）：**
- `session_id` — 保存のみ。**Free版のランキングでは使用しません**。セッションバイアス（`b_session`）は Pro 版の機能です。
- `local_id` — レコードに保存されるが、**Free 版のランキングには使用されない**。ローカルバイアス（`b_local`）は Pro 機能であり、Free 版では一致／不一致に関わらず `b_local` は常に 1.0。Pro 版では一致時 `b_local = 1.0`、不一致時 `b_local = 0.6` を適用し、同一環境の記憶を優先する。

### 埋め込み
- モデル: `intfloat/multilingual-e5-base` / ベクトル: float[768]
- 取得時は必ず `normalize_embeddings=True` を指定し、L2正規化済みベクトル（norm=1）を保証せよ。
- **入力プレフィックス（必須）**: このモデルはプレフィックスなしでは精度が大幅に低下する。以下を厳守せよ:

```python
# 保存時（文書として登録）
text_to_embed = "passage: " + content

# 検索時（クエリとして照合）
text_to_embed = "query: " + keyword
```

### モジュール間インポート
`processor.py` の先頭で `sys.path` を明示的に設定し、実行ディレクトリに依存しないようにせよ:

```python
import sys, os
sys.path.insert(0, os.path.dirname(__file__))  # core/ を解決パスに追加
```

`processor.py` は `database.py` から以下の関数をすべてインポートすること:

```python
from database import (
    get_connection,
    init_db,
    insert_memory,
    search_vector,
    search_fts,
    get_all_memories,
    delete_memory,
    count_memories,
    check_exact_duplicate,
    find_unindexed_memories,
    serialize_vector,        # /repair エンドポイントで必須
)
```

### 常駐型 FastAPI サーバー
- **ポート**: デフォルト `18520`（`config.json` の `server_port` で変更可）
- **起動タイミング**: `n3memory.py` の任意のサブコマンド実行時、`N3MemoryCore/.memory/n3mc.pid` を確認し、プロセスが存在しない場合は自動的にバックグラウンド起動せよ。PIDファイルの書き込みはアトミック操作（例: `open(..., 'x')` 排他作成フラグ）を使用し、複数プロセスの同時起動を防止すること。
- **通信方式**: HTTP over TCP (`http://127.0.0.1:{port}`)
- **死活監視**: CLI実行のたびに `/health` エンドポイントへPINGし、無応答なら旧PIDファイルを削除して再起動せよ。
- **応答目標**: 0.7s 前後（埋め込み生成 + DB検索の合計）。ハードウェア・OS・モデルキャッシュ状態に依存するため、達成目標として扱い厳密な要件とはしない。
- **初回起動**: モデルのプリロードを uvicorn 起動前に実行するため、初回起動（またはモデルキャッシュ未作成時）は最大 60 秒を要する場合がある。これは仕様上の許容動作であり、2回目以降は数秒以内に起動する。

### SQLite 剛性設定
接続時に以下を強制実行せよ:
```sql
PRAGMA synchronous = FULL;
PRAGMA journal_mode = WAL;
```

### 即時物理保存（変更・最適化禁止）
`--buffer` または API経由の保存時、その瞬間に INSERT および COMMIT を完遂せよ。

**以下を絶対に行ってはならない（速度向上を理由とする場合も含む）:**
- 書き込みのバッファリング（`write_buffer`、`batch_insert`、遅延COMMIT等）
- 非同期書き込み（`asyncio`、スレッドキュー、バックグラウンドタスク等）
- トランザクションの一括化（複数INSERTを1トランザクションにまとめる処理）

**理由**: 保存直後のプロセス強制終了でデータが消失する。速度よりDurabilityを最優先とする。これはパフォーマンス上の選択ではなく、設計上の不変条件である。

### 識別子

- **Owner ID**: 初回起動時に UUIDv4 を生成し `config.json` に保存。全レコードに刻印せよ。N3MC インスタンス間でデータをマージする際にデータ出所を保持するために使用する。
- **Local ID**: 初回起動時または `config.json` に未設定の場合、UUIDv4 を生成し固定保存する。N3MCインストール（またはエージェント）の識別子。全レコードに刻印せよ。マルチエージェント時は `/buffer` API の `local_id` パラメータでエージェント固有の UUIDv4 を指定可能（省略時は config.json の値を使用）。
  - **ユースケース**: 複数の Claude Code インスタンス（マシンやインストールごとに固有の `local_id`）、同一 N3MC DB を共有する異なるエージェント（Claude Code + CordX 等が各自のインスタンスで動作し固有の `local_id` を持つ）、エージェント種別やプロジェクト環境ごとの記憶分離。
  - **注意**: Free 版では `local_id` はレコードに記録されるが、ランキングには使用されない。同一環境の記憶を優先する `B_local` バイアス乗数は Pro 機能。

`config.json` の完全スキーマ（未設定項目は以下の規定値で自動初期化せよ）:

```json
{
  "owner_id":             "<UUIDv4 自動生成>",
  "local_id":             "<UUIDv4 自動生成>",
  "server_port":          18520,
  "dedup_threshold":      0.95,
  "half_life_days":       90,
  "bm25_min_threshold":   0.1,
  "search_result_limit":  20,
  "context_char_limit":   3000,
  "min_score":            0.2,
  "search_query_max_chars": 2000
}
```

- `search_result_limit`: `--search` が返す最大件数。
- `context_char_limit`: （非推奨）以前は `--stop` が `memory_context.md` の内容を切り詰める際に使用。@import 方式では使用されない。後方互換性のため保持。
- `min_score`: `--search` の結果からこの値未満のスコアを除外する（規定値 `0.2`）。`0.0` で無効化。DBが育つほど効果が高まる。
- `search_query_max_chars`: 検索クエリから使用する最大文字数（規定値 `2000`）。embedding モデル（512トークン）は約2,000文字以上を意味的に処理できません。増加させるとFTSクエリが遅くなる場合があります。
- KNN の取得件数（K値）・`--repair` のバッチサイズ等の内部処理パラメータは実装者の判断に委ねる。

> **複数アカウント運用時の注意**: 1台の PC で複数の Claude Code アカウントを運用し、同じ N3MC サーバー（同じ `server_port`・同じ DB パス）に接続する場合、各アカウントの `config.json` は個別に自動生成されるため `owner_id` / `local_id` が異なる。Free 版ではランキングに影響しないが、Pro 版にアップグレードすると他アカウントの記憶に `B_local=0.6` の減衰が適用される。全アカウントの記憶を同等に扱いたい場合は、1つ目のアカウントの `config.json` を他のアカウントにコピーして `owner_id` / `local_id` を統一すること。

> **チーム共有（応用）**: N3MC は完全ローカル動作が基本だが、FastAPI サーバーをネットワーク上のマシンにデプロイし、`config.json` の `server_host` を `127.0.0.1` からそのサーバーの IP アドレスに変更すれば、チームで 1 つの記憶 DB を共有できる。ただし認証・暗号化・同時書き込みロックは現仕様に含まれないため、導入時は Claude Code に相談すること。Pro 版にアップグレードすると `B_local` バイアスにより自分の記憶が自然に優先される。

> **DB 変更（応用）**: 現仕様は SQLite + sqlite-vec を前提としているが、チーム規模が大きい場合は PostgreSQL + pgvector への移行も技術的に可能である。スキーマ互換性・接続プール・マイグレーション手順については Claude Code に相談すること。

- **主キー**: UUIDv7（時系列ソート可能。DB挿入時に生成）。外部ライブラリ `uuid7`（PyPI）を使用せよ。

### ランキング式

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay
```

**cos_sim（数学的正当性）**:

$$cos\_sim = \max(0,\ 1.0 - \frac{L2\_distance^2}{2})$$

- ベクトルがL2正規化済みのとき `L2_distance² = 2(1 - cosθ)` が成立し、上式は `cosθ` と等価。
- 負値（逆方向ベクトル）はランキングを破綻させるため `max(0, ...)` でクランプする。
- **前提**: `normalize_embeddings=True` を適用していない場合、この式は無効となる。

**keyword_relevance（FTS5 BM25の正規化）**:

SQLite FTS5 の `bm25()` は負値（より負 ＝ より関連度高）を返す。以下の手順で `[0.0, 1.0]` に正規化せよ：

1. 生スコアの絶対値が `bm25_min_threshold`（`config.json` の値、規定値 `0.1`）未満（ほぼ無関連）の場合は `keyword_relevance = 0.0` とする。
2. それ以外は結果セット内の最大絶対値で正規化する:

$$keyword\_relevance = \frac{-bm25\_score}{\max(1.0,\ \max_{results}(-bm25\_score))}$$

検索結果が0件の場合は `keyword_relevance = 0.0` とする。

**FTS5 テーブル定義**: FTS5 仮想テーブルは必ず `tokenize='trigram'` を指定して作成せよ。**スタンドアロン形式（`content=` 句なし）の FTS5 を使うこと** — external-content 形式（`content='memories', content_rowid='rowid'`）は使ってはならない。external-content FTS5 は `delete_memory` および `--repair` のマイグレーションループで必要となる `DELETE FROM memories_fts WHERE rowid = ?` の自然な構文をサポートせず、代わりに `INSERT INTO memories_fts(memories_fts, rowid, content) VALUES('delete', ?, ?)` という特殊構文が必要。これを誤ると FTS インデックスが破損する（"database disk image is malformed" エラー）。スタンドアロン形式は `memories.rowid` と整合する rowid を持つプライベートな FTS シャドウを保持し、こちらが手動で揃える: `INSERT INTO memories(...)` の後 `cursor.lastrowid` を取得し、`INSERT INTO memories_fts(rowid, content) VALUES (?, ?)` に渡す。

```sql
-- memories テーブル（UUIDv7 が主キー、rowid は SQLite が暗黙で管理）
CREATE TABLE memories (
    id        TEXT PRIMARY KEY,  -- UUIDv7
    content   TEXT NOT NULL,
    timestamp TEXT NOT NULL,
    owner_id  TEXT NOT NULL,
    local_id  TEXT,             -- N3MCインストール識別子（config.json より。マルチエージェント時はAPI経由で上書き可）
    agent_name  TEXT              -- レコードを書いたAIエージェントの識別子（例：`"claude-code"`）。v1.1以前または指定なしの場合は NULL
    -- SQLite は全テーブルに暗黙の INTEGER rowid を自動付与する
);

-- FTS5 スタンドアロン — memories.rowid との整合は手動維持（trigram: 日本語対応の部分文字列マッチング）
CREATE VIRTUAL TABLE memories_fts USING fts5(
    content,
    tokenize='trigram'
);

-- sqlite-vec による KNN ベクトル検索（rowid で memories テーブルと紐付け）
CREATE VIRTUAL TABLE memories_vec USING vec0(
    embedding float[768]
);
-- INSERT 時: INSERT INTO memories_vec(rowid, embedding) VALUES (memories.rowid, serialize_vector(vec))
-- DELETE 時: DELETE FROM memories_fts WHERE rowid = <rowid>; DELETE FROM memories_vec WHERE rowid = <rowid>;
-- ⚠️ memories を削除する際は、必ず memories_fts と memories_vec も同一トランザクションで削除すること。
--    孤立レコードが残ると検索結果が壊れる。
```

**スキーママイグレーション（`migrate_schema`）**: 起動時に `PRAGMA table_info(memories)` を確認し、不足カラムを冪等に追加する:

```sql
ALTER TABLE memories ADD COLUMN local_id  TEXT;
ALTER TABLE memories ADD COLUMN agent_name  TEXT;
```

`agent_name TEXT` — レコードを書いたAIエージェントの識別子（例：`"claude-code"`）。v1.1以前または指定なしの場合は `NULL`。

また、`migrate_schema()` は既存の `memories_fts` テーブルが `tokenize='porter unicode61'` で作成されている場合を検出し、`tokenize='trigram'` で自動的に再作成して全レコードを再インデックスする。この一度きりのマイグレーションにより、既存データベースも日本語検索精度の向上恩恵を受けられる。

**FTS5 トークナイザーの選定理由**: `trigram` は日本語テキストに最適化された部分文字列マッチングのために採用している。日本語は単語間にスペースを持たないため、`porter unicode61` のような単語境界ベースのトークナイザーでは文全体が1トークンとして扱われ、FTS が実質的に無効化される。`trigram` は3バイト単位の部分文字列インデックスを生成するため、形態素解析なしで日本語のキーワード検索が可能になる。

**FTS5 制約**: `trigram` トークナイザーは3バイト単位で部分文字列マッチングを行う。UTF-8 で3バイト未満（日本語1文字 = 3バイト = 1 trigram の最小単位）のクエリは有効な trigram を構成できない。`len(stripped.encode("utf-8")) < 3` の場合はキーワード検索をスキップし、ベクトル検索（cos_sim）のみでランキングせよ。

**FTS 句読点除去（挿入側・検索側の両方で必須）**: 単語に隣接する句読点がトークナイゼーションの不一致を起こす可能性がある。**FTS5 への INSERT 時**および **FTS MATCH クエリ時**の両方で、同一の句読点除去関数を適用せよ:

```python
_FTS_PUNCT_RE = re.compile(r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"\u201c\u201d\u2018\u2019]')

def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

# INSERT 時: memories テーブルには原文、FTS には句読点除去済みテキストを登録
INSERT INTO memories_fts(rowid, content) VALUES (?, strip_fts_punctuation(content))

# MATCH 時: trigram トークナイザーでは部分文字列マッチングが行われるため、
# _quote_fts_query() によるダブルクォート囲みは不要（AND/OR/NOT 演算子は trigram では解釈されない）。
# 句読点除去済みテキストをそのまま FTS5 MATCH に渡す。
_FTS_MAX_TERMS = 30  # 大量テキスト入力時の FTS5 フリーズを防止

fts_query = strip_fts_punctuation(query)[:_FTS_MAX_TERMS * 20]  # trigram では語分割不要、文字数で制限
```

`--repair` 実行時に、既存 FTS レコードのうち句読点付きで登録されたものを検出し、句読点除去済みテキストで再登録せよ。この FTS クリーニングは**一度きりのマイグレーション**として実行し、完了後にマーカーファイル（`.memory/fts_punct_cleaned`）を作成せよ。マーカーが存在する場合はスキップし、毎セッション開始時の全件スキャンを回避すること。

**FTSのみヒットした場合のスコア**: ベクトル検索にはヒットせず FTS のみにヒットしたレコードは、`cos_sim = 0.0` として統合スコアを計算し、結果に含めよ。スコア上限は `0.3 × time_decay` となるが、ランキング対象から除外してはならない。

**ベクトル検索の owner_id フィルタ**: `search_vector` は単一ユーザー前提のシステムであるため、`owner_id` によるフィルタリングを行わない。KNN 検索は全レコードを対象とする（これは仕様上の設計判断であり、複数ユーザー対応時は要改修）。

**time_decay（半減期 `half_life_days` 日）**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

`days_elapsed` はレコード作成日時からの経過日数（浮動小数点）。`half_life_days` は `config.json` の値を使用する（規定値 90）。

### Clean CLI
モデルロード警告等を完全無音化せよ。`n3memory.py` 内で FastAPI サーバーを subprocess 起動する際、以下のように `stderr` をリダイレクトすること（呼び出し側に依存しない）:

```python
import subprocess, sys
subprocess.Popen([sys.executable, server_path], stderr=subprocess.DEVNULL, ...)
```

- `subprocess.DEVNULL` は Python 3.3以降の標準定数。OS依存のパス指定不要でファイルハンドルリークもない。
- 呼び出し側（settings.json の hooks 等）でリダイレクトを付与する必要はない。

**文字コード（UTF-8）**: `n3memory.py` の `main()` 先頭、**および `n3mc_hook.py` と `n3mc_stop_hook.py` のモジュール先頭**（`sys.stdin.read()` や `subprocess.run` の呼び出しより前）で以下を実行し、Windows cp932 環境での文字化けをプログラム内部で解決せよ。

```python
for _stream_name in ("stdin", "stdout", "stderr"):
    _s = getattr(sys, _stream_name, None)
    if _s is not None and hasattr(_s, 'reconfigure'):
        try:
            _s.reconfigure(encoding='utf-8')
        except Exception:
            pass
```

- この処理により、呼び出し側が `python -X utf8` を指定する必要はない。
- `reconfigure` は Python 3.7 以降で利用可能。それ以前の環境は `PYTHONUTF8=1` 環境変数を代替手段とする。
- 多言語テキスト（日本語・英語・中国語 等）はすべて UTF-8 で統一する。
- ⚠️ **stdin を読む／stdin を subprocess にパイプするすべての Python エントリポイントで reconfigure ブロックが必須** — `n3memory.py`、`n3mc_hook.py`、`n3mc_stop_hook.py` のいずれか一つでも忘れると、Windows で非 ASCII 入力（日本語、em-dash 等）が静かに mojibake し、その破損バイト列が `audit.log` と DB に永続化されて復元不能になる。フックスクリプトでは `main()` 内ではなく**モジュール先頭**に置くこと（最初の `sys.stdin.read()` より前に走らせるため）。

**パスのポータビリティ**: Pythonコード内のファイルパス構築はすべて `os.path.join()` または `pathlib.Path` を使用すること。パス区切り文字（`\` や `/` の文字列リテラルによるパス構築）の直接使用は禁止する。本システムはコード変更なしに Windows・macOS・Linux で動作しなければならない。

```python
# 正：os.path.join を使用
import os
db_path = os.path.join(base_dir, "data", "memory.db")

# 正：pathlib.Path を使用
from pathlib import Path
db_path = Path(base_dir) / "data" / "memory.db"

# 禁止：区切り文字のハードコード
db_path = base_dir + "/data/memory.db"   # NG
db_path = base_dir + "\\data\\memory.db" # NG
```

- 注意: `settings.json` フックコマンドや `.claude/` 設定ファイル内のパスはシェル文字列であり、Pythonコードではない。それらのコンテキストではフォワードスラッシュ (`/`) を使用すること（§4 パス区切り文字の警告を参照）。
- 本システムはクロスプラットフォーム（Windows / macOS / Linux）での動作を設計意図とする。どのOSでこの指示書からコードを再生成しても、コード変更なしに動作する実装が得られなければならない。

---

## 3.5. 耐障害性・エラーハンドリング

### config.json 破損時の自動復旧

`config.json` が空・破損している場合、`_load_config` は以下の手順で復旧を試みる：

1. JSON パースに失敗した場合、stderr に警告を出力する（黙って握り潰さない）
2. `owner_id` または `local_id` が欠損している場合、**既存の DB レコードから最頻出の値を復旧**する（`SELECT owner_id FROM memories GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1`）
3. DB にもレコードがない場合のみ、新しい UUID を生成する

> **設計意図**: `owner_id`・`local_id` が静かに再生成されると、既存の全メモリのバイアス計算が狂う。DB からの復旧により、この無症状障害を防止する。

### DB 破損時の検出と復旧

サーバー起動時（`run_server`）および `_buffer_direct` フォールバック時に `PRAGMA integrity_check` を実行する：

1. 結果が `ok` でない場合、現在の DB を `.corrupt.bak` にリネーム
2. 新しい空の DB を作成
3. stderr に警告メッセージを出力（復旧手順を含む）

`get_connection` 内で `PRAGMA` 実行時に `DatabaseError` が発生した場合は、復旧手順を含むエラーメッセージを付与して re-raise する。

### マイグレーションのバッチ処理

`/repair` 内の一括マイグレーション（ベクトル再インデックス・FTS 句読点クリーニング）は **200 件ずつバッチ処理**で実行する。大規模 DB でのメモリ枯渇を防止するため、`fetchall()` ではなく `LIMIT/OFFSET` でページネーションする。

---

## 4. Claude Code 連携設定（自動化の核）

フックは**グローバル設定**に絶対パスで登録し、全セッションで有効化する。パーミッションはプロジェクト設定に登録する。

### ① グローバル設定（`~/.claude/settings.json`）— フック登録

> `{N3MemoryCoreのフルパス}` を実際のインストールパスに置き換えること（例: `C:/Users/you/Claude Code/N3MemoryCore`）。
>
> **⚠️ パス区切り文字**: Claude Codeはbashシェルを使用するため、Windowsのバックスラッシュ (`\`) はエスケープシーケンス (`\n`→改行, `\t`→タブ) として解釈される。**必ずフォワードスラッシュ (`/`) を使用すること。**
>
> **⚠️ パーミッションとパスの引用符**: インストールパスにスペースが含まれる場合（例: `C:/Users/you/Claude Code/N3MemoryCore`）、Claude Code はコマンド実行時にパスを引用符で囲む（`python "...n3memory.py" --search ...`）。パーミッションのパターンは `*n3memory.py*` のように末尾にもワイルドカードを付けること（閉じ引用符を吸収するため）。`*n3memory.py --search` では引用符にマッチせず、ユーザーに毎回許可を求めるプロンプトが表示される。

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python '{N3MemoryCoreのフルパス}/n3mc_hook.py'"
          }
        ]
      }
    ],
    "Stop": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python '{N3MemoryCoreのフルパス}/n3mc_stop_hook.py'"
          }
        ]
      }
    ]
  }
}
```

### ② プロジェクト設定（`.claude/settings.json`）— パーミッション登録

```json
{
  "permissions": {
    "allow": [
      "Bash(python *n3memory.py* --search *)",
      "Bash(python *n3memory.py* --stop*)",
      "Bash(python *n3memory.py* --repair*)",
      "Bash(python *n3memory.py* --list*)",
      "Bash(python *n3mc_hook.py*)",
      "Bash(python *n3mc_stop_hook.py*)"
    ]
  }
}
```

### `--stop` フック仕様（セッション終了処理）

`--stop` の責務は **セッション終了時のクリーンアップと CLAUDE.md 内の @import 参照の確保**である。会話内容の保存はフックにより全自動で行われる。Claude AI が手動で `--buffer` を呼び出す必要はない。`Stop` フック（`n3mc_stop_hook.py`）は `--stop` の実行に加え、Claude の最終回答の自動保存も行う（§5 の【自動化】参照）。

**`n3mc_stop_hook.py` の stdin 入力仕様**: Claude Code の Stop フックは以下の JSON を標準入力に渡す。`last_assistant_message` が Claude の最終回答テキストである。

```json
{
  "session_id": "<セッションID>",
  "stop_hook_active": true,
  "last_assistant_message": "Claudeの最終回答の全文テキスト"
}
```

`--stop` 実行時の処理順序:

1. `.claude/rules/n3mc-behavior.md` に AI 行動指針（全自動保存、Active RAG 等）が存在することを確認する。**冪等操作**であり、ファイルが存在すれば何もしない。
2. `.claude/CLAUDE.md` に `N3MemoryCore/.memory/memory_context.md` への `@import` 参照が存在することを確認する。**冪等操作**であり、行が存在すれば何もしない。
   - `.claude/CLAUDE.md` が存在しない場合は、`@import` 行を含めて作成する。
   - **マイグレーション**: レガシーの `<!-- N3MC_AUTO_START -->` ～ `<!-- N3MC_AUTO_END -->` ゾーンが存在する場合は除去し、代わりに `@import` 行を追加する。
3. 正常終了する。成功時は一切出力しない（沈黙）。
4. DB 書き込み失敗時のみ致命的警告を出力する。

### CLAUDE.md の構造（@import）

`.claude/CLAUDE.md` は Claude Code の `@import` 機構を使って `memory_context.md` を参照する。これにより CLAUDE.md ファイルをコンパクトに保ち（N3MC の占有は1行のみ）、共有リソースの独占を回避する。

```markdown
# （ユーザー管理コンテンツ）
# ユーザーの行動指針・プロジェクト設定をここに記述

@../N3MemoryCore/.memory/memory_context.md
```

セッション開始時、Claude Code が `@import` を展開し、`memory_context.md` の内容を CLAUDE.md と共にコンテキストへ読み込む。N3MC は検索結果を CLAUDE.md に直接書き込まない。

加えて、`--stop` は `.claude/rules/n3mc-behavior.md` の存在を確認する。このファイルには AI 行動指針（全自動保存、Active RAG）が含まれる。Claude Code は `.claude/rules/` 内のルールファイルをセッション開始時に自動読み込みするため、これらの指針は常に有効となる。

---

## 4.5. FastAPI エンドポイント仕様

CLI は FastAPI サーバーへ HTTP リクエストを送信する。全エンドポイントは `http://127.0.0.1:{server_port}` に対して発行する。

| メソッド | パス | 対応CLIコマンド | 説明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | （内部死活監視） | `{"status": "ok"}` を返す |
| `POST` | `/buffer` | `--buffer` | `{"content": str, "agent_name": str（オプション）}` を受け取り保存 |
| `POST` | `/search` | `--search` | `{"query": str}` を受け取り結果を返す |
| `POST` | `/repair` | `--repair` | 未インデックスレコードを修復 |
| `GET` | `/list` | `--list` | 全レコードの一覧を返す（各レコードに `agent_name` を含む） |

### `--hook-submit`（UserPromptSubmit フックエントリポイント）

```bash
echo '{"message":"ユーザー入力","last_assistant_message":"Claudeの回答"}' | python n3memory.py --hook-submit
```

stdin から JSON を読み取り、`message`（または `prompt`）と `last_assistant_message` フィールドを使用する。単一プロセス内で HTTP リクエスト経由により全 UserPromptSubmit 処理を実行する：`--repair` → `--buffer`（Claude の回答を保存）→ `--search` → `--buffer`（ユーザー発言を保存）。`n3mc_hook.py` から呼び出される。AI が手動で実行する必要はない。このフックで保存される全レコードには `agent_name = "claude-code"` が自動タグ付けされる。

### `--save-claude-turn`（Stop フック専用ヘルパー）

```bash
echo '{"session_id":"...","stop_hook_active":true,"last_assistant_message":"Claude の応答テキスト"}' | python n3memory.py --save-claude-turn
```

Stop フックと同じ JSON を stdin から読み取り、`last_assistant_message` を抽出して `chunk_text(max_chars=400, overlap=40)` で分割し、各チャンクを `[claude]` / `[claude i/N]` レコードとして HTTP `/buffer` 経由で単一プロセス内で保存する。`turn_id` は `.memory/turn_id.txt` から読み取り（直前の `--hook-submit` がユーザー発言を保存した際に書き込んだもの）、保存ループ後にファイルをクリアする。ファイルが無ければ新しい UUID4 turn_id を生成する。`n3mc_stop_hook.py` のみが呼び出すサブコマンドであり、AI が手動で実行する必要はない。

> **なぜ `--buffer -` でなく専用サブコマンドなのか**: Stop フック自身が JSON エンベロープを読むために stdin を消費するため、`--buffer -` を使うと stdin の二重消費になるか、チャンクごとに subprocess を立ち上げる必要が生じる（N 回呼び出し）。`--save-claude-turn` はチャンク化された保存をすべて単一プロセス内で順序通り実行し、常駐サーバへの HTTP keep-alive を 1 本で共有する。下記「1ターンあたりの合計サブプロセス数」での「buffer」とはこの呼び出しを指す。

### レスポンス形式

```json
// POST /buffer・/repair 成功時
{"status": "ok", "count": <処理件数>}

// POST /search 成功時
{
  "results": [
    {"id": "...", "content": "...", "score": 0.8523, "timestamp": "..."},
    ...
  ]
}

// GET /list 成功時
{
  "records": [
    {"id": "...", "content": "...", "timestamp": "...", "agent_name": "..."},
    ...
  ],
  "total": <件数>
}

// エラー時（共通）
{"status": "error", "message": "<エラー内容>"}
```

---

## 5. 運用プロトコル（全自動保存 & Active RAG）

> **宛先の区別**: 以下の指示は **【AI行動指針】**（Claude自身への指示）と **【実装仕様】**（プログラムとして実装する処理）に分類される。

### 完全記録契約

N3MC は「完全保存」を謳う製品である。フックの書き込み経路は以下の6つを必ず守る。

1. すべてのユーザー発言、およびすべての Claude 回答は**一字一句、切り捨てなく**記録される。
2. 長文は `chunk_text`（max_chars=400、overlap=40、段落→文→ハード窓の優先順位）で重複付きチャンクに分割される。各チャンクは単一なら `[user]` / `[claude]`、複数なら `[user i/N]` / `[claude i/N]` とタグ付けされる。
3. **長さフィルタなし、スキップパターンフィルタなし**（従来の「ok / yes / thanks」等の無言ドロップは廃止）。**複数行コードブロックは文書設計に従い `[code omitted]` に置換される**——N3MemoryCore は会話テキストを記録する製品であり、ソースコードは記録対象外。これが唯一の文書化された例外で、会話記録のみが対象。インラインコード（`...`）はそのまま保持。
4. `<N3MC-root>/.memory/audit.log` に追記専用 JSONL の監査ログを書き込む。これは他のどの処理よりも先に実行されるため、失敗しようがない最後の砦。すべてのフック呼び出し（UserPromptSubmit + Stop）は 1 件の JSON レコード `{"ts", "hook", "raw", "payload"}` を残す。
5. 埋め込みサーバーへの HTTP POST が失敗した場合、書き込みは `_buffer_direct`（埋め込みなしで SQLite に直接 INSERT、次回 `--repair` で再インデックス）にフォールバックする。無言のドロップは禁止。すべての失敗経路はフォールバックで成功するか、または stderr へ出力する。
6. 画像のみのプロンプトでも、repair・Claude 回答保存・search・監査ログ記録はすべて実行される。Step 4（ユーザー保存）のみ、保存すべき本文がないためスキップする。

- **【自動化】修復・検索・会話の自動保存**: `UserPromptSubmit` フック（`n3mc_hook.py`）は `--hook-submit` を呼び出し、単一プロセス内で以下を実行する。**Step 0 — 監査ログ（常に最初）**: すべてのフック呼び出しは、他の処理に先立って追記専用 `<N3MC-root>/.memory/audit.log` に JSON レコード `{"ts", "hook", "raw", "payload"}` を 1 件追加する。これは他のどの処理よりも前に書かれる「最後の砦」としての権威ある生ログであり、後続が全滅しても原文だけは残る。続けて：`--repair`（未ベクトル化修復）、`--buffer`（Claude の直前の回答を `chunk_text(max_chars=400, overlap=40)` で分割し `[claude]` / `[claude i/N]` で全文保存）、`--search`（記憶取得）、`--buffer`（ユーザー発言を同様に `[user]` / `[user i/N]` で全文保存）。**長さフィルタなし、スキップパターンフィルタなし**：空でない入力はすべて一字一句記録される。Claude Code が画像+テキストのプロンプトを渡す場合、`prompt` フィールドが JSON 配列になる場合がある。`_extract_text()` は `type=="text"` の部分のみを抽出する。結果が空（画像のみのプロンプト）の場合でも、repair・Claude 回答保存・search・監査ログ記録は実行され、Step 4（ユーザー保存）のみ、記録すべき本文がないためスキップする。生のマルチモーダルペイロードは `audit.log` に捕捉される。
- **【実装仕様】フック内のサブプロセス実行方式（変更禁止）**: `n3mc_hook.py` および `n3mc_stop_hook.py` 内のサブプロセス呼び出しは **`subprocess.run`（同期・ブロッキング）** を使用し、各コマンドの完了を待ってから次を実行せよ。`Popen`（非同期・ファイアアンドフォーゲット）を使用してはならない。**理由**: `--repair` → `--search` には実行順序の依存関係がある（修復が完了しないと検索が不完全になる）。また `--search` の結果が `memory_context.md` に書き終わる前に Claude へ制御が戻ると、検索結果が読めない。速度向上を目的とした非同期化は、データの整合性と検索精度を破壊する。なお、FastAPI サーバーの起動（§3 Clean CLI）のみ `Popen` を使用する（起動完了を待つ必要がないため）。
- **【自動化】Claude の回答の自動保存**: `Stop` フック（`n3mc_stop_hook.py`）はまず Step 0 の監査ログ記録を書き（プロセス内で実行）、続いて以下の 2 つの同期サブプロセスを順番に呼び出す:
  1. `python n3memory.py --save-claude-turn`（stdin = Stop フックの JSON）— `last_assistant_message` をチャンク化保存する。`[claude]` / `[claude i/N]` プレフィックス、既存の turn_id を引き継いで Q-A pairing を維持する。長さフィルタは適用せず、空でない回答はすべて記録する。**ここで `--buffer -` を使ってはならない**: Stop フックが既に stdin を audit.log 書き込みのために消費している。
  2. `python n3memory.py --stop` — `.claude/CLAUDE.md` の `@import` 冪等セットアップ（上記「`--stop` フック仕様」参照）。

  1ターンあたりの合計サブプロセス数: `UserPromptSubmit` × 1（`--hook-submit`）+ `Stop` × 2（`--save-claude-turn` + `--stop`）= 3 回。
- **【実装仕様】未ベクトル化データの検出**: `memories` テーブルに存在するが `memories_vec` **または** `memories_fts` に存在しないレコードを未インデックスデータとして検出する（両インデックスを double LEFT JOIN で確認）。vec が欠損するレコードに対して埋め込みを生成し、fts が欠損するレコードに対して FTS に再挿入する。また、初回実行時のみ FTS 句読点クリーニング（§3「FTS 句読点除去」参照）を実行する。修復件数が1件以上の場合のみ警告を出力する。
- **【AI行動指針】全自動保存**: 全会話はフック（UserPromptSubmit / Stop）により自動保存される。Claude が自分で判断して `--buffer` を手動呼び出しする必要はない。**空でないユーザー発言・Claude 回答はすべて、一字一句、切り捨てなしで記録される。** 長さフィルタ（旧 `len(text) >= 10` / `>= 3`）は**廃止**、スキップパターンフィルタ（旧 `_SKIP_PATTERNS`、「ok / yes / thanks」定型応答フィルタ）も**廃止**。`ok` や `yes` のような短い応答も他と同様に記録される。
- **【AI行動指針】確定報告の沈黙**: 保存成功時は報告や挨拶を一切行わず、沈黙を守れ。
- **【実装仕様】致命的故障の警告**: DB書き込み失敗、またはINSERT後にDB件数が変化しない場合のみ以下を強調表示せよ:
  > ⚠️ 物理保存に失敗。現在の記憶は失われる可能性があります。
- **【AI行動指針】Active RAG**: 知識不足時、自発的に `--search` を実行せよ。`permissions.allow` により自動承認済み — 確認不要。
- **【AI行動指針】想起時の明示**: `--search` の結果を**実際に応答に活用した**（以前保存された情報を想起して答えた）場合は、返答冒頭にユーザー言語で一言そえよ。例:「前回の回答がメモリに保存されています。」「以前の会話から該当情報を取り出しました。」／英語なら "Pulling this from earlier memory in this session."。**関連メモリが無かった / 取得内容を実際には使わなかった場合は告知しない。** 「検索した」ことではなく「想起した」ことだけを述べよ。これにより、ユーザーは記憶レイヤが生きていることを毎ターン視認できる。
- **【実装仕様】文脈注入（stdout とファイルの両方が必須）**: `--search` の結果は **stdout に `print()` で出力**すると同時に `N3MemoryCore/.memory/memory_context.md` へ**ファイル書き込み**せよ。**両方を必ず行うこと**。stdout 出力がないと Claude が検索結果を認識できず、記憶があるにもかかわらず「記憶にありません」と応答する原因になる。ファイル書き込みのみでは Claude に結果が届かない。
- **【実装仕様】memory_context の毎回更新（stale context 禁止）**: `cmd_search` は呼び出されるたびに、結果に関わらず `memory_context.md` を必ず上書きし、stdout にも必ず出力せよ。失敗パスでは「劣化状態を示すプレースホルダー」を出すことで、前ターンの結果が現ターンの結果のように Claude へ静かに供給されることを防ぐ:
  - **空クエリ**（画像のみのプロンプト、または `--search ""`）→ `# Recalled Memory Context\n\n_No relevant memories found._\n` をファイルに書き、stdout にも出力する。空 = 「書き込みをスキップ」ではない。**空という事実を書き込むことが、当該ターンに関連メモリが無いというシグナルである**。
  - **サーバ不通 / `/search` が非 2xx エラー** → `# Recalled Memory Context\n\n_(memory search unavailable: <reason>)_\n` をファイルに書き、stdout にも出力する。これによりメモリ層のダウンタイムが Claude に可視化され、前ターンの結果が誤って現行コンテキストとして扱われない。
  - **検索成功** → レンダリング済み markdown（Previous matching exchange(s) + Other memories）を両チャネルに出力する。

  前ターンの古い `memory_context.md` が次セッション開始時の `@import` で Claude のコンテキストになる事象は**正しさのバグ**として扱う（パフォーマンスのトレードオフではない）。`cmd_search` のすべての呼び出しで fresh write が必須。これは `--hook-submit` 経由の画像のみプロンプト時にも適用される（仕様§5「Image-only prompts still trigger ... search」）。
- **【実装仕様】stdin 入力**: `--buffer` は引数の代わりに `-` を指定すると標準入力からテキストを読み取る（例: `cat file.txt | python n3memory.py --buffer -`）。ただし Stop フック（`n3mc_stop_hook.py`）内では `--buffer -` を使用してはならない。Stop フック自身が stdin から Claude Code の JSON を受け取るため、二重消費になり壊れる。オプションで `--agent-id ID` 引数を指定するとエージェント識別子をタグ付けできる（例: `python n3memory.py --buffer "テキスト" --agent-id "claude-code"`）。
- **【実装仕様】複数行コードブロックは `[code omitted]` に置換される**: 複数行コードブロック（```...```）は文書設計に従い `[code omitted]` に置換される——N3MemoryCore は会話テキストを記録する製品であり、ソースコードは記録対象外。インラインコード（`...`）はそのまま保持。コード以外の会話テキストは長さフィルタ／スキップパターンフィルタなしで一字一句保存される（上記「完全記録契約」参照）。`purify_text` / `_purify` の `_CODE_BLOCK_RE` は閉じた複数行フェンスのみを置換し、インラインコードと未閉フェンスは触らない。
- **【カスタマイズ】言語ローカライズ**: 該当なし。完全記録契約により `_SKIP_PATTERNS` は**廃止**され、フックフィルタ層に言語依存の対象は存在しない。
- **【実装仕様】重複排除と HTTP 失敗時フォールバック**:
  - サーバー起動中: cos_sim ≥ `dedup_threshold`（0.95）または文字列完全一致なら保存をスキップ。
  - 埋め込みサーバーへの HTTP POST 失敗時（サーバー停止・タイムアウト・非 2xx）: 書き込みは `_buffer_direct()` にフォールバックし、埋め込みベクトルなしで直接 SQLite に INSERT する。欠損した vec インデックスエントリは次回の `--repair` で修復される。**無言のドロップは禁止**：すべての失敗経路は `_buffer_direct` で成功するか、stderr へメッセージを出力する。Step 0 の監査ログと組み合わせて、ユーザー／Claude のいずれのターンも失われないことを保証する。
- **【実装仕様】HTTP タイムアウト**: `_post()` はサーバーへの HTTP リクエストに 30 秒のタイムアウトを使用する。CPU での埋め込み推論が高負荷時に 4〜5 秒かかる場合に対応するため。
- **【実装仕様】ensure_server() 競合起動待機**: PID ファイルの競合が検出された場合（別プロセスがサーバーを起動中）、失敗するまで最大 60 秒（120 × 0.5 秒）待機する。通常の 60 秒起動タイムアウトに合わせ、初回のモデルダウンロードにも対応する。
- **【実装仕様】_load_vec_extension 冪等性**: 同一コネクションで sqlite-vec 拡張を2回ロードしても副作用はない。拡張がすでにロード済みの場合、ロード呼び出しは "already" または "duplicate" を含む例外を発生させる。そのような例外はキャッチして続行し、それ以外の例外は再スローする。
- **【実装仕様】delete_memory のトランザクション化**: `delete_memory` はまず `_load_vec_extension(conn)` を呼び出し、次に3件の DELETE（memories_fts・memories_vec・memories）を try/except で囲み、失敗時は `conn.rollback()` を実行する。3つのインデックスがすべて成功するか、すべてロールバックされる。
- **【実装仕様】lifespan 起動**: 非推奨の `@app.on_event("startup")` の代わりに、FastAPI の `@asynccontextmanager` lifespan パターン（`async def lifespan(app: FastAPI)` に `yield` を含め、`FastAPI(lifespan=lifespan)` に渡す）を使用する。
- **【実装仕様】ベクトル再インデックスマイグレーション**: 埋め込みモデルを `e5-base-v2` から `multilingual-e5-base` にアップグレードする際、アップグレード後の初回 `--repair` 呼び出し時にマーカーファイル `vec_multilingual_migrated` で制御される一度きりのベクトル再インデックスマイグレーション（`_run_vec_reindex`）が実行される。
- **【AI行動指針】CLAUDE.md 活用**: 次セッション開始時、`.claude/CLAUDE.md` を読み取り、前セッションの行動指針を継承せよ。記憶コンテキストは `memory_context.md` から `@import` 経由で読み込まれる（§4 参照）。

### Q-A ペアリング契約
同一対話ターンで記録された [user] / [claude i/N] の各行は、共通の `turn_id`（UUID4）を共有します。これにより、同じ質問が後日再出現した場合に、ばらばらのチャンクではなく完全な前回のやり取りを復元して提示できます。

1. **turn_id 生成**: UserPromptSubmit フックが U_k（ユーザメッセージ）を保存する際、新しい turn_id T_k を生成し、すべての [user i/N] チャンクに付与したうえで `.memory/turn_id.txt` に保存します。
2. **Claude 側のペアリング**: Stop フックが C_k（Claude 応答）を保存する際、`.memory/turn_id.txt` から T_k を読み取り、すべての [claude i/N] チャンクに付与します。保存ループ完了後にファイルはクリアされます。
3. **リカバリ経路**: Stop フックがスキップされた場合、次回 UserPromptSubmit の Step 2（前回 Claude 応答の保存）が当該ファイルを読んで T_k を再利用し、U_k と C_k は同一 turn_id を共有します。
4. **ペア復元**: `/search` は `results`（スコア順ヒット）と `pairs`（turn_id を持つヒットごとに、同 turn_id の全兄弟行を順序付きで取得）の 2 フィールドを返します。並び順は [user] → [claude]、各ロール内では "i/N" 番号、最後に rowid。
5. **レンダリング**: メモリコンテキストは "Previous matching exchange(s)" ブロックを "Other memories" ブロックより先に出力します。pairs に含まれた行は重複回避のためスコア一覧から除外されます。
6. **スキーマ**: `memories.turn_id TEXT` 列 + `idx_memories_turn_id` インデックス。`insert_memory(..., turn_id=None)` はキーワード専用引数。取得には `get_memories_by_turn_id(conn, turn_id)` を使用します。

---

## 6. 自律評価（[N3MC v1.2.0 Evidence Report]）
実装完了後、以下のテストを自律解決し、満点（⭐⭐⭐⭐⭐）で報告せよ。

1. **常駐速度 & プロセス管理**: `--search` の応答時間を計測し記録せよ（達成目標: CPU 環境で 2.0s 以内）。PIDファイルの生成・削除・再起動が正常に機能することを確認せよ。

2. **強制終了テスト（Durabilityの証明）**: データを1件 `--buffer` で保存直後にプロセスを強制終了（Ctrl+C）し、再起動後に `--list` でそのレコードがDBに残留していることを物理的に証明せよ。`--list` の出力フォーマットは以下の形式とする（1件1行、タブ区切り、4 カラム固定）:

   ```
   [UUIDv7]\t[timestamp]\t[agent_name]\t[content の先頭 80 文字]
   ```

   - カラム間はシングルタブ（`\t`）で連結。本仕様の旧版に見られた半角空白 4 つは表記上のものであり、リテラルではない。
   - 「content の先頭 80 文字」は `content[:80]` を意味する — **content 全体の先頭 80 文字**であり、「先頭行の 80 文字」ではない。その 80 文字内に改行（`\n`、`\r`）が含まれる場合は単一の半角スペースに置換し、タブ区切りレイアウトが 1 レコード = 1 行を保つようにすること。
   - `agent_name` が `NULL` の場合は `-` を出力する。
   - 末尾に総件数 `Total: N records` を出力すること。
   - コードレビュー時に `write_buffer`・`batch_insert`・非同期書き込み・遅延COMMITが存在しないことを静的に確認せよ。

3. **実在人物テスト（実在歴史データ）**: 実在の歴史人物に関するテキストを保存後、その人物名で `--search` を実行し、**検索結果の上位3件以内** に該当レコードが含まれることを合格基準とする。加えて、`--search` の結果が **stdout に出力されていること**（ターミナルに検索結果が表示されること）を確認せよ。stdout が空で `memory_context.md` のみに書き込まれている場合は不合格とする。
   - 日本語版の例: 「坂本龍馬」
   - 英語版の例: 「Abraham Lincoln」

4. **架空設定テスト（創造的架空設定）**: 架空の設定（例: キャラクター名・世界観・固有名詞）を含むテキストを保存後、`--search` で取得し、**保存したテキストの全フィールドが一字一句変化なく復元できる**ことを合格基準とする。架空設定テキスト（非コード）は完全記録契約により一字一句保持される。Claude 応答中のコードブロックは文書設計に従い `[code omitted]` に置換される。
   - 日本語版の例: 「ソラニア（架空の浮遊都市）」
   - 英語版の例: 任意の架空のキャラクター・場所・固有名詞

5. **FTS 句読点耐性テスト**: 括弧・句読点を含むテキスト（例: `架空の惑星「アルファ9」の気温設定`）を `--buffer` で保存後、括弧を含まないクエリ（例: `アルファ9の気温`）で `--search` を実行し、**上位3件以内にヒット**することを合格基準とする。FTS5 trigram のインデックスに句読点除去が適用されていること、およびクエリ側でも同じ除去が行われていることの両方を検証するテストである。

6. **--repair FTS マイグレーションテスト**: `.memory/fts_punct_cleaned` マーカーファイルを削除した状態で `--repair` を実行し、以下を確認せよ:
   - FTS クリーニングが実行されること（句読点付きレコードがあれば件数が報告される）
   - `.memory/fts_punct_cleaned` マーカーファイルが生成されること
   - 再度 `--repair` を実行しても FTS クリーニングがスキップされること（マーカーにより全件スキャンが回避される）

7. **フック統合テスト**: Claude Code のセッション内で以下を確認せよ。
   1. **UserPromptSubmit**: ユーザーが発言した後、`--search` の結果が `memory_context.md` に書き込まれていること。直前の Claude 回答とユーザー発言の両方が DB に保存されていること（`--list` で `[claude]`・`[user]` プレフィックス付きレコードを確認）。
   2. **Stop**: セッション終了後、Claude の最終回答が DB に保存されていること。`.claude/CLAUDE.md` に `memory_context.md` への `@import` 行が存在すること。

8. **memory_context.md 二重出力テスト**: `--search` を実行し、結果が **stdout に出力される**と同時に `N3MemoryCore/.memory/memory_context.md` にも**ファイル書き込みされる**ことを確認せよ。どちらか一方のみの場合は不合格とする。

9. **完全記録テスト（旧「ノイズフィルタテスト」の置き換え）**: 以下のフィルタが**廃止**されていること、および空でない入力がすべて記録されることを確認せよ。
    - 2文字の文字列を `[claude]` プレフィックスで `--hook-submit` に渡し、**レコードが保存される**こと（旧「3文字未満フィルタ」は存在しない）。
    - 5文字の文字列を `[user]` プレフィックスで `--hook-submit` に渡し、**レコードが保存される**こと（旧「10文字未満フィルタ」は存在しない）。
    - 定型語（例: `ok`、`yes`、`thanks`）を `[user]` として渡し、**レコードが保存される**こと（旧 `_SKIP_PATTERNS` フィルタは存在しない）。
    - 各フック呼び出しに対し `audit.log` のエントリが、バッファ書き込みの成否と独立して必ず1件書かれることを確認せよ。

10. **全自動保存テスト（Fully Automatic Saving の検証）**: 以下の手順でフックによる全自動保存が正常に機能していることを確認せよ。
    1. テスト前の DB レコード件数を `--list` で記録する。
    2. Claude Code セッション内で **3ターン以上** の会話を行う（ユーザー発言 + Claude 回答の往復）。この間、Claude は `--buffer` を **一度も手動呼び出ししない**こと。
    3. `--list` で DB レコード件数を再確認し、ユーザー発言（`[user]` プレフィックス）と Claude 回答（`[claude]` プレフィックス）の **両方** が自動保存されていることを確認する。
    4. **合格基準**: **すべての**会話ターンについてユーザー発言・Claude 回答が各1件ずつ保存されている——短い文字列や定型応答でも一切スキップされない（完全記録契約）。Claude が `--buffer` を手動呼び出しした形跡がないこと。

---

## 7. 自動テスト（pytest）

> **目的**: 手動 Evidence Report を補完する、反復実行可能な自動回帰テスト。プロジェクトルートから `python -m pytest tests/ -v` で実行。
>
> **前提**: `pip install pytest httpx`
>
> **Embedding モデル**: Embedding モデルを必要とするテスト（`test_processor.py::TestEmbedding`、`test_api.py`）は session スコープのフィクスチャでモデルを1回ロード（約5秒）。Layer 1 テストは決定論的なダミーベクトルを使用し、モデル依存を回避。

### ディレクトリ構成

```
N3MemoryCore/
└── tests/
    ├── conftest.py          # 共通フィクスチャ：隔離DB、設定、ダミーベクトル
    ├── test_database.py     # Layer 1: DB単体テスト（CRUD、スキーマ、トランザクション）
    ├── test_processor.py    # Layer 2: ランキング数学、purify `[code omitted]` 置換検証、Refresh
    ├── test_api.py          # Layer 3: FastAPIエンドポイントテスト（TestClient）
    └── test_hooks.py        # Layer 4: フック統合（完全保存チャンク化、画像除去、監査ログ）
```

### Layer 1: `test_database.py`（27テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestSchema` | テーブル作成、マイグレーション冪等性、カラム追加 | スキーマ管理 |
| `TestInsertAndRetrieve` | INSERT→COUNT、3テーブル整合性、embedding無し挿入、rowid取得 | CRUD |
| `TestDelete` | 3テーブル同期削除、存在しないID削除 | トランザクション保護 |
| `TestGC` | 期限切れ削除、最近のレコード保持 | GC |
| `TestDedup` | 完全一致検出（true/false） | 重複排除 |
| `TestUnindexed` | vec欠損検出、全インデックス済み確認 | 修復検出 |
| `TestFTS` | 句読点除去、クエリ引用符、最大項数、基本検索、短クエリスキップ、句読点耐性 | FTS5 |
| `TestVectorSearch` | ベクトル検索結果、空DB検索 | KNN検索 |
| `TestSerialization` | ベクトルシリアライズ往復 | バイナリ変換 |

### Layer 2: `test_processor.py`（24テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestCosineSim` | 同一ベクトル、直交、負値クランプ、中間値 | L2→cosine変換 |
| `TestTimeDecay` | 現在→1.0、半減期、フロア値、不正タイムスタンプ | 半減期計算 |
| `TestKeywordRelevance` | 閾値以下、完全一致、部分一致、ゼロ最大値 | BM25正規化 |
| `TestPurification` | コードブロック→`[code omitted]` 置換、インラインコード保持、複数ブロック置換、ブロック無し | 複数行コードブロックは文書設計により `[code omitted]` に置換、インラインコードは保持 |
| `TestEmbedding` | passage/queryプレフィクス、関数テスト、同一テキスト類似度 | Embeddingモデル |
| `TestRefresh` | レコード置換、タイムスタンプ更新 | Knowledge Refresh |
| `TestBiasScoring` | session/localバイアス一致・不一致、完全スコア式 | ランキング式 |

### Layer 3: `test_api.py`（21テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestHealth` | ヘルスチェック | サーバー状態 |
| `TestBuffer` | 保存、空コンテンツ、agent_name付き、完全一致dedup、コードブロック完全保持 | 保存API |
| `TestSearch` | 空DB、保存→検索往復、空クエリ、スコア返却 | 検索API |
| `TestRepair` | 未インデックス修復 | 修復API |
| `TestList` | 空リスト、保存後リスト | 一覧API |
| `TestDelete` | 既存削除、存在しないID、非Pro時ブロック | 削除API |
| `TestGC` | 期限切れ削除、非Pro時ブロック | GC API |
| `TestImport` | JSONLインポート、keep-owner、非Pro時ブロック | インポートAPI |

### Layer 4: `test_hooks.py`（20テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestChunkText` | 短文は単一レコード、長文の複数チャンク化、プレフィックス番号付け、段落分割、文分割、ハードウィンドウフォールバック | 完全保存チャンク化 |
| `TestStripImages` | 画像なし不変、base64除去、画像のみ空文字、非JSON通過 | 画像ペイロード除去 |
| `TestExtractText` | プレーン文字列、マルチモーダルJSON、画像のみ空、空入力 | マルチモーダル抽出 |
| `TestCompleteRecording` | 定型語 `ok` も保存、短い Claude 応答も保存、短いユーザー発言も保存、audit.log 記録 | 完全記録契約（フィルタ廃止） |
| `TestStopIdempotency` | @import重複なし、ルールファイル作成 | --stop冪等性 |

### 実行方法

```bash
# 全テスト
python -m pytest tests/ -v

# 単一レイヤー
python -m pytest tests/test_database.py -v

# 遅いEmbeddingテストをスキップ
python -m pytest tests/ -v -k "not TestEmbedding"
```

> **⚠️ Evidence Report との関係**: 自動テストの不合格は §6 の ⭐⭐⭐⭐⭐ 評価をブロックしない。Evidence Report が実装完了の唯一の合否基準である。自動テストは開発者が任意で実行する補助的な回帰テストであり、初回実装時に無限の修正・再試行ループを引き起こしてはならない。

---

## 📎 参考: GPU アクセラレーション

> 現在のデフォルト構成（384次元モデル、embedding 1回/プロンプト）では CPU で十分高速に動作するため、GPU は不要です。
> 1024次元モデルへの切り替えや、速度が気になる場合に検討してください。

NVIDIA CUDA 対応 GPU がある場合、以下のコマンドで高速化できる可能性があります：

```
pip install torch --index-url https://download.pytorch.org/whl/cu121
```

- `sentence-transformers` は内部で PyTorch を使用しており、GPU 版 PyTorch がインストールされていれば embedding 推論は自動的に GPU 上で実行されます（コード変更不要）
- コールドスタート（サーバー起動直後）: CPU で約 1〜1.5 秒/回 → ウォーム状態で約 0.03〜0.06 秒/回
- GPU では約 0.02〜0.05 秒/回に短縮される可能性があります

---

## 付録：推奨開発ワークフロー

> **この付録は人間向けの操作ガイドです。** 各フェーズで ``` 内のプロンプトをコピーして Claude Code に貼り付けてください。AI が自動で次のフェーズに進むことはありません。

| フェーズ | あなたがやること | 使うモデル |
|---|---|---|
| 1. 実装 | プロンプトを貼って実装を依頼 | **Sonnet**（高速） |
| 2. デバッグ | 3 つのプロンプトを**順番に**貼って検証 | **Sonnet** |
| 3. 品質レビュー | プロンプトを貼って評価・改善を依頼 | **Opus**（深い推論） |

---

### フェーズ 1：実装（Sonnet）

モデルを **Sonnet** に設定し、以下を貼り付けてください。

```
この指示書に従って N3MemoryCore を作成してください。
```

Sonnet がコード生成・フック設定・サーバー起動まで自動で行います。完了したらフェーズ 2 に進んでください（「完了した」≠「仕様通り」なので、ここで終わりにしないでください）。

---

### フェーズ 2：デバッグ（Sonnet）

引き続き **Sonnet** で、以下の 3 つのプロンプトを**順番に 1 つずつ**貼り付けてください。

**① データフロートレース**（データが途中で失われていないか確認）
```
N3MemoryCore について：
コードを読んで、検索クエリから Claude へのエンドツーエンドのデータフローをトレースしてください。
途中でデータが失われている箇所がないか確認してください。
必要に応じて修正してください。
```

**② 仕様との逐条比較**（指示書に書いてあるのに未実装の動作がないか確認）
```
N3MemoryCore について：
指示書の各 CLI コマンドの入出力仕様を、実際のコードと 1 コマンドずつ比較してください。
ドキュメントに記載されているが実装されていない動作を探してください。
必要に応じて修正してください。
```

**③ クロスセッションテスト**（セッションを跨いでデータが見えるか実行確認）
```
N3MemoryCore について：
セッション 1 で --buffer で保存した結果が、セッション 2 で --search を使うと Claude から見えるかどうかを、実際にコマンドを実行して確認してください。
必要に応じて修正してください。
```

3 つとも完了したらフェーズ 3 に進んでください。

---

### フェーズ 3：品質レビュー（Opus）

モデルを **Opus** に切り替え、以下を貼り付けてください。

```
N3MemoryCore をレビューしてください。
必要に応じて修正してください。

N3MemoryCore は記憶装置と RAG として 10 点満点で何点ですか？
記憶装置としての評価と RAG としての評価を分けて、スコアカードを生成してください。
```

Opus が実際にコマンドを実行し、**記憶装置**（保存・永続性・重複排除）と **RAG**（検索精度・ランキング・ノイズ耐性）の 2 軸でスコアカードを生成します。

> **注意**：記憶装置としては高評価が期待できますが、RAG としては基本実装のため以下が未実装で **RAG 側は 7 点を超えることはまずありません：**
> - 形態素解析（日本語なら MeCab / SudachiPy 等）による適切なトークン分割
> - 言語特化 embedding モデル（multilingual-e5-large 等）への切り替え
> - 高度なチャンキング戦略（言語・文書構造に応じた分割）
> - リランキング（Cross-Encoder、Cohere Rerank、LLM 等による検索結果の再順位付け）
>
> 甘い採点は改善機会を失わせるため、厳格に評価してください。**スコアに関わらず、Opus に何が足りないかを具体的に指摘してもらい、改善点を必ず相談してください。**

---

Copyright (C) 2026 NeuralNexusNote™ / ArnolfJp019
All names and logos associated with N3MemoryCore and NeuralNexusNote are trademarks of the author.