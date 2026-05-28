# Changelog

All notable changes to `mcp-maruru-browser` will be documented in this file.

The format is based on [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.9.1] - 2026-05-28

### Added — サーバ初期化時の利用ガイド注入

- `InitializationOptions` に `instructions` を追加。MCP接続したクライアント
  （Claude Code/Desktop, Codex, Gemini など）に対し、起動時に
  「カテゴリ別ツールガイド + 埋没ツール + 落とし穴 + AI連携の使い分け」を
  システム指示として自動配信する。
- これにより、ツール数が40+と多くなった本サーバでも、LLM側が
  `browser_popup_flow` / `wait_for_navigation` / `iframe_evaluate` /
  `record_replay` などの専用ツールの存在を見落とすケースを減らす狙い。
- 既存ツールの動作変更はなし。後方互換。

## [0.9.0] - 2026-05-27

### Added — 新規ウインドウ/ポップアップ対応

- `browser_click` / `browser_press_key` / `browser_type`（submit=true 時）に
  `follow_new_tab` オプションを追加。`true` の場合、Playwright の
  `page.expect_popup()` でクリック/キー押下をラップし、新タブが開いたら
  自動で `_page` を新タブに切り替えて操作続行できるようにする。
  - `new_tab_timeout`（デフォルト 5000ms）で待機時間を制御。
  - 新タブが出なかった場合は TimeoutError（厳密モード）。
  - `browser_type` の場合は typing 中の長いフォールバックを待機に含めず、
    Enter 押下のみをラップする。
- `browser_tabs` に新アクションを追加:
  - `action="latest"`: 末尾タブにフォーカスを切替（`follow_new_tab` の付け忘れ
    リカバリ用）。
  - `action="wait_close"`: 指定タブ（index 省略時は末尾）が閉じるまで待機し、
    閉じた後アクティブタブを末尾に切り替え。`timeout`（デフォルト 60000ms）
    で待機時間を制御。OAuth ポップアップの閉鎖検出に使う。
- `browser_popup_flow` ツールを新規追加。OAuth 認証のような「クリック→
  ポップアップ→閉鎖→元タブ遷移」の 4 ステップを 1 コールで完結。
  - `trigger_selector` でポップアップを開く要素を指定。
  - `expected_url_substring` で元タブの遷移先 URL の部分一致条件を指定。
  - `popup_timeout` / `close_timeout` / `redirect_timeout`（10s/120s/30s）で
    各段階のタイムアウトを制御。`close_timeout` は手動認証想定で長め。
  - 実行中は `_tool_lock` を保持するため他ツールはブロックされる。

### Changed

- `_handle_browser_click` / `_handle_browser_type` 内の locator フォールバック
  ロジックを `_resolve_clickable_locator` / `_resolve_typeable_locator` に
  抽出。`browser_popup_flow` でも同じフォールバックを再利用。
- 新タブ切替の共通処理を `_switch_to_new_page` に抽出（`domcontentloaded`
  待ち→`_page` 更新→`bring_to_front`）。
- Bumped MCP `server_version` from `0.8.0` to `0.9.0`.

## [0.8.0] - 2026-05-16

### Added — 複数セッション対策・ハング防止・タブ通知

- ツール呼び出しを `asyncio.Lock`（`_tool_lock`）で直列化。並行/複数セッション
  からの同時アクセスでも共有ブラウザ状態の競合を防ぐ。
- ロック取得に `TOOL_LOCK_TIMEOUT`（200s）の上限を設定。別処理が長く占有して
  いる場合は無限待ちせず "busy" エラーを返す。
- 1ツール呼び出しごとに `TOOL_CALL_TIMEOUT`（300s）の実行上限。超えたら中断し、
  ハングを防止する。
- ブラウザ起動に `BROWSER_LAUNCH_TIMEOUT`（60s）の上限。別セッションが Chrome
  プロファイルを使用中で起動が長引く場合、明確なエラーにして無限待ちを避ける。
- 各ツール応答末尾にタブ数を付記（`[tabs: N]`）。前回から変化した場合は
  `⚠️ タブ数が N → M に変化しました` と警告し、意図しない新規タブの増殖に
  即座に気づけるようにした。

### Changed

- `call_tool` をロック直列化＋タイムアウト＋タブ通知のラッパに変更。実際の
  ディスパッチは `_dispatch_tool` に分離（個別ハンドラの実装は不変）。
- Bumped MCP `server_version` from `0.7.0` to `0.8.0`.

## [0.7.0] - 2026-04-30

### Added — Phase 6: Manual Recording Mode

- `browser_record_start`: begin capturing manual interactions on the active
  page. Click, keydown, and scroll events are intercepted at the document's
  capture phase via an injected listener, and each event is stamped with a
  CSS path of its target so replays can survive cosmetic DOM shifts.
- `browser_record_stop`: end the active recording and persist it to
  `<MARURU_BROWSER_RECORDINGS>/<name>.json` (default
  `~/.maruru-browser/recordings/`) with a per-event-type summary in the
  response.
- `browser_record_replay`: re-run a saved recording. The replay engine
  prefers selector-based clicks (with the click point jittered inside the
  element's bounding box for bot-detection mitigation) and falls back to the
  raw recorded coordinates if the selector is not found, then sleeps a
  randomized interval between actions. Supports a `variables` list to
  iterate the same script with `{{var}}` substitutions in selectors —
  intended for repetitive workflows that iterate over a long list of
  items.
- Special action type `wait_max_lv` for fixed-duration waits followed by a
  single dismiss click, designed for animated overlays whose end state is
  hard to detect in the DOM.

### Changed

- Bumped MCP `server_version` from `0.6.0` to `0.7.0`.
- Bumped `pyproject.toml` version from `0.4.0` to `0.7.0`.

## [0.6.0] - 2026-04-30

### Added

- `gemini_ask`: send a question to Gemini and return the reply. Default
  URL is the public Gemini landing page; override via `gem_url` or the
  `MARURU_GEMINI_URL` environment variable to target a specific Gem.
- `grok_ask`: send a question to Grok and return the reply. Default URL
  is the public Grok landing page; override via `project_url` or the
  `MARURU_GROK_URL` environment variable to target a specific Project.

### Changed

- Bumped MCP `server_version` from `0.5.0` to `0.6.0`.

## [0.5.0] - 2026-04-30

### Added

- `iframe_evaluate`: run JavaScript inside a child iframe. Locate the frame by
  `frame_url` (substring), `frame_name` (exact), `frame_selector` (CSS selector
  for the host `<iframe>` element), or `frame_index` (0-based index over
  non-main frames). Calling the tool with no selector or with `expression`
  omitted returns the list of child frames so you can pick one.

### Changed

- Bumped MCP `server_version` from `0.4.0` to `0.5.0`.

## [0.4.0] - 2026-04-29

### Added

- `cookies_get`: read browser cookies, optionally filtered by URL and cookie name.
- `cookies_set`: add or overwrite browser cookies.
- `local_storage_get`: read one localStorage key or dump all localStorage for the current origin.
- `local_storage_set`: set or remove a localStorage key for the current origin.
- `wait_for_navigation`: wait for a URL pattern and page load state.
- `x_search`: search X/Twitter and extract tweet-like results from the DOM.
- `google_search`: search Google and extract results plus best-effort AI Overview text.
- `generic_form_fill`: fill forms by matching field names against common DOM attributes and labels.

### Changed

- Bumped MCP `server_version` from `0.1.0` to `0.4.0`.

### Documentation

- Added `README.md`, `CHANGELOG.md`, `SECURITY.md`, `LICENSE`.
- Expanded `.gitignore`.
- Added `pyproject.toml` for editable installs.

## [0.3.0] - 2026-04-28

### Added

- Phase 1-3 browser foundation: expanded to 24 tools across navigation, snapshots, input, wait/scroll/view helpers, tabs/hover, debug monitoring, advanced interactions, and AI integrations.

## [0.1.0] - 2026-03-24

### Added

- Initial 13-tool implementation for browser automation and early AI service integration.
