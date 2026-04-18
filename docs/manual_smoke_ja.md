# N3MemoryCore 日本語 / エンコーディング手動スモーク手順

## 目的

Windows (cp932) 環境で発生した 2 系統の無音データ破壊バグ — (1) lone Unicode
surrogate による SQLite クラッシュ、(2) cp932 誤デコードによる mojibake — が
再発していないことを 5 分以内に確認する手順書です。完全保存 (complete
preservation) 契約の最低保証ラインです。

## いつ実行するか

- N3MemoryCore 配下のコード (`n3memory.py`, `n3mc_hook.py`, `n3mc_stop_hook.py`,
  `core/processor.py`, `core/database.py`) を編集したあと
- デプロイ前 / コミット前
- セッション開始時の異常を疑ったとき
- Windows バージョン更新 / Python バージョン更新後

## 自動部分: `scripts/smoke_ja.py`

```powershell
# フル (10-30 秒)
python scripts/smoke_ja.py

# クイック (<3 秒) — /health と /buffer ラウンドトリップのみ
python scripts/smoke_ja.py --quick
```

フルモードは以下を順に検証します:

| # | 検証内容 | 失敗した場合の意味 |
|---|---|---|
| 1 | `/health` が 200 を返す | サーバが起動していない or ポート衝突 |
| 2 | 日本語を `/buffer` → `/search` ラウンドトリップで復元できる | FTS / 埋め込み / sanitize のいずれかが壊れた |
| 3 | lone surrogate 混入 JSON を Stop hook に投げても Unicode 例外なく exit 0 | surrogate sanitize 経路が壊れた (昨日の真因クラス) |
| 4 | `tests/test_hooks_encoding.py` 全テスト緑 | 回帰テスト網が壊れた |

すべて pass すると最終行に `smoke_ja PASS: N/N in XXXms` が出ます。

## 手動部分 (目視 1 項目)

自動スモークの後、以下 1 点だけ目視確認します:

5. **`.memory/memory_context.md` を開き、直近数ターンの `[claude ...]` 行に
   mojibake (`縺`, `繧`, `菫`, `繝` 等) や `\udcXX` が含まれていない**こと。

`[recovered]` プレフィックス付きの古い行は過去の破損データを部分復元したもの
なので、残っていて正常です。新しく追加される行には `[recovered]` は付きません。

## 失敗時の対応

| 症状 | 最初に見る場所 |
|---|---|
| `/health` 到達不能 | サーバ未起動。`python n3memory.py --start` で起動 |
| `/buffer` が 500 | `.memory/server.log` の末尾。UnicodeError なら sanitizer が抜けた |
| stop hook 失敗 | `n3mc_stop_hook.py` の stdin 読み取りが `sys.stdin.buffer.read()` 経由か確認 |
| pytest 失敗 | 失敗テスト名でコード側の該当 sanitize を確認 |

修正後は必ずスモークを再実行し、緑を確認してから作業を続けること。

## SessionStart 自動実行 (層 4)

`.claude/settings.json` の `hooks.SessionStart` で `smoke_ja.py --quick` が自動
実行されます。セッション開始時にサーバ疎通と encoding 経路を +2〜3 秒で確認
し、失敗すれば警告を surface します。Claude の判断を経由しないため、最も
強固な再発防止層です。
