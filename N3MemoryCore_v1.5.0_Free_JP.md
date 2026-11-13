# N3MemoryCore (N3MC) v1.5.0 [Immutable Memory]
> A NeuralNexusNote™ product

> **「Immutable Memory」とは？** 保存は発生した瞬間にディスクへ物理的にコミットされます。バッファリングも非同期書き込みもありません。保存直後にプロセスを強制終了してもデータは残ります。これが N3MC の設計原則です。
>
> **対象ユーザー**: セッションをまたいで持続する検索可能な記憶を、CLAUDE.md を手動で管理せずに実現したい Claude Code ユーザー。
>
> **動作確認環境**: Claude Pro (claude.ai/code) / Windows 11・Ubuntu

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
| pip パッケージ | `fastapi` `uvicorn` `sqlite-vec` `sentence-transformers` `uuid7` `psutil`（**v1.5.0 から必須** — PID ファイルのプロセスが起動中のこのフォルダのサーバーかどうかを CLI が確かめるのに使う。§3「PID ファイルの扱い」参照）|
| Claude Code | 最新版であれば可（フック機能が必要ですが、設定は自動で行われます。事前知識は不要）|

### クイックスタート

導入経路は以下の 2 通りをサポートします。

**経路 A — `pip` で導入する（リファレンス実装をそのまま使う）**

```bash
git clone https://github.com/NeuralNexusNote/n3mc-free.git
cd n3mc-free
pip install -e .          # editable install。または  pip install .
n3mc --init               # ~/.n3mc/ を作成し、Claude Code のフックを登録
```

その後 **Claude Code を再起動**してください。次回セッションから記憶機能が有効化されます。

**経路 B — AI ネイティブセットアップ（指示書から再生成）**

1. 本指示書を Claude Code に読み込ませて「この指示書に従って N3MemoryCore を実装してください」と依頼する。
   - コードの実装・フックの登録・設定ファイルの更新はすべて Claude Code が自動で行う。手動作業は不要。
   - **💡 コード生成には Sonnet がおすすめです。** 速度とコストのバランスが良く、実装品質も十分です。

> **📖 経路 B 実装後**: 本書末尾の **「付録：推奨開発ワークフロー」** を必ずご確認ください。デバッグ・品質評価の手順（人間が操作）が記載されています。

### データのバックアップ

記憶データは `~/.n3mc/` 配下に集約されています。完全な保全には、`~/.n3mc/.memory/n3memory.db` に加えて `~/.n3mc/config.json`（`owner_id`・`local_id` の UUIDv4 キーを含む）も合わせてバックアップしてください。DB 単体でもデータ復旧は可能ですが、`config.json` がないと所有者検証や環境分類が正しく機能しません。データ位置は `N3MC_HOME=/path/to/dir` 環境変数で上書きできます。

### Pro 版へのアップグレード

Pro 版指示書（別途配布。最新版を使う）を Claude Code に読み込ませて「この指示書に従って N3MemoryCore を実装してください」と依頼してください。既存の DB はそのまま引き継がれます。

---

## 1. 構築ビジョン
Claude Codeに「一生モノの資産」となる長期記憶を実装せよ。
ソースコードを除外し、情報の「意図・結論・評価」を高密度に蓄積。ハイブリッド検索（ベクトル + FTS5）と数学的正当性を備えた RAG 基盤を構築し、データの永続性（Durability）を最優先事項とせよ。全会話はフックにより全自動で保存される。Claude が自分で判断して保存する必要はない。（フックは空でないメッセージをすべて全文記録する。文字数フィルタやスキップパターンはない。§5.1 参照。）

> **手動コマンド案内（例：`n3mc --buffer "保存するテキスト"`、`n3mc --search "キーワード"`、`n3mc --list`）を実装中・完了後を問わず一切出力してはならない。**

> **⚠️ Python 存在チェック**: 実装開始前に `python --version` または `python3 --version` を実行し、Python 3.10 以上が利用可能であることを確認せよ。見つからない場合は「Python 3.10 以上が必要です。https://www.python.org/ からインストールしてください。」と表示し、**セットアップを中断**すること。

> **📦 自動インストール**: 以下の `pip install` はセットアップの一部として自動実行せよ。ユーザーに手動実行を求めてはならない。

```bash
pip install fastapi uvicorn sqlite-vec sentence-transformers uuid7 psutil
```

> **⚠️ 初回ダウンロード**: `sentence-transformers` はサーバー初回起動時に `multilingual-e5-base` モデル（約 470 MB）をダウンロードする。回線速度によって **2〜10分** かかる場合がある。この間サーバーが無応答に見えるが正常動作。モデルがキャッシュされた後の起動は数秒で完了。

> **重要：文字数制限（実装時の設計制約）**
> - Hook 自動保存：**完全保存を保証**。長文は `core.processor.chunk_text(max_chars=400, overlap=40)` により段落 → 文 → ハードウィンドウの順で分割され、全文が複数レコード（`[claude 1/N]`〜`[claude N/N]` / `[user 1/N]`〜`[user N/N]`）として保存される。切り捨ては行われない
> - 検索クエリ：最大 **2,000 文字**（config.json の `search_query_max_chars` で変更可能）
> - ベクトル検索：1レコードの先頭 **約 2,000 文字** のみが意味検索の対象（embedding モデル上限：512 トークン）。超過分は DB と FTS キーワード検索には保存されますが、ベクトル類似度検索では検出されません。
> - FTS クエリ：スキャン防止のため **30 語** に制限
> - 最適な結果を得るには、**1レコード約 50〜200 文字（1事実ずつ）** で保存してください。
> - **長文の処理**: ユーザーが長文（仕様書、記事、ログ等）を貼り付けた場合、フックが全文をチャンクに分けて保存する。Claude はそのために `--buffer` を呼ば**ない**（§5.3 と rules ファイルに「`--buffer` を手動で呼ばない」とある）。Claude が返答で要点をまとめれば、その返答も Stop フックで保存され、検索にとってより密度の高い入口がもう 1 つ増える。

## 2. ディレクトリ構成（厳守）

コード本体はリポジトリ内（`n3memorycore/` 配下）に、個人データはリポジトリ外（`~/.n3mc/` 配下）に置く。これにより同一パッケージを誰でも `pip install` でき、他人の記憶と衝突しない。

### リポジトリのレイアウト（`git clone` で取得される範囲）
```
n3mc-free/                       # リポジトリルート
├── pyproject.toml               # ★ pip メタデータとエントリポイント宣言
├── README.md / README_JP.md
├── LICENSE / NOTICE / CHANGELOG.md / PHILOSOPHY.md
├── N3MemoryCore_v1.5.0_Free_EN.md / _JP.md   # 本指示書
├── n3memorycore/                # ★ Python パッケージ（PEP 8 小文字）
│   ├── __init__.py              # __version__ を公開
│   ├── paths.py                 # import 時に ~/.n3mc/（または $N3MC_HOME）を解決
│   ├── n3memory.py              # メイン CLI モジュール（--init / --buffer / --search / --list / --recall-thread / --show-turn / --stop / --repair / --hook-submit / --save-claude-turn / --run-server）+ FastAPI アプリ
│   ├── n3mc_hook.py             # UserPromptSubmit フックエントリ: audit.log 書き込み → --hook-submit 呼び出し
│   ├── n3mc_stop_hook.py        # Stop フックエントリ: audit.log 書き込み → --save-claude-turn → --stop 呼び出し
│   └── core/
│       ├── __init__.py
│       ├── database.py          # DB 層: スキーマ定義・CRUD・PRAGMA 設定・マイグレーション
│       └── processor.py         # 処理層: 埋め込み生成・ランキング計算・purify（文書設計によるコードブロック置換）
├── tests/                       # pytest スイート（§7 参照）
│   ├── conftest.py
│   ├── test_api.py
│   ├── test_database.py
│   ├── test_hooks.py
│   ├── test_hooks_encoding.py
│   ├── test_processor.py
│   └── test_regressions.py      # v1.4.0 / v1.5.0 の修正の回帰テスト（モデル不要、§7 Layer 5）
└── .claude/                     # プロジェクト固有 Claude Code 設定（git 追跡）
    ├── settings.json            # パーミッション許可リスト（フックはユーザー全体の ~/.claude/settings.json に登録 — §4 参照）
    ├── CLAUDE.md                # プロジェクト用ガイダンス（N3MC は何も書き足さない。旧版の memory_context.md への @import は --stop が取り除く）
    └── rules/
        └── n3mc-behavior.md     # AI 行動指針（--stop により自動生成・版の更新）
```

### ユーザーデータのレイアウト（`n3mc --init` が生成。リポジトリには含まれない）
```
~/.n3mc/                            # 必要に応じて $N3MC_HOME で上書き可能
├── config.json                     # 永続設定（完全なスキーマは §3「識別子」）
└── .memory/
    ├── n3memory.db                 # SQLite DB (vec0 + FTS5)
    ├── n3mc.pid                    # FastAPI サーバーの PID ファイル（サーバー稼働中のみ存在）
    ├── audit.log                   # JSONL フック監査ログ（追記専用、他の処理に先立って書き込まれる。コードブロックを含む生のテキスト — §5.4「audit.log の大きさと中身」）
    ├── audit.log.1                 # 1 つ前の世代の audit.log（audit.log が 50 MB を超えたときに作られる）
    ├── server.log                  # バックグラウンドサーバーの stderr（§3「サーバーの起動」。上限 1 MB）
    ├── n3memory.db.corrupt-<YYYYmmddHHMMSS>.bak  # 整合性チェックに失敗したときに退避した DB（-wal / -shm も一緒。§3.5）
    ├── notices.json                # 人が対応すべき知らせ（ポートの重なり、読めない config.json 等。§3「知らせ」）
    ├── .memory_context_retired     # 旧 memory_context.md を片付け済みであることを示すマーカー（§4 --stop）
    ├── turn_id_<session>.txt       # UserPromptSubmit と Stop フック間で Q-A pairing をハンドオフ（Claude Code の session_id ごと。session_id がないときは turn_id.txt。30 日より古いファイルは --stop が削除する）
    ├── fts_punct_cleaned           # FTS5 句読点クリーニングの一回限りマイグレーションマーカー（--repair が touch）
    └── vec_e5v2_migrated           # ベクトル再インデックスの一回限りマイグレーションマーカー（--repair が touch）
```

### コンソールスクリプト（`pip install` でインストール）
| スクリプト名 | 対応モジュール | 用途 |
|---|---|---|
| `n3mc` | `n3memorycore.n3memory:main` | 人間と Claude Code 用の主 CLI |
| `n3mc-hook` | `n3memorycore.n3mc_hook:main` | UserPromptSubmit フック（`n3mc --init` が `~/.claude/settings.json` に登録） |
| `n3mc-stop-hook` | `n3memorycore.n3mc_stop_hook:main` | Stop フック（`n3mc --init` が `~/.claude/settings.json` に登録） |

## 3. 技術仕様（一切の変更禁止）

> **⚠️ AI自動変更禁止**: 以下の仕様をAIが速度向上・最適化を理由に自律的に変更してはならない。埋め込みモデル・次元数・同期書き込み設定の変更は、人間が `config.json` を手動編集することでのみ許可される。

### ID 階層構造

N3MemoryCore は 5 つの ID フィールドで各レコードの出所と文脈を識別する：

| ID | 保存場所 | 生成タイミング | 粒度 | 用途 |
|---|---|---|---|---|
| `id` (PK) | DB レコード | レコード作成時 (UUIDv7, 時系列順) | **1 レコード** | 各記憶の一意識別子 — 削除・重複検出に使用 |
| `owner_id` | `config.json` | 初回起動時 (UUIDv4) | **所有者 / N3MC サーバー** | N3MC FastAPI インスタンス／データ所有者の識別 — 共有・マルチユーザーシナリオやインポート時の出所保持に使用 |
| `session_id` | ホストから供給（フック stdin JSON） または メモリ内フォールバック | 会話単位 | **タスク / プロジェクト / 会話** | 1 つのタスク・プロジェクト・会話スレッドに属する記憶をまとめて浮上させるためのグルーピングキー。Claude Code は UserPromptSubmit / Stop フックの stdin JSON に `session_id` フィールド（会話セッション ID）を供給するため、フックはこれを `/buffer` `/search` に転送する。他のホスト（独自タスク番号、Ollama 風 UI 等）も自前のセッション識別子を API で渡せる。ホストが `session_id` を供給しない場合、または CLI 直接呼び出し（フックを介さない）の場合は、`n3memory.py` 起動時に生成された UUIDv4 にプロセス単位でフォールバックする。Free 版・Pro 版ともに `b_session` ランキングバイアスを駆動する |
| `local_id` | `config.json` / API | 初回起動時 (UUIDv4)、またはリクエストごと | **エージェント / インストール** | 発話するエージェントの UUIDv4 識別子（1 つの Claude Code インストール = 1 つの `local_id`、同一 DB 上の異なるエージェントは別 UUID を持つ）。レコードに保存される。**Free 版では `b_local` は無効（常に 1.0）** — エージェント単位のランキング乗数は Pro 機能。下記「検索ランキングバイアス」を参照 |
| `agent_name` | DB レコード | バッファ呼び出し時 (任意文字列) | **エージェント表示名** | エージェントの人間可読なラベル（例：`"claude-code"`）。`--buffer --agent-id <name>` オプションが設定するのは**この**フィールド（オプション名は互換のため残している。§3.7）。`local_id` を設定することはない |

**階層関係：**

```
owner_id  (1 つの N3MC サーバー / データ所有者)
  └── session_id  (1 つのタスク / プロジェクト / 会話)
        └── local_id  (そのセッション内で発話するエージェント)
              ├── agent_name  (その表示名: "claude-code" 等)
              └── id  (1 件の記憶レコード)
```

**検索ランキングバイアス（Free 版）：**
- `session_id` 一致 → `b_session = 1.0`（不一致または NULL: `0.6`）— **タスク／プロジェクト単位で会話をまとめる**ことで、進行中のやり取りに属する記憶が無関係な過去セッションの記憶より上位に来る。Free 版では類似度・新鮮度に次ぐ主要ランキングシグナル。
- `local_id` — レコードに保存されるが、**Free 版のランキングには使用されない**。ローカルバイアス（`b_local`）は Pro 機能であり、Free 版では一致／不一致に関わらず `b_local` は常に 1.0。Pro 版では一致時 `b_local = 1.0`、不一致時 `b_local = 0.8` を適用し、同一 DB 上の他エージェントの記憶よりも自エージェントの記憶を追加で優先する。

### 埋め込み
- **デフォルトモデル**: `intfloat/multilingual-e5-base` / ベクトル: float[768]
- デフォルトを多言語モデルにしているのは意図的 — 日本語・英語・中国語・韓国語など 100 言語以上を箱出しでインデックス可能。単一言語で高精度を出したい場合はユーザー側でモデルを差し替える（後述「言語特化モデルへの切り替え」参照）。デフォルトリリースでは言語特化の判断をしない。
- 取得時は必ず `normalize_embeddings=True` を指定し、L2 正規化済みベクトル（norm=1）を保証せよ。
- **入力プレフィックス（必須）**: このモデルはプレフィックスなしでは精度が大幅に低下する。以下を厳守せよ:

```python
# 保存時（文書として登録）
text_to_embed = "passage: " + content

# 検索時（クエリとして照合）
text_to_embed = "query: " + keyword
```

#### 言語特化モデルへの切り替え（ユーザー側のカスタマイズ）

埋め込みモデルは `~/.n3mc/config.json` の `embed_model` フィールドで決まる（デフォルト `intfloat/multilingual-e5-base`。初回起動時にファイルへ書き込まれる）。**環境変数による上書きはない**（v1.5.0）: `config.json` には常に `embed_model` があるので、以前の版が記載していた `$N3MC_EMBED_MODEL` 変数は一度も効いていなかった。また §3 はモデルの変更を人間による `config.json` の編集だけに限っている。

**読み込みの失敗（v1.5.0）**: モデルを読み込めない場合（オフラインでの初回起動、Hugging Face のキャッシュがない、`embed_model` の綴り間違い）、保存や検索のたびに読み込みを再試行しない。失敗したあと 60 秒間（`MODEL_RETRY_SECONDS`）は `get_model()` が再試行せずにすぐ例外を出す。これにより、保存はそれぞれダウンロードの試行を待たずにベクトルなしのレコードに切り替わり（後の `--repair` で再インデックスされる）、検索は `embed_query` の警告（§5）付きでキーワードのみになる。失敗した試行ごとに stderr へ 1 行出す（多くても 1 分に 1 回）。

ユーザーが設定し得る例:

| 目的 | `embed_model` 値 | ベクトル次元 |
|---|---|---|
| 英語特化、若干高精度 | `intfloat/e5-base-v2` | 768 |
| 多言語、高精度（より遅く・大きい） | `intfloat/multilingual-e5-large` | 1024 |
| 日本語特化 | `cl-nagoya/sup-simcse-ja-base` | 768 |
| 英語、より小さく・高速 | `intfloat/e5-small-v2` | 384 |

**⚠️ ベクトル次元の制約**: `memories_vec` は固定の `float[768]` 次元で作成される。異なる次元のモデルへ切り替える場合は vec テーブルの再作成（および全レコードの再埋め込み）が必要。リファレンス実装は同一次元のモデル切り替えのみ手動介入なしでサポートする。

**アップグレード手順（同一次元のモデル切り替え）**:
1. `~/.n3mc/config.json` を編集 → `embed_model` を新モデル名に変更。
2. サーバー再起動（PID ファイルのプロセスを kill するか、次回 CLI 呼び出しで自動的に再起動される）。
3. 次回 `n3mc --repair` 呼び出しで `~/.n3mc/.memory/vec_model.txt` マーカーによりモデル不一致が検出され、警告が出力される。ディスク上のベクトルは旧モデルのままなので、再生成するまで検索品質は劣化する。
4. 強制全再埋め込み: `~/.n3mc/.memory/vec_model.txt` を削除し、対象レコードの `memories_vec` 行を削除（または vec テーブルをまるごと再作成）してから `n3mc --repair` を実行 → 新モデルでベクトルが再構築される。

**⚠️ 異なる次元のモデル切り替え**: リファレンス実装では手動 SQL なしには非対応。`memories_vec` テーブルを drop → 新次元で再作成 → `vec_model.txt` を削除 → `n3mc --repair`。

### モジュール間インポート
`sys.path` ハックは使用せず、パッケージ相対インポートを使用すること。パッケージ内部:

```python
# n3memorycore/core/processor.py
from .database import (
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
    get_memories_by_turn_id, # Q-A pairing で必須
    strip_fts_punctuation,   # /repair の FTS マイグレーションで必須
)
```

```python
# n3memorycore/n3memory.py
from .paths import HOME_DIR, MEMORY_DIR, DB_PATH, PID_FILE, AUDIT_LOG, CONFIG_FILE, NOTICES_FILE, claude_paths
from .core.database import (
    get_connection, init_db, migrate_schema, insert_memory,
    check_exact_duplicate, search_vector, find_unindexed_memories,
    strip_fts_punctuation, serialize_vector,
)
from .core.processor import (
    embed_passage, cosine_sim_from_l2, hybrid_search,
    chunk_text, add_chunk_prefixes, purify_text, render_memory_context,
)
```

`paths.py` は `$N3MC_HOME` が設定されていればそれを、未設定なら `~/.n3mc/` を `HOME_DIR` として解決する。`MEMORY_DIR`、`DB_PATH`、`PID_FILE` 等はすべてこれから派生する。プロジェクト固有の `.claude/` パスは `claude_paths()` がカレントディレクトリから解決する。

### 常駐型 FastAPI サーバー
- **ポート**: デフォルト `18520`（`config.json` の `server_port` で変更可）。同じ PC で別の N3MC（Pro 版など）を動かすときは、フォルダごとに `server_port` を分けること。
- **待ち受けアドレス**: `bind_host`（既定 `127.0.0.1`）。**既定のまま使うこと。** サーバーの API（検索・一覧・`/shutdown` 等）には認証がないため、ほかの値にするとネットワークから届く。`127.0.0.1` 以外が設定されていたら、起動時に stderr へ警告を出す。
- **起動タイミング**: `n3mc` のいずれかのサブコマンド（または `python -m n3memorycore.n3memory`）の実行時、下の「サーバーの確認と入れ替え（`ensure_server`）」に従い、必要なら `python -m n3memorycore.n3memory --run-server` として自動的にバックグラウンド起動せよ。PID ファイルの書き込みはアトミック操作（例: `open(..., 'x')` 排他作成フラグ）を使用し、複数プロセスの同時起動を防止すること。
- **通信方式**: HTTP over TCP (`http://127.0.0.1:{port}`)
- **Host ヘッダーの確認（v1.5.0）**: すべてのエンドポイントは、リクエストの `Host` ヘッダーが `127.0.0.1:<port>` または `localhost:<port>` でなければ 403 を返す。そうしないと、ユーザーのブラウザで開いた Web ページが DNS リバインディング（127.0.0.1 に解決される名前）でサーバーに届き、`GET /list` を読めてしまう。CLI は常に `Host: 127.0.0.1:<port>` を送る。FastAPI の `TestClient` は `testserver` を送るので、テストではヘッダーを明示するか、確認を差し替える。
- **エンドポイントでイベントループを止めない（v1.5.0）**: 埋め込みや DB を扱うエンドポイント（`/buffer`・`/search`・`/repair`・`/list`）は `async def` ではなく普通の `def` で定義し、FastAPI のスレッドプールで動かす。これで長い `/repair` の間も `/health` が応答し続ける。（v1.4.0 では中で同期処理をする `async def` だったため、長い `/repair` の間 `/health` がタイムアウトし、CLI がサーバーなしと判断して起動・入れ替えの処理に入っていた。）エンドポイントが使う SQLite 接続はそのエンドポイントの呼び出しの中で開き（リクエストごとに 1 接続）、スレッドをまたいで共有しない。
- **死活監視**: CLI実行のたびに `/health` エンドポイントへPINGし、応答の中身でサーバーが自分のものか・最新のコードか を判定せよ（下記）。

#### サーバーの名乗り（`/health`）

`GET /health` は次を返す:

```json
{"status": "ok", "root": "<HOME_DIR の絶対パス>", "code_version": "<16桁>", "pid": 12345}
```

- `root`: そのサーバーのデータフォルダ（`$N3MC_HOME` または `~/.n3mc`）の絶対パス。比較は `os.path.normcase(os.path.abspath(...))` で行う。
- `code_version`: `n3memorycore/n3memory.py`・`core/database.py`・`core/processor.py` のファイル内容と、`config.json` の中身（JSON として読めたら、キーの順に並べ直して `json.dumps(..., sort_keys=True)` したもの。読めなければ生のバイト列）をまとめた SHA-256 の先頭 16 桁。**空白やキーの順だけの違いでは変わらない。** `_load_config` が `config.json` を書き直した場合は計算し直す。
- `pid`: サーバープロセスの PID。

#### サーバーの確認と入れ替え（`ensure_server`）

CLI は `/health` の応答を次のように判定する（`_health_decision(health)`。戻り値は `'ok'`・`'stale'`・`'foreign'`・`'legacy'`・`'none'` のどれか）。この確認をするのは、サーバーを使うコマンド（`--buffer`・`--search`・`--repair`・`--hook-submit`・`--save-claude-turn`）だけで、自分の DB を直接読む `--list`・`--recall-thread`・`--show-turn` と、`--stop`・`--init` では行わない:

| 応答 | 判定 | 動作 |
|---|---|---|
| `root` が自分と同じ、`code_version` も同じ | 正常 | そのまま使う |
| `root` が自分と同じ、`code_version` が違う | 自分の古いサーバー | 止めて起動し直す |
| `root` が自分と違う | 別のフォルダのサーバー | **止めない。使わない。** 下記「ポートが別のフォルダのサーバーのとき」 |
| `root` を返さない（v1.3.x 以前のサーバー） | 確かめが要る | psutil で自分のサーバーだと確かめられたら止めて起動し直す。確かめられない（または止められない）ときは別のフォルダのサーバーと同じ扱い（`_FOREIGN_SERVER_ROOT` には `(unknown: the server did not report its folder)` を入れ、知らせ `port_conflict` を出す） |
| 応答なし | サーバーなし | 下記「PID ファイルの扱い」 |

- **止め方（`_stop_stale_server`）**: まず `POST /shutdown`（本文 `{"root": "<自分の HOME_DIR>"}`）を頼む。v1.3.x のように `/shutdown` を持たないサーバーにだけ、psutil で確かめてからプロセスを終了する。**PID ファイルの番号では止めない**（別のプロセスに使い回されている可能性がある）。
- **psutil での確かめ（`_is_own_server_process`）**: PID は `/health` の `pid`、それがなければポートで待ち受けているプロセス（`psutil.net_connections`）から得る。そのプロセスのコマンドラインに `n3memorycore.n3memory` と `--run-server` があり、環境変数 `N3MC_HOME` から解いたデータフォルダが自分の `HOME_DIR` と同じときだけ「自分のサーバー」とする。psutil が入っていない・権限がない・どれかが一致しない、のときは「確かめられない」。
- 確かめられないときは止めず、「古いサーバーを止められない。同じ `server_port` を別の N3MC のフォルダが使っている可能性もある。各フォルダの `config.json` の `server_port` を確かめ、手で起動し直すこと」という知らせ（`stale_server`）を出す。自分の古いサーバー（`root` が同じ）を止められなかったときは、同じ DB を使っているので、そのまま使い続ける（`ensure_server` は True）。
- **PID ファイルの扱い**: `/health` が応答しないとき、PID ファイルの番号のプロセスが**このフォルダのサーバーだと確かめられたとき（起動中）だけ**、PID ファイルを残したまま `/health` を最大 60 秒（0.5 秒間隔）見て待つ。60 秒たってもまだ起動中なら（初回起動はモデルのダウンロードで 2〜10 分かかる）、**別のサーバーを起動せず、PID ファイルも消さない**。このコマンドはサーバーなしで進め（保存は `_buffer_direct` へ、検索は `_(memory search unavailable: the memory server is still starting)_` を出す）、次のコマンドでまた確かめる。それ以外（番号のプロセスが死んでいる、別のプロセスに使い回されている）は、待たずに PID ファイルを消して起動する。
- **psutil は必須（v1.5.0）**: 上の PID ファイルの確かめには psutil が要る。psutil がないと、v1.4.0 は起動中のサーバーを一度も確かめられず、モデルの読み込み中にコマンドのたびに PID ファイルを消して別のサーバーを起動していた（`open(..., 'x')` による防止はこの削除で効かなくなっていた）。それでも `import psutil` に失敗した場合（壊れたインストール）は、PID ファイルの更新時刻から 60 秒間は PID ファイルのプロセスをすべて「起動中」、それ以降は死んでいるものとして扱う。作られてから 60 秒未満の PID ファイルがある間は、2 つ目のサーバーを決して起動しない。
- 自分で起動したサーバーが応答した時点で、`stale_server`・`port_conflict`・`server_start_failed` の知らせを消す。

#### サーバーの起動（`_start_server`、v1.5.0）

1. 下の「Clean CLI」のとおり `subprocess.Popen` で `python -m n3memorycore.n3memory --run-server` を起動する。stdout と stdin は `DEVNULL`、**stderr は `.memory/server.log` に追記する**（ファイルが 1 MB を超えていたら起動前に切り詰める）。サーバーはフックのプロセスから**切り離し**、フックが終わっても動き続けるようにする: Windows では `creationflags=subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW`、それ以外では `start_new_session=True`。
2. `/health` を 0.5 秒ごとに最大 60 秒見る。**待っている間に子プロセスが終了したら（`proc.poll() is not None`）、すぐに待つのをやめる** — ポートを N3MC 以外のプログラムが使っている、依存パッケージがない、DB を開けない、などの場合。
3. そうして早く終了したときは、知らせ `server_start_failed`（"The N3MC memory server could not start (exit code <n>). See <path of server.log> for the reason."）を出し、時刻を `.memory/server_start_failed`（マーカーファイル）に書き、PID ファイルが終了したその子プロセスの PID を持っていれば消し、サーバーなしで進める（直接保存 / `memory search unavailable`）。`Popen` そのものが例外を出したときも、サーバーなしで進む（stderr に 1 行。知らせとマーカーは書かない）。
4. `.memory/server_start_failed` が作られてから 5 分未満の間は、サーバーの起動を試み直さず、そのままサーバーなしの処理に進む。これにより、壊れた環境でフックのたびに最大 60 秒かかることを防ぐ。サーバーが応答したらマーカーを消し、知らせも消す。
5. 60 秒たっても子プロセスが生きていれば、まだ起動中（モデルのダウンロード）なので、そのまま動かしておき、上の「起動中」の決まりに従う。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は子プロセスを見ずに 60 秒待ち切り、その stderr を `DEVNULL` に捨てていた。ポートが別のプログラムに使われていたり、サーバーが起動時に落ちたりすると、フックのたびに 60 秒止まり、理由もどこにも残らなかった。

#### `/shutdown`

`POST /shutdown`（本文 `{"root": str}`）。`root` がサーバー自身の `root` と同じときだけ `{"status": "ok"}` を返し、応答を返し終えたあとに自分で終了する（PID ファイルが自分の PID なら消す）。違えば 403。テストでは本物のサーバーに送らないよう、`_request_shutdown` と `_post` を必ず差し替えること。

#### ポートが別のフォルダのサーバーのとき

`ensure_server` は False を返し、モジュール変数 `_FOREIGN_SERVER_ROOT` にそのサーバーの `root` を入れ、知らせ `port_conflict` を出す。このとき **記録が別のフォルダの DB に混ざらないように**:

- 保存（フック・`--buffer`・`--save-claude-turn`）は自分の DB に `_buffer_direct` で直接書く（埋め込みは、自分のサーバーが動いたときの `/repair` で付く）。
- 検索（フック・`--search`）はしない。出力は `# Recalled Memory Context\n\n_(memory search unavailable: another N3MC folder's server is using port <port>)_\n`。
- `--repair` は何もせずに戻り、理由を stderr に 1 行出す。`/repair` も送らない。
- `--list`・`--recall-thread`・`--show-turn` はもともと自分の DB を直接読むので、そのまま動く。

#### 知らせ（`_notify`）

人が何かをする必要がある知らせ（`stale_server`、`port_conflict`、`server_start_failed`、`config_unreadable`、`config_type`、`db_corrupt`、`behavior_md_edited:<hash>`）は `_notify(key, message)` で出す:

1. stderr に出す。
2. `audit.log` に `{"ts", "hook": "notice", "key", "message"}` を記録する（同じ key・同じ文面は 1 回だけ）。
3. `.memory/notices.json` に残し、検索結果の先頭（検索結果では `Search query:` の行の直後、それ以外の出力では見出し `# Recalled Memory Context` の直後）に **1 日 1 回**（前回出してから 24 時間たったら） `> ⚠️ N3MC notice: <message>` として出す（`_take_notices`）。
4. 原因がなくなったら `_clear_notice(key)` で消す。
5. `notices.json` は `config.json` と同じく（§3.5）アトミックに書く（同じフォルダの一時ファイル + `os.replace`）。

テストでは `NOTICES_FILE` と `AUDIT_LOG` を必ず一時フォルダに向けること（本物の `~/.n3mc` に書かない）。
- **応答目標**: 0.7s 前後（埋め込み生成 + DB検索の合計）。ハードウェア・OS・モデルキャッシュ状態に依存するため、達成目標として扱い厳密な要件とはしない。
- **初回起動**: モデルのプリロードを uvicorn 起動前に実行するため、初回起動（またはモデルキャッシュ未作成時）は最大 60 秒を要する場合がある。これは仕様上の許容動作であり、2回目以降は数秒以内に起動する。

### SQLite 剛性設定
接続時に以下を強制実行せよ:
```sql
PRAGMA synchronous = FULL;
PRAGMA journal_mode = WAL;
PRAGMA wal_autocheckpoint = 1000;
```

- `PRAGMA wal_autocheckpoint = 1000;` — WAL ファイルが 1000 ページ（約 4MB）を超えたら自動的にチェックポイント。長期稼働で WAL が肥大化することを防止。

### 即時物理保存（変更・最適化禁止）
`--buffer` または API経由の保存時、その瞬間に INSERT および COMMIT を完遂せよ。

**以下を絶対に行ってはならない（速度向上を理由とする場合も含む）:**
- 書き込みのバッファリング（`write_buffer`、`batch_insert`、遅延COMMIT等）
- 非同期書き込み（`asyncio`、スレッドキュー、バックグラウンドタスク等）
- トランザクションの一括化（複数INSERTを1トランザクションにまとめる処理）

> **補足: 即時物理保存の設計背景**
> すぐにコミットせずメモリ上に保持（バッファリング）した記録は、遅らせた書き込みの前にプロセスが強制終了されると消失する。保存の瞬間にコミットすれば、保存直後に強制終了されても何も失われない。速度よりDurabilityを最優先とする。これはパフォーマンス上の選択ではなく、設計上の不変条件である。

### 識別子

- **Owner ID**: 初回起動時に UUIDv4 を生成し `config.json` に保存。全レコードに刻印せよ。N3MC インスタンス間でデータをマージする際にデータ出所を保持するために使用する。
- **Local ID**: 初回起動時または `config.json` に未設定の場合、UUIDv4 を生成し固定保存する。N3MCインストール（またはエージェント）の識別子。全レコードに刻印せよ。マルチエージェント時は `/buffer` API の `local_id` パラメータでエージェント固有の UUIDv4 を指定可能（省略時は config.json の値を使用）。
  - **ユースケース**: 複数の Claude Code インスタンス（マシンやインストールごとに固有の `local_id`）、同一 N3MC DB を共有する異なるエージェント（Claude Code + CordX 等が各自のインスタンスで動作し固有の `local_id` を持つ）、エージェント種別やプロジェクト環境ごとの記憶分離。
  - **注意**: Free 版では `local_id` はレコードに記録されるが、ランキングには使用されない。同一環境の記憶を優先する `B_local` バイアス乗数は Pro 機能。
- **Session ID**: 1 つのタスク／プロジェクト／会話に属する記憶をまとめるためのグルーピングキー。**解決順**: (1) `/buffer`・`/search` API 呼び出しでの明示的な `session_id` 引数（最優先 — 既にセッションを管理しているホストが使う。例：n3memory-lite はタスク番号を割り当てる、Ollama 風のチャット切替 UI はチャット ID を渡せる）; (2) `N3MC_SESSION_ID` 環境変数; (3) サーバー起動時に生成するプロセス単位の UUIDv4（ホスト由来のセッション ID を取得できない Claude Code 等のフォールバック）。全レコードに刻印するが、**`config.json` には永続化しない** — `session_id` は永続的なインストールではなく、一時的なタスク／プロジェクトを識別するためのもの。`b_session` ランキングバイアス（一致 1.0／不一致 0.6）を駆動し、現在のタスクの記憶を無関係な過去セッションより上位に浮上させる。

`config.json` の完全スキーマ（未設定項目は以下の規定値で自動初期化せよ）:

```json
{
  "owner_id":             "<UUIDv4 自動生成>",
  "local_id":             "<UUIDv4 自動生成>",
  "bind_host":            "127.0.0.1",
  "server_port":          18520,
  "embed_model":          "intfloat/multilingual-e5-base",
  "half_life_days":       90,
  "bm25_min_threshold":   0.1,
  "search_result_limit":  20,
  "min_score":            0.2,
  "search_query_max_chars": 2000,
  "vector_min_cos":       0.85,
  "relative_min_score_ratio": 0.6,
  "pair_top_n":           3,
  "pair_max_chars":       3000,
  "version_order_enabled": true,
  "version_sim_threshold": 0.98,
  "version_min_chars":    100
}
```

- `bind_host`: サーバーが待ち受けるアドレス。既定の `127.0.0.1` のまま使う（§3「常駐型 FastAPI サーバー」）。
- `search_result_limit`: `--search` が返す最大件数。
- `min_score`: `--search` の結果からこの値未満のスコアを除外する（規定値 `0.2`）。`0.0` で無効化。
- `search_query_max_chars`: 検索クエリから使用する最大文字数（規定値 `2000`）。embedding モデル（512トークン）は約2,000文字以上を意味的に処理できません。増加させるとFTSクエリが遅くなる場合があります。
- `vector_min_cos`: ベクトル検索の候補にする cos 類似度の下限（規定値 `0.85`）。multilingual-e5 は無関係な記録どうしでも 0.80〜0.85 になるため、これ未満の候補を捨てる。**0.85 は `multilingual-e5-base` で測った値。** ほかの埋め込みモデルでは測り直すこと。目安は 0.80〜0.88（下げると拾いやすくなるが関係の薄い記録も混ざり、上げると差し込みは短くなるが関係のある記録も落ちやすい）。
- `relative_min_score_ratio`: 1 位のスコアのこの倍率未満の結果を捨てる（規定値 `0.6`、§5「検索結果の絞り込みと並べ替え」）。`0.0` で無効化。
- `pair_top_n` / `pair_max_chars`: Q-A の往復を差し込むのは上位 `pair_top_n` 件の往復だけ、合計 `pair_max_chars` 字まで（§5「Q-A ペアリング契約」）。
- `version_order_enabled` / `version_sim_threshold` / `version_min_chars`: 同じ内容の新しい版を上に並べる処理（§5）。`0.98` も `multilingual-e5-base` で測った値。
- v1.3.x の `dedup_threshold` と `context_char_limit` は**廃止**。既存の `config.json` に残っていても読まれない（消さなくてよい）。
- 値は引用符を付けずに書く（`"vector_min_cos": 0.83`）。型が違う値や許される範囲外の値は無視される（§3.5「許される値」）。
- `config.json` を書き換えると `code_version` が変わり、次のコマンドでサーバーが自動で起動し直して新しい値が効く。
- KNN の取得件数（K値）・`--repair` のバッチサイズ等の内部処理パラメータは実装者の判断に委ねる。

> **複数アカウント運用時の注意**: 1台の PC で複数の Claude Code アカウントを運用し、同じ N3MC サーバー（同じ `server_port`・同じ DB パス）に接続する場合、各アカウントの `config.json` は個別に自動生成されるため `owner_id` / `local_id` が異なる。Free 版ではランキングに影響しないが、Pro 版にアップグレードすると他アカウントの記憶に `B_local=0.8` の減衰が適用される。全アカウントの記憶を同等に扱いたい場合は、1つ目のアカウントの `config.json` を他のアカウントにコピーして `owner_id` / `local_id` を統一すること。

> **チーム共有: 非対応。** CLI は常に `127.0.0.1:<server_port>` に接続する（リモートのサーバーアドレスを指定する設定はない）。サーバーには認証も暗号化もなく、`Host` ヘッダーがローカルでないリクエストは拒否する（§3「常駐型 FastAPI サーバー」）。`bind_host` は既定のままにすること。チームで 1 つの記憶 DB を共有するには別の設計が必要になる。

> **DB 変更（応用）**: 現仕様は SQLite + sqlite-vec を前提としているが、チーム規模が大きい場合は PostgreSQL + pgvector への移行も技術的に可能である。スキーマ互換性・接続プール・マイグレーション手順については Claude Code に相談すること。

- **主キー**: UUIDv7（時系列ソート可能。DB挿入時に生成）。外部ライブラリ `uuid7`（PyPI）を使用せよ。

### ランキング式

```
Final Score = (cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_session
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
    agent_name  TEXT,             -- レコードを書いたAIエージェントの識別子（例：`"claude-code"`）。v1.1以前または指定なしの場合は NULL
    session_id  TEXT,             -- 会話／タスクのグルーピングキー（§3「識別子」）。b_session を駆動する
    turn_id     TEXT              -- 1 ターンの [user] 行と [claude] 行が共有する Q-A 往復のキー（§5「Q-A ペアリング契約」）
    -- SQLite は全テーブルに暗黙の INTEGER rowid を自動付与する
);
CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id);

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
ALTER TABLE memories ADD COLUMN session_id  TEXT;
ALTER TABLE memories ADD COLUMN turn_id  TEXT;
CREATE INDEX IF NOT EXISTS idx_memories_turn_id ON memories(turn_id);  -- turn_id ができたあとで
```

`agent_name TEXT` — レコードを書いたAIエージェントの識別子（例：`"claude-code"`）。v1.1以前または指定なしの場合は `NULL`。

また、`migrate_schema()` は既存の `memories_fts` テーブルが `tokenize='porter unicode61'` で作成されている場合を検出し、`tokenize='trigram'` で自動的に再作成して全レコードを再インデックスする。この一度きりのマイグレーションにより、既存データベースも日本語検索精度の向上恩恵を受けられる。

> **補足: FTS5 トークナイザーの選定理由**
> `trigram` は日本語テキストに最適化された部分文字列マッチングのために採用している。日本語は単語間にスペースを持たないため、`porter unicode61` のような単語境界ベースのトークナイザーでは文全体が1トークンとして扱われ、FTS が実質的に無効化される。`trigram` は3文字単位の部分文字列インデックスを生成するため、形態素解析なしで日本語のキーワード検索が可能になる。英語のように空白で区切る言語でも動く。Free 版は PyPI でどこの国でも使われるため、既定はどの言語でも動くものにする（英語の語形変化 "running" → "run" はまとめないが、ベクトル検索で補える）。最終的なトークナイザーは、利用者が自分の言語・環境に合わせて直す（例: 英語中心なら `porter unicode61`、日本語の精度を上げるなら形態素解析）。README にもそう案内している。

**FTS5 制約**: `trigram` トークナイザーは **3 文字**単位（バイトではなく文字）でインデックスを作る。3 文字未満の語 — `AI`、`東京`、`龍馬` — は決して一致しない（FTS5 はその語に対して 0 行を返す）。そのため `_build_fts_query` は 3 文字未満の語を捨てる。語が 1 つも残らなければキーワード検索をスキップし、ベクトル検索（cos_sim）のみでランキングせよ。（v1.4.0 以前は単位を「3 バイト」と書いていたため、`東京` のような 2 文字の日本語の語が確認を通り、常に 0 行を返していた。）

**FTS 句読点除去（挿入側・検索側の両方で必須）**: 単語に隣接する句読点がトークナイゼーションの不一致を起こす可能性がある。**FTS5 への INSERT 時**および **FTS MATCH クエリ時**の両方で、同一の句読点除去関数を適用せよ:

```python
_FTS_PUNCT_RE = re.compile(r'[「」『』【】（）()\[\]{}<>〈〉《》・、。,.!！?？;；:：\-―─…\'\"\u201c\u201d\u2018\u2019]')

def strip_fts_punctuation(text: str) -> str:
    cleaned = _FTS_PUNCT_RE.sub('', text)
    return re.sub(r'\s+', ' ', cleaned).strip()

# INSERT 時: memories テーブルには原文、FTS には句読点除去済みテキストを登録
INSERT INTO memories_fts(rowid, content) VALUES (?, strip_fts_punctuation(content))

# MATCH 時（v1.5.0）: 制御文字と句読点を空白に（日本語の文が句読点で語に
# 分かれる）→ 空白で分割 → 3 文字未満の語を捨てる → 最大 30 語 → 各語を "..." で
# 囲む（" は "" にする）→ OR でつなぐ。
_FTS_MAX_TERMS = 30  # 大量テキスト入力時の FTS5 フリーズを防止
_FTS_CTRL_RE = re.compile(r'[\x00-\x1f\x7f]')

def _build_fts_query(query: str) -> str:
    text = _FTS_PUNCT_RE.sub(' ', _FTS_CTRL_RE.sub(' ', query or ''))
    terms = [t for t in text.split() if len(t) >= 3][:_FTS_MAX_TERMS]
    if not terms:
        return ""                                  # → キーワード検索をスキップ
    return " OR ".join('"' + t.replace('"', '""') + '"' for t in terms)
```

- **INSERT 側は変えない**（`strip_fts_punctuation` は従来どおり句読点を取り除いて語をつなげたままにする）ので、FTS の登録し直しも新しいマイグレーションマーカーも要らない。句読点を空白にするのは検索側だけ。例: 保存された `架空の惑星「アルファ9」の気温設定` は `架空の惑星アルファ9の気温設定` としてインデックスされる。クエリ `「アルファ9」の気温は？` は語 `アルファ9` と `の気温は`（OR）になり、1 つ目が一致する。
- **OR にする理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は引用符で囲んだ語を AND（FTS5 の暗黙の演算子）でつないでいた。フックはユーザーのメッセージ全体をクエリにするので、`what is the quick fox doing in my DB` のような文は、`quick` と `fox` を含む記録があっても 0 行を返し、空白のない日本語の文は 1 つの長い語になって、その文そのものを含む記録にしか一致しなかった。そのため自動検索ではスコアのキーワード部分（30 %）がほとんど常に 0 だった。OR なら、どれか 1 つの語を含む記録が一致し、FTS5 の `bm25()` が、より多く・より珍しい語を含む記録を上位にする。3 文字未満の語は trigram では決して一致しないので捨てる。
- 句読点のない長い日本語の文は、今も 1 つの語のままになる。これは受け入れる: ベクトル検索が補い、すべての trigram に分けるとほぼすべての記録に一致してしまう。

> **⚠️ 語の引用符囲みは必須（v1.4.0）**: 句読点除去のあとも `\ / # = @ * $ % + ^ ~ |` などの ASCII 記号や、単独の `AND` / `OR` / `NOT` / `NEAR` が残る。これをそのまま `MATCH` に渡すと FTS5 の文法エラー（`fts5: syntax error near "\"` 等）になり、`/search` が HTTP 500 を返して記憶の差し込みが「memory search unavailable」になる（実測で質問の約 12% で発生）。各語を FTS5 の文字列として `"..."` で囲めば、記号も演算子も文字どおりに照合される。（v1.5.0 からは引用符で囲んだ語を `OR` でつなぐ。語の間に書く `OR` は引用符の外にあるので演算子になり、語の中にあるユーザーの `OR` は文字どおりのまま。）
>
> **例外時も検索を続ける**: `hybrid_search` は `search_fts` の例外を捕まえ、キーワードの結果なし（ベクトル検索だけ）で続行する。FTS の失敗で検索全体を失敗させてはならない。

`--repair` 実行時に、既存 FTS レコードのうち句読点付きで登録されたものを検出し、句読点除去済みテキストで再登録せよ。この FTS クリーニングは**一度きりのマイグレーション**として実行し、完了後にマーカーファイル（`.memory/fts_punct_cleaned`）を作成せよ。マーカーが存在する場合はスキップし、毎セッション開始時の全件スキャンを回避すること。

**FTSのみヒットした場合のスコア**: ベクトル検索にはヒットせず FTS のみにヒットしたレコードは、`cos_sim = 0.0` として統合スコアを計算し、結果に含めよ。最終スコアは最大でも `0.3 × time_decay × b_session` だが、ランキング対象から除外してはならない。`min_score`（0.2）と相対足切りは `filter_score`（`b_session` を**掛けない**スコア、下記）で判定するので、別セッションのキーワードのみのヒット（最終スコアは最大 0.3 × 0.6 = 0.18）も、`filter_score`（最大 0.3 × time_decay）が下限に届けば残る。

**ベクトル検索の owner_id フィルタ**: `search_vector` は単一ユーザー前提のシステムであるため、`owner_id` によるフィルタリングを行わない。KNN 検索は全レコードを対象とする（これは仕様上の設計判断であり、複数ユーザー対応時は要改修）。

**絞り込み付きのベクトル検索（`--since` / `--until`）**: vec0 の KNN（`k = ?`）は全体から近い k 件を先に選ぶため、そのあとで時間範囲を絞ると、範囲内の記録がほとんど残らない。絞り込みがあるときは、**絞り込んだ rowid だけを対象に KNN を行う**こと。2 段階で行う:

```sql
-- 1) memories_vec で rowid と距離を得る
SELECT rowid, distance FROM memories_vec
 WHERE embedding MATCH :vec AND k = :k
   AND rowid IN (SELECT rowid FROM memories WHERE timestamp >= :since AND timestamp <= :until)
-- 2) その rowid で memories を引く
```

- `rowid IN (サブクエリ)` 付きの KNN は sqlite-vec v0.1.7 で確認済み。これが `sqlite3.OperationalError` になった場合（古い sqlite-vec 等）は、絞り込んだ行を `vec_distance_l2(embedding, :vec)` で全件計算して距離順に並べる方法（`_search_vector_exact`）に切り替える。
- 絞り込みがないときは従来どおり `search_vector`（全体の KNN）を使う。

**time_decay（半減期 `half_life_days` 日）**:

$$time\_decay = 2^{-\frac{days\_elapsed}{half\_life\_days}}$$

`days_elapsed` はレコード作成日時からの経過日数（浮動小数点）。`half_life_days` は `config.json` の値を使用する（規定値 90）。

**バイアス係数（Free 版）**:

| バイアス | 条件 | 係数（固定値） |
| :--- | :--- | :---: |
| **$b_{session}$** | 現在の `session_id` と一致 | **1.0** |
| | 不一致または NULL | **0.6** |

- `b_session` は Free 版で**唯一の**バイアス乗数。同一タスク／プロジェクト／会話に属する記憶をまとめて浮上させ、無関係な過去セッションの記憶を後ろに下げる。`0.6` という強めの不一致ペナルティにより、類似度・新鮮度に次ぐ Free 版の主要ランキングシグナルとして機能する。
- バイアス係数は設定ファイルによる変更不可（固定値）。
- `b_session` は、ベクトルと FTS の候補をまとめるのと同じループの中で Python で計算する（2 つの結果は Python でまとめるので、SQL の式にしても役に立たない）。

**`b_session` は並べるためのもので、絞り込みには使わない（v1.5.0）**: 各候補は 2 つの値を持つ:

| 値 | 式 | 使い道 |
|---|---|---|
| `score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay × b_session` | 結果の並び順（と表示する `score=`） |
| `filter_score` | `(cos_sim × 0.7 + keyword_relevance × 0.3) × time_decay` | `min_score` と相対足切り（§5「検索結果の絞り込みと並べ替え」の手順 2 と 4） |

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 では 2 つの下限とも最終スコアと比べていたため、`b_session`（0.6）と相対足切り（0.6）が掛け合わさっていた。現在のセッションにベクトルのヒットが 1 件（cos ≥ 0.85、score ≥ 0.6）あるだけで、別セッションの記録が残るには `cos × time_decay ≥` 1 位の cos が必要になった。半減期 90 日では、**約 21 日より古い別セッションの記録は、内容が完全に一致していてもすべて落ちた**（再現: 現在のセッションの cos 0.86 の記録は残り、30 日前の別セッションの cos 0.99 の記録は 0.33 になって足切り 0.602 × 0.6 = 0.361 を下回った）。別セッションのキーワードのみのヒット（最大 0.18）は常に `min_score` 0.2 を下回り、手動の `--search` はすべての記録を別セッションとして扱うので、キーワードのみのヒットを一度も出さなかった。現在のセッションの記録はすでに Claude のコンテキストにある。それ以外をすべて切ってしまうと、セッションをまたぐ記憶の意味がなくなる。

- `b_local`（エージェント／インストール粒度の分離）は Free 版では **適用しない** — 全レコードが一致／不一致に関わらず `b_local = 1.0` として扱われる。エージェント／インストール単位で記憶を分離する `B_local` 乗数は **Pro 機能**であり、Pro 版では一致時 `b_local = 1.0`、不一致時 `b_local = 0.8` を適用する。

### Clean CLI
モデルロード警告等を CLI の stdout（Claude のコンテキストになる）に出さないこと。`n3memory.py` 内で FastAPI サーバーを subprocess 起動する際、以下のようにストリームをリダイレクトすること（呼び出し側に依存しない）:

```python
import os, subprocess, sys
log = open(SERVER_LOG, "ab")          # .memory/server.log。1 MB を超えていたら先に切り詰める
kwargs = {}
if os.name == "nt":
    kwargs["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
else:
    kwargs["start_new_session"] = True
proc = subprocess.Popen(
    [sys.executable, '-m', 'n3memorycore.n3memory', '--run-server'],
    stderr=log, stdout=subprocess.DEVNULL, stdin=subprocess.DEVNULL, **kwargs,
)
```

- stderr は `DEVNULL` ではなく `.memory/server.log` に送る（v1.5.0）。起動に失敗したサーバーが理由を残せるようにするため（§3「サーバーの起動」）。
- 切り離しのフラグにより、サーバーを起動したフックのプロセスが終わってもサーバーは動き続ける。

- `-m n3memorycore.n3memory` 形式が必須。これによりパッケージの相対インポートが解決される。サーバーを絶対ファイルパスで起動してはならない。
- 呼び出し側（settings.json の hooks 等）でリダイレクトを付与する必要はない。

**文字コード（UTF-8）**: `n3memorycore/n3memory.py` の `main()` 先頭、**および `n3memorycore/n3mc_hook.py` と `n3memorycore/n3mc_stop_hook.py` のモジュール先頭**（`sys.stdin.read()` や `subprocess.run` の呼び出しより前）で以下を実行し、Windows cp932 環境での文字化けをプログラム内部で解決せよ。

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
- ⚠️ **stdin を読む／stdin を subprocess にパイプするすべての Python エントリポイントで reconfigure ブロックが必須** — `n3memorycore/n3memory.py`、`n3memorycore/n3mc_hook.py`、`n3memorycore/n3mc_stop_hook.py` のいずれか一つでも忘れると、Windows で非 ASCII 入力（日本語、em-dash 等）が静かに mojibake し、その破損バイト列が `audit.log` と DB に永続化されて復元不能になる。フックスクリプトでは `main()` 内ではなく**モジュール先頭**に置くこと（最初の `sys.stdin.read()` より前に走らせるため）。

**パスのポータビリティ**: Pythonコード内のファイルパス構築はすべて `os.path.join()` または `pathlib.Path` を使用すること。パス区切り文字（`\` や `/` の文字列リテラルによるパス構築）の直接使用は禁止する。本システムはコード変更なしに Windows・Linux で動作しなければならない。

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
- 対応プラットフォームは **Windows と Linux**（Windows 11 と Ubuntu で確認）。macOS は対象外（v1.5.0。以前の版は対象に挙げていた）。どちらの OS でこの指示書からコードを再生成しても、コード変更なしに動作する実装が得られなければならない。

---

## 3.5. 耐障害性・エラーハンドリング

### config.json 破損時の自動復旧

`config.json` はユーザーが手で書き換える（`vector_min_cos` の調整など）ので、書き間違いを前提に扱う。**書き間違えた `config.json` を上書きしてはならない**（以前の実装は、読めないと既定値で上書きし、ユーザーが変えたポートなどの設定を消していた）。

| 場合 | 動作 |
|---|---|
| ファイルがない | 既定値に新しい `owner_id`・`local_id` を入れて保存する。**ファイルは排他的に作成する**（下記「config.json の書き込み」） |
| JSON として読めない、または一番外がオブジェクトでない | **`config.json` は書き換えない。** その間は既定値で動く。`owner_id`・`local_id` は DB から復旧し（下記）、DB にもなければ**その回だけ**の新しい UUID を使う（保存しない）。知らせ `config_unreadable` を出す |
| 値の型が既定値と違う（例 `"vector_min_cos": "0.83"` — 引用符付きの数値）、または値が許される範囲の外（下の表） | **その項目だけ**無視して既定値を使い、ほかの項目は使う。`config.json` は書き換えない。知らせ `config_type` に項目名を出す |
| 正しく読める | ID や既定のキーが足りなければ足して保存する。`config_unreadable`・`config_type` の知らせを消す |

- 型の確認（`_config_value_ok(key, value)`）: 既定値が真偽値なら真偽値だけ、文字列なら文字列だけを受け付ける。数値は下の表に従う（真偽値は数値として扱わない）。既定値が `None` の項目（`owner_id`・`local_id`）と、既定値にない項目は確かめない。

**許される値（v1.5.0）**:

| キー | 型 | 許される範囲 |
|---|---|---|
| `server_port` | 整数 | 1〜65535 |
| `search_result_limit`, `pair_top_n`, `pair_max_chars`, `search_query_max_chars`, `version_min_chars` | 整数 | ≥ 1 |
| `half_life_days` | 数値（int または float） | > 0 |
| `bm25_min_threshold` | 数値 | ≥ 0 |
| `min_score`, `vector_min_cos`, `relative_min_score_ratio`, `version_sim_threshold` | 数値 | 0〜1 |

- 「整数」は JSON の整数（`2000`）のこと。`2000.0` のような float は、値が整数であっても**受け付けない**。これらのキーはスライスの境界や件数として使われる（`user_text[:search_query_max_chars]`）。v1.4.0 は `2000.0` を受け付け、そのあと `--hook-submit` はすべて検索の前に `TypeError` で失敗し、ユーザーのメッセージは DB に届かなかった（`audit.log` にしか残らなかった）。負の `server_port` や `half_life_days: 0` も v1.4.0 の確認を通っていた。
- 範囲外の値は型の違う値とまったく同じに扱う: そのキーだけ既定値を使い、知らせ `config_type` にそのキーを出す。

**`config.json` の書き込み（v1.5.0）**:

- ないファイルを**作る**ときは排他作成（`open(path, "x")`）を使う。`FileExistsError` で失敗したら、別のプロセスが先に作ったということなので、書かずにそのファイルを読む。これにより、新規インストールで 2 つのフックが同時に始まっても `owner_id` は 1 つになる（v1.4.0 では 2 つの違う値が書かれることがあり、レコードにその値が刻印されたあとで 2 つ目が 1 つ目を上書きしていた）。
- 既存のファイルを**更新する**とき（足りないキーを足すとき）は、同じフォルダに一時ファイルを書いて `os.replace` で元のファイルと置き換える。これで書き込みが途中で止まっても `config.json` が途中で切れたままになることはない。`notices.json` も同じ方法で書く。
- 知らせは §3「知らせ」の仕組み（stderr、`audit.log`、検索結果の先頭に 1 日 1 回）で出る。直すと次の読み込みで消える。
- **DB からの ID 復旧**: `owner_id` または `local_id` が得られない場合、既存の DB レコードから最頻出の値を使う（`SELECT owner_id FROM memories GROUP BY owner_id ORDER BY COUNT(*) DESC LIMIT 1`）。DB にもレコードがない場合のみ、新しい UUID を生成する。

> **補足: DB からの ID 復旧の設計背景**
> `owner_id`・`local_id` が静かに再生成されると、既存の全メモリのバイアス計算が狂う。DB からの復旧により、この無症状障害を防止する。

### DB 破損時の検出と復旧

サーバー起動時（`lifespan`）には `PRAGMA integrity_check` を 1 回だけ実行する。一方 `_buffer_direct` フォールバック経路では **`PRAGMA quick_check`** を実行する（`integrity_check` は全テーブル全行走査で大型 DB ではフォールバックの度に数秒〜数分を要し、HTTP 失敗ごとに走らせる経路としては不適切なため、軽量な `quick_check` を使う）。**`quick_check` は 1 プロセスにつき最大 1 回**（v1.5.0）: 結果をモジュール変数に覚えておく。別のフォルダのサーバーがポートを使っている間はすべてのチャンクが `_buffer_direct` を通り、v1.4.0 はチャンクごとに `quick_check` を実行していた。いずれも：

1. 結果が `ok` でない場合、DB を `n3memory.db.corrupt-<YYYYmmddHHMMSS>.bak`（ローカル時刻。その名前がすでにあれば `…-2.bak`、`…-3.bak`、…）として退避する。
2. `n3memory.db-wal` および `n3memory.db-shm` のサイドカーファイルは削除せず、同じ名前の形（`n3memory.db.corrupt-<YYYYmmddHHMMSS>.bak-wal`、`…-shm`）で一緒に**移動する**。新しい空 DB が古い WAL を読み込んではならない — 移動しておけば削除したときと同じくそれは防げる — うえ、WAL には最新の記録が入っていることがあり、バックアップの隣に名前をそろえて置いてあれば `sqlite3 <backup> ".recover"` でまだ読める（復旧の前に `<name>-wal` / `<name>-shm` に名前を戻す）。
3. 新しい空の DB を作成する。
4. stderr に警告メッセージを出力し（復旧手順を含む）、知らせ `db_corrupt`（"The memory DB failed its integrity check and was set aside as <path>. A new empty DB is in use. To recover records: sqlite3 <path> .recover | sqlite3 recovered.db"）を出す。この知らせは、ほかの知らせと同じく検索結果の先頭に出る（§3「知らせ」）。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は固定の名前 `.corrupt.bak` を使っていたので、2 回目の破損で 1 回目のバックアップが上書きされた（Windows で確認）。また WAL と SHM のファイルを削除し、復旧に使えたはずの記録を捨てていた。知らせる手段は、フックでは表示されない stderr の 1 行だけだった。

`get_connection` 内で `PRAGMA` 実行時に `DatabaseError` が発生した場合は、復旧手順を含むエラーメッセージを付与して re-raise する。

### マイグレーションのバッチ処理

`/repair` 内の一括マイグレーション（ベクトル再インデックス・FTS 句読点クリーニング）は **200 件ずつバッチ処理**で実行する。大規模 DB でのメモリ枯渇を防止するため、`fetchall()` ではなく `LIMIT 200` でフェッチする。**`OFFSET` は常に `0` に固定すること**——修復済みレコードは `WHERE mv.rowid IS NULL OR mf.rowid IS NULL` の結果セットから自動的に消えるため、`OFFSET` をバッチごとに加算すると残りの未修復レコードをスキップしてしまう（例：400 件未修復 → 200 件修復 → `OFFSET 200` で 0 件ヒット → 残り 200 件が永久に未修復になる）。各バッチで 1 件も修復できなかった場合（埋め込みモデル利用不可等）は無限ループを防ぐために即時終了する。

**フックからの呼び出しごとの上限（v1.5.0）**: `/repair` は省略可能な本文 `{"max_records": int}` を受け付ける。`--hook-submit` は `{"max_records": 200}`（1 バッチ）を送る。これにより、大量の未処理分（例: 別のフォルダのサーバーがポートを使っていた間に `_buffer_direct` で保存された多数の記録）は、1 つの質問を何分も止めるのではなく、1 ターンに 1 バッチずつ片付けられる。`n3mc --repair` は上限を送らず、すべてを処理する。一回限りのマイグレーション（FTS 句読点クリーニング、ベクトル再インデックス）も同じ上限に従い、マーカーはマイグレーションが終わったときにだけ書く。

ただし **FTS 句読点クリーニング**は、処理した行も結果から消えない（UPDATE するだけ）ため、`OFFSET 0` では終わらない。こちらは `WHERE rowid > <前のバッチの最後の rowid> ORDER BY rowid LIMIT 200` のキーセット方式で進める。

---

## 3.6. 取り出し拡張（Retrieval Extensions）

> **目的**: 保存済み記憶からの取り出しを強化する。すべて**追加機能**であり、既存スキーマ・既存 CLI・既存挙動には一切手を加えない。

### `--since` / `--until`（時間範囲フィルタ）

```bash
python -m n3memorycore.n3memory --search "キーワード" --since 2026-01-01 --until 2026-03-31
```

- `--search` および `--list` のオプションとして受け付ける。
- 既存の `timestamp` 列に対し `WHERE timestamp >= ? AND timestamp <= ?` を AND 結合する。新規スキーマ追加なし。
- **解釈（`_parse_time_bound`、v1.5.0）**: 日付（`2026-11-13`）、または日付と時刻の間が `T` か空白の ISO 8601 の日時（`2026-11-13T09:00`、`2026-11-13 09:00:00`）を受け付ける。オフセット（`+09:00`、`Z`）はあってもなくてもよい。日付だけの `since` は 00:00:00、日付だけの `until` は 23:59:59.999999 とする。
- **タイムゾーン**: オフセットのない値（日付だけの値はすべてこれ）は、そのマシンの**ローカルタイムゾーン** — 出力の `saved=` と同じ時計 — で読み、UTC に変換する。オフセットのある値は、そのオフセットのまま UTC に変換する。変換した境界は、保存されている `timestamp` の値（`insert_memory` が書く形式、UTC）とまったく同じ形式にそろえるので、SQL の文字列比較が正しくなる。
- **読めない値**: 境界を解釈できない場合（`yesterday`、`2026-13-01`）は、stderr に 1 行（`--since: cannot read 'yesterday' (use 2026-11-13 or 2026-11-13T09:00)`）出し、検索せずに終了ステータス 2 で終わる。`since` が `until` より後のときも、`--since is later than --until` で同じようにする。
- 片方のみの指定（範囲を片開きにする）も可能。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は引数の文字列をそのまま DB の UTC のタイムスタンプと比べていた。日本では `--since 2026-11-13` が、13 日のローカル時刻 00:00〜08:59 に保存された記録（UTC では 12 日）を落としていた（`saved=` には 13 日と出ているのに）。`yesterday` はエラーにならず文字列として比べられ、`2026-01-01 10:00:00`（空白区切り）は `T` 区切りで保存された値との並びが正しくなかった。

### `--recall-thread`（会話再構成）

```bash
python -m n3memorycore.n3memory --recall-thread <turn_id> [--before N] [--after N]
```

- 指定 `turn_id` のターンと、それ以前 N 件・それ以後 N 件のターンを時系列で返す。
- `--before` / `--after` のデフォルトは各 2。
- 既存 `idx_memories_turn_id` インデックスを使用。新規インデックス不要。
- 出力は `--list` と同じタブ区切り（`--format jsonl` も対応）。
- **運用**: Claude は `--search` のスニペットで文脈不足と判断した場合、ヒットの `turn_id` を `--recall-thread` に渡して周辺会話を取得すること。ユーザーがこのコマンドを直接叩く必要はない。

---

## 3.7. 後方互換性（厳守）

本指示書のいかなる機能追加も、既存の `memories` / `memories_vec` / `memories_fts` スキーマおよび既存 CLI フラグの挙動を破壊してはならない。新機能は次のいずれかで実装すること：

- 新しい CLI フラグ・新しいサブコマンドの追加
- 新しい DB テーブル・新しいインデックスの追加（`CREATE TABLE IF NOT EXISTS` / `CREATE INDEX IF NOT EXISTS`）
- 既存テーブルへのカラム追加は `ALTER TABLE ... ADD COLUMN` のみ（`DROP` / `RENAME` は禁止）

既存ユーザの DB は、再生成なしにそのまま新仕様で動作すること。

---

## 4. Claude Code 連携設定（自動化の核）

フック**と N3MC のコマンドのパーミッションのルール**は、**ユーザーグローバル**の Claude Code 設定（`~/.claude/settings.json`）に絶対パスで登録し、全セッションで有効化する。`n3mc --init` が両方を書く（v1.5.0。§4 ①、②）。

> **🛑 【必須・自動実行ステップ】フック登録は省略不可・ユーザー保留不可**
>
> 「インストール成功したのにセッション間 RAG が機能しない」という症状の最大の原因は、本指示書を実装する AI が Python コード一式を生成し、サーバを起動し、`--buffer` / `--search` のスモークテストを通したところで**`~/.claude/settings.json` のフック登録だけユーザー任せにする**（あるいはグローバル設定ファイルに触れることを避けてスキップする）ケースである。これが起こると、プロジェクトディレクトリの外で起動した Claude Code セッションでは**フックが一切発火せず**、毎ターンの `--search` も、会話の自動保存も、何ひとつ走らない。ユーザーはスモークテストの成功画面を見て「セットアップ完了」だと信じ込み、数ヶ月後になって「Claude に話した内容がひとつも記録されていなかった」と気付くことになる。
>
> **実装は、§4 ① の hook ブロックを `~/.claude/settings.json` に冪等にマージする処理を、完了報告の前に必ず自動で実行すること。** 経路 A（`pip install` + `n3mc --init`）はこれを内蔵で行う。経路 B（AI ネイティブ再生成）はプログラムで行うこと — ユーザーに手動編集を依頼してはならず、「グローバル設定ファイルだから触れない」という理由でスキップしてもならない。
>
> 検証ゲート（§6 テスト 7 と同等）: グローバルの `~/.claude/settings.json` のフックエントリが、`command` 文字列に `n3mc-hook`（UserPromptSubmit）と `n3mc-stop-hook`（Stop）を含むまで、実装は**完了していない**。pip インストール経由の場合、これらは絶対パス（Windows なら `.../Scripts/n3mc-hook.exe`、Unix なら `.../bin/n3mc-hook`）に解決される。

### ① グローバル設定（`~/.claude/settings.json`）— フック登録

#### 経路 A — `n3mc --init` に任せる（推奨）

`pip install` 後、以下を実行:

```bash
n3mc --init
```

これにより `shutil.which('n3mc-hook')` / `shutil.which('n3mc-stop-hook')` が解決した絶対パスで hook ブロックが `~/.claude/settings.json` に書き込まれ、§4 ② のパーミッションのルールが `permissions.allow` に足される。再実行は no-op（冪等 — **このパッケージの**エントリは重複追加せず置換する。ほかの N3MC の版のフックには触れない。経路 B 参照）。

書き込まれる JSON はこんな形（パスはマシンによって異なる）:

```json
{
  "hooks": {
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/Scripts/n3mc-hook.EXE"
          }
        ]
      }
    ],
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "C:/Users/you/AppData/Local/Programs/Python/Python312/Scripts/n3mc-stop-hook.EXE"
          }
        ]
      }
    ]
  }
}
```

> **⚠️ パス区切り文字**: Claude Code は bash シェルを使用するため、Windows のバックスラッシュ (`\`) はエスケープシーケンス (`\n`→改行, `\t`→タブ) として解釈される。`n3mc --init` は常にフォワードスラッシュで書き込む。手動編集する場合も同様にフォワードスラッシュを使用すること。

#### 経路 B — 冪等フック登録アルゴリズム（AI ネイティブ再生成用）

指示書から再生成する場合、AI は `~/.claude/settings.json` に対して以下と等価な処理を実行すること。マージは**既存フィールド（`permissions`、`autoUpdatesChannel`、ユーザーが入れている任意のキー）をすべて保全**し、足りない 2 件のフックエントリと、足りないパーミッションのルールのみを追記する。**このパッケージの**フックエントリは置換する（重複追加しない）。**ほかのプログラムのフック — フックが同じく `n3mc_hook.py` という名前のスクリプトである Pro など、ほかの N3MC の版を含む — は決して取り除かない。** 導入済みの環境での再実行は no-op（何も書かない）。

```python
# n3memorycore/n3memory.py:cmd_init — 必須ステップの実行可能版
import datetime, json, os, pathlib, shlex, shutil, sys, tempfile
import n3memorycore

SETTINGS = pathlib.Path(os.path.expanduser("~/.claude/settings.json"))
PKG_DIR = os.path.normcase(os.path.abspath(os.path.dirname(n3memorycore.__file__)))

OURS = {  # イベント -> (エントリポイント名, モジュール名)
    "UserPromptSubmit": ("n3mc-hook", "n3memorycore.n3mc_hook"),
    "Stop":             ("n3mc-stop-hook", "n3memorycore.n3mc_stop_hook"),
}

def _tokens(command: str) -> list:
    try:
        return shlex.split(command, posix=True)
    except ValueError:
        return command.split()

def is_own_hook(command: str, event: str) -> bool:
    """このパッケージのフックを実行するコマンドのときだけ True（決定 D1）。"""
    exe_name, module = OURS[event]
    toks = _tokens(command)
    if not toks:
        return False
    # (a) 実行ファイルが自分のエントリポイント: n3mc-hook / n3mc-hook.exe（フォルダは問わない）
    first = os.path.basename(toks[0]).lower()
    if first.endswith(".exe"):
        first = first[:-4]
    if first == exe_name:
        return True
    # (b) python -m n3memorycore.n3mc_hook
    for i, t in enumerate(toks[:-1]):
        if t == "-m" and toks[i + 1] == module:
            return True
    # (c) このパッケージのフォルダの中のスクリプト
    script = module.split(".")[-1] + ".py"
    for t in toks[1:]:
        if os.path.basename(t).lower() == script:
            d = os.path.normcase(os.path.abspath(os.path.dirname(os.path.expanduser(t))))
            if d == PKG_DIR:
                return True
    return False

def resolve_cmd(script_name: str) -> str:
    exe = shutil.which(script_name)
    if exe:
        # Claude Code はフックを bash で実行する: 常にフォワードスラッシュ。
        return pathlib.Path(exe).as_posix()
    module = {"n3mc-hook": "n3memorycore.n3mc_hook",
              "n3mc-stop-hook": "n3memorycore.n3mc_stop_hook"}[script_name]
    return f'"{pathlib.Path(sys.executable).as_posix()}" -m {module}'

# 1. 読む。読めないファイルには触れない（書き直しも「修復」もしない）。
if SETTINGS.exists():
    raw = SETTINGS.read_text(encoding="utf-8")
    try:
        settings = json.loads(raw)
    except json.JSONDecodeError as e:
        sys.exit(f"{SETTINGS} is not valid JSON ({e}). Nothing was changed. "
                 "Fix the file, then run n3mc --init again.")
    perms = settings.get("permissions", {}) if isinstance(settings, dict) else None
    if (not isinstance(settings, dict) or not isinstance(settings.get("hooks", {}), dict)
            or not isinstance(perms, dict) or not isinstance(perms.get("allow", []), list)):
        sys.exit(f"{SETTINGS} has an unexpected structure. Nothing was changed.")
    before = json.loads(raw)
else:
    raw, settings, before = None, {}, None

# 2. フックをマージする: 自分のエントリだけを置き換え、ほかの N3MC のフックは残す（そして知らせる）。
hooks = settings.setdefault("hooks", {})
for event, (exe_name, _module) in OURS.items():
    kept = []
    for entry in hooks.get(event, []):
        inner = [h for h in entry.get("hooks", [])
                 if not is_own_hook(h.get("command", ""), event)]
        for h in inner:
            c = h.get("command", "")
            if any(m in c for m in ("n3mc_hook", "n3mc-hook", "n3mc_stop_hook", "n3mc-stop-hook")):
                print(f"Another N3MC hook is registered for {event} and was left in place: {c}")
        if inner:
            kept.append({**entry, "hooks": inner})
    kept.append({"hooks": [{"type": "command", "command": resolve_cmd(exe_name)}]})
    hooks[event] = kept

# 3. §4 ② のパーミッションのルールで足りないものを足す（何も取り除かない）。
allow = settings.setdefault("permissions", {}).setdefault("allow", [])
for rule in PERMISSION_RULES:            # §4 ② の一覧
    if rule not in allow:
        allow.append(rule)

# 4. 何か変わったときだけ書く: 控えを取ってから、アトミックに置き換える。
if settings != before:
    SETTINGS.parent.mkdir(parents=True, exist_ok=True)
    if raw is not None:
        stamp = datetime.datetime.now().strftime("%Y%m%d%H%M%S")
        shutil.copy2(SETTINGS, SETTINGS.with_name(f"settings.json.bak-n3mc-{stamp}"))
    fd, tmp = tempfile.mkstemp(dir=SETTINGS.parent, prefix=".settings.json.")
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
    os.replace(tmp, SETTINGS)
```

- **どのエントリが自分のものか（決定 D1、v1.5.0）**: (a) 実行ファイルが `n3mc-hook` / `n3mc-stop-hook` のコマンド（`.exe` の拡張子は無視し、フォルダは問わない）、(b) `-m n3memorycore.n3mc_hook` / `-m n3memorycore.n3mc_stop_hook` を含むコマンド、(c) **このパッケージのフォルダから** `n3mc_hook.py` / `n3mc_stop_hook.py` を実行するコマンド、だけ。エントリポイント形と `-m` 形の両方を見分けないと、再実行のたびに重複が漏れて残る（両方の形を照合するようにする前は、実機で 4 件重複事例あり）。
- **ほかの N3MC のフックは残す**: `n3mc_hook` を*含むだけ*のコマンド — 例えば `python ".../N3MC-Pro_Arnolf/N3MemoryCore/n3mc_hook.py"` — はほかの版のもので、残す。`--init` は `Another N3MC hook is registered for <event> and was left in place: <command>` と出す。両方が登録されていると、両方が毎ターン動く。どちらかを取り除くかはユーザーが決める。
- **控えとアトミックな書き込み（v1.5.0）**: 既存のファイルを変える前に `~/.claude/settings.json.bak-n3mc-<YYYYmmddHHMMSS>` にコピーする。同じフォルダの一時ファイルに書いて `os.replace` で置き換える。ファイルを JSON オブジェクトとして読めない場合（または `hooks` / `permissions` / `permissions.allow` の型が違う場合）は、何も変えず、理由を出して終了ステータス 1 で終わる。
- **「`hooks` キーが無ければ `{...}` を入れる」だけの判定はダメ**: 空の `hooks: {}` が既に存在するケースで素通りし、結果フックが入らないままになる。必ず内側の配列まで点検すること。
- **クロスプラットフォームのパス**: Windows でも `command` 文字列の中はフォワードスラッシュ厳守。`shutil.which()` の戻り値は `pathlib.Path(exe).as_posix()` で必ず正規化すること。Claude Code が起動する bash シェルはバックスラッシュをエスケープシーケンス（`\n` → 改行、`\t` → タブ）として解釈するため、`\Users\...` をそのまま書くとパスが破壊される。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は、コマンドに `n3mc-hook` か `n3mc_hook` を含むだけのフックをすべて置き換えていた。N3MC Pro（`python ".../N3MemoryCore/n3mc_hook.py"` として登録される）も動かしている PC では、`n3mc --init` が Pro のフックを黙って消していた。§3 は `server_port` を分ければ 1 台の PC で両方を動かせると明記しているのに、である。また `settings.json` を控えなしにその場で書き換え、不正なファイルでは `--init` がトレースバックで終わっていた。

### ② パーミッションのルール（`PERMISSION_RULES`、`n3mc --init` が書く）

`n3mc --init` は、下のルールをユーザーグローバルの `~/.claude/settings.json` の `permissions.allow` に足す（足りないものだけ。何も取り除かない）。これにより、rules ファイルに書かれているとおり、Claude はどのプロジェクトでも確認なしで記憶のコマンドを実行できる（決定 M12、v1.5.0）。v1.4.0 はフックしか登録しなかった。このリポジトリ（自分の `.claude/settings.json` に一覧がある）の外ではコマンドが許可されず、rules ファイルの「Both invocation forms are auto-allowed」という記述は正しくなかった。

Claude が直接呼び出すエントリポイントは 2 系統ある。**Claude AI 側からの呼び出しは原則 `python -m n3memorycore.n3memory ...` 形式を一級市民とする** — Claude Code が起動する bash サブシェルの `PATH` には Python の `Scripts/` ディレクトリが含まれるとは限らず、`n3mc` バイナリが「対話シェルでは通るのに本番フックでは command not found になる」差異が報告されている。`python` 自体はインストール済み環境ならほぼ確実に `PATH` に乗っているため、`python -m` で起動すれば PATH 解決問題を完全に回避できる。フックスクリプト（`n3mc-hook` / `n3mc-stop-hook`）は Claude Code 自身が絶対パスで起動するので `Bash(...)` パーミッションは不要。

```json
{
  "permissions": {
    "allow": [
      "Bash(python -m n3memorycore.n3memory --search *)",
      "Bash(python -m n3memorycore.n3memory --buffer *)",
      "Bash(python -m n3memorycore.n3memory --list*)",
      "Bash(python -m n3memorycore.n3memory --repair*)",
      "Bash(python -m n3memorycore.n3memory --stop*)",
      "Bash(python -m n3memorycore.n3memory --show-turn *)",
      "Bash(python -m n3memorycore.n3memory --recall-thread *)",
      "Bash(n3mc --search *)",
      "Bash(n3mc --buffer *)",
      "Bash(n3mc --list*)",
      "Bash(n3mc --repair*)",
      "Bash(n3mc --stop*)",
      "Bash(n3mc --show-turn *)",
      "Bash(n3mc --recall-thread *)"
    ]
  }
}
```

> **🛑 推奨呼び出し形式（重要）**: `.claude/rules/n3mc-behavior.md` の Active RAG ルールでは、Claude に `python -m n3memorycore.n3memory --search "<keywords>"` を呼ばせる。`n3mc --search ...` は対話シェル経由のスモークテストでは通るが、Claude Code が UserPromptSubmit やツール呼び出しで起動する bash サブシェルでは PATH に `n3mc` が乗っていないケースが多く、サイレントに「検索が走らないだけ」の状態になる。`python -m` 形式なら起動経路を問わず安定する。
>
> **⚠️ パスの引用符**: `n3mc` コンソールスクリプト形式は対話シェル用途では便利だが、PATH 依存。venv 内などで `python` インタプリタの位置に依存する場合は、ユーザーが `Bash(python *n3memorycore.n3memory* --search *)` のようなワイルドカードを足して、インタプリタパスのバリエーションを吸収させてもよい（`--init` はこれを足さない）。
>
> リポジトリ自身の `.claude/settings.json` にも同じ一覧があり、`--init` を実行せずにリポジトリの中で作業する人のためのものである。

### `--stop` フック仕様（セッション終了処理）

`--stop` の責務は **セッション終了時のクリーンアップ（AI 行動指針の用意と、旧版の memory_context.md の片付け）**である。会話内容の保存はフックにより全自動で行われる。Claude AI が手動で `--buffer` を呼び出す必要はない。`Stop` フック（`n3memorycore/n3mc_stop_hook.py`、エントリポイント名 `n3mc-stop-hook`）は `--stop` の実行に加え、Claude の最終回答の自動保存も行う（§5 の【自動化】参照）。

**`n3mc-stop-hook` の stdin 入力仕様**: Claude Code の Stop フックは以下の JSON を標準入力に渡す。`last_assistant_message` が Claude の最終回答テキストである。

```json
{
  "session_id": "<セッションID>",
  "stop_hook_active": true,
  "last_assistant_message": "Claudeの最終回答の全文テキスト"
}
```

`--stop` 実行時の処理順序:

1. `<cwd>/.claude/rules/n3mc-behavior.md`（AI 行動指針）を用意する（`_ensure_behavior_md`、下記「AI 行動指針ファイルの版」）。
2. `<cwd>/.claude/CLAUDE.md` に旧版が書いた memory_context の `@import` 行があれば取り除く（`_retire_memory_context`、下記）。**N3MC は CLAUDE.md に何も書き足さない。** CLAUDE.md がなければ作らない。
3. 後片付け（v1.5.0）: 更新時刻が 30 日より古い `.memory/turn_id_*.txt` を削除し（Claude Code のセッションごとに 1 つ作られ、これまで一度も消されていなかった）、`audit.log` が 50 MB を超えていたらローテーションする（§5.4「audit.log の大きさと中身」）。
4. 正常終了する。成功時は一切出力しない（沈黙）。`--stop` は DB に書かないので、物理保存の警告を出すことはない。手順 1〜3 のエラーはそれぞれ 1 行ずつ stderr に出し、終了ステータスは変えない。

> **`--stop` はサーバーを止めない**: 名前に反して、`--stop` は Stop フック用のクリーンアップで、記憶サーバーは止まらない。サーバーのコードや `config.json` を変えたときは、次のコマンドで `code_version` の違いからサーバーが自動で入れ替わる（§3）。

### memory_context.md の廃止（v1.4.0）

v1.3.x までは、`--search` の結果を `~/.n3mc/.memory/memory_context.md` にも書き、`<cwd>/.claude/CLAUDE.md` に絶対パスの `@import` 行を `--stop` が書き足していた。**v1.4.0 でこの仕組みを廃止した。**

- **理由**: `memory_context.md` は PC に 1 つしかなく、どのセッション・どのプロジェクトの質問でも上書きされる。新しいセッションの最初に `@import` で読み込まれるのは**別の会話の検索結果**で、毎回コンテキストを消費していた。毎ターンの記憶は UserPromptSubmit フックの**標準出力**から入るので、ファイルも `@import` も要らない。
- **実装**: `--search` / `--hook-submit` は検索結果を **stdout にだけ**出す。`memory_context.md` を作らない・書かない。`paths.py` に `CONTEXT_FILE` 定数は残してよい（片付けにだけ使う）。
- **片付け（`_retire_memory_context`、`--stop` のたびに実行）**:
  1. `<cwd>/.claude/CLAUDE.md` に**このインストール自身の `CONTEXT_FILE`**（`HOME_DIR/.memory/memory_context.md`）**を指す** `@import` 行があれば、その行と、その直前に続く N3MC のコメントのまとまり（`# N3MemoryCore — …` / `# Path resolves to …` / `# If you have not yet run …`。コメントと `@import` 行の間の空行も含む）を取り除く。ファイルのほかの場所は変えない。候補の行は正規表現 `^[ \t]*@[^\n]*memory_context\.md[ \t]*$`（行単位、空白を含むパスも当たる）で探す。候補を取り除くのは、`@` のあとのパス（前後の空白を除き、`~` を展開したもの）が絶対パスで、`os.path.normcase(os.path.normpath(path)) == os.path.normcase(os.path.normpath(CONTEXT_FILE))` のとき**だけ**（スラッシュとバックスラッシュは同じとみなす）。相対パス（`@../.memory/memory_context.md`）、ほかのフォルダ（`@C:/other/.memory/memory_context.md`、ほかの版のファイル）、末尾が同じなだけの名前（`@docs/notes_about_memory_context.md`）は残す。書き換える前に `CLAUDE.md.bak-n3mc-<YYYYmmddHHMMSS>` に控えを取る。**改行の形（LF / CRLF）は元のまま書き戻す。** 該当行がなければファイルに触れない（読むだけ）。プロジェクトごとに CLAUDE.md があるので、マーカーで 1 回きりにはせず、毎回確かめる。
  2. `~/.n3mc/.memory/memory_context.md` が残っていれば消し、消したときに `.memory/.memory_context_retired` を作る。マーカーがあれば何もしない（ファイルがなければマーカーは作らず、次の `--stop` でまた確かめる）。
- ユーザーが自分で `@import` を書いたほかの CLAUDE.md（`~/.claude/CLAUDE.md` など）には触れない。プロジェクトの CLAUDE.md の中でも、取り除くのはこのインストール自身のファイルを指す行だけ。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 は、パスに関係なく正規表現に当たる行をすべて取り除いていた。Stop フックは Claude Code が作業しているフォルダで動くので、ほかのプログラムやユーザーが書いた `@` の行 — まだ `memory_context.md` を使っているほかの N3MC の版、相対パスの import、さらには `@docs/notes_about_memory_context.md` まで（再現済み）— を取り除いていた。以前の Free の版は常に自分の `CONTEXT_FILE` の絶対パスを書いていたので、そのパスと比べれば、それらが書いたものはすべて取り除け、それ以外は何も取り除かない。

### AI 行動指針ファイルの版（`_ensure_behavior_md`）

`.claude/rules/n3mc-behavior.md` は Claude Code がセッション開始時に自動で読み込む。ひな形（`_BEHAVIOR_MD_CONTENT`）の 1 行目は版の目印 `<!-- n3mc-behavior v2 -->` とし、次の節を含める:

- **Fully Automatic Saving** — 全会話はフックが保存する。`--buffer` を手で呼ばない。保存成功時は沈黙。
- **Recalled Memory Context** — 毎ターン、UserPromptSubmit フックの出力として `# Recalled Memory Context` が入る。`## Top matches` を先に読み、`## Previous matching Q-A exchanges` は補助として使う。同じ内容の版が複数あれば `saved` が新しいほうが今の版。`> ⚠️ N3MC notice:` の行があれば、その内容をユーザーに伝える。
- **Active RAG** — 足りないときは `python -m n3memorycore.n3memory --search "<keywords>"` を自分で実行する（`n3mc` ではなく `python -m` 形式。理由は §4 ②）。
- **Cut Exchanges** — Q-A の往復に `_(… omitted …)_` の行があり、省略された部分が答えに要るときは、そこに書かれた `python -m n3memorycore.n3memory --show-turn <turn_id>` を実行して全文を読む。
- **Recall Acknowledgment** — 検索結果を実際に使ったときだけ、返答の冒頭でユーザーの言語で一言そえる。
- **Fatal-Failure Warning** — 物理保存の失敗を強調表示する。

ファイルの扱い:

| 既存ファイル | 動作 |
|---|---|
| ない | ひな形を書く |
| 1 行目が今の目印と同じ | ファイルには何もしない。このパスの知らせ `behavior_md_edited:<hash>` を消す（ユーザーがひな形を取り込んだ） |
| 中身が**過去に配布したひな形のどれかと同じ** | 控え（`n3mc-behavior.md.bak-n3mc-<日時>`）を取ってから、今のひな形で置き換える |
| それ以外（ユーザーが手で編集した） | **置き換えない。** 知らせ `behavior_md_edited:<hash>` を出す（"The rules file <absolute path> has been edited by hand, so it was not updated. Merge the new template manually if needed."）。`<hash>` はファイルの絶対パスの SHA-256 の先頭 12 桁（16 進） |

- 「過去のひな形と同じ」の判定は、改行を LF にそろえた中身の SHA-256 の先頭 16 桁を `_KNOWN_BEHAVIOR_MD_HASHES` と比べる。v1.3.x までのひな形は `ade0a59b94f59f9a` と `34ed39efe5a3833d`（git の履歴から）。
- **ひな形を変えたら、目印の版を上げ、変える前のひな形のハッシュを `_KNOWN_BEHAVIOR_MD_HASHES` に足すこと。**
- **古い知らせのキー**: v1.4.0 のキー `behavior_md_edited`（パスのハッシュなし）は上の決まりでは消えないので、`_ensure_behavior_md` が動くたびに消す。
- **ファイルごとに 1 つの知らせ（v1.5.0）**: 知らせは PC に 1 つの `notices.json` に入るが、rules ファイルはプロジェクトごとにあるので、キーにパスのハッシュを含め、メッセージにファイル名を出す。v1.4.0 はパスを含まない 1 つのキーを使っていた。メッセージはどのプロジェクトのファイルのことかを示さず、ユーザーがひな形を取り込んだあとも、消すものがなかったので 1 日 1 回出続けていた。

---

## 4.5. FastAPI エンドポイント仕様

CLI は FastAPI サーバーへ HTTP リクエストを送信する。全エンドポイントは `http://127.0.0.1:{server_port}` に対して発行する。

| メソッド | パス | 対応CLIコマンド | 説明 |
| :--- | :--- | :--- | :--- |
| `GET` | `/health` | （内部死活監視） | `{"status": "ok", "root", "code_version", "pid"}` を返す（§3「サーバーの名乗り」） |
| `POST` | `/buffer` | `--buffer`、フック | 本文 `{"content": str, "agent_name": str?, "local_id": str?, "session_id": str?, "turn_id": str?}`（`local_id` 省略時 → `config.json`、`session_id` 省略時 → サーバーのセッション。§3「識別子」）。保存する |
| `POST` | `/search` | `--search`、`--hook-submit` | 本文 `{"query": str, "session_id": str?, "since": str?, "until": str?}`。`since` / `until` は CLI と同じ規則で解釈する（§3.6）。読めない値には 400 を返す。結果を返す |
| `POST` | `/repair` | `--repair`、`--hook-submit` | 本文は省略可: `{"max_records": int?}`（§3.5「マイグレーションのバッチ処理」）。未インデックスレコードを修復 |
| `GET` | `/list` | （対応する CLI コマンドなし） | 全レコードの一覧を返す（各レコードに `agent_name` を含む）。API クライアントのために残している。`--list`・`--recall-thread`・`--show-turn` は DB を直接読み、これを呼ばない |
| `POST` | `/shutdown` | （内部、サーバーの入れ替え） | 本文 `{"root": str}`。`root` が自分の `root` と同じときだけ終了する。違えば 403（§3） |
| `DELETE` | `/delete/{id}` | — | **Pro のみ。** Free は常に 403（`{"detail": "Pro feature"}`）を返す |
| `POST` | `/gc` | — | **Pro のみ。** Free は 403 を返す |
| `POST` | `/import` | — | **Pro のみ。** Free は 403 を返す |

どのエンドポイントも、`Host` ヘッダーがローカルでなければ 403 を返す（§3「常駐型 FastAPI サーバー」）。

- `/buffer` の重複の扱い: **完全に同じ文だけ**を飛ばす（`check_exact_duplicate`）。`turn_id` があるときは**同じ `turn_id` の中だけ**で比べ、`turn_id` がないとき（手動の `--buffer`）は全体で比べる。完全一致を先に調べてから埋め込みを計算する。**類似度（cos_sim）による重複排除は行わない**（v1.4.0 で廃止。§5.4）。

### `--show-turn`（往復の全文）

```bash
python -m n3memorycore.n3memory --show-turn <turn_id>
```

- 指定した `turn_id` の全レコードを、Q-A ペアと同じ並び（[user] → [claude]、各ロール内は rowid 順）で、**content を切り詰めずに**出す。各レコードの前に `--- <id> saved=<ローカル時刻> ---` の行を置き、最後に `Total: N records`。
- 自分の DB を直接読む（サーバー不要）。
- 差し込みの Q-A の往復が `pair_max_chars` で切れたときの省略の行（§5）に、このコマンドが書かれる。

### `--hook-submit`（UserPromptSubmit フックエントリポイント）

```bash
echo '{"session_id":"…","transcript_path":"/path/to/session.jsonl","prompt":"ユーザー入力"}' | n3mc --hook-submit
```

stdin から UserPromptSubmit の JSON を読み取る。Claude Code は `session_id`、`transcript_path`、`cwd`、`permission_mode`、`hook_event_name`、`prompt` を送る（新しい版ではキーが増える。知らないキーは無視する）。`message` は `prompt` の別名として受け付ける。**この入力に `last_assistant_message` はない**（このキーは Stop の入力にだけある）。それでも呼び出し側が渡してきた場合は、トランスクリプトを読む代わりにそれを使う。単一プロセス内で HTTP リクエスト経由により全 UserPromptSubmit 処理を実行する：`--repair` → `--buffer`（Claude の前の回答を保存。トランスクリプトから読む — 下記）→ `--search` → `--buffer`（ユーザー発言を保存）。`n3mc-hook` エントリポイント（`n3memorycore/n3mc_hook.py`）から呼び出される。AI が手動で実行する必要はない。このフックで保存される全レコードには `agent_name = "claude-code"` が自動タグ付けされる。

- **トランスクリプトから前の Claude の返答を読む（決定 D3、v1.5.0）**: `transcript_path` はセッションの JSONL トランスクリプトである。末尾から読み、**今のプロンプトより前にある、最後の assistant のテキストブロック**を取り出す。これは Stop フックが `last_assistant_message` として受け取るテキストと同じものである。
  1. 行を後ろから順にたどる。JSON オブジェクトでない行、`isSidechain: true` の行（サブエージェント）、`type` が `user` でも `assistant` でもない行は飛ばす。
  2. **本物のプロンプトでない** `user` 行は飛ばす。`isMeta: true` の行、`message.content` に文字列も `type: "text"` のパートもない行（`tool_result` だけ。同じ返答の一部）、テキストが `[Request interrupted by user`（ユーザーが割り込んだときに Claude Code が書く印）、`<command-`、`<local-command-`（スラッシュコマンドとその出力）、または `_SYSTEM_PROMPT_PREFIXES` のどれかで始まる行である。
  3. 今のプロンプトそのものは飛ばす。末尾にある本物のプロンプトの行のうち、テキストが新しいプロンプトと同じもの（トランスクリプトにまだ入っていないこともある）。
  4. 最初に見つかった、空でない `type: "text"` のパートを持つ `assistant` 行が答えになる。その行の**最後の**空でないテキストパートを取る。`thinking` と `tool_use` のパートは無視する。先に別の本物のユーザープロンプトに着いたら、前の返答はない。
  - **最後のブロックだけを取る理由**: Claude Code の Stop の入力には、返答の最後のテキストブロックだけが入る（2026-11-13 にこのセッションのトランスクリプトで測定。直近 6 件の Stop の入力は、どれもトランスクリプトのテキストブロック 1 つと一致し、そのターンのブロックをつなげたものと一致したものはなかった）。同じブロックを取れば、Stop が保存済みのときは完全一致の検査で飛ばされる。ブロックをつなげると、ツールを使ったターンごとに、少し違う写しがもう 1 つ保存される。同じ返答のうちツール呼び出しより前に書いたテキストは保存されない。Stop も保存しない。
  - **割り込みの印を飛ばす理由**: ユーザーが割り込むと、Claude Code は途中までの返答の後に、テキストが `[Request interrupted by user]`（または `… for tool use]`）の `user` 行を書く。これをプロンプトとして扱うと、たどるのがそこで止まり、割り込まれた返答（この読み取りが必要になる場面）が見つからない。
  - ファイルは最後の 5 MB までしか読まない（末尾から seek する）。失敗（パスがない、ファイルを読めない、構造が想定と違う）はすべて「前の回答なし」として扱い、`audit.log` に `{"hook": "UserPromptSubmit", "stage": "transcript_unreadable", "reason": "<短い理由>"}` を書いて続ける。トランスクリプトは取り戻すための材料にすぎず、これでフックを失敗させてはならない。
  - 回答は `turn_id_<session>.txt` の turn_id（1 つ前の往復）で、通常のチャンク分割（全文に `purify_text` をかけてから `[claude]` / `[claude i/N]`）を通して保存する。Stop フックがすでに保存していれば、チャンクは同じものになり、同じ `turn_id` の中での完全一致の確認（§5.4）がすべてのチャンクを飛ばすので、二重には保存されない。
  - **実測した入力（2026-11-13）**: 開発 PC の Pro 版の監査ログでは、最新の UserPromptSubmit の入力 47 件（2026-11-13）は、どれもキー `session_id, transcript_path, cwd, scratchpad_dir, prompt_id, permission_mode, hook_event_name, prompt`（+ `session_title`）を持ち、`last_assistant_message` を持つものはなかった。2026-05-31 の Free の監査ログでも 201 件の入力で同じだった。トランスクリプトの行は、上に書いたとおり `type`、`isSidechain`、`message.role` / `message.content` を持つ。

> **⚠️ 理由（v1.5.0、Free v1.4.0 のレビューで見つかった）**: v1.4.0 の手順 2 は UserPromptSubmit の入力から `last_assistant_message` を読んでいたが、Claude Code はそこには決して送らない。そのため Q-A 契約の「回復経路」は一度も動かず、Stop フックが動かなかった返答（中断したターン、クラッシュ）は DB にも `audit.log` にもなかった。

- **画像だけの質問（v1.5.0）**: `_extract_text()` が何も返さない（質問が画像だけ）ときも、**新しい** `turn_id` を作って `turn_id_<session>.txt` に書く。飛ばすのは `[user]` の保存だけ。そうしないと、画像への返答が 1 つ前の質問の `turn_id` で保存され（v1.4.0 から Stop フックはファイルを消さない）、関係のない往復の一部として出てしまう。

- **システムの入力は飛ばす**: ユーザー発言が `_SYSTEM_PROMPT_PREFIXES`（現在は `<task-notification>` だけ）で始まる場合（バックグラウンド処理の完了通知など、ユーザーが打ったものではない入力）は、repair・保存・検索をせず、**新しい `turn_id` も振らない**（その通知への Claude の返答は、今の往復の `turn_id` に入る）。`audit.log` に `{"hook": "UserPromptSubmit", "stage": "skipped", "reason": "system_prompt"}` の行を書き、空の結果（`_No relevant memories found._`）を stdout に出して終わる。ほかのシステムの入力が見つかったら `_SYSTEM_PROMPT_PREFIXES` に足す。
- ポートが別のフォルダのサーバーのときは §3「ポートが別のフォルダのサーバーのとき」に従う（保存は直接、検索はしない）。

### `--save-claude-turn`（Stop フック専用ヘルパー）

```bash
echo '{"session_id":"...","stop_hook_active":true,"last_assistant_message":"Claude の応答テキスト"}' | n3mc --save-claude-turn
```

Stop フックと同じ JSON を stdin から読み取り、`last_assistant_message` を抽出して全文に `purify_text` をかけ、そのあと `chunk_text(max_chars=400, overlap=40)` で分割し、各チャンクを `[claude]` / `[claude i/N]` レコードとして HTTP `/buffer` 経由で単一プロセス内で保存する。`turn_id` は `~/.n3mc/.memory/turn_id_<session>.txt` から読み取る（直前の `--hook-submit` がユーザー発言を保存した際に書き込んだもの）。**保存のあとも turn_id のファイルは消さない**（次の UserPromptSubmit が上書きする）。同じ質問の中で Stop が 2 回来ても（バックグラウンド処理の通知への返答など）、同じ往復の完全一致の判定で二重保存が止まる。ファイルが無ければ新しい UUID4 turn_id を生成する。`n3mc-stop-hook` エントリポイント（`n3memorycore/n3mc_stop_hook.py`）のみが呼び出すサブコマンドであり、AI が手動で実行する必要はない。

> **補足: 専用サブコマンドを使う理由**
> なぜ `--buffer -` でなく専用サブコマンドなのか — Stop フック自身が JSON エンベロープを読むために stdin を消費するため、`--buffer -` を使うと stdin の二重消費になるか、チャンクごとに subprocess を立ち上げる必要が生じる（N 回呼び出し）。`--save-claude-turn` はチャンク化された保存をすべて単一プロセス内で順序通り実行し、常駐サーバへの HTTP keep-alive を 1 本で共有する。下記「1ターンあたりの合計サブプロセス数」での「buffer」とはこの呼び出しを指す。

### レスポンス形式

```json
// POST /buffer・/repair 成功時
{"status": "ok", "count": <処理件数>}

// POST /buffer 保存しなかったとき（空、または完全一致）
{"status": "skipped", "count": 0, "reason": "empty" | "exact_duplicate"}

// POST /search 成功時
{
  "results": [
    {"id": "...", "content": "...", "score": 0.8523, "timestamp": "...", "turn_id": "..."},
    ...
  ],
  "pairs": {"<turn_id>": [{"id": "...", "content": "...", "timestamp": "..."}, ...]},
  "pairs_omitted": {"<turn_id>": <pair_max_chars で省略した分割の数>}
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

> **宛先の区別**: 以下の指示は **【AI行動指針】**（Claude自身への指示）と **【実装仕様】**（プログラムとして実装する処理）に分類される。本節は次の 4 つのサブセクションに分かれる：
> - **5.1 完全記録契約** — 「何が必ず保存されるか」の不変条件（6 項目）
> - **5.2 フック自動化フロー** — UserPromptSubmit / Stop の実行モデルと処理順
> - **5.3 AI 行動指針** — Claude 自身が遵守する利用ルール
> - **5.4 実装仕様の詳細** — その他のサーバ／フック実装上の規定

### 5.1 完全記録契約

N3MC は「完全保存」を謳う製品である。フックの書き込み経路は以下の6つを必ず守る。

1. すべてのユーザー発言、およびすべての Claude 回答は**一字一句、切り捨てなく**記録される。
2. 長文は `chunk_text`（max_chars=400、overlap=40、段落→文→ハード窓の優先順位）で重複付きチャンクに分割される。各チャンクは単一なら `[user]` / `[claude]`、複数なら `[user i/N]` / `[claude i/N]` とタグ付けされる。
   - **文の終わり（v1.5.0）**: 文は、`.` `!` `?` のあとに空白が続くところ、**または** `。` `！` `？` のところ（空白は不要）で終わる。v1.4.0 は ASCII の記号しか知らなかったので、空行のない日本語の文章は 400 字のハード窓まで落ち、文の途中で切られていた。
   - **「一字一句」の範囲**: メッセージのすべての文字が、順番どおりにどれかのチャンクに入る。チャンクの境目はバイト単位で正確ではない: 段落は前後の空白を除いて 1 つの空行（`\n\n`）でつなぎ直し、文は 1 つの空白でつなぎ、隣り合うチャンクは最大 40 字の重なりを共有する。そのため、チャンクをつなげても段落や文の間の元の空白は再現されない。生の入力は `audit.log` に残る。
3. **長さフィルタなし、スキップパターンフィルタなし**（従来の「ok / yes / thanks」等の無言ドロップは廃止）。**複数行コードブロックは文書設計に従い `[code omitted]` に置換される**（チャンク分割の前にメッセージ**全体**に 1 回かける — §5.4）——N3MemoryCore は会話テキストを記録する製品であり、ソースコードは記録対象外。これが唯一の文書化された例外で、会話記録のみが対象。インラインコード（`...`）はそのまま保持。
4. `~/.n3mc/.memory/audit.log` に追記専用 JSONL の監査ログを書き込む。これは他のどの処理よりも先に実行されるため、失敗しようがない最後の砦。すべてのフック呼び出し（UserPromptSubmit + Stop）は 1 件の JSON レコード `{"ts", "hook", "raw", "payload"}` を残す。
5. 埋め込みサーバーへの HTTP POST が失敗した場合、書き込みは `_buffer_direct`（埋め込みなしで SQLite に直接 INSERT、次回 `--repair` で再インデックス）にフォールバックする。無言のドロップは禁止。すべての失敗経路はフォールバックで成功するか、または stderr へ出力する。
6. 画像のみのプロンプトでも、repair・Claude 回答保存・search・監査ログ記録はすべて実行され、新しい `turn_id` も作られる（v1.5.0）。Step 4（ユーザー保存）のみ、保存すべき本文がないためスキップする。

### 5.2 フック自動化フロー

> 上記「完全記録契約」を実現する具体的な実行モデル。`audit.log` 書き込みは常に Step 0 として最初に走る（契約 4 を担保する最後の砦）。

- **【自動化】修復・検索・会話の自動保存**: `UserPromptSubmit` フック（`n3memorycore/n3mc_hook.py`、エントリポイント名 `n3mc-hook`）は `n3mc --hook-submit` を呼び出し、単一プロセス内で以下を実行する。**Step 0 — 監査ログ（常に最初）**: すべてのフック呼び出しは、他の処理に先立って追記専用 `~/.n3mc/.memory/audit.log` に JSON レコード `{"ts", "hook", "raw", "payload"}` を 1 件追加する。これは他のどの処理よりも前に書かれる「最後の砦」としての権威ある生ログであり、後続が全滅しても原文だけは残る。続けて：`--repair`（未ベクトル化修復。1 ターンに最大 200 件、§3.5）、`--buffer`（Claude の直前の回答を **`transcript_path` から読み**（§4.5 `--hook-submit`）、全文保存する — 全文に `purify_text` をかけてから `chunk_text(max_chars=400, overlap=40)` で分割し `[claude]` / `[claude i/N]` で保存。保存済みのチャンクは完全一致の判定で飛ばされる）、`--search`（記憶取得）、`--buffer`（ユーザー発言を全文保存する — メッセージ全体に `purify_text` をかけてから分割し `[user]` / `[user i/N]` で保存）。**長さフィルタなし、スキップパターンフィルタなし**：空でない入力はすべて一字一句記録される。Claude Code が画像+テキストのプロンプトを渡す場合、`prompt` フィールドが JSON 配列になる場合がある。`_extract_text()` は `type=="text"` の部分のみを抽出する。結果が空（画像のみのプロンプト）の場合でも、repair・Claude 回答保存・search・新しい `turn_id`・監査ログ記録は実行され、Step 4（ユーザー保存）のみ、記録すべき本文がないためスキップする。生のマルチモーダルペイロードは `audit.log` に捕捉される。
- **【実装仕様】フック内のサブプロセス実行方式（変更禁止）**: `n3memorycore/n3mc_hook.py` および `n3memorycore/n3mc_stop_hook.py` 内のサブプロセス呼び出しは **`subprocess.run`（同期・ブロッキング）** を使用し、各コマンドの完了を待ってから次を実行せよ。`Popen`（非同期・ファイアアンドフォーゲット）を使用してはならない。**理由**: `--repair` → `--search` には実行順序の依存関係がある（修復が完了しないと検索が不完全になる）。また `--search` の結果が stdout に出し終わる前に Claude へ制御が戻ると、検索結果が読めない。速度向上を目的とした非同期化は、データの整合性と検索精度を破壊する。なお、FastAPI サーバーの起動（§3 Clean CLI）のみ `Popen` を使用する（起動完了を待つ必要がないため）。
- **【自動化】Claude の回答の自動保存**: `Stop` フック（`n3memorycore/n3mc_stop_hook.py`、エントリポイント名 `n3mc-stop-hook`）はまず Step 0 の監査ログ記録を書き（プロセス内で実行）、続いて以下の 2 つの同期サブプロセスを順番に呼び出す:
  1. `python -m n3memorycore.n3memory --save-claude-turn`（stdin = Stop フックの JSON）— `last_assistant_message` をチャンク化保存する。`[claude]` / `[claude i/N]` プレフィックス、既存の turn_id を引き継いで Q-A pairing を維持する。長さフィルタは適用せず、空でない回答はすべて記録する。**ここで `--buffer -` を使ってはならない**: Stop フックが既に stdin を audit.log 書き込みのために消費している。
  2. `python -m n3memorycore.n3memory --stop` — AI 行動指針の用意と旧 memory_context の片付け（§4「`--stop` フック仕様」参照）。

  1ターンあたりの合計サブプロセス数: `UserPromptSubmit` × 1（`--hook-submit`）+ `Stop` × 2（`--save-claude-turn` + `--stop`）= 3 回。
- **【実装仕様】未ベクトル化データの検出**: `memories` テーブルに存在するが `memories_vec` **または** `memories_fts` に存在しないレコードを未インデックスデータとして検出する（両インデックスを double LEFT JOIN で確認）。vec が欠損するレコードに対して埋め込みを生成し、fts が欠損するレコードに対して FTS に再挿入する。また、初回実行時のみ FTS 句読点クリーニング（§3「FTS 句読点除去」参照）を実行する。修復件数が1件以上の場合のみ警告を出力する。
### 5.3 AI 行動指針

- **【AI行動指針】全自動保存**: 全会話はフック（UserPromptSubmit / Stop）により自動保存される。Claude が自分で判断して `--buffer` を手動呼び出しする必要はない。**空でないユーザー発言・Claude 回答はすべて、一字一句、切り捨てなしで記録される。** 長さフィルタ（旧 `len(text) >= 10` / `>= 3`）は**廃止**、スキップパターンフィルタ（旧 `_SKIP_PATTERNS`、「ok / yes / thanks」定型応答フィルタ）も**廃止**。`ok` や `yes` のような短い応答も他と同様に記録される。
- **【AI行動指針】確定報告の沈黙**: 保存成功時は報告や挨拶を一切行わず、沈黙を守れ。
- **【AI行動指針】Active RAG**: 知識不足時、自発的に `--search` を実行せよ。`permissions.allow` により自動承認済み — 確認不要。
- **【AI行動指針】想起時の明示**: `--search` の結果を**実際に応答に活用した**（以前保存された情報を想起して答えた）場合は、返答冒頭にユーザー言語で一言そえよ。例:「前回の回答がメモリに保存されています。」「以前の会話から該当情報を取り出しました。」／英語なら "Pulling this from earlier memory in this session."。**関連メモリが無かった / 取得内容を実際には使わなかった場合は告知しない。** 「検索した」ことではなく「想起した」ことだけを述べよ。これにより、ユーザーは記憶レイヤが生きていることを毎ターン視認できる。
- **【AI行動指針】記憶コンテキストの読み方**: 毎ターンの記憶は UserPromptSubmit フックの出力（`# Recalled Memory Context`）として入る。`## Top matches` を先に読み、Q-A の往復は補助として使う。同じ内容の版が複数あれば `saved` が新しいほうが今の版。往復が省略されていて全文が要るときは、省略の行にある `--show-turn` を実行する。`> ⚠️ N3MC notice:` があればユーザーに伝える。

### 5.4 実装仕様の詳細

- **【実装仕様】致命的故障の警告**: DB書き込み失敗、またはINSERT後にDB件数が変化しない場合のみ以下を強調表示せよ:
  > ⚠️ 物理保存に失敗。現在の記憶は失われる可能性があります。
- **【実装仕様】文脈注入（stdout のみ）**: `--search` の結果は **stdout に `print()` で出力**せよ。UserPromptSubmit フックの stdout は Claude Code がそのターンのコンテキストに入れる。stdout 出力がないと Claude が検索結果を認識できず、記憶があるにもかかわらず「記憶にありません」と応答する原因になる。**`memory_context.md` などのファイルには書かない**（v1.4.0 で廃止。§4「memory_context.md の廃止」）。
- **【実装仕様】毎回の出力（劣化状態も必ず出す）**: `cmd_search` は呼び出されるたびに、結果に関わらず stdout に必ず出力せよ。失敗パスでは「劣化状態を示すプレースホルダー」を出し、記憶層の状態を Claude に見えるようにする:
  - **空クエリ**（画像のみのプロンプト、または `--search ""`）→ `# Recalled Memory Context\n\n_No relevant memories found._\n`。
  - **サーバ不通 / `/search` が非 2xx エラー** → `# Recalled Memory Context\n\n_(memory search unavailable: <reason>)_\n`。
  - **ポートが別のフォルダのサーバー** → §3 の文面。
  - **検索成功** → レンダリング済み markdown（Top matches + Q-A exchanges）。
  - どの場合も、出す知らせ（§3「知らせ」）があれば、見出し `# Recalled Memory Context` の直後（検索結果では `Search query:` の行の直後）に入れる。
- **【実装仕様】stdin 入力**: `--buffer` は引数の代わりに `-` を指定すると標準入力からテキストを読み取る（例: `cat file.txt | n3mc --buffer -`）。ただし Stop フック（`n3memorycore/n3mc_stop_hook.py`）内では `--buffer -` を使用してはならない。Stop フック自身が stdin から Claude Code の JSON を受け取るため、二重消費になり壊れる。オプションで `--agent-id NAME` 引数を指定すると、レコードの **`agent_name`**（表示名）を設定できる（例: `n3mc --buffer "テキスト" --agent-id "claude-code"`）。名前に反して `local_id` は設定しない。名前は互換のために残している（§3.7）。
- **【実装仕様】複数行コードブロックは `[code omitted]` に置換される**: 複数行コードブロック（```...```）は文書設計に従い `[code omitted]` に置換される——N3MemoryCore は会話テキストを記録する製品であり、ソースコードは記録対象外。インラインコード（`...`）はそのまま保持。コード以外の会話テキストは長さフィルタ／スキップパターンフィルタなしで一字一句保存される（上記「完全記録契約」参照）。`purify_text` / `_purify` の `_CODE_BLOCK_RE` は閉じた複数行フェンスのみを置換し、インラインコードと未閉フェンスは触らない。
  - **メッセージ全体を purify してから分割する（v1.5.0）**: ユーザー発言と Claude の回答のどちらも、`purify_text` を **`chunk_text` の前に全文に 1 回**かける。v1.4.0 はユーザー発言を先に分割してから各チャンクを purify していた。チャンクをまたいで分かれたフェンスや、40 字の重なりで繰り返された閉じフェンスが間違った開きフェンスと組になり、2 つのコードブロックの間の文章が `[code omitted]` に置き換えられて失われ、2 つ目のブロックのコードが保存されていた（563 字のメッセージで再現）。また、400 字より長いコードブロックはフェンスが別々のチャンクに入るので、そのまま保存されていた。`/buffer` と `_buffer_direct` でのレコードごとの purify は残す（手動の `--buffer` のため。全文に一度かけたあとは組になるフェンスが残らないので、フックのレコードには何も変えない）。
- **【カスタマイズ】言語ローカライズ**: 該当なし。完全記録契約により `_SKIP_PATTERNS` は**廃止**され、フックフィルタ層に言語依存の対象は存在しない。
- **【実装仕様】重複排除と HTTP 失敗時フォールバック**:
  - 保存を飛ばすのは**完全に同じ文**だけ（`check_exact_duplicate(conn, content, turn_id=...)`）。`turn_id` があるときは**同じ `turn_id` の中だけ**で比べる。`turn_id` がないとき（手動の `--buffer`）は全体で比べる。`/buffer` と `_buffer_direct` の両方で同じ規則。
  - **類似度（cos_sim）による重複排除は行わない（v1.4.0 で廃止）。** v1.3.x は cos_sim ≥ 0.95 の記録があると新しいほうを捨てていた。multilingual-e5 では、形のそろった長い返答の分割どうしや、ほぼ同じ文の設定の新版と旧版が 0.95 前後になり、「同じ内容の新版」と「形の似た別の内容」を区別できない。その結果、約 2,000 字・12 項目の設定を保存すると分割の 1 つが残らない、設定を作り直すと新しい版の一部が欠ける、ということが起き、完全記録契約が破れていた。新しい版を優先したいときは、消さずに検索の並べ替えで扱う（§5「検索結果の絞り込みと並べ替え」）。
  - **全体で比べないのは**、作り直した設定のうち変えていない分割が旧版と全く同じ文になり、全体で比べると新しい版の往復が欠けるため。
  - 埋め込みサーバーへの HTTP POST 失敗時（サーバー停止・タイムアウト・非 2xx）: 書き込みは `_buffer_direct()` にフォールバックし、埋め込みベクトルなしで直接 SQLite に INSERT する。欠損した vec インデックスエントリは次回の `--repair` で修復される。**無言のドロップは禁止**：すべての失敗経路は `_buffer_direct` で成功するか、stderr へメッセージを出力する。Step 0 の監査ログと組み合わせて、ユーザー発言と、フックに届いた Claude の回答が失われないことを保証する。Claude の回答は Stop フックで、Stop が動かなかったときは次の質問でのトランスクリプトの読み取り（§4.5）でフックに届く。どちらのフックも動かない回答（例: Stop が飛ばされたまま Claude Code を閉じる前の最後の返答）は保存されない。
- **【実装仕様】HTTP タイムアウト**: `_post()` はサーバーへの HTTP リクエストに 30 秒のタイムアウトを使用する。CPU での埋め込み推論が高負荷時に 4〜5 秒かかる場合に対応するため。
- **【実装仕様】ensure_server() 競合起動待機**: PID ファイルの番号のプロセスが**このフォルダのサーバーだと確かめられたとき（別プロセスが起動中）だけ**、最大 60 秒（120 × 0.5 秒）待機する。それでもまだ起動中なら（初回のモデルダウンロードは 2〜10 分かかる）、このコマンドはサーバーなしで進め、2 つ目のサーバーは起動しない。番号のプロセスが死んでいる・別のプロセスであるときは待たずに起動する（§3「PID ファイルの扱い」）。
- **【実装仕様】audit.log の大きさと中身（v1.5.0）**: `audit.log` はフックの生の入力 — すべての質問と返答の全文。**コードブロックや貼り付けたものも含む**（purify しない）— を持つ。個人的なデータとして扱うこと。50 MB を超えたら `--stop` が `audit.log.1` に名前を変え（古い `audit.log.1` は置き換える）、次のフックが新しいファイルを始めるので、残るのは最大で約 100 MB。`audit.log` の書き込みの失敗は stderr に出し、フックを止めることはない。
- **【実装仕様】_load_vec_extension 冪等性**: 同一コネクションで sqlite-vec 拡張を2回ロードしても副作用はない。拡張がすでにロード済みの場合、ロード呼び出しは "already" または "duplicate" を含む例外を発生させる。そのような例外はキャッチして続行し、それ以外の例外は再スローする。
- **【実装仕様】delete_memory のトランザクション化**: `delete_memory` はまず `_load_vec_extension(conn)` を呼び出し、次に3件の DELETE（memories_fts・memories_vec・memories）を try/except で囲み、失敗時は `conn.rollback()` を実行する。3つのインデックスがすべて成功するか、すべてロールバックされる。
- **【実装仕様】lifespan 起動**: 非推奨の `@app.on_event("startup")` の代わりに、FastAPI の `@asynccontextmanager` lifespan パターン（`async def lifespan(app: FastAPI)` に `yield` を含め、`FastAPI(lifespan=lifespan)` に渡す）を使用する。
- **【実装仕様】ベクトルモデルマーカー**: 各 `--repair` 呼び出し時、サーバーは現在有効な埋め込みモデル名を `~/.n3mc/.memory/vec_model.txt` に書き込む（初回時に作成）。既存ファイルの内容が `cfg['embed_model']` と異なる場合は stderr に警告を出力する — ディスク上のベクトルは現在の設定とは異なるモデルで生成されており、再生成するまで類似度検索が劣化する。リファレンス実装は再埋め込みを自動起動しない（大規模 DB では数分〜数十分かかる可能性があるため）。再埋め込みの実行はユーザーの判断に委ねられる（手動アップグレード手順は §3「言語特化モデルへの切り替え」を参照）。

### Q-A ペアリング契約
同一対話ターンで記録された [user] / [claude i/N] の各行は、共通の `turn_id`（UUID4）を共有します。これにより、同じ質問が後日再出現した場合に、ばらばらのチャンクではなく完全な前回のやり取りを復元して提示できます。

1. **turn_id 生成**: UserPromptSubmit フックが U_k（ユーザメッセージ）を保存する際、新しい turn_id T_k を生成し、すべての [user i/N] チャンクに付与したうえで `.memory/turn_id_<session>.txt`（Claude Code の session_id がないときは `.memory/turn_id.txt`）に保存します。
2. **Claude 側のペアリング**: Stop フックが C_k（Claude 応答）を保存する際、`.memory/turn_id_<session>.txt` から T_k を読み取り、すべての [claude i/N] チャンクに付与します。ファイルはクリアしません（次の UserPromptSubmit が上書きします）。
3. **リカバリ経路**: Stop フックがスキップされた場合、次回 UserPromptSubmit の Step 2 が `transcript_path`（§4.5 `--hook-submit`）から C_k を読み、ファイルの T_k で保存するので、U_k と C_k は同一 turn_id を共有します。（v1.4.0 は C_k が UserPromptSubmit の入力にあるものとしていましたが、Claude Code はそこには決して送らないため、この経路は動いていませんでした。）
4. **ペア復元**: `/search` は `results`（スコア順ヒット）、`pairs`（同 turn_id の兄弟行を順序付きで取得）、`pairs_omitted` の 3 フィールドを返します。並び順は [user] → [claude]、各ロール内では rowid 順。
   - **差し込む往復の数と量（v1.4.0）**: `pairs` に入れるのは、`results` の上から順に見て最初の `pair_top_n`（3）個の turn_id だけ。兄弟行を順に足し、全往復の合計が `pair_max_chars`（3,000 字）を超える行からあとは入れない。その往復の残りの行数を `pairs_omitted[turn_id]` に入れ、そのあとの往復は入れない。最初の行だけで上限を超える往復は `pairs` に入れず、`pairs_omitted[turn_id]` にその往復の全行数を入れる（描画では `### turn_id: <turn_id>` と省略の行だけを出す）。v1.3.x は上位すべての往復の全兄弟行を入れていたため、差し込みが 1 ターンで 2 万字を超えることがあった。

   **実装上の重要規定**: `hybrid_search` は `{"results", "pairs"}` を返すが、**`results` から pair 所属の record を除外してはならない**。`results` は score-ranked の全 hits を含む（pair 所属でも残す）。`pairs` はそれとは独立に、各 turn_id ごとの全 siblings を含む補助情報として返す。重複は許容（renderer 側が Top matches を先頭、pairs を後に配置することで Claude の読み順を制御するため、重複しても害はない）。
5. **レンダリング（v1.2.0+、v1.4.0 で日時と省略の行を追加）**: 検索結果の markdown（`render_memory_context`）は **`## Top matches (use these to answer the question)` ブロックを先頭** に置く。Top matches はスコア順の高スコア record を含み、**Q-A pair に含まれているレコードも除外せずそのまま含める**（pair 所属で result から消さない）。Q-A pairs は **`## Previous matching Q-A exchanges (supplementary context)`** ブロックとして Top matches **の後** に配置する補助情報。Top matches と Q-A pairs に同じ record が重複出現することを許容する（無害 — Claude は Top matches を先に読んで使うため）。

   出力の形（v1.4.0）:

   ```markdown
   # Recalled Memory Context
   Search query: <query>

   > ⚠️ N3MC notice: <知らせがあれば、1 日 1 回>

   ## Top matches (use these to answer the question)

   _If several versions of the same content appear, the one with the newest `saved` time is the current one._

   ## [1] score=0.8123 saved=2026-11-13 10:34 (UTC+0900) claude-code
   <content>

   ## Previous matching Q-A exchanges (supplementary context)

   ### turn_id: <turn_id> saved=2026-11-13 10:34 (UTC+0900)
   <兄弟行の content>
   _(N more chunk(s) of this exchange omitted. Full text: `python -m n3memorycore.n3memory --show-turn <turn_id>`)_
   ```

   - `saved=` は DB の UTC の値を**ローカル時刻**に直し、`(UTC±hhmm)` を付ける（`_fmt_saved`）。往復の `saved=` はその往復の最初の行の時刻。
   - Top matches の先頭に「同じ内容の版が複数あれば saved が新しいほうが今の版」という一文を入れる。設定を作り直すと古い版と新しい版の両方が出るため、どちらが今の版か Claude が見分けられるようにする。
   - 省略の行は `pairs_omitted` にある往復の最後に出す。
6. **スキーマ**: `memories.turn_id TEXT` 列 + `idx_memories_turn_id` インデックス。`insert_memory(..., turn_id=None)` はキーワード専用引数。取得には `get_memories_by_turn_id(conn, turn_id)` を使用します。

> **補足: レンダリング順序の設計背景**
> 旧仕様は逆順（Q-A pairs を先頭、pair 化 record を results から排他）だった。これは特定の話題の会話が頻出する DB（例：プロジェクト管理に関する会話を毎日繰り返すユーザー）で、その turn_id 内の chunks が score-ranked top を独占し、pair 抽出によって `## Top matches` から data record が消える致命的失敗パターンを引き起こした。Claude が context window 先頭で「無関係な履歴」を見て「答えがない」と誤判断し、Pro の data record が DB に存在するのに「見つかりません」と回答する根本原因となっていた。新仕様で完全に解消。

### embed_query 失敗時の警告出力

`embed_query` が例外を投げた場合（モデル未ロード・GPU 障害等）、ベクトル候補ゼロのまま BM25 のみで検索を続行する。**ただしこのフォールバックは精度が著しく低下するため、stderr に `Warning: vector search degraded (embed_query failed: <reason>)` を 1 回出力する**。サイレントに精度劣化させない。

### Top-K のチャンク重複対策

1 つの長い Claude 応答が複数チャンクに分割されると、ハイブリッド検索で同一 `turn_id` の兄弟チャンクが Top-K を埋め尽くす病的ケースが発生する。これを防ぐため、`hybrid_search` の結果整形時に **同一 `turn_id` あたり最大 2 チャンクまで** とし、3 件目以降は Top-K リストから除外する（除外されたチャンクは `pairs`（Q-A 兄弟）セクションには出力される）。`turn_id` が NULL のレコード（手動 `--buffer` 等）はこの cap の対象外。

### 検索結果の絞り込みと並べ替え（v1.4.0。v1.5.0 から下限は `filter_score` で判定）

`hybrid_search` はスコアを計算したあと、次の順で結果を整える。v1.3.x は `min_score`（0.2）だけで、無関係な記録も cos 類似度 0.80〜0.85 で候補に残り、差し込みが長く・関係の薄いものになっていた。

1. **ベクトル候補の下限**: cos_sim が `vector_min_cos`（0.85）未満のベクトル候補は捨てる（FTS にもヒットしていれば、`cos_sim = 0.0` の FTS のみヒットとして残る）。
2. **スコア計算と `min_score`**: `score` と `filter_score` を計算する（§3「バイアス係数」）。**`filter_score`** が `min_score` 未満の結果を捨てる。
3. **同じ内容の版の並べ替え（`_order_versions_newest_first`、`version_order_enabled` が true のとき）**: 設定を作り直すと、古い版と新しい版の両方が出て、古い版が上に来ることがある。Q-A の差し込みは上から `pair_max_chars` まで入れるので、古い版しか入らないおそれがある。そこで:
   - 結果の各 turn_id について、その往復の「最初の `[claude` の行で、content が `version_min_chars`（100）字以上のもの」の埋め込み（`memories_vec` に保存済みのもの）を代表とする。代表のない往復・turn_id のない結果は対象外。
   - **別の turn_id どうし**で代表の cos 類似度が `version_sim_threshold`（0.98）以上なら同じ内容の版とみなし、グループにする（推移的に結ぶ）。
   - 各グループの中で、往復を新しい順（その往復の最初の行の `(timestamp, id)` の降順。`id` は UUIDv7 なので同じ秒でも順が決まる）に並べ直す。**結果の位置とその位置のスコアはそのまま残し**、その位置に入る行だけを入れ替える（新しい版がグループの最高スコアを受け継ぐ）。同じ往復の行どうしの順は変えない。
   - 並べ替えた行には `_version_group` の印を付ける。**記録は消さない。**
   - 0.98 の根拠（multilingual-e5-base で測定）: 同じ設定の新旧 1.0、形が同じで中身の違う設定 0.88〜0.91、1 つの返答の中の違う項目は最大 0.95。**質問の文だけでは比べない**（「X の設定を作成して」と「Y の設定を作成して」は 0.985 になるため）。
   - 限界: 書き出しだけ同じ往復も版とみなされうる。全文を書き直さない新版（代表の分割が変わらない）は検出しない。
4. **相対の足切り**: **`filter_score`** が、最も高い `filter_score` の `relative_min_score_ratio`（0.6）倍未満の結果を捨てる。ただし `_version_group` の印の付いた行は落とさない（古い版も残す）。手順 3 は位置の間で行を動かし、各位置は自分の `score` を保つ。`filter_score` も同じように位置とともに動く。
5. **完全に同じ content の集約（`_collapse_identical_content`）**: content が完全に同じ結果は、いちばん上の位置に 1 件だけ残し、その中身（id・timestamp・turn_id）は**新しい記録**のものにする。
6. **turn_id ごとの 2 件の上限**（上記「Top-K のチャンク重複対策」）と `search_result_limit`。
7. **Q-A の往復の選択**（`pair_top_n` / `pair_max_chars`、上記「Q-A ペアリング契約」）。

---

## 6. 自律評価（[N3MC v1.5.0 Evidence Report]）
実装完了後、以下のテストを自律解決し、満点（⭐⭐⭐⭐⭐）で報告せよ。

> **実装完了の定義**: 以下のテスト 1〜7 を自律実施 → `n3mc --init` 実行（「次のセッションから保存できます。」が表示される）→ 満点（⭐⭐⭐⭐⭐）で報告するまでが「実装完了」。テスト 8〜10 はセッション中の実動作（`--search` 実行・フック発火・会話ターン）で自動的に確認される。3ステップすべてが完了して初めて実装完了とみなす。

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

3. **実在人物テスト（実在歴史データ）**: 実在の歴史人物に関するテキストを保存後、その人物名で `--search` を実行し、**検索結果の上位3件以内** に該当レコードが含まれることを合格基準とする。加えて、`--search` の結果が **stdout に出力されていること**（ターミナルに検索結果が表示されること）を確認せよ。stdout が空の場合は不合格とする。
   - 日本語版の例: 「坂本龍馬」
   - 英語版の例: 「Abraham Lincoln」

4. **架空設定テスト（創造的架空設定）**: 架空の設定（例: キャラクター名・世界観・固有名詞）を含むテキストを保存後、`--search` で取得し、**保存したテキストの全フィールドが一字一句変化なく復元できる**ことを合格基準とする。架空設定テキスト（非コード）は完全記録契約により一字一句保持される。Claude 応答中のコードブロックは文書設計に従い `[code omitted]` に置換される。
   - 日本語版の例: 「ソラニア（架空の浮遊都市）」
   - 英語版の例: 任意の架空のキャラクター・場所・固有名詞

5. **FTS 句読点耐性テスト**: 括弧・句読点を含むテキスト（例: `架空の惑星「アルファ9」の気温設定`）を `--buffer` で保存後、括弧を含まないクエリ（例: `アルファ9の気温`）で `--search` を実行し、**上位3件以内にヒット**することを合格基準とする。FTS5 trigram のインデックスに句読点除去が適用されていること、およびクエリ側でも同じ除去が行われていることの両方を検証するテストである。
   - 加えて、記号を含むクエリ（例: `C:\Users\test #tag = @a * $b % AND OR NOT "q"`）で `--search` を実行し、**エラーにならず**（stdout に `memory search unavailable` が出ない）結果が返ることを確認せよ（§3「語の引用符囲みは必須」）。

6. **--repair FTS マイグレーションテスト**: `~/.n3mc/.memory/fts_punct_cleaned` マーカーファイルを削除した状態で `n3mc --repair` を実行し、以下を確認せよ:
   - FTS クリーニングが実行されること（句読点付きレコードがあれば件数が報告される）
   - `~/.n3mc/.memory/fts_punct_cleaned` マーカーファイルが生成されること
   - 再度 `n3mc --repair` を実行しても FTS クリーニングがスキップされること（マーカーにより全件スキャンが回避される）

7. **フック統合テスト**: Claude Code のセッション内で以下を確認せよ。
   1. **グローバルフック登録の存在確認**（必須ゲート — §4 ①「必須・自動実行ステップ」参照）。以下を実行:
      ```bash
      python -c "import json,os; s=json.load(open(os.path.expanduser('~/.claude/settings.json'))); h=s.get('hooks',{}); assert any('n3memorycore.n3mc_hook' in c.get('command','').lower() or 'n3mc-hook' in c.get('command','').lower() for e in h.get('UserPromptSubmit',[]) for c in e.get('hooks',[])), 'UserPromptSubmit hook missing'; assert any('n3memorycore.n3mc_stop_hook' in c.get('command','').lower() or 'n3mc-stop-hook' in c.get('command','').lower() for e in h.get('Stop',[]) for c in e.get('hooks',[])), 'Stop hook missing'; assert 'Bash(python -m n3memorycore.n3memory --search *)' in s.get('permissions',{}).get('allow',[]), 'permission rules missing'; print('OK')"
      ```
      合格基準: `OK` が出力されること（この確認はこのパッケージのエントリポイントかモジュールを探すので、ほかの版の `n3mc_hook.py` では満たされない。また §4 ② のパーミッションのルールも確かめる）。失敗した場合は**実装が完了していない** — `n3mc --init`（経路 A）または §4 ① の冪等インストーラ（経路 B）を再実行してから次のサブテストへ進むこと。このゲートを飛ばすと、プロジェクトディレクトリ内で実行する次の 2 サブテストは（ローカルフックや手動実行のおかげで）通ってしまうが、ユーザーが他のディレクトリで開くセッションでは静かに発火しない — まさにこのゲートが防ぎたい「セッション間 RAG が機能しない」事故が温存される。
   2. **UserPromptSubmit**: **n3mc-free プロジェクトディレクトリの外**（例: `~`）で起動した Claude Code セッションからユーザー発言を送り、`# Recalled Memory Context` がそのターンのコンテキストに入っていること（フックの stdout）。直前の Claude 回答（Stop が保存しなかったときはトランスクリプトから読む。§4.5）とユーザー発言の両方が DB に保存されていること（`n3mc --list` で `[claude]`・`[user]` プレフィックス付きレコードを確認）。「プロジェクト外で実行する」ことが必須 — プロジェクト内で実行するとグローバル登録の欠落を見逃す。
   3. **Stop**: 同じプロジェクト外セッションを終了した後、Claude の最終回答が DB に保存されていること。プロジェクトの `.claude/rules/n3mc-behavior.md` の 1 行目が `<!-- n3mc-behavior v2 -->` であること。プロジェクトの `.claude/CLAUDE.md` に `memory_context.md` の `@import` 行が**ない**こと。

8. **stdout 出力テスト（memory_context.md を作らない）**: `--search` を実行し、結果が **stdout に出力される**こと、および `~/.n3mc/.memory/memory_context.md` が**作られない**ことを確認せよ。

9. **完全記録テスト（旧「ノイズフィルタテスト」の置き換え）**: 以下のフィルタが**廃止**されていること、および空でない入力がすべて記録されることを確認せよ。
    - 2文字の文字列を `[claude]` プレフィックスで `--hook-submit` に渡し、**レコードが保存される**こと（旧「3文字未満フィルタ」は存在しない）。
    - 5文字の文字列を `[user]` プレフィックスで `--hook-submit` に渡し、**レコードが保存される**こと（旧「10文字未満フィルタ」は存在しない）。
    - 定型語（例: `ok`、`yes`、`thanks`）を `[user]` として渡し、**レコードが保存される**こと（旧 `_SKIP_PATTERNS` フィルタは存在しない）。
    - 各フック呼び出しに対し `audit.log` のエントリが、バッファ書き込みの成否と独立して必ず1件書かれることを確認せよ。
    - 形のそろった長い返答（例: 12 項目・約 2,000 字の設定。400 字ずつ分割される）を 1 つの往復として保存し、**分割がすべて DB に残る**こと。続けてほぼ同じ文の設定（一部の値だけ変えたもの）を別の往復として保存し、**1 つ目の分割が 1 件も消えず、2 つ目の分割もすべて残る**こと（類似度による重複排除の廃止）。

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

テストはリポジトリルートの `tests/` 配下に置く（パッケージ内には**置かない**）。インポートはインストール済みの `n3memorycore` パッケージ経由で解決する（`sys.path` ハックは禁止）。

```
n3mc-free/
└── tests/
    ├── conftest.py          # 共通フィクスチャ：隔離DB、設定、ダミーベクトル
    ├── test_database.py     # Layer 1: DB単体テスト（CRUD、スキーマ、トランザクション）
    ├── test_processor.py    # Layer 2: ランキング数学、purify `[code omitted]` 置換検証、Refresh
    ├── test_api.py          # Layer 3: FastAPIエンドポイントテスト（TestClient）
    ├── test_hooks.py        # Layer 4: フック統合（完全保存チャンク化、画像除去、監査ログ）
    ├── test_hooks_encoding.py # サロゲート除去・UTF-8 の回帰テスト
    └── test_regressions.py  # Layer 5: v1.4.0 / v1.5.0 の修正の回帰テスト（モデル不要）
```

**本物のデータを守る（必須）**: `conftest.py` に autouse の fixture を置き、`n3memory` モジュールの `HOME_DIR`・`MEMORY_DIR`・`DB_PATH`・`PID_FILE`・`AUDIT_LOG`・`NOTICES_FILE`・`CONFIG_FILE`・`TURN_ID_FILE`・`SERVER_LOG` とサーバー起動のマーカー — **およびフックのモジュール**（`n3mc_hook`・`n3mc_stop_hook`）**の `AUDIT_LOG`** — を一時フォルダに向け、`--init` が使うユーザーの `~/.claude/settings.json` を一時ファイルに向け（例: テストで `HOME` / `USERPROFILE` を設定する）、`_request_shutdown` と HTTP の送信（`_post`・`_get`）を本物のサーバーに届かない偽物に差し替えること。テストが本物の `~/.n3mc` の `notices.json`・`audit.log` に書いたり、本物のサーバー（18520）に `/shutdown` を送ったりしてはならない。

標準的なフィクスチャインポート:

```python
# tests/conftest.py
from n3memorycore.core.database import get_connection, init_db
from n3memorycore.core.processor import get_model
```

テスト内で `sys.path.insert(...)` を使用してはならない。`pip install -e ".[test]"` 後、リポジトリルートから:

```bash
python -m pytest tests/ -v
```

### Layer 1: `test_database.py`（27テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestSchema` | `tables_created`, `migrate_idempotent`, `add_missing_columns`, `fts_tokenizer_is_trigram`, `migrate_tokenizer_to_trigram` | スキーマ作成とマイグレーション（`session_id` / `turn_id` を含む） |
| `TestInsertAndRetrieve` | `insert_count`, `three_table_consistency`, `insert_without_embedding`, `rowid_returned` | CRUD と 3 テーブルの整合性 |
| `TestDelete` | `delete_three_tables`, `delete_nonexistent` | トランザクション保護された削除 |
| `TestDedup` | `exact_duplicate_detected`, `no_false_positive` | 完全一致の重複排除 |
| `TestUnindexed` | `vec_missing_detected`, `all_indexed` | 修復検出 |
| `TestFTS` | `punctuation_removal`, `short_query_skipped`, `basic_search`, `punctuation_tolerance`, `max_terms_limit` | FTS5 |
| `TestVectorSearch` | `vector_search_results`, `empty_db_vector_search` | KNN検索 |
| `TestRetentionAndRange` | `old_records_retained`, `no_gc_decrements_count`, `since_filter_excludes_old`, `until_filter_excludes_new` | Free は古い記録を消さない。時間範囲フィルタ（v1.5.0 より前の名前は `TestGC`） |
| `TestSerialization` | `roundtrip` | バイナリ変換 |

### Layer 2: `test_processor.py`（24テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestCosineSim` | `identical_vectors`, `orthogonal_vectors`, `negative_clamped`, `midpoint` | L2→cosine変換 |
| `TestTimeDecay` | `current_time_is_one`, `half_life`, `floor_value`, `bad_timestamp_returns_one` | 半減期計算 |
| `TestKeywordRelevance` | `below_threshold_is_zero`, `full_relevance`, `partial_relevance`, `zero_max` | BM25正規化 |
| `TestPurification` | `code_block_replaced`, `inline_code_preserved`, `multiple_blocks_replaced`, `no_code_block_unchanged` | 複数行コードブロック → `[code omitted]`、インラインコードは保持 |
| `TestEmbedding` | `passage_embedding_shape`, `query_embedding_shape`, `same_text_similar` | Embeddingモデル |
| `TestRefresh` | `exact_duplicate_not_inserted`, `below_threshold_not_duplicate` | 重複の扱い（完全一致のみ） |
| `TestBiasScoring` | `session_match`, `session_mismatch_lower`, `full_score_formula` | ランキング式（`b_session` のみ。Free に `b_local` はない） |

### Layer 3: `test_api.py`（21テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestHealth` | `health_ok` | サーバー状態 |
| `TestBuffer` | `save_content`, `empty_content_skipped`, `agent_name_saved`, `exact_dedup`, `code_block_purified` | 保存API |
| `TestSearch` | `empty_db_search`, `save_then_search`, `empty_query`, `score_returned` | 検索API |
| `TestRepair` | `repair_runs` | 修復API |
| `TestList` | `list_returns_records`, `list_after_save` | 一覧API |
| `TestDelete` | `delete_returns_403`, `delete_nonexistent_also_403`, `delete_detail_is_pro` | Pro 専用エンドポイント → 403 |
| `TestGC` | `gc_returns_403`, `gc_detail_is_pro` | Pro 専用エンドポイント → 403 |
| `TestImport` | `import_returns_403`, `import_with_data_returns_403`, `import_detail_is_pro` | Pro 専用エンドポイント → 403 |

### Layer 4: `test_hooks.py`（20テスト）と `test_hooks_encoding.py`（10テスト）

| テストクラス | テスト内容 | カバレッジ |
|---|---|---|
| `TestChunkText` | `short_text_single_record`, `long_text_multiple_chunks`, `prefixes_numbered`, `single_chunk_no_number`, `paragraph_split`, `hard_window_fallback` | 完全保存チャンク化 |
| `TestStripImages` | `plain_text_unchanged`, `base64_image_stripped`, `image_only_payload_returns_empty` | 画像ペイロード除去 |
| `TestExtractText` | `plain_string`, `multimodal_json_list`, `image_only_empty`, `empty_input` | マルチモーダル抽出 |
| `TestCompleteRecording` | `short_claude_response_saved`, `short_user_message_saved`, `skip_pattern_abolished`, `code_block_replaced_in_purify`, `inline_code_preserved_in_purify` | 完全記録契約（フィルタ廃止） |
| `TestStopIdempotency` | `no_import_added`, `rules_file_created` | --stop冪等性 |
| `TestSanitizerUnit` | `strips_lone_high_surrogate`, `strips_lone_low_surrogate`, `clean_japanese_unchanged`, `complete_emoji_pair_preserved`, `recursive_dict`, `recursive_list`, `none_passthrough` | サロゲート除去（`test_hooks_encoding.py`） |
| `TestSanitizedAcceptedBySQLite` | `sanitized_text_inserts_without_unicode_error` | 〃 |
| `TestPurifyAppliesSanitization` | `purify_strips_surrogates_alongside_code_blocks`, `purify_handles_empty` | 〃 |

> 上の件数は v1.4.0 のテスト一式のもの。v1.5.0 はテストを Layer 5 にだけ足すので、これらの表はそのまま有効。再生成したテスト一式と違う場合は、件数ではなく、ここにあるクラス名とテスト名に合わせること。

### Layer 5: `test_regressions.py`（v1.4.0 / v1.5.0）

埋め込みは `zlib.crc32` から作る決定論的な偽のベクトル（`fake_embed`）に差し替え、モデルなしで数秒で終わること。

| テストクラス | テスト内容 |
|---|---|
| `TestFtsQuoting` | `\ / # = @ * $ %`・`AND/OR/NOT`・`"`・制御文字を含むクエリで例外にならない、記号を含む語がヒットする、`search_fts` が例外を出しても `hybrid_search` が続く。**v1.5.0**: 語を `OR` でつなぐ（文のようなクエリが、その語の一部だけを含む記録にヒットする）、3 文字未満の語（`AI`、`東京`）は捨て、そういう語だけのクエリは FTS を飛ばす、クエリの句読点で語が分かれる（`「アルファ9」の気温は？` が `架空の惑星「アルファ9」の気温設定` にヒットする） |
| `TestVectorFilter` | `since` 付きの検索で範囲内の記録が KNN の上位外にあっても返る、`rowid IN` が使えないときに全件計算に切り替わる |
| `TestNoSimilarityDedup` | 似た分割（cos ≥ 0.95）がすべて保存される、同じ `turn_id` の完全一致は飛ばす、別の `turn_id` の完全一致は保存する、`turn_id` なしは全体で完全一致を飛ばす |
| `TestSearchShaping` | `vector_min_cos` 未満の候補を捨てる、相対の足切り、`_version_group` は足切りで落ちない、新しい版が上に来る、同じ content の集約、`pair_top_n`・`pair_max_chars` と `pairs_omitted`。**v1.5.0**: 類似度の低い現在のセッションの記録があっても、類似度の高い 30 日前の別セッションの記録が相対の足切りで残る、別セッションのキーワードのみのヒットが `min_score` で残る、並びは今も最終スコア順（現在のセッションが先） |
| `TestRender` | `saved=` がローカル時刻、版の一文、省略の行に `--show-turn`、知らせが先頭に出る |
| `TestNoContextFile` | `--search` が `memory_context.md` を作らない、`--stop` が CLAUDE.md からこのインストールの `CONTEXT_FILE` の `@import` を取り除く（控えあり・CRLF のまま・空白を含むパス・スラッシュでもバックスラッシュでも）、該当行がなければ触れない、旧ファイルを消してマーカーを作る。**v1.5.0**: 相対の `@../.memory/memory_context.md`、ほかのフォルダの `memory_context.md` を指す `@` の行、`@docs/notes_about_memory_context.md` は残す |
| `TestBehaviorMd` | 目印が同じなら触れない、過去のひな形なら控えを取って置き換える、手で編集したものは置き換えず知らせを出す。**v1.5.0**: 知らせのキーにパスのハッシュが入り、メッセージにパスが入る、手で編集したファイルが 2 つあれば知らせも 2 つ、ファイルの 1 行目が今の目印になったら知らせが消える |
| `TestServerDecision` | 正常／`code_version` 違い／別のフォルダ／`root` なしで確かめられない、`/shutdown` を先に使う、`root` が違えば 403、死んだ PID ファイルは待たない |
| `TestServerStart`（v1.5.0） | 待っている間に終了した子プロセスでは待つのをすぐやめ、`server.log` を示す `server_start_failed` を出す、失敗から 5 分以内は新たに起動しない、あとで起動に成功したら知らせとマーカーが消える、60 秒たってもまだ起動中の PID ファイルのプロセスは入れ替えも重複起動もしない、`Popen` にプラットフォームに応じた切り離しのフラグが渡り stderr が `server.log` に行く（プロセスの作成は偽物に差し替え） |
| `TestServerApi`（v1.5.0） | `Host: evil.example` のリクエストはどのエンドポイントでも 403、`127.0.0.1:<port>` と `localhost:<port>` は通る、`/buffer`・`/search`・`/repair`・`/list` は普通の `def` 関数、`{"max_records": 200}` 付きの `/repair` は最大 200 件しか処理せず、`--hook-submit` はその上限を送る |
| `TestForeignServer` | 別のフォルダのサーバーのとき、保存は自分の DB に直接、検索はしない、`--repair` は何もしない |
| `TestConfig` | 読めない `config.json` を上書きしない、型の違う項目だけ無視、直したら知らせが消える、`config.json` の中身が変わったときだけ `code_version` が変わる。**v1.5.0**: `search_query_max_chars` の `2000.0`、`half_life_days` の `0`、`server_port` の `70000` / `-1`、`min_score` の `1.5` は `config_type` 付きで無視される、ないファイルは排他作成で作り、同時に作った別のプロセスのファイルは上書きせず読む（`owner_id` は 1 つ）、更新は一時ファイルと `os.replace` を通す |
| `TestHookSubmit` | `<task-notification>` で始まる入力は保存も検索もせず `turn_id` を変えない、Stop で `turn_id` を消さない。**v1.5.0**: 前の Claude の回答をサンプルの `transcript_path` の JSONL から読み（最後のテキストブロックだけ。`tool_result` だけの user の行、`isMeta` の行、`[Request interrupted by user]` の印、スラッシュコマンドの行、`thinking`、`tool_use`、`isSidechain` の行は飛ばす。今の質問も飛ばす。割り込まれた返答も見つかる）、前の `turn_id` で保存する、Stop がすでに保存していれば何も重複しない、ないか壊れたトランスクリプトは `transcript_unreadable` として記録され、フックはそれでも検索して質問を保存する、画像だけの質問は新しい `turn_id` を書く |
| `TestPurifyBeforeChunk`（v1.5.0） | 2 つのコードブロックとその間の文章を持つ 500 字を超えるユーザー発言で、文章は残り、どちらのコードブロックも保存されない、400 字より長いコードブロックは保存されず置き換えられる |
| `TestChunkSentences`（v1.5.0） | 空行のない日本語の文章が、文の途中ではなく `。` / `！` / `？` のあとで分かれる |
| `TestTimeBounds`（v1.5.0） | 日付だけの `--since` はローカルの 0 時を UTC に直したもの（UTC 以外の固定のタイムゾーンで）、オフセット付きの値はそのオフセットを守る、`T` と空白の区切りで同じ境界になる、`yesterday` はエラーの行を出して終了ステータス 2、`since > until` はエラー |
| `TestDbCorrupt`（v1.5.0） | 2 回の破損で 2 つのバックアップ（`n3memory.db.corrupt-<time>.bak`）ができ、`-wal` / `-shm` は削除されずに隣へ移される、知らせ `db_corrupt` が出る、`quick_check` は 1 プロセスに 1 回 |
| `TestInit`（v1.5.0、一時的な `settings.json` だけ） | このパッケージの外の Pro 形式のフック `python ".../N3MemoryCore/n3mc_hook.py"` は残して知らせる、自分のエントリポイント（`.exe` 付きでもなしでも）と `-m` のエントリは重複せず置き換える、再実行では何も書かない、変える前に控えを取る、不正な `settings.json` は変えずに終了ステータス 1、§4 ② のルールを既存のルールを取り除かずに `permissions.allow` に足す |
| `TestHousekeeping`（v1.5.0） | `--stop` が 30 日より古い `turn_id_*.txt` を消し、新しいものは残す、50 MB を超えた `audit.log` を `audit.log.1` にローテーションする（テストではサイズのしきい値を差し替える） |
| `TestModelRetry`（v1.5.0） | モデルの読み込みに失敗したあと、`get_model()` は 60 秒間再試行しない（読み込み関数は偽物に差し替え）、`$N3MC_EMBED_MODEL` は効かない |
| `TestPackaging`（v1.5.0） | `psutil` が `pyproject.toml` の `dependencies` に入っている |

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

> 現在のデフォルト構成（768次元 multilingual-e5-base モデル、embedding 1回/プロンプト）では CPU で十分高速に動作するため、GPU は不要です。
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
この指示書に従って N3MemoryCore を実装してください。
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