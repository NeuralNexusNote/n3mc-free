# データフロートレースと問題点の記録(Free v1.9.1 再実装、2026-11-20)

指示書 v1.9.1(JP)から `n3memorycore/` を作り直したときに見つけた問題と、その扱いを残す。

## 1. 経路

`UserPromptSubmit` の入力 → `n3mc-hook`(audit.log を先に書く)→ `--hook-submit`
→ `_extract_text`(画像を除く)→ システム入力なら終了 → `ensure_server`
→ `/repair`(1 バッチ)→ 前の返答をトランスクリプトから保存 → 新しい `turn_id`
→ `/search` → `hybrid_search`(KNN + trigram FTS + バイグラム FTS、足切り、版の並べ替え、弱い候補の補充、Q-A 往復)
→ `render_memory_context` → stdout(Claude のコンテキスト)→ 質問を保存。

保存側: 全文 `purify_text` → `chunk_text(400/40)` → チャンクごとに `/buffer`。失敗すれば `_buffer_direct`(ベクトルなし、`--repair` で補う)。

## 2. 見つかった問題

| # | 場所 | 問題 | 対応 |
|---|---|---|---|
| 1 | `processor.keyword_relevance` | 記録が 1〜2 件の DB で、FTS5 の生スコアが約 `-2e-6` になり、`bm25_min_threshold` で 0 になる。埋め込みが使えないと一致した記録が消え、検索結果が 0 件になる(実測: 1 件 -2e-6、3 件 -1.04) | 記録が 2 件以下のときだけ、最良の一致を 1.0 とする。指示書 §3 に例外を追記。テスト追加 |
| 2 | 指示書 §3(主キー) | `uuid7`(PyPI)のモジュール名が `uuid_extensions` と書かれていない。`import uuid7` は失敗する | 指示書(JP/EN)に追記 |
| 3 | 指示書 §1(自動インストール) | `pip install -e .` がなく、`n3mc` などのコンソールスクリプトができない。`n3mc --init` を実行できない | 指示書(JP/EN)に追記 |
| 4 | 開発機 | 別の版(Pro)のフックとサーバーが残っていると、同じ `~/.n3mc` とポート 18520 を使うので、Free は検索できず直接保存になる | 別の版のフックを外し、そのサーバーを止めた |
| 5 | 開発機 | プロジェクトの `.claude/rules/n3mc-behavior.md` が別の版向けに手で編集されていて、Free は置き換えず通知を出す | 未対応(置き換えるかは利用者の判断) |

## 3. 指示書どおりだが、データが減る点(直していない)

- 同じ `turn_id` の中で完全に同じ文のチャンクは保存されない(§5.4)。同じ段落を繰り返した長文では、繰り返しの分が残らない。
- `search_query_max_chars`(2000)を超える質問は、検索には先頭だけを使う(保存は全文)。
- ひらがなだけの 3 文字の語(`してく` など)は、本文では捨てると書かれているが、擬似コードでは残る。実装は擬似コードに合わせた。
- `Layer 2` の表にある `TestTimeDecay.floor_value` は、指示書に下限の定義がなく、対応するテストを作っていない。

## 4. 確認したこと

- pytest 125 件が合格(フィクスチャは本物の `~/.n3mc` と `settings.json` に触れない)。
- 実モデルで、保存 → 検索(0.44 秒)、記号入りクエリ、強制終了後の復旧、サーバーの入れ替え(`code_version`)、`--init` の冪等性。
- 開発機で Free のフックが動き、`# Recalled Memory Context` が差し込まれることを確認した。

## 5. 指示書と CLI の逐条比較(2026-11-20)

`--init` `--buffer` `--search` `--list` `--recall-thread` `--show-turn` `--stop` `--repair` `--reembed` `--hook-submit` `--save-claude-turn` `--run-server` の 12 コマンドを、指示書の入出力と 1 つずつ照らした。

| 差 | 内容 | 対応 |
|---|---|---|
| `--list` の時間範囲のエラー | 指示書(§3.6)は `--search` と `--list` の両方で `--since: cannot read '<値>' (use ...)` の 1 行と終了ステータス 2 とする。`--list` は別の文面だった | 共通の `_checked_range` にして揃えた。テスト追加 |

そのほかのコマンドは、指示書にあって実装にない動作は見つからなかった。DB がないときの `--list` / `--recall-thread` / `--show-turn` は `No DB found at <path>`、空のクエリは `_No relevant memories found._`、引数なしはヘルプ(終了ステータス 1)になることも確かめた。

## 6. クロスセッションテスト(2026-11-20)

一時フォルダ(`N3MC_HOME`)とポート 18997 で、別々のプロセスとして実行した。

| 手順 | 結果 |
|---|---|
| セッション 1(`N3MC_SESSION_ID=session-1`)で `--buffer` に架空の設定を保存 | 終了ステータス 0 |
| セッション 2(別プロセス、`session-2`)で `--search "ゼフィリアの首都"` | `## Top matches` の 1 位に保存した文が一字一句そのまま出た(score 0.5596) |
| サーバーを止め、セッション 3 のプロセスで `--search "ルミナ市の人口"` | サーバーが起動し直され、同じ記録が 1 位(score 0.1800。別セッションなので `b_session` 0.6 が掛かる。足切りは `time_decay` と `b_session` を見ないので残る) |
| 別の `session_id` のフック(`--hook-submit`)で質問 | 同じ記録が `## [1]` に出た(score 0.5531) |

セッションをまたいで見えることを確かめた。修正は要らなかった。
