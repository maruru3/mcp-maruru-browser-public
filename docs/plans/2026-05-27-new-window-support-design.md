# 2026-05-27 maruru-browser 新規ウインドウ対応 設計

## 背景

`window.open()` で別タブを開くサイトや、OAuth 認証で別ウインドウが立ち上がる
サイトを操作する際、現状の maruru-browser には以下の不便がある:

- 新規タブ/ポップアップは `context.on("page", ...)` で自動検知され listener は
  張られるが、`_page`（アクティブタブ）は元のままなので、`browser_tabs list →
  switch` と手動 2-3 コールしないと新タブを操作できない
- OAuth ポップアップが**閉じた**タイミングを待つ手段がない
- ポップアップ閉鎖後の元タブの URL 遷移を待つ統合 API がない

## ゴール

以下のケースを、最小コール数で扱えるようにする:

1. **window.open() 系の新規タブ**: クリック → 新タブ → そのまま操作続行
2. **OAuth ポップアップ**: クリック → ポップアップ → 認証完了で閉鎖 → 元タブ遷移完了

## 設計

### 1. `follow_new_tab` オプション（ミニマル拡張）

`browser_click` / `browser_press_key` / `browser_type`（submit=true 時）に
以下のオプションを追加する。

```json
{
  "follow_new_tab": {
    "type": "boolean",
    "default": false,
    "description": "クリック/キー押下で新タブが開く場合、自動で _page を新タブに切替"
  },
  "new_tab_timeout": {
    "type": "integer",
    "default": 5000,
    "description": "新タブ出現待ち timeout(ms)"
  }
}
```

実装パターン:

```python
follow = args.get("follow_new_tab", False)
if follow:
    ctx = page.context
    async with ctx.expect_page(timeout=args.get("new_tab_timeout", 5000)) as popup_info:
        await _do_click(page, selector)
    new_page = await popup_info.value
    await new_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
    global _page
    _page = new_page
    await _page.bring_to_front()
    new_idx = ctx.pages.index(new_page)
    return [...f"Clicked '{selector}', switched to new tab [{new_idx}]: {new_page.url}"]
```

**timeout 時の振る舞い**: TimeoutError をそのまま投げて呼出側に明確に伝える
（厳密モード）。クリック対象が新タブを開かない要素だったケースを誤魔化さない。

### 2. `browser_tabs` 拡張: `latest` / `wait_close`

#### `action="latest"` — 最新タブにフォーカス切替

`follow_new_tab` を付け忘れた後、後追いで最新タブに切り替えるための退路。

```python
elif action == "latest":
    if not pages:
        raise ValueError("No tabs open")
    _page = pages[-1]
    await _page.bring_to_front()
    messages.append(f"Switched to latest tab [{len(pages)-1}]: {_page.url}")
```

#### `action="wait_close"` — 指定タブが閉じるまで待機

OAuth ポップアップを手動 or 自動で閉じた瞬間を検出する。

```python
elif action == "wait_close":
    timeout_ms = args.get("timeout", 60000)
    target_idx = index if index >= 0 else len(pages) - 1
    if target_idx < 0 or target_idx >= len(pages):
        raise ValueError(f"Invalid tab index: {target_idx}")
    target_page = pages[target_idx]

    closed_event = asyncio.Event()
    target_page.once("close", lambda _: closed_event.set())
    if target_page.is_closed():
        closed_event.set()
    try:
        await asyncio.wait_for(closed_event.wait(), timeout=timeout_ms / 1000)
    except asyncio.TimeoutError as e:
        raise RuntimeError(f"Tab [{target_idx}] did not close within {timeout_ms}ms") from e

    pages = ctx.pages
    if pages:
        _page = pages[-1]
        await _page.bring_to_front()
    messages.append(f"Tab [{target_idx}] closed. Active tab: [{len(pages)-1}]" if pages else "All tabs closed")
```

`index` 省略時のデフォルトは**末尾タブ**（ポップアップは通常末尾に入るため）。

### 3. `browser_popup_flow` — OAuth 専用高レベルヘルパー

「クリック → ポップアップ → 認証完了で閉鎖 → 元タブが遷移」を 1 コールで完結。

スキーマ:

```json
{
  "name": "browser_popup_flow",
  "inputSchema": {
    "properties": {
      "trigger_selector": {"type": "string"},
      "expected_url_substring": {"type": "string", "default": ""},
      "popup_timeout": {"type": "integer", "default": 10000},
      "close_timeout": {"type": "integer", "default": 120000},
      "redirect_timeout": {"type": "integer", "default": 30000}
    },
    "required": ["trigger_selector"]
  }
}
```

ロジック:

1. `ctx.expect_page(timeout=popup_timeout)` でクリック+ポップアップ追従
2. ポップアップの `domcontentloaded` を待つ
3. `popup.once("close", ...)` + `asyncio.wait_for(close_timeout)` で閉鎖待ち
4. 元タブを `bring_to_front`
5. `expected_url_substring` 指定があれば `original_page.wait_for_url(...)` で遷移待ち

**TOOL_CALL_TIMEOUT (300s) との関係**: デフォルト合計 10+120+30=160s。
ユーザーが timeout を伸ばす場合は外側 300s で打ち切られることを description に明記。

## v0.8.0 機能との関係

v0.8.0 で導入された `_tool_lock` (asyncio.Lock) と `_dispatch_tool` 分離により:

- 新ハンドラは `_dispatch_tool` の elif に追加するだけでロック直列化＋
  TOOL_CALL_TIMEOUT＋タブ通知が自動適用される
- popup_flow は 1 ツール呼び出しの中で完結するため、`_tool_lock` を長時間
  保持する点に注意（OAuth 中に他のセッションがブロックされる）
- v0.8.0 のタブ通知 (`⚠️ タブ数が N → M に変化しました`) は follow_new_tab
  使用時にも自然に発火して挙動が見える

## 後方互換

- 全オプションはデフォルト false / 既存 action はそのまま
- 既存呼び出しは挙動変化なし

## バージョン

server_version: 0.8.0 → 0.9.0

## 実装順

1. `browser_click` に follow_new_tab 追加
2. `browser_press_key` に follow_new_tab 追加
3. `browser_type` (submit=true) に follow_new_tab 追加
4. `browser_tabs` に latest / wait_close action 追加
5. `browser_popup_flow` 新規追加
6. server_version 更新, CHANGELOG.md 追記
7. `python -c "import ast; ast.parse(...)"` で構文チェック
