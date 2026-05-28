#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-maruru-browser: ブラウザ操作MCPサーバー

Playwright async APIを使用し、永続Chromeプロファイルでブラウザを操作する。
Perplexity検索・ChatGPT連携も統合予定。
"""

import sys
import os
import json
import asyncio
import time
import random
from collections import deque
from pathlib import Path
from typing import Any

try:
    import msvcrt  # Windows: Chromeプロファイルのプロセス間排他ロック用
except ImportError:  # 非Windows環境ではロックをスキップ
    msvcrt = None  # type: ignore

from mcp.server import Server
from mcp.server.models import InitializationOptions
from mcp.server.stdio import stdio_server
import mcp.types as types
from mcp.server.lowlevel.server import NotificationOptions

# --- 定数 ---
PAGE_LOAD_TIMEOUT = 30000       # ページロードタイムアウト (ms)
ELEMENT_WAIT_TIMEOUT = 10000    # 要素待機タイムアウト (ms)
PERPLEXITY_TIMEOUT = 120_000    # Perplexity回答待ちタイムアウト (ms)
CHATGPT_TIMEOUT = 180_000      # ChatGPT回答待ちタイムアウト (ms)
GEMINI_TIMEOUT = 180_000       # Gemini回答待ちタイムアウト (ms)
GROK_TIMEOUT = 180_000         # Grok回答待ちタイムアウト (ms)
# Chromeプロファイルパス。環境変数 MARURU_BROWSER_PROFILE で上書き可能。
# 別MCPクライアント（例: Claude Code と Claude Desktop）から同時に同じプロファイル
# を掴むと msvcrt 排他ロックでエラーになるため、共存させたい場合は別パスを env で割り当てる。
CHROME_PROFILE_PATH = os.environ.get(
    "MARURU_BROWSER_PROFILE",
    os.path.join(os.path.expanduser("~"), ".maruru-browser", "chrome-profile"),
)
CONSOLE_BUFFER_SIZE = 500       # コンソールメッセージのリングバッファ容量
NETWORK_BUFFER_SIZE = 500       # ネットワークリクエストのリングバッファ容量
# 手動記録モード(record/replay)のJSON保存先。環境変数 MARURU_BROWSER_RECORDINGS で上書き可能。
RECORDINGS_DIR = Path(os.environ.get(
    "MARURU_BROWSER_RECORDINGS",
    os.path.join(os.path.expanduser("~"), ".maruru-browser", "recordings"),
))
# screenshot / pdf_save の出力先。環境変数 MARURU_BROWSER_ARTIFACTS で上書き可能。
ARTIFACTS_DIR = Path(os.environ.get(
    "MARURU_BROWSER_ARTIFACTS",
    os.path.join(os.path.expanduser("~"), ".maruru-browser", "artifacts"),
))

# --- ハング防止・複数セッション対策のタイムアウト (秒) ---
TOOL_CALL_TIMEOUT = 300.0       # 1ツール呼び出しの実行上限。超えたら中断（AI連携系の最長180s+余裕）
TOOL_LOCK_TIMEOUT = 200.0       # 他の処理がロック保持中の待機上限。超えたら busy エラー
BROWSER_LAUNCH_TIMEOUT = 60.0   # ブラウザ起動の上限。プロファイル競合時に無限待ちを避ける

# --- 手動記録モード (record/replay) のページ注入JS ---
# document level capture phase で click / keydown / scroll を window.__maruruRecord に push する。
# - cssPathOf: target の一意セレクタを生成（id優先、無ければ tag + nth-of-type を辿る）
# - keydown: 純粋な修飾キー単独押しは無視
# - scroll: 100ms throttle で連続発火を抑制
_RECORD_INJECT_JS = r"""
(function() {
  if (window.__maruruRecord) return 'already-recording';
  window.__maruruRecord = [];
  window.__maruruRecordStartedAt = Date.now();

  function cssPathOf(el) {
    if (!(el instanceof Element)) return null;
    const path = [];
    let cur = el;
    while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
      let seg = cur.nodeName.toLowerCase();
      if (cur.id) {
        seg += '#' + CSS.escape(cur.id);
        path.unshift(seg);
        return path.join(' > ');
      }
      let nth = 1, sib = cur;
      while ((sib = sib.previousElementSibling) != null) {
        if (sib.nodeName.toLowerCase() === seg) nth++;
      }
      if (nth !== 1) seg += ':nth-of-type(' + nth + ')';
      path.unshift(seg);
      cur = cur.parentNode;
    }
    return path.join(' > ');
  }
  window.__maruruCssPathOf = cssPathOf;

  function targetInfo(t) {
    if (!t || t.nodeType !== 1) return null;
    return {
      selector: cssPathOf(t),
      tag: t.tagName ? t.tagName.toLowerCase() : null,
      text: ((t.innerText || t.value || '') + '').slice(0, 80),
    };
  }

  // 重複検出: 直前と同type+同selector+座標一致が 100ms 以内なら捨てる
  // SPA内で click を再ディスパッチして document-capture に2回ヒットするケース対策
  function pushDeduped(rec) {
    const buf = window.__maruruRecord;
    const last = buf.length ? buf[buf.length - 1] : null;
    if (last && rec.type === last.type && (rec.ts - last.ts) <= 100) {
      const sa = (rec.target || {}).selector || null;
      const sb = (last.target || {}).selector || null;
      if (sa === sb) {
        if (rec.type === 'click' && rec.x === last.x && rec.y === last.y) return;
        if (rec.type === 'keydown' && rec.key === last.key) return;
        if (rec.type === 'scroll' && rec.x === last.x && rec.y === last.y) return;
      }
    }
    buf.push(rec);
  }

  document.addEventListener('click', function(e) {
    try {
      pushDeduped({
        type: 'click',
        x: e.clientX,
        y: e.clientY,
        ts: Date.now() - window.__maruruRecordStartedAt,
        target: targetInfo(e.target),
      });
    } catch (_) {}
  }, true);

  document.addEventListener('keydown', function(e) {
    if (['Shift', 'Control', 'Alt', 'Meta'].indexOf(e.key) >= 0) return;
    try {
      pushDeduped({
        type: 'keydown',
        key: e.key,
        ctrl: e.ctrlKey, alt: e.altKey, shift: e.shiftKey, meta: e.metaKey,
        ts: Date.now() - window.__maruruRecordStartedAt,
        target: targetInfo(e.target),
      });
    } catch (_) {}
  }, true);

  let __scrollLast = 0;
  window.addEventListener('scroll', function() {
    const now = Date.now();
    if (now - __scrollLast < 100) return;
    __scrollLast = now;
    try {
      pushDeduped({
        type: 'scroll',
        x: window.scrollX, y: window.scrollY,
        ts: now - window.__maruruRecordStartedAt,
      });
    } catch (_) {}
  }, true);

  return 'started';
})();
"""

# --- グローバル状態 ---
server = Server("maruru-browser")
_browser_context = None  # persistent_context (context IS the browser)
_page = None
_playwright = None  # async_playwright インスタンス（driverプロセス解放のため保持）
_console_buffer: deque = deque(maxlen=CONSOLE_BUFFER_SIZE)
_network_buffer: deque = deque(maxlen=NETWORK_BUFFER_SIZE)
_listener_attached_pages: set = set()  # リスナー登録済みページのid()
_dialog_policy: dict = {"action": "manual", "prompt_text": ""}  # alert/confirm/prompt自動応答ポリシー
_dialog_log: deque = deque(maxlen=50)  # 直近の自動応答ログ
_active_recording: dict | None = None  # 記録中セッションメタ {name, started_at, variables_template}
_tool_lock: asyncio.Lock = asyncio.Lock()  # ツール呼び出しの直列化（並行/複数セッションのハング防止）
_last_tab_count: int | None = None  # 直近のタブ数（変化検出・通知用）
_profile_lock_handle: Any = None  # Chromeプロファイル排他ロックのファイルハンドル（二重起動防止）


def _attach_page_listeners(page: Any) -> None:
    """ページにconsole/pageerrorリスナーを登録する（多重登録防止）"""
    page_id = id(page)
    if page_id in _listener_attached_pages:
        return
    _listener_attached_pages.add(page_id)

    def _on_console(msg: Any) -> None:
        try:
            loc = msg.location or {}
            _console_buffer.append({
                "ts": time.time(),
                "type": msg.type,
                "text": msg.text,
                "url": loc.get("url", ""),
                "line": loc.get("lineNumber", 0),
                "col": loc.get("columnNumber", 0),
            })
        except Exception:
            pass

    def _on_pageerror(err: Any) -> None:
        try:
            _console_buffer.append({
                "ts": time.time(),
                "type": "pageerror",
                "text": str(err),
                "url": "",
                "line": 0,
                "col": 0,
            })
        except Exception:
            pass

    def _on_request(req: Any) -> None:
        try:
            _network_buffer.append({
                "ts": time.time(),
                "phase": "request",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "status": None,
                "from_cache": False,
            })
        except Exception:
            pass

    def _on_response(resp: Any) -> None:
        try:
            req = resp.request
            _network_buffer.append({
                "ts": time.time(),
                "phase": "response",
                "method": req.method,
                "url": resp.url,
                "resource_type": req.resource_type,
                "status": resp.status,
                "from_cache": resp.from_service_worker,
            })
        except Exception:
            pass

    def _on_requestfailed(req: Any) -> None:
        try:
            _network_buffer.append({
                "ts": time.time(),
                "phase": "failed",
                "method": req.method,
                "url": req.url,
                "resource_type": req.resource_type,
                "status": None,
                "from_cache": False,
                "error": req.failure or "",
            })
        except Exception:
            pass

    async def _on_dialog(dialog: Any) -> None:
        action = _dialog_policy.get("action", "manual")
        try:
            if action == "accept":
                prompt_text = _dialog_policy.get("prompt_text", "") or ""
                await dialog.accept(prompt_text) if dialog.type == "prompt" else await dialog.accept()
                _dialog_log.append({"ts": time.time(), "type": dialog.type, "message": dialog.message, "action": "accepted"})
            elif action == "dismiss":
                await dialog.dismiss()
                _dialog_log.append({"ts": time.time(), "type": dialog.type, "message": dialog.message, "action": "dismissed"})
            else:
                # manualモードでは何もしない（ダイアログは表示されたまま）
                _dialog_log.append({"ts": time.time(), "type": dialog.type, "message": dialog.message, "action": "manual"})
        except Exception as e:
            _dialog_log.append({"ts": time.time(), "type": dialog.type, "message": dialog.message, "action": f"error: {e}"})

    page.on("console", _on_console)
    page.on("pageerror", _on_pageerror)
    page.on("request", _on_request)
    page.on("response", _on_response)
    page.on("requestfailed", _on_requestfailed)
    page.on("dialog", _on_dialog)


def _safe_artifact_path(filename: str) -> Path:
    """Resolve a safe path under ARTIFACTS_DIR.

    Rejects absolute paths, parent traversal (..), and any path separator in
    `filename`. Callers can pass only a plain filename; the file is always
    written inside ARTIFACTS_DIR. This prevents path traversal from a tool
    argument writing outside the artifacts directory.
    """
    if not filename:
        raise ValueError("filename must be non-empty")
    if (
        os.path.isabs(filename)
        or "/" in filename
        or "\\" in filename
        or ".." in filename.split(os.sep)
        or filename in (".", "..")
    ):
        raise ValueError(
            f"filename must be a plain name without path separators or '..': {filename!r}"
        )
    ARTIFACTS_DIR.mkdir(parents=True, exist_ok=True)
    resolved = (ARTIFACTS_DIR / filename).resolve()
    # 二重防御: 解決後パスが ARTIFACTS_DIR 配下に収まることを確認
    if ARTIFACTS_DIR.resolve() not in resolved.parents and resolved != ARTIFACTS_DIR.resolve():
        raise ValueError(f"resolved path escapes artifacts directory: {resolved}")
    return resolved


def _acquire_profile_lock() -> None:
    """Chromeプロファイルの二重使用を防ぐプロセス間排他ロックを取得する。

    Chromeプロファイルは1プロセスしか開けない。別のmaruru-browserインスタンス
    （別のMCPクライアント／別セッション）が同じプロファイルを使用中だと、launch時に
    60秒ハングしてからエラーになる――その前に即座に明確なエラーで弾く。
    Claude Code と Claude Desktop を同時に使いたい場合は、片方の
    MARURU_BROWSER_PROFILE 環境変数で別パスを指定すること。
    OSはプロセス終了時にロックを自動解放するため、セッションが落ちてもロックは残らない。
    """
    global _profile_lock_handle
    if _profile_lock_handle is not None:
        return
    if msvcrt is None:
        return  # 非Windows環境ではスキップ
    os.makedirs(CHROME_PROFILE_PATH, exist_ok=True)
    lock_path = os.path.join(CHROME_PROFILE_PATH, ".maruru_browser.lock")
    fh = open(lock_path, "a+")
    try:
        fh.seek(0)
        msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)  # 非ブロッキング排他ロック
    except OSError:
        fh.close()
        raise RuntimeError(
            "別のmaruru-browserインスタンスが同じChromeプロファイルを使用中です。\n"
            f"  プロファイル: {CHROME_PROFILE_PATH}\n"
            "Chromeプロファイルは1プロセスしか開けません。先に動いている方のクライアント"
            "（例: Claude Code / Claude Desktop）を終了するか、片方の MARURU_BROWSER_PROFILE "
            "環境変数で別パスを指定してください。"
        )
    _profile_lock_handle = fh


def _release_profile_lock() -> None:
    """Chromeプロファイルの排他ロックを解放する。"""
    global _profile_lock_handle
    if _profile_lock_handle is None:
        return
    try:
        _profile_lock_handle.seek(0)
        if msvcrt is not None:
            msvcrt.locking(_profile_lock_handle.fileno(), msvcrt.LK_UNLCK, 1)
    except OSError:
        pass
    try:
        _profile_lock_handle.close()
    except OSError:
        pass
    _profile_lock_handle = None


async def _ensure_browser() -> tuple[Any, Any]:
    """ブラウザを遅延起動し、(context, page)を返す。既に起動済みならそのまま返す。"""
    global _browser_context, _page, _playwright

    if _browser_context is not None and _page is not None:
        # 既存ページにリスナー未登録なら登録
        for p in _browser_context.pages:
            _attach_page_listeners(p)
        return _browser_context, _page

    # 起動前にプロファイル排他ロックを取得（別インスタンスが使用中なら即エラー）
    _acquire_profile_lock()

    # import / playwright start / launch をすべて try で覆う。
    # ここで失敗するとロックが残る（リーク）ため、失敗時は必ず手放す。
    pw = None
    try:
        from playwright.async_api import async_playwright

        pw = await async_playwright().start()
        # ブラウザ起動はタイムアウト付き。別セッションがプロファイルを使用中だと
        # 起動が失敗/長引くため、無限待ちを避けて明確なエラーにする。
        _browser_context = await asyncio.wait_for(
            pw.chromium.launch_persistent_context(
                user_data_dir=CHROME_PROFILE_PATH,
                headless=False,
                channel="chrome",
                args=["--disable-blink-features=AutomationControlled"],
            ),
            timeout=BROWSER_LAUNCH_TIMEOUT,
        )
    except BaseException as e:
        # 失敗時は driver プロセスを止め、プロファイルロックを手放す。
        # asyncio.CancelledError も拾うため BaseException で受ける。
        _browser_context = None
        if pw is not None:
            try:
                await pw.stop()
            except Exception:
                pass
        _release_profile_lock()
        if isinstance(e, asyncio.TimeoutError):
            raise RuntimeError(
                f"ブラウザ起動が{BROWSER_LAUNCH_TIMEOUT:.0f}秒以内に完了しませんでした。"
                "別のセッション/プロセスがChromeプロファイルを使用中の可能性があります。"
            ) from e
        if isinstance(e, (asyncio.CancelledError, KeyboardInterrupt, SystemExit)):
            raise  # 制御フロー系の例外はRuntimeErrorに変換せずそのまま伝播
        raise RuntimeError(
            f"ブラウザ起動に失敗しました（プロファイル競合の可能性）: {e}"
        ) from e

    _playwright = pw  # 起動成功。driver は _cleanup() で stop する

    # 新規ページが作られたら自動でリスナー登録
    _browser_context.on("page", lambda p: _attach_page_listeners(p))

    # persistent_contextではページが自動作成される
    if _browser_context.pages:
        _page = _browser_context.pages[0]
    else:
        _page = await _browser_context.new_page()

    for p in _browser_context.pages:
        _attach_page_listeners(p)

    _page.set_default_timeout(ELEMENT_WAIT_TIMEOUT)
    _page.set_default_navigation_timeout(PAGE_LOAD_TIMEOUT)

    return _browser_context, _page


async def _cleanup() -> None:
    """ブラウザリソースを解放する"""
    global _browser_context, _page, _playwright
    if _browser_context is not None:
        try:
            await _browser_context.close()
        except Exception:
            pass
    if _playwright is not None:
        # Playwright driver プロセス（node）を確実に停止（プロセスリーク防止）
        try:
            await _playwright.stop()
        except Exception:
            pass
    _browser_context = None
    _page = None
    _playwright = None
    _release_profile_lock()  # プロファイル排他ロックを解放（次のセッションが起動可能になる）
    _listener_attached_pages.clear()
    _console_buffer.clear()
    _network_buffer.clear()
    _dialog_log.clear()
    _dialog_policy["action"] = "manual"
    _dialog_policy["prompt_text"] = ""


# --- ツール定義 ---

@server.list_tools()
async def list_tools() -> list[types.Tool]:
    """利用可能なツール一覧を返す"""
    return [
        types.Tool(
            name="browser_navigate",
            description="指定URLにブラウザでナビゲートする",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "ナビゲート先のURL"
                    }
                },
                "required": ["url"]
            }
        ),
        types.Tool(
            name="browser_snapshot",
            description="現在のページのアクセシビリティスナップショットを取得する",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="browser_click",
            description="ページ上の要素をクリックする。follow_new_tab=true で新タブに自動切替",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "クリック対象のCSSセレクタまたはテキスト"
                    },
                    "follow_new_tab": {
                        "type": "boolean",
                        "description": "クリックで新タブが開く場合、自動で _page を新タブに切替。新タブが出ない場合は TimeoutError",
                        "default": False
                    },
                    "new_tab_timeout": {
                        "type": "integer",
                        "description": "follow_new_tab 時の新タブ出現待ち timeout(ms)",
                        "default": 5000
                    }
                },
                "required": ["selector"]
            }
        ),
        types.Tool(
            name="browser_type",
            description="指定要素にテキストを入力する。submit=true + follow_new_tab=true で Enter押下時に新タブ追従",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "入力対象のCSSセレクタ"
                    },
                    "text": {
                        "type": "string",
                        "description": "入力するテキスト"
                    },
                    "submit": {
                        "type": "boolean",
                        "description": "入力後にEnterを押すか",
                        "default": False
                    },
                    "follow_new_tab": {
                        "type": "boolean",
                        "description": "submit=true の Enter で新タブが開く場合、自動で _page を新タブに切替（submit=false 時は無視）",
                        "default": False
                    },
                    "new_tab_timeout": {
                        "type": "integer",
                        "description": "follow_new_tab 時の新タブ出現待ち timeout(ms)",
                        "default": 5000
                    }
                },
                "required": ["selector", "text"]
            }
        ),
        types.Tool(
            name="browser_take_screenshot",
            description="現在のページのスクリーンショットをPNGファイルに保存する",
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "保存ファイル名（省略時は自動生成）",
                        "default": ""
                    },
                    "full_page": {
                        "type": "boolean",
                        "description": "ページ全体をキャプチャするか",
                        "default": False
                    }
                }
            }
        ),
        types.Tool(
            name="browser_press_key",
            description="キーボードのキーを押す（Enter, Tab, Escape, ArrowDown等）。follow_new_tab=true で新タブ自動切替",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "キー名（Enter, Tab, Escape, ArrowDown, Control+a 等）"
                    },
                    "follow_new_tab": {
                        "type": "boolean",
                        "description": "キー押下で新タブが開く場合、自動で _page を新タブに切替",
                        "default": False
                    },
                    "new_tab_timeout": {
                        "type": "integer",
                        "description": "follow_new_tab 時の新タブ出現待ち timeout(ms)",
                        "default": 5000
                    }
                },
                "required": ["key"]
            }
        ),
        types.Tool(
            name="browser_evaluate",
            description="ページ上でJavaScriptを実行し、結果を返す",
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "実行するJavaScript式"
                    }
                },
                "required": ["expression"]
            }
        ),
        types.Tool(
            name="browser_wait_for",
            description="待機する。selector/text/timeのいずれか1つを指定（selector=要素出現、text=テキスト出現、time=ミリ秒スリープ）",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "待機対象のCSSセレクタ（要素出現待ち）"
                    },
                    "text": {
                        "type": "string",
                        "description": "待機対象のテキスト（ページ内に出現するまで待機）"
                    },
                    "time": {
                        "type": "integer",
                        "description": "固定スリープ時間（ミリ秒）"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "タイムアウト（ミリ秒）。selector/text用、time指定時は無効",
                        "default": 10000
                    }
                }
            }
        ),
        types.Tool(
            name="browser_select_option",
            description="ドロップダウン（select要素）のオプションを選択する",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "select要素のCSSセレクタ"
                    },
                    "value": {
                        "type": "string",
                        "description": "選択するvalue値またはラベルテキスト"
                    }
                },
                "required": ["selector", "value"]
            }
        ),
        types.Tool(
            name="browser_navigate_back",
            description="ブラウザの戻るボタンを押す",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="browser_navigate_forward",
            description="ブラウザの進むボタンを押す",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="browser_resize",
            description="ビューポートサイズを変更する（レスポンシブテスト用）",
            inputSchema={
                "type": "object",
                "properties": {
                    "width": {
                        "type": "integer",
                        "description": "幅（ピクセル）"
                    },
                    "height": {
                        "type": "integer",
                        "description": "高さ（ピクセル）"
                    }
                },
                "required": ["width", "height"]
            }
        ),
        types.Tool(
            name="browser_scroll",
            description="ページをスクロールする。direction(up/down/top/bottom/to)とamount(px)またはselector(指定要素まで)を指定",
            inputSchema={
                "type": "object",
                "properties": {
                    "direction": {
                        "type": "string",
                        "enum": ["up", "down", "top", "bottom", "to"],
                        "description": "スクロール方向。to=指定要素までscroll_into_view",
                        "default": "down"
                    },
                    "amount": {
                        "type": "integer",
                        "description": "スクロール量（ピクセル）。up/down時のみ有効",
                        "default": 500
                    },
                    "selector": {
                        "type": "string",
                        "description": "direction=to時の対象要素CSSセレクタ"
                    }
                }
            }
        ),
        types.Tool(
            name="browser_console_messages",
            description="ブラウザのコンソールログ・JSエラーを取得する。filterで種別絞り込み可（log/info/warning/error/pageerror）",
            inputSchema={
                "type": "object",
                "properties": {
                    "filter": {
                        "type": "string",
                        "description": "種別フィルタ。カンマ区切りで複数指定可。省略で全件",
                        "default": ""
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返却件数の上限（最新側から）",
                        "default": 100
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "取得後にバッファをクリアするか",
                        "default": False
                    }
                }
            }
        ),
        types.Tool(
            name="browser_mouse_xy",
            description="マウスを座標指定で操作する。canvas/地図/PDF等のセレクタが効かない場面用",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["move", "click", "double_click", "down", "up"],
                        "description": "操作種別",
                        "default": "click"
                    },
                    "x": {
                        "type": "number",
                        "description": "X座標（ビューポート左上から、ピクセル）"
                    },
                    "y": {
                        "type": "number",
                        "description": "Y座標（ビューポート左上から、ピクセル）"
                    },
                    "button": {
                        "type": "string",
                        "enum": ["left", "right", "middle"],
                        "description": "マウスボタン",
                        "default": "left"
                    },
                    "steps": {
                        "type": "integer",
                        "description": "moveの中間ステップ数（自然な動きのため）",
                        "default": 1
                    }
                },
                "required": ["x", "y"]
            }
        ),
        types.Tool(
            name="browser_pdf_save",
            description="現在のページをPDFファイルに保存する。Chromium DevTools Protocolで実装",
            inputSchema={
                "type": "object",
                "properties": {
                    "filename": {
                        "type": "string",
                        "description": "保存ファイル名（省略時は自動生成）",
                        "default": ""
                    },
                    "format": {
                        "type": "string",
                        "description": "用紙サイズ（A4/A3/Letter等）",
                        "default": "A4"
                    },
                    "landscape": {
                        "type": "boolean",
                        "description": "横向きで出力",
                        "default": False
                    },
                    "print_background": {
                        "type": "boolean",
                        "description": "背景色・背景画像を含める",
                        "default": True
                    }
                }
            }
        ),
        types.Tool(
            name="browser_drag_drop",
            description="ある要素を別の要素にドラッグ&ドロップする",
            inputSchema={
                "type": "object",
                "properties": {
                    "source": {
                        "type": "string",
                        "description": "ドラッグ元のCSSセレクタ"
                    },
                    "target": {
                        "type": "string",
                        "description": "ドロップ先のCSSセレクタ"
                    }
                },
                "required": ["source", "target"]
            }
        ),
        types.Tool(
            name="browser_file_upload",
            description="<input type=file>要素にファイルを設定する。複数ファイル可（カンマ区切り）",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "input[type=file]要素のCSSセレクタ"
                    },
                    "files": {
                        "type": "string",
                        "description": "アップロードするファイルの絶対パス。カンマ区切りで複数指定可"
                    }
                },
                "required": ["selector", "files"]
            }
        ),
        types.Tool(
            name="browser_hover",
            description="ページ上の要素にマウスホバーする（メニュー展開・ツールチップ表示用）",
            inputSchema={
                "type": "object",
                "properties": {
                    "selector": {
                        "type": "string",
                        "description": "ホバー対象のCSSセレクタまたはテキスト"
                    }
                },
                "required": ["selector"]
            }
        ),
        types.Tool(
            name="browser_handle_dialog",
            description="alert/confirm/promptダイアログの自動応答ポリシーを設定する。設定後に表示されたダイアログを自動処理。最近の処理ログも返す",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["accept", "dismiss", "manual"],
                        "description": "accept=OK押下、dismiss=キャンセル、manual=何もせず表示維持（既定）",
                        "default": "manual"
                    },
                    "prompt_text": {
                        "type": "string",
                        "description": "promptダイアログでaccept時に入力するテキスト",
                        "default": ""
                    }
                }
            }
        ),
        types.Tool(
            name="browser_network_requests",
            description="ブラウザのネットワーク通信(リクエスト/レスポンス/失敗)を取得する。url_contains/method/resource_type/status_minで絞り込み可",
            inputSchema={
                "type": "object",
                "properties": {
                    "url_contains": {
                        "type": "string",
                        "description": "URLに含まれる部分文字列で絞り込み",
                        "default": ""
                    },
                    "method": {
                        "type": "string",
                        "description": "HTTPメソッド絞り込み（GET/POST等、カンマ区切り可）",
                        "default": ""
                    },
                    "resource_type": {
                        "type": "string",
                        "description": "リソース種別絞り込み（xhr/fetch/document/script/image等、カンマ区切り可）",
                        "default": ""
                    },
                    "phase": {
                        "type": "string",
                        "enum": ["", "request", "response", "failed"],
                        "description": "フェーズ絞り込み。省略で全フェーズ",
                        "default": ""
                    },
                    "status_min": {
                        "type": "integer",
                        "description": "ステータスコード下限（例: 400で4xx/5xxのみ）",
                        "default": 0
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返却件数の上限（最新側から）",
                        "default": 100
                    },
                    "clear": {
                        "type": "boolean",
                        "description": "取得後にバッファをクリアするか",
                        "default": False
                    }
                }
            }
        ),
        types.Tool(
            name="browser_tabs",
            description="タブ操作。action=list/switch/new/close/latest/wait_close。latest=最新タブへ切替、wait_close=指定タブの閉鎖待ち",
            inputSchema={
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["list", "switch", "new", "close", "latest", "wait_close"],
                        "description": "操作種別。latest=末尾タブにフォーカス切替、wait_close=指定タブの閉鎖を待つ（OAuthポップアップ追跡用）",
                        "default": "list"
                    },
                    "index": {
                        "type": "integer",
                        "description": "switch/close/wait_close の対象タブインデックス（0始まり）。close/wait_close で省略時はそれぞれアクティブ/末尾タブ",
                        "default": -1
                    },
                    "url": {
                        "type": "string",
                        "description": "new時に開くURL（省略時はabout:blank）",
                        "default": ""
                    },
                    "switch_to": {
                        "type": "integer",
                        "description": "[後方互換] 切り替え先インデックス。actionより優先される",
                        "default": -1
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "wait_close 用 timeout(ms)。デフォルト60秒",
                        "default": 60000
                    }
                }
            }
        ),
        types.Tool(
            name="perplexity_search",
            description="Perplexityで検索を実行し、結果を取得する",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索クエリ"
                    },
                    "space_url": {
                        "type": "string",
                        "description": "PerplexityスペースURL（省略可）",
                        "default": ""
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="chatgpt_ask",
            description="ChatGPTに質問を送信し、回答を取得する",
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "ChatGPTへの質問"
                    },
                    "project_url": {
                        "type": "string",
                        "description": "ChatGPTプロジェクトURL（省略可）",
                        "default": ""
                    }
                },
                "required": ["question"]
            }
        ),
        # --- Phase 4: Cookie / LocalStorage / Navigation / Search / Form ---
        types.Tool(
            name="cookies_get",
            description="ブラウザのCookieを取得する。urlで対象を絞り込み、nameで名前一致フィルタ可",
            inputSchema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Cookie取得対象のURL（省略時は全Cookie）",
                        "default": ""
                    },
                    "name": {
                        "type": "string",
                        "description": "Cookie名フィルタ（完全一致）",
                        "default": ""
                    }
                }
            }
        ),
        types.Tool(
            name="cookies_set",
            description="Cookieを追加・上書きする。各Cookieは name+value 必須、url または domain+path のいずれかが必要",
            inputSchema={
                "type": "object",
                "properties": {
                    "cookies": {
                        "type": "array",
                        "description": "Cookie定義の配列。各要素: {name, value, url?, domain?, path?, expires?, httpOnly?, secure?, sameSite?}",
                        "items": {"type": "object"}
                    }
                },
                "required": ["cookies"]
            }
        ),
        types.Tool(
            name="local_storage_get",
            description="localStorageを取得する。keyを指定すると単一値、省略で全件JSONを返す",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "取得するキー（省略時は全件）",
                        "default": ""
                    }
                }
            }
        ),
        types.Tool(
            name="local_storage_set",
            description="localStorageに値を保存する。removeを指定するとキーを削除",
            inputSchema={
                "type": "object",
                "properties": {
                    "key": {
                        "type": "string",
                        "description": "保存先キー"
                    },
                    "value": {
                        "type": "string",
                        "description": "保存する値（文字列）。removeがtrueの場合は無視",
                        "default": ""
                    },
                    "remove": {
                        "type": "boolean",
                        "description": "trueの場合は該当キーを削除",
                        "default": False
                    }
                },
                "required": ["key"]
            }
        ),
        types.Tool(
            name="wait_for_navigation",
            description="ページ遷移完了を待つ。url_patternで遷移先URLパターン（部分一致）、state(load/domcontentloaded/networkidle)で読み込み状態を指定",
            inputSchema={
                "type": "object",
                "properties": {
                    "url_pattern": {
                        "type": "string",
                        "description": "遷移先URLの部分一致パターン（省略時はURL条件なし）",
                        "default": ""
                    },
                    "state": {
                        "type": "string",
                        "enum": ["load", "domcontentloaded", "networkidle"],
                        "description": "読み込み状態",
                        "default": "load"
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "タイムアウト（ミリ秒）",
                        "default": 30000
                    }
                }
            }
        ),
        types.Tool(
            name="x_search",
            description="X(Twitter)を検索してツイート一覧を抽出する。f=live(最新) or top(人気)",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索クエリ"
                    },
                    "mode": {
                        "type": "string",
                        "enum": ["latest", "top"],
                        "description": "latest=新着順、top=人気順",
                        "default": "latest"
                    },
                    "scrolls": {
                        "type": "integer",
                        "description": "スクロール回数（読み込みツイート数調整）",
                        "default": 5
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返却ツイート数の上限",
                        "default": 50
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="google_search",
            description="Googleを検索して結果一覧を抽出する。AI Overview(SGE)も併記",
            inputSchema={
                "type": "object",
                "properties": {
                    "query": {
                        "type": "string",
                        "description": "検索クエリ"
                    },
                    "limit": {
                        "type": "integer",
                        "description": "返却件数の上限",
                        "default": 10
                    },
                    "lang": {
                        "type": "string",
                        "description": "検索言語(hl)コード",
                        "default": "ja"
                    }
                },
                "required": ["query"]
            }
        ),
        types.Tool(
            name="generic_form_fill",
            description="フォームを自動入力する。dataのkeyからname/id/placeholder/aria-label/labelテキストを推定して該当inputを埋める",
            inputSchema={
                "type": "object",
                "properties": {
                    "data": {
                        "type": "object",
                        "description": "{フィールド名: 値} の辞書。フィールド名は name/id/placeholder/label/aria-label のいずれかに一致を試行",
                        "additionalProperties": {"type": "string"}
                    },
                    "submit": {
                        "type": "boolean",
                        "description": "入力後に送信ボタン（type=submit / role=button[text=送信/Submit]）をクリック",
                        "default": False
                    }
                },
                "required": ["data"]
            }
        ),
        types.Tool(
            name="gemini_ask",
            description=(
                "Gemini に質問を送って回答を取得する。デフォルトの URL は環境変数"
                " MARURU_GEMINI_URL で上書き可能（個人用 Gem を使う場合等）"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Geminiへの質問（英語推奨）"
                    },
                    "gem_url": {
                        "type": "string",
                        "description": "Gem URL を直接指定する場合に使用（省略時は MARURU_GEMINI_URL or 公式トップ）"
                    }
                },
                "required": ["question"]
            }
        ),
        types.Tool(
            name="grok_ask",
            description=(
                "Grok に質問を送って回答を取得する。デフォルトの URL は環境変数"
                " MARURU_GROK_URL で上書き可能（個人用 Project を使う場合等）"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "question": {
                        "type": "string",
                        "description": "Grokへの質問（英語推奨）"
                    },
                    "project_url": {
                        "type": "string",
                        "description": "Project URL を直接指定する場合に使用（省略時は MARURU_GROK_URL or 公式トップ）"
                    }
                },
                "required": ["question"]
            }
        ),
        types.Tool(
            name="iframe_evaluate",
            description=(
                "iframe内でJavaScriptを実行する。frame_url/frame_name/frame_selector/frame_index"
                "のいずれかでiframeを指定。指定なしまたはexpression省略時は子フレーム一覧を返す（探索用）"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "iframe内で実行するJavaScript式。省略時はフレーム一覧のみ返す"
                    },
                    "frame_url": {
                        "type": "string",
                        "description": "frame.urlの部分文字列で一致（複数マッチ時はエラー、frame_indexで絞る）"
                    },
                    "frame_name": {
                        "type": "string",
                        "description": "<iframe name=...> 属性で完全一致"
                    },
                    "frame_selector": {
                        "type": "string",
                        "description": "親ページ側の <iframe> 要素のCSSセレクタ"
                    },
                    "frame_index": {
                        "type": "integer",
                        "description": "子フレーム配列(page.frames - main_frame)の0-basedインデックス"
                    }
                }
            }
        ),
        types.Tool(
            name="browser_record_start",
            description=(
                "手動操作の記録を開始する。click / keydown / scroll を document level で捕捉し "
                "window.__maruruRecord に蓄積する。停止は browser_record_stop。"
                "注意: ナビゲーションするとページの注入が消えるため、単一ページ内の操作向け。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "記録名（英数字/_/- のみ。MARURU_BROWSER_RECORDINGS で指定された保存先（既定 ~/.maruru-browser/recordings/）の <name>.json に保存）"
                    },
                    "variables_template": {
                        "type": "array",
                        "description": "後から再生時に差し替える変数名のリスト（メタとして保存。selectorに{{var}}を手動で埋める）",
                        "items": {"type": "string"}
                    }
                },
                "required": ["name"]
            }
        ),
        types.Tool(
            name="browser_record_stop",
            description="手動記録を停止して JSON へ保存。保存先は MARURU_BROWSER_RECORDINGS（既定 ~/.maruru-browser/recordings/）の <name>.json",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        ),
        types.Tool(
            name="browser_record_replay",
            description=(
                "保存された記録を再生する。selector優先・座標フォールバック・±jitter pxで bot 検知を緩和。"
                "variables を渡すと iteration 毎に {{var}} を差し替えて複数回再生（長尺リストを順に処理する繰り返し用）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "name": {
                        "type": "string",
                        "description": "再生する記録名"
                    },
                    "variables": {
                        "type": "array",
                        "description": "iteration毎の変数辞書のリスト。空ならvariableなしで1回再生",
                        "items": {"type": "object"}
                    },
                    "jitter": {
                        "type": "integer",
                        "description": "クリック座標の±ジッター(px)。bot検知緩和用。デフォルト10",
                        "default": 10
                    },
                    "wait_min": {
                        "type": "number",
                        "description": "アクション間ランダム待機の最小秒。デフォルト2",
                        "default": 2
                    },
                    "wait_max": {
                        "type": "number",
                        "description": "アクション間ランダム待機の最大秒。デフォルト5",
                        "default": 5
                    }
                },
                "required": ["name"]
            }
        ),
        types.Tool(
            name="browser_popup_flow",
            description=(
                "OAuthポップアップフロー専用ヘルパー: トリガー要素クリック→ポップアップ追従→閉鎖待ち→元タブのURL変化待ちを1コールで完結。"
                "実行中は _tool_lock を保持するため他ツールはブロックされる。手動認証込みで最大 popup_timeout+close_timeout+redirect_timeout"
                " かかるが、TOOL_CALL_TIMEOUT(300s) を超えると外側で打ち切られる点に注意。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "trigger_selector": {
                        "type": "string",
                        "description": "ポップアップを開くトリガー要素（テキスト/CSSセレクタ）"
                    },
                    "expected_url_substring": {
                        "type": "string",
                        "description": "元タブが認証後に遷移するURLの部分一致文字列（空なら閉鎖検出のみ）",
                        "default": ""
                    },
                    "popup_timeout": {
                        "type": "integer",
                        "description": "ポップアップ出現待ち timeout(ms)",
                        "default": 10000
                    },
                    "close_timeout": {
                        "type": "integer",
                        "description": "ポップアップ閉鎖待ち timeout(ms)。手動認証想定で長め",
                        "default": 120000
                    },
                    "redirect_timeout": {
                        "type": "integer",
                        "description": "元タブのURL変化待ち timeout(ms)",
                        "default": 30000
                    }
                },
                "required": ["trigger_selector"]
            }
        ),
    ]


# --- ツールディスパッチャ ---

@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any] | None) -> list[types.TextContent]:
    """ツール呼び出しのディスパッチャ。

    複数セッション/並行呼び出しでも安全なよう、ロックで直列化し、1呼び出し
    ごとにタイムアウトを設けてハングを防ぐ。完了後にタブ数変化を通知する。
    """
    args = arguments or {}

    # --- 複数セッション対策: ロックを待ち過ぎ防止のタイムアウト付きで取得 ---
    try:
        await asyncio.wait_for(_tool_lock.acquire(), timeout=TOOL_LOCK_TIMEOUT)
    except asyncio.TimeoutError:
        return [types.TextContent(type="text", text=(
            f"⚠️ maruru-browser is busy: 別の処理がブラウザを{TOOL_LOCK_TIMEOUT:.0f}"
            f"秒以上占有しています。ツール '{name}' は未実行です。少し待って再試行してください。"
        ))]

    # --- ハング防止: 1呼び出しごとにタイムアウト ---
    try:
        result = await asyncio.wait_for(
            _dispatch_tool(name, args), timeout=TOOL_CALL_TIMEOUT)
    except asyncio.TimeoutError:
        result = [types.TextContent(type="text", text=(
            f"⚠️ ツール '{name}' は{TOOL_CALL_TIMEOUT:.0f}秒で完了せずハング防止のため"
            f"中断しました。browser_tabs / browser_take_screenshot でブラウザ状態を確認してください。"
        ))]
    finally:
        _tool_lock.release()

    return _append_tab_notice(result)


def _append_tab_notice(result: list[types.TextContent]) -> list[types.TextContent]:
    """ツール応答末尾にタブ数を付記し、前回から変化していれば警告する。"""
    global _last_tab_count
    cur: int | None = None
    if _browser_context is not None:
        try:
            cur = len(_browser_context.pages)
        except Exception:
            cur = None
    if cur is None:
        return result

    if _last_tab_count is not None and cur != _last_tab_count:
        diff = cur - _last_tab_count
        sign = f"+{diff}" if diff > 0 else str(diff)
        notice = (
            f"\n\n⚠️ タブ数が {_last_tab_count} → {cur} に変化しました（{sign}）。"
            f"意図しない新規タブが開いていないか browser_tabs で確認してください。"
        )
    else:
        notice = f"\n\n[tabs: {cur}]"
    _last_tab_count = cur

    if result and isinstance(result[-1], types.TextContent):
        result[-1].text = (result[-1].text or "") + notice
    else:
        result.append(types.TextContent(type="text", text=notice.strip()))
    return result


async def _dispatch_tool(name: str, args: dict[str, Any]) -> list[types.TextContent]:
    """ツール名に応じて個別ハンドラへ振り分ける。"""
    try:
        if name == "browser_navigate":
            return await _handle_browser_navigate(args)
        elif name == "browser_snapshot":
            return await _handle_browser_snapshot(args)
        elif name == "browser_click":
            return await _handle_browser_click(args)
        elif name == "browser_type":
            return await _handle_browser_type(args)
        elif name == "browser_take_screenshot":
            return await _handle_browser_take_screenshot(args)
        elif name == "browser_press_key":
            return await _handle_browser_press_key(args)
        elif name == "browser_evaluate":
            return await _handle_browser_evaluate(args)
        elif name == "browser_wait_for":
            return await _handle_browser_wait_for(args)
        elif name == "browser_select_option":
            return await _handle_browser_select_option(args)
        elif name == "browser_navigate_back":
            return await _handle_browser_navigate_back(args)
        elif name == "browser_navigate_forward":
            return await _handle_browser_navigate_forward(args)
        elif name == "browser_resize":
            return await _handle_browser_resize(args)
        elif name == "browser_scroll":
            return await _handle_browser_scroll(args)
        elif name == "browser_console_messages":
            return await _handle_browser_console_messages(args)
        elif name == "browser_network_requests":
            return await _handle_browser_network_requests(args)
        elif name == "browser_handle_dialog":
            return await _handle_browser_handle_dialog(args)
        elif name == "browser_hover":
            return await _handle_browser_hover(args)
        elif name == "browser_file_upload":
            return await _handle_browser_file_upload(args)
        elif name == "browser_drag_drop":
            return await _handle_browser_drag_drop(args)
        elif name == "browser_pdf_save":
            return await _handle_browser_pdf_save(args)
        elif name == "browser_mouse_xy":
            return await _handle_browser_mouse_xy(args)
        elif name == "browser_tabs":
            return await _handle_browser_tabs(args)
        elif name == "perplexity_search":
            return await _handle_perplexity_search(args)
        elif name == "chatgpt_ask":
            return await _handle_chatgpt_ask(args)
        elif name == "cookies_get":
            return await _handle_cookies_get(args)
        elif name == "cookies_set":
            return await _handle_cookies_set(args)
        elif name == "local_storage_get":
            return await _handle_local_storage_get(args)
        elif name == "local_storage_set":
            return await _handle_local_storage_set(args)
        elif name == "wait_for_navigation":
            return await _handle_wait_for_navigation(args)
        elif name == "x_search":
            return await _handle_x_search(args)
        elif name == "google_search":
            return await _handle_google_search(args)
        elif name == "generic_form_fill":
            return await _handle_generic_form_fill(args)
        elif name == "gemini_ask":
            return await _handle_gemini_ask(args)
        elif name == "grok_ask":
            return await _handle_grok_ask(args)
        elif name == "iframe_evaluate":
            return await _handle_iframe_evaluate(args)
        elif name == "browser_record_start":
            return await _handle_browser_record_start(args)
        elif name == "browser_record_stop":
            return await _handle_browser_record_stop(args)
        elif name == "browser_record_replay":
            return await _handle_browser_record_replay(args)
        elif name == "browser_popup_flow":
            return await _handle_browser_popup_flow(args)
        else:
            return [types.TextContent(type="text", text=f"Unknown tool: {name}")]
    except Exception as e:
        return [types.TextContent(type="text", text=f"Error in {name}: {e}")]


# --- スタブハンドラ ---

async def _handle_browser_navigate(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_navigate: URLに移動"""
    url = args["url"]
    _ctx, page = await _ensure_browser()
    response = await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    status = response.status if response else "unknown"
    title = await page.title()
    return [types.TextContent(
        type="text",
        text=f"Navigated to {url}\nStatus: {status}\nTitle: {title}",
    )]


async def _handle_browser_snapshot(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_snapshot: アクセシビリティツリーを取得"""
    _ctx, page = await _ensure_browser()
    title = await page.title()
    url = page.url

    # アクセシビリティスナップショット取得
    snapshot = await page.accessibility.snapshot()
    if snapshot is None:
        # フォールバック: innerTextを取得
        text = await page.inner_text("body")
        if len(text) > 10000:
            text = text[:10000] + "\n... (truncated)"
        return [types.TextContent(
            type="text",
            text=f"URL: {url}\nTitle: {title}\n\n{text}",
        )]

    def _format_tree(node: dict, indent: int = 0) -> str:
        """アクセシビリティツリーをテキストに変換"""
        lines = []
        prefix = "  " * indent
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")
        parts = [role]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f"value={value}")
        lines.append(f"{prefix}{' '.join(parts)}")
        for child in node.get("children", []):
            lines.append(_format_tree(child, indent + 1))
        return "\n".join(lines)

    tree_text = _format_tree(snapshot)
    if len(tree_text) > 15000:
        tree_text = tree_text[:15000] + "\n... (truncated)"

    return [types.TextContent(
        type="text",
        text=f"URL: {url}\nTitle: {title}\n\n{tree_text}",
    )]


async def _resolve_clickable_locator(page: Any, selector: str) -> tuple[Any, str]:
    """クリック対象のlocatorを解決する。text→button→link→CSS の順にフォールバック。

    Returns:
        (locator, label) — locatorはclick可能な単一要素、labelはログ用ラベル
    """
    try:
        locator = page.get_by_text(selector, exact=False)
        if await locator.count() > 0:
            return locator.first, f"text: {selector}"
    except Exception:
        pass
    try:
        locator = page.get_by_role("button", name=selector)
        if await locator.count() > 0:
            return locator.first, f"button: {selector}"
    except Exception:
        pass
    try:
        locator = page.get_by_role("link", name=selector)
        if await locator.count() > 0:
            return locator.first, f"link: {selector}"
    except Exception:
        pass
    # CSSセレクタとしてのフォールバック
    return page.locator(selector).first, f"selector: {selector}"


async def _switch_to_new_page(new_page: Any, page_ctx: Any) -> int:
    """新規ページにフォーカスを切り替える。domcontentloaded まで待ってから _page を更新。"""
    global _page
    await new_page.wait_for_load_state("domcontentloaded", timeout=PAGE_LOAD_TIMEOUT)
    _page = new_page
    await _page.bring_to_front()
    return page_ctx.pages.index(new_page)


async def _handle_browser_click(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_click: 要素をクリック（follow_new_tab=true で新タブ追従）"""
    selector = args["selector"]
    follow = args.get("follow_new_tab", False)
    new_tab_to = args.get("new_tab_timeout", 5000)
    _ctx, page = await _ensure_browser()

    locator, label = await _resolve_clickable_locator(page, selector)

    if follow:
        async with page.expect_popup(timeout=new_tab_to) as popup_info:
            await locator.click(timeout=ELEMENT_WAIT_TIMEOUT)
        new_page = await popup_info.value
        new_idx = await _switch_to_new_page(new_page, page.context)
        return [types.TextContent(
            type="text",
            text=f"Clicked {label}, switched to new tab [{new_idx}]: {new_page.url}",
        )]
    await locator.click(timeout=ELEMENT_WAIT_TIMEOUT)
    return [types.TextContent(type="text", text=f"Clicked {label}")]


async def _fill_or_type(locator, text: str, submit: bool = False) -> None:
    """fill()を試し、ダメならclick+type()にフォールバック（contenteditable対応）"""
    try:
        await locator.fill(text, timeout=5000)
    except Exception:
        await locator.click(timeout=5000)
        await locator.press_sequentially(text, delay=30)
    if submit:
        await locator.press("Enter")


async def _resolve_typeable_locator(page: Any, selector: str) -> tuple[Any, str]:
    """テキスト入力対象のlocatorを解決する。placeholder→textbox→label→CSSの順。"""
    try:
        locator = page.get_by_placeholder(selector, exact=False)
        if await locator.count() > 0:
            return locator.first, f"placeholder '{selector}'"
    except Exception:
        pass
    try:
        locator = page.get_by_role("textbox", name=selector)
        if await locator.count() > 0:
            return locator.first, f"textbox '{selector}'"
    except Exception:
        pass
    try:
        locator = page.get_by_label(selector, exact=False)
        if await locator.count() > 0:
            return locator.first, f"label '{selector}'"
    except Exception:
        pass
    return page.locator(selector).first, f"'{selector}'"


async def _handle_browser_type(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_type: テキストを入力（submit + follow_new_tab=true で Enter押下時に新タブ追従）"""
    selector = args["selector"]
    text = args["text"]
    submit = args.get("submit", False)
    follow = args.get("follow_new_tab", False) and submit
    new_tab_to = args.get("new_tab_timeout", 5000)
    _ctx, page = await _ensure_browser()

    locator, label = await _resolve_typeable_locator(page, selector)

    # 1. テキスト入力（Enter押下は別途）— follow_new_tab 時は typing をラップしないため、
    #    submit=True でも _fill_or_type には submit=False を渡し、Enter は後段で扱う
    await _fill_or_type(locator, text, submit=(submit and not follow))

    if follow:
        # Enter押下のみを expect_popup でラップ（typing中の長いフォールバックを timeout に含めない）
        async with page.expect_popup(timeout=new_tab_to) as popup_info:
            await locator.press("Enter")
        new_page = await popup_info.value
        new_idx = await _switch_to_new_page(new_page, page.context)
        return [types.TextContent(
            type="text",
            text=f"Typed into {label}: {text}, submitted, switched to new tab [{new_idx}]: {new_page.url}",
        )]
    return [types.TextContent(type="text", text=f"Typed into {label}: {text}")]


async def _handle_browser_take_screenshot(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_take_screenshot: スクリーンショットを保存"""
    from datetime import datetime
    _ctx, page = await _ensure_browser()
    filename = args.get("filename", "")
    full_page = args.get("full_page", False)

    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"screenshot_{ts}.png"

    filepath = _safe_artifact_path(filename)
    await page.screenshot(path=str(filepath), full_page=full_page)
    return [types.TextContent(type="text", text=f"Screenshot saved: {filepath}")]


async def _handle_browser_press_key(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_press_key: キーを押す（follow_new_tab=true で新タブ追従）"""
    key = args["key"]
    follow = args.get("follow_new_tab", False)
    new_tab_to = args.get("new_tab_timeout", 5000)
    _ctx, page = await _ensure_browser()

    if follow:
        async with page.expect_popup(timeout=new_tab_to) as popup_info:
            await page.keyboard.press(key)
        new_page = await popup_info.value
        new_idx = await _switch_to_new_page(new_page, page.context)
        return [types.TextContent(
            type="text",
            text=f"Pressed key: {key}, switched to new tab [{new_idx}]: {new_page.url}",
        )]
    await page.keyboard.press(key)
    return [types.TextContent(type="text", text=f"Pressed key: {key}")]


async def _handle_browser_evaluate(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_evaluate: JavaScriptを実行"""
    expression = args["expression"]
    _ctx, page = await _ensure_browser()
    result = await page.evaluate(expression)
    result_str = json.dumps(result, ensure_ascii=False, default=str) if result is not None else "undefined"
    if len(result_str) > 15000:
        result_str = result_str[:15000] + "\n... (truncated)"
    return [types.TextContent(type="text", text=f"Result: {result_str}")]


async def _handle_browser_wait_for(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_wait_for: selector/text/timeのいずれかで待機"""
    selector = args.get("selector")
    text = args.get("text")
    time_ms = args.get("time")
    timeout = args.get("timeout", ELEMENT_WAIT_TIMEOUT)

    specified = sum(x is not None for x in (selector, text, time_ms))
    if specified == 0:
        raise ValueError("selector / text / time のいずれか1つを指定してください")
    if specified > 1:
        raise ValueError("selector / text / time は同時に1つだけ指定できます")

    _ctx, page = await _ensure_browser()

    if time_ms is not None:
        await page.wait_for_timeout(int(time_ms))
        return [types.TextContent(type="text", text=f"Slept {time_ms}ms")]

    if text is not None:
        # ページ内に指定テキストが出現するまで待機
        escaped = text.replace('"', '\\"')
        await page.wait_for_function(
            f'document.body && document.body.innerText.includes("{escaped}")',
            timeout=timeout,
        )
        return [types.TextContent(type="text", text=f"Text found: {text}")]

    await page.wait_for_selector(selector, timeout=timeout)
    return [types.TextContent(type="text", text=f"Element found: {selector}")]


async def _handle_browser_select_option(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_select_option: ドロップダウンの値を選択"""
    selector = args["selector"]
    value = args["value"]
    _ctx, page = await _ensure_browser()
    # valueで試し、ダメならlabelで試す
    try:
        await page.select_option(selector, value=value, timeout=ELEMENT_WAIT_TIMEOUT)
    except Exception:
        await page.select_option(selector, label=value, timeout=ELEMENT_WAIT_TIMEOUT)
    return [types.TextContent(type="text", text=f"Selected '{value}' in {selector}")]


async def _handle_browser_navigate_back(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_navigate_back: 戻る"""
    _ctx, page = await _ensure_browser()
    await page.go_back(timeout=PAGE_LOAD_TIMEOUT)
    title = await page.title()
    return [types.TextContent(type="text", text=f"Navigated back. Title: {title}")]


async def _handle_browser_navigate_forward(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_navigate_forward: 進む"""
    _ctx, page = await _ensure_browser()
    await page.go_forward(timeout=PAGE_LOAD_TIMEOUT)
    title = await page.title()
    return [types.TextContent(type="text", text=f"Navigated forward. Title: {title}")]


async def _handle_browser_resize(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_resize: ビューポート変更"""
    width = int(args["width"])
    height = int(args["height"])
    _ctx, page = await _ensure_browser()
    await page.set_viewport_size({"width": width, "height": height})
    return [types.TextContent(type="text", text=f"Viewport resized to {width}x{height}")]


async def _handle_browser_console_messages(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_console_messages: コンソールログ・JSエラー取得"""
    filter_str = (args.get("filter") or "").strip()
    limit = int(args.get("limit", 100))
    do_clear = bool(args.get("clear", False))

    # 起動だけしておく（リスナーを必ず登録するため）
    await _ensure_browser()

    if filter_str:
        wanted = {t.strip() for t in filter_str.split(",") if t.strip()}
    else:
        wanted = None

    items = list(_console_buffer)
    if wanted is not None:
        items = [m for m in items if m["type"] in wanted]

    items = items[-limit:]

    if do_clear:
        _console_buffer.clear()

    if not items:
        return [types.TextContent(type="text", text="(no console messages)")]

    lines = [f"Console messages ({len(items)} of {len(_console_buffer)} buffered):"]
    for m in items:
        ts_str = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
        loc = ""
        if m.get("url"):
            loc = f" @ {m['url']}:{m.get('line', 0)}"
        lines.append(f"[{ts_str}] {m['type'].upper()}: {m['text']}{loc}")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_browser_mouse_xy(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_mouse_xy: 座標指定マウス操作"""
    action = args.get("action", "click")
    x = float(args["x"])
    y = float(args["y"])
    button = args.get("button", "left")
    steps = int(args.get("steps", 1))

    _ctx, page = await _ensure_browser()
    mouse = page.mouse

    if action == "move":
        await mouse.move(x, y, steps=steps)
        return [types.TextContent(type="text", text=f"Mouse moved to ({x}, {y})")]
    if action == "click":
        await mouse.click(x, y, button=button)
        return [types.TextContent(type="text", text=f"Mouse {button}-clicked at ({x}, {y})")]
    if action == "double_click":
        await mouse.dblclick(x, y, button=button)
        return [types.TextContent(type="text", text=f"Mouse double-clicked at ({x}, {y})")]
    if action == "down":
        await mouse.move(x, y, steps=steps)
        await mouse.down(button=button)
        return [types.TextContent(type="text", text=f"Mouse down at ({x}, {y})")]
    if action == "up":
        await mouse.move(x, y, steps=steps)
        await mouse.up(button=button)
        return [types.TextContent(type="text", text=f"Mouse up at ({x}, {y})")]

    raise ValueError(f"Unknown action: {action}")


async def _handle_browser_pdf_save(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_pdf_save: ページをPDFで保存（CDP経由）"""
    from datetime import datetime
    import base64

    _ctx, page = await _ensure_browser()
    filename = args.get("filename", "")
    paper_format = args.get("format", "A4")
    landscape = bool(args.get("landscape", False))
    print_background = bool(args.get("print_background", True))

    if not filename:
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        filename = f"page_{ts}.pdf"
    if not filename.lower().endswith(".pdf"):
        filename += ".pdf"

    filepath = _safe_artifact_path(filename)

    # 用紙サイズ（インチ）対応表
    paper_sizes = {
        "A4": (8.27, 11.69),
        "A3": (11.69, 16.54),
        "A5": (5.83, 8.27),
        "Letter": (8.5, 11.0),
        "Legal": (8.5, 14.0),
        "Tabloid": (11.0, 17.0),
    }
    width_in, height_in = paper_sizes.get(paper_format, paper_sizes["A4"])

    # CDP経由でPDF生成（headful Chromeでも動く）
    cdp = await _ctx.new_cdp_session(page)
    try:
        result = await cdp.send("Page.printToPDF", {
            "landscape": landscape,
            "printBackground": print_background,
            "paperWidth": width_in,
            "paperHeight": height_in,
        })
    finally:
        await cdp.detach()

    pdf_bytes = base64.b64decode(result["data"])
    with open(filepath, "wb") as f:
        f.write(pdf_bytes)

    return [types.TextContent(type="text", text=f"PDF saved: {filepath} ({len(pdf_bytes):,} bytes)")]


async def _handle_browser_drag_drop(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_drag_drop: D&D操作"""
    source = args["source"]
    target = args["target"]
    _ctx, page = await _ensure_browser()
    src_locator = page.locator(source).first
    dst_locator = page.locator(target).first
    await src_locator.drag_to(dst_locator, timeout=ELEMENT_WAIT_TIMEOUT)
    return [types.TextContent(type="text", text=f"Dragged: {source} -> {target}")]


async def _handle_browser_file_upload(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_file_upload: input[type=file]にファイル設定"""
    selector = args["selector"]
    files_str = args["files"]
    file_paths = [p.strip() for p in files_str.split(",") if p.strip()]

    if not file_paths:
        raise ValueError("files が空です")

    missing = [p for p in file_paths if not os.path.isfile(p)]
    if missing:
        raise FileNotFoundError(f"ファイルが見つかりません: {', '.join(missing)}")

    _ctx, page = await _ensure_browser()
    await page.locator(selector).first.set_input_files(file_paths, timeout=ELEMENT_WAIT_TIMEOUT)

    return [types.TextContent(type="text", text=f"Uploaded {len(file_paths)} file(s) to {selector}: {', '.join(file_paths)}")]


async def _handle_browser_hover(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_hover: 要素にホバー"""
    selector = args["selector"]
    _ctx, page = await _ensure_browser()

    # まずテキストで検索、ダメならCSSセレクタとして試す
    try:
        locator = page.get_by_text(selector, exact=False)
        count = await locator.count()
        if count > 0:
            await locator.first.hover(timeout=ELEMENT_WAIT_TIMEOUT)
            return [types.TextContent(type="text", text=f"Hovered text: {selector}")]
    except Exception:
        pass

    await page.locator(selector).first.hover(timeout=ELEMENT_WAIT_TIMEOUT)
    return [types.TextContent(type="text", text=f"Hovered selector: {selector}")]


async def _handle_browser_handle_dialog(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_handle_dialog: ダイアログ自動応答ポリシー設定"""
    action = args.get("action", "manual")
    prompt_text = args.get("prompt_text", "") or ""

    if action not in ("accept", "dismiss", "manual"):
        raise ValueError(f"Unknown action: {action}")

    # 起動＆既存ページにリスナー登録（_ensure_browserが面倒見る）
    await _ensure_browser()

    _dialog_policy["action"] = action
    _dialog_policy["prompt_text"] = prompt_text

    lines = [f"Dialog policy: action={action}"]
    if action == "accept" and prompt_text:
        lines.append(f"  prompt_text='{prompt_text}'")

    if _dialog_log:
        lines.append("")
        lines.append(f"Recent dialog log ({len(_dialog_log)}):")
        for entry in list(_dialog_log)[-10:]:
            ts_str = time.strftime("%H:%M:%S", time.localtime(entry["ts"]))
            lines.append(f"[{ts_str}] {entry['type']} '{entry['message']}' -> {entry['action']}")

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_browser_network_requests(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_network_requests: ネットワーク通信取得"""
    url_contains = (args.get("url_contains") or "").strip()
    method_filter = (args.get("method") or "").strip().upper()
    rtype_filter = (args.get("resource_type") or "").strip().lower()
    phase_filter = (args.get("phase") or "").strip()
    status_min = int(args.get("status_min", 0))
    limit = int(args.get("limit", 100))
    do_clear = bool(args.get("clear", False))

    await _ensure_browser()

    methods = {m.strip() for m in method_filter.split(",") if m.strip()} if method_filter else None
    rtypes = {t.strip() for t in rtype_filter.split(",") if t.strip()} if rtype_filter else None

    items = list(_network_buffer)

    if url_contains:
        items = [m for m in items if url_contains in m["url"]]
    if methods:
        items = [m for m in items if m["method"] in methods]
    if rtypes:
        items = [m for m in items if m["resource_type"] in rtypes]
    if phase_filter:
        items = [m for m in items if m["phase"] == phase_filter]
    if status_min > 0:
        items = [m for m in items if (m.get("status") or 0) >= status_min]

    items = items[-limit:]

    if do_clear:
        _network_buffer.clear()

    if not items:
        return [types.TextContent(type="text", text="(no network records)")]

    lines = [f"Network records ({len(items)} of {len(_network_buffer)} buffered):"]
    for m in items:
        ts_str = time.strftime("%H:%M:%S", time.localtime(m["ts"]))
        status = m.get("status")
        status_str = f" [{status}]" if status else ""
        err = m.get("error", "")
        err_str = f" ERROR={err}" if err else ""
        lines.append(
            f"[{ts_str}] {m['phase'].upper()} {m['method']}{status_str} "
            f"({m['resource_type']}) {m['url']}{err_str}"
        )

    return [types.TextContent(type="text", text="\n".join(lines))]


async def _handle_browser_scroll(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_scroll: スクロール"""
    direction = args.get("direction", "down")
    amount = int(args.get("amount", 500))
    selector = args.get("selector")

    _ctx, page = await _ensure_browser()

    if direction == "to":
        if not selector:
            raise ValueError("direction='to'にはselectorが必要です")
        locator = page.locator(selector).first
        await locator.scroll_into_view_if_needed(timeout=ELEMENT_WAIT_TIMEOUT)
        return [types.TextContent(type="text", text=f"Scrolled to: {selector}")]

    if direction == "top":
        await page.evaluate("window.scrollTo({top: 0, behavior: 'instant'})")
        return [types.TextContent(type="text", text="Scrolled to top")]

    if direction == "bottom":
        await page.evaluate("window.scrollTo({top: document.body.scrollHeight, behavior: 'instant'})")
        return [types.TextContent(type="text", text="Scrolled to bottom")]

    if direction == "up":
        await page.evaluate(f"window.scrollBy(0, -{amount})")
        return [types.TextContent(type="text", text=f"Scrolled up {amount}px")]

    if direction == "down":
        await page.evaluate(f"window.scrollBy(0, {amount})")
        return [types.TextContent(type="text", text=f"Scrolled down {amount}px")]

    raise ValueError(f"Unknown direction: {direction}")


async def _handle_browser_tabs(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_tabs: list/switch/new/close"""
    global _page
    ctx, page = await _ensure_browser()

    action = args.get("action", "list")
    index = args.get("index", -1)
    url = args.get("url", "")
    legacy_switch_to = args.get("switch_to", -1)

    # 後方互換: switch_toが指定されていればswitchとして扱う
    if legacy_switch_to >= 0:
        action = "switch"
        index = legacy_switch_to

    pages = ctx.pages
    messages: list[str] = []

    if action == "new":
        new_page = await ctx.new_page()
        if url:
            await new_page.goto(url, timeout=PAGE_LOAD_TIMEOUT)
        _page = new_page
        await _page.bring_to_front()
        pages = ctx.pages
        new_idx = pages.index(new_page)
        messages.append(f"Opened new tab [{new_idx}]: {new_page.url}")

    elif action == "close":
        target_idx = index if index >= 0 else pages.index(_page)
        if target_idx < 0 or target_idx >= len(pages):
            raise ValueError(f"Invalid tab index: {target_idx}")
        target_page = pages[target_idx]
        was_active = target_page == _page
        await target_page.close()
        pages = ctx.pages
        if was_active:
            if pages:
                _page = pages[-1]
                await _page.bring_to_front()
            else:
                _page = None  # type: ignore[assignment]
        messages.append(f"Closed tab [{target_idx}]")

    elif action == "switch":
        if index < 0 or index >= len(pages):
            raise ValueError(f"Invalid tab index: {index}")
        _page = pages[index]
        await _page.bring_to_front()
        messages.append(f"Switched to tab [{index}]: {_page.url}")

    elif action == "latest":
        if not pages:
            raise ValueError("No tabs open")
        _page = pages[-1]
        await _page.bring_to_front()
        new_idx = len(pages) - 1
        messages.append(f"Switched to latest tab [{new_idx}]: {_page.url}")

    elif action == "wait_close":
        # OAuthポップアップ等の閉鎖待ち。index省略時は末尾タブ（ポップアップは通常末尾に入るため）。
        # 呼び出し時点で既に対象タブが閉じている（=ctx.pagesに無い）場合は「成功」として扱う。
        # ポップアップが wait_close を呼ぶ前に閉じてしまうケース（即時OAuthリダイレクト等）の救済。
        timeout_ms = args.get("timeout", 60000)
        target_idx = index if index >= 0 else len(pages) - 1
        if target_idx < 0 or target_idx >= len(pages):
            # 既に閉じている可能性が高い。アクティブタブを末尾に切り替えて成功扱い
            if pages:
                _page = pages[-1]
                await _page.bring_to_front()
                messages.append(
                    f"Tab [{target_idx}] not found (likely already closed before wait_close). "
                    f"Active tab: [{len(pages)-1}]: {_page.url}"
                )
            else:
                _page = None  # type: ignore[assignment]
                messages.append(f"Tab [{target_idx}] not found. All tabs closed")
        else:
            target_page = pages[target_idx]
            if not target_page.is_closed():
                try:
                    # Playwright標準の wait_for_event を使う（自前 asyncio.Event より簡潔）
                    await target_page.wait_for_event("close", timeout=timeout_ms)
                except Exception as e:
                    raise RuntimeError(
                        f"Tab [{target_idx}] did not close within {timeout_ms}ms (url: {target_page.url})"
                    ) from e
            # 閉じた後はアクティブタブを末尾に切り替え
            pages = ctx.pages
            if pages:
                _page = pages[-1]
                await _page.bring_to_front()
                messages.append(f"Tab [{target_idx}] closed. Active tab: [{len(pages)-1}]: {_page.url}")
            else:
                _page = None  # type: ignore[assignment]
                messages.append(f"Tab [{target_idx}] closed. All tabs closed")

    elif action != "list":
        raise ValueError(f"Unknown action: {action}")

    pages = ctx.pages
    tab_list = []
    for i, p in enumerate(pages):
        marker = " [active]" if p == _page else ""
        tab_list.append(f"  [{i}] {p.url}{marker}")

    body = f"Tabs ({len(pages)}):\n" + "\n".join(tab_list)
    if messages:
        body += "\n\n" + "\n".join(messages)
    return [types.TextContent(type="text", text=body)]


async def _wait_for_perplexity_answer(page, timeout: int = PERPLEXITY_TIMEOUT) -> str:
    """Perplexityの回答完了を待ち、回答テキストを返す"""
    start = time.time()
    deadline = start + timeout / 1000

    # 最低5秒は待つ
    await asyncio.sleep(5)

    while time.time() < deadline:
        # 完了の確実な指標: 「フォローアップ」テキストまたは「さらに質問」入力欄
        try:
            snapshot_text = await page.inner_text("body", timeout=3000)
            if "フォローアップ" in snapshot_text or "さらに質問" in snapshot_text:
                break
            if "Follow-up" in snapshot_text or "follow up" in snapshot_text.lower():
                break
        except Exception:
            pass

        # ソースボタン出現も完了サイン（「N 件のソース」）
        try:
            source_btn = page.locator('button:has-text("件のソース"), button:has-text("sources")')
            if await source_btn.count() > 0:
                break
        except Exception:
            pass

        await asyncio.sleep(2)

    # ページ安定化を待つ
    await asyncio.sleep(2)

    # 回答テキストを取得: tabpanel "回答" から
    try:
        answer_panel = page.locator('[role="tabpanel"]')
        if await answer_panel.count() > 0:
            text = await answer_panel.first.inner_text()
            if text.strip():
                return text.strip()
    except Exception:
        pass

    # フォールバック: アクセシビリティツリーのtabpanelセクション
    try:
        # メインコンテンツ領域を探す
        main_area = page.locator('main, [role="main"], article')
        if await main_area.count() > 0:
            text = await main_area.first.inner_text()
            if text.strip():
                return text.strip()
    except Exception:
        pass

    # 最終フォールバック: body全体
    text = await page.inner_text("body")
    if len(text) > 15000:
        text = text[:15000] + "\n... (truncated)"
    return text


async def _check_perplexity_rate_limit(page) -> str | None:
    """Perplexity無料プランのレート制限を検出。制限中ならメッセージを返す。"""
    try:
        body_text = await page.inner_text("body", timeout=3000)
        # 実際に表示される制限メッセージ
        rate_limit_phrases = [
            "無料プレビューの上限に達しました",
            "上限に達しました",
            "基本検索を使用しています",
            "limit reached",
            "rate limit",
            "too many requests",
            "upgrade to pro",
        ]
        lower = body_text.lower()
        body_text_combined = body_text + lower  # 日本語+英語両方チェック
        for phrase in rate_limit_phrases:
            if phrase in body_text_combined:
                # レート制限ボタンのテキストを取得
                try:
                    limit_btn = page.locator('button:has-text("上限"), button:has-text("limit"), button:has-text("アップグレード")')
                    if await limit_btn.count() > 0:
                        msg = await limit_btn.first.inner_text()
                        return f"RATE_LIMITED: {msg[:300]}"
                except Exception:
                    pass
                return f"RATE_LIMITED: {phrase}"
    except Exception:
        pass
    return None


async def _handle_perplexity_search(args: dict[str, Any]) -> list[types.TextContent]:
    """perplexity_search: Perplexity検索ワークフロー"""
    query = args["query"]
    space_url = args.get("space_url", "")
    _ctx, page = await _ensure_browser()

    # 1. Perplexityに移動
    target_url = space_url if space_url else "https://www.perplexity.ai/"
    await page.goto(target_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # 1.5. レート制限チェック（入力前）
    rate_msg = await _check_perplexity_rate_limit(page)
    if rate_msg:
        return [types.TextContent(
            type="text",
            text=f"Perplexity rate limit detected (before query).\n{rate_msg}\n\n"
                 f"Suggestion: Use browser_navigate + browser_type to manually retry later, "
                 f"or use WebSearch as fallback.",
        )]

    # 2. 検索ボックスにクエリを入力
    input_box = page.get_by_role("textbox").first
    await input_box.wait_for(timeout=ELEMENT_WAIT_TIMEOUT)
    await _fill_or_type(input_box, query, submit=True)

    # 2.5. 送信後のレート制限チェック
    await asyncio.sleep(3)
    rate_msg = await _check_perplexity_rate_limit(page)
    if rate_msg:
        return [types.TextContent(
            type="text",
            text=f"Perplexity rate limit detected (after submit).\n{rate_msg}\n\n"
                 f"Query was: {query}\n"
                 f"Suggestion: Wait a few minutes and retry, or use WebSearch as fallback.",
        )]

    # 3. 回答を待つ
    answer = await _wait_for_perplexity_answer(page)

    return [types.TextContent(
        type="text",
        text=f"Query: {query}\n\n{answer}",
    )]


async def _wait_for_chatgpt_answer(page, timeout: int = CHATGPT_TIMEOUT) -> str:
    """ChatGPTの回答完了を待ち、回答テキストを返す"""
    start = time.time()
    deadline = start + timeout / 1000

    # 最低5秒は待つ（短い回答でも生成開始を待つ）
    await asyncio.sleep(5)

    while time.time() < deadline:
        try:
            # 「Stop generating」ボタンが存在する = まだ生成中
            stop_btn = page.locator('button[aria-label*="Stop"], button:has-text("Stop generating"), button:has-text("停止")')
            if await stop_btn.count() > 0:
                await asyncio.sleep(2)
                continue
        except Exception:
            pass

        try:
            # textboxが編集可能 = 生成完了
            input_box = page.get_by_role("textbox").first
            if await input_box.count() > 0:
                is_editable = await input_box.is_editable()
                if is_editable:
                    break
        except Exception:
            pass

        await asyncio.sleep(2)

    # ページ安定化を待つ
    await asyncio.sleep(2)

    # 最後の回答を取得（複数セレクタ試行）
    selectors = [
        '[data-message-author-role="assistant"]',
        '.markdown.prose',
        'article [data-message-author-role="assistant"]',
    ]
    for sel in selectors:
        try:
            answers = page.locator(sel)
            count = await answers.count()
            if count > 0:
                text = await answers.last.inner_text()
                if text.strip():
                    return text.strip()
        except Exception:
            pass

    # アクセシビリティツリーからのフォールバック:
    # "ChatGPT:" heading の後のコンテンツを取得
    try:
        snapshot = await page.accessibility.snapshot()
        if snapshot:
            return _extract_chatgpt_answer_from_tree(snapshot)
    except Exception:
        pass

    # 最終フォールバック: bodyテキスト
    text = await page.inner_text("body")
    if len(text) > 15000:
        text = text[:15000] + "\n... (truncated)"
    return text


def _extract_chatgpt_answer_from_tree(node: dict, found_marker: bool = False) -> str:
    """アクセシビリティツリーから最後のChatGPT回答を抽出"""
    children = node.get("children", [])
    # ChatGPT: headingの後のコンテンツを探す
    last_answer = ""
    capture = False
    for child in children:
        name = child.get("name", "")
        role = child.get("role", "")
        if role == "heading" and "ChatGPT" in name:
            capture = True
            last_answer = ""
            continue
        if role == "heading" and capture:
            capture = False
        if capture:
            if child.get("children"):
                for sub in child["children"]:
                    if sub.get("name"):
                        last_answer += sub["name"] + "\n"
            elif name:
                last_answer += name + "\n"
        # 再帰的に子要素も探す
        sub_result = _extract_chatgpt_answer_from_tree(child)
        if sub_result:
            last_answer = sub_result
    return last_answer.strip()


async def _handle_chatgpt_ask(args: dict[str, Any]) -> list[types.TextContent]:
    """chatgpt_ask: ChatGPT質問ワークフロー"""
    question = args["question"]
    project_url = args.get("project_url", "")
    _ctx, page = await _ensure_browser()

    # 1. ChatGPTに移動。
    # 既定で開くChatGPT URLは MARURU_CHATGPT_URL で上書き可能。
    # 自分専用Projectを使いたい場合は環境変数で指定する（個人URLをコードに書かないため）。
    default_project = os.environ.get(
        "MARURU_CHATGPT_URL",
        "https://chatgpt.com/",
    )
    target_url = project_url if project_url else default_project
    await page.goto(target_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # 2. 質問を入力（contenteditable対応: get_by_role → fill or type）
    input_box = page.get_by_role("textbox").first
    await input_box.wait_for(timeout=ELEMENT_WAIT_TIMEOUT)
    try:
        await input_box.fill(question, timeout=5000)
    except Exception:
        await input_box.click(timeout=5000)
        await input_box.press_sequentially(question, delay=30)
    await asyncio.sleep(0.5)

    # 3. 送信
    send_btn = page.locator('button[data-testid="send-button"], button[aria-label*="Send"]')
    if await send_btn.count() > 0:
        await send_btn.first.click()
    else:
        await input_box.press("Enter")

    # 4. 回答を待つ
    answer = await _wait_for_chatgpt_answer(page)

    return [types.TextContent(
        type="text",
        text=f"Question: {question}\n\n{answer}",
    )]


# --- Phase 4 ハンドラ ---

async def _handle_cookies_get(args: dict[str, Any]) -> list[types.TextContent]:
    """cookies_get: ブラウザCookieを取得"""
    url = (args.get("url") or "").strip()
    name_filter = (args.get("name") or "").strip()
    ctx, _page = await _ensure_browser()

    cookies = await ctx.cookies([url] if url else None)
    if name_filter:
        cookies = [c for c in cookies if c.get("name") == name_filter]

    payload = json.dumps(cookies, ensure_ascii=False, indent=2, default=str)
    if len(payload) > 15000:
        payload = payload[:15000] + "\n... (truncated)"
    return [types.TextContent(type="text", text=f"{len(cookies)} cookie(s):\n{payload}")]


async def _handle_cookies_set(args: dict[str, Any]) -> list[types.TextContent]:
    """cookies_set: Cookie追加/上書き"""
    cookies = args.get("cookies")
    if not isinstance(cookies, list) or not cookies:
        raise ValueError("cookies は1件以上の辞書配列で指定してください")
    for c in cookies:
        if not isinstance(c, dict) or not c.get("name") or "value" not in c:
            raise ValueError("各Cookieは name と value が必須です")
        if not c.get("url") and not c.get("domain"):
            raise ValueError(f"Cookie '{c.get('name')}' に url または domain が必要です")

    ctx, _page = await _ensure_browser()
    await ctx.add_cookies(cookies)
    return [types.TextContent(type="text", text=f"Set {len(cookies)} cookie(s)")]


async def _handle_local_storage_get(args: dict[str, Any]) -> list[types.TextContent]:
    """local_storage_get: localStorage取得"""
    key = (args.get("key") or "").strip()
    _ctx, page = await _ensure_browser()

    if key:
        value = await page.evaluate("(k) => window.localStorage.getItem(k)", key)
        if value is None:
            return [types.TextContent(type="text", text=f"Key '{key}' not found")]
        return [types.TextContent(type="text", text=f"{key} = {value}")]

    dump = await page.evaluate(
        "() => { const o={}; for (let i=0;i<localStorage.length;i++){"
        " const k=localStorage.key(i); o[k]=localStorage.getItem(k);} return o; }"
    )
    payload = json.dumps(dump, ensure_ascii=False, indent=2, default=str)
    if len(payload) > 15000:
        payload = payload[:15000] + "\n... (truncated)"
    return [types.TextContent(type="text", text=f"{len(dump)} entries:\n{payload}")]


async def _handle_local_storage_set(args: dict[str, Any]) -> list[types.TextContent]:
    """local_storage_set: localStorage 保存/削除"""
    key = args["key"]
    value = args.get("value", "")
    remove = bool(args.get("remove", False))
    _ctx, page = await _ensure_browser()

    if remove:
        await page.evaluate("(k) => window.localStorage.removeItem(k)", key)
        return [types.TextContent(type="text", text=f"Removed '{key}'")]

    await page.evaluate(
        "([k, v]) => window.localStorage.setItem(k, v)", [key, str(value)]
    )
    return [types.TextContent(type="text", text=f"Set {key} = {value}")]


async def _handle_wait_for_navigation(args: dict[str, Any]) -> list[types.TextContent]:
    """wait_for_navigation: ページ遷移完了を待つ"""
    url_pattern = (args.get("url_pattern") or "").strip()
    state = args.get("state", "load")
    timeout = int(args.get("timeout", 30000))
    _ctx, page = await _ensure_browser()

    if url_pattern:
        # 部分一致を満たすURLになるまで待機（lambda predicate）
        await page.wait_for_url(lambda u: url_pattern in u, timeout=timeout)

    await page.wait_for_load_state(state, timeout=timeout)
    return [types.TextContent(
        type="text",
        text=f"Navigation done. URL: {page.url} (state={state})",
    )]


async def _handle_x_search(args: dict[str, Any]) -> list[types.TextContent]:
    """x_search: X(Twitter)検索ヘルパー"""
    from urllib.parse import quote_plus

    query = args["query"]
    mode = args.get("mode", "latest")
    scrolls = int(args.get("scrolls", 5))
    limit = int(args.get("limit", 50))

    f_param = "&f=live" if mode == "latest" else ""
    url = f"https://x.com/search?q={quote_plus(query)}{f_param}"

    _ctx, page = await _ensure_browser()
    await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector('article[data-testid="tweet"]', timeout=15000)
    except Exception:
        return [types.TextContent(
            type="text",
            text=f"X search loaded but no tweets found (login required or zero hits): {url}",
        )]

    # スクロールして遅延読み込み
    for _ in range(max(0, scrolls)):
        await page.evaluate("window.scrollBy(0, 1500)")
        await asyncio.sleep(1.2)

    tweets = await page.evaluate(
        """
        (max) => {
          const arts = document.querySelectorAll('article[data-testid=\"tweet\"]');
          const out = [];
          for (const a of arts) {
            const time = a.querySelector('time');
            const author = a.querySelector('div[data-testid=\"User-Name\"]');
            const text = a.querySelector('div[data-testid=\"tweetText\"]');
            const link = time && time.parentElement && time.parentElement.tagName === 'A'
              ? time.parentElement.href : null;
            out.push({
              ts: time ? time.dateTime : null,
              author: author ? author.innerText.replace(/\\n/g, ' ').slice(0, 200) : null,
              text: text ? text.innerText.slice(0, 600) : a.innerText.slice(0, 600),
              url: link,
            });
            if (out.length >= max) break;
          }
          return out;
        }
        """,
        limit,
    )

    payload = json.dumps(tweets, ensure_ascii=False, indent=2, default=str)
    if len(payload) > 15000:
        payload = payload[:15000] + "\n... (truncated)"
    return [types.TextContent(
        type="text",
        text=f"{len(tweets)} tweet(s) for '{query}' ({mode}):\n{payload}",
    )]


async def _handle_google_search(args: dict[str, Any]) -> list[types.TextContent]:
    """google_search: Google検索結果抽出"""
    from urllib.parse import quote_plus

    query = args["query"]
    limit = int(args.get("limit", 10))
    lang = args.get("lang", "ja")

    url = f"https://www.google.com/search?q={quote_plus(query)}&hl={lang}"
    _ctx, page = await _ensure_browser()
    await page.goto(url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    try:
        await page.wait_for_selector("#search, #rcnt, #main", timeout=10000)
    except Exception:
        pass

    results = await page.evaluate(
        """
        (max) => {
          const out = [];
          const seen = new Set();
          const anchors = document.querySelectorAll('#search a h3, #rso a h3');
          for (const h3 of anchors) {
            const a = h3.closest('a');
            if (!a || !a.href || seen.has(a.href)) continue;
            seen.add(a.href);
            const block = a.closest('div[data-snc], div.MjjYud, div.g') || a.parentElement;
            const snip = block ? (block.querySelector('[data-sncf=\"1\"], .VwiC3b, .lEBKkf')?.innerText || '') : '';
            out.push({ title: h3.innerText, url: a.href, snippet: snip.slice(0, 400) });
            if (out.length >= max) break;
          }
          // AI Overview (best-effort, classes change frequently)
          const ai = document.querySelector('[data-attrid*=\"AIOverview\"], div[jsname][data-async-context*=\"sge\"]');
          return { ai_overview: ai ? ai.innerText.slice(0, 1500) : null, results: out };
        }
        """,
        limit,
    )

    payload = json.dumps(results, ensure_ascii=False, indent=2, default=str)
    if len(payload) > 15000:
        payload = payload[:15000] + "\n... (truncated)"
    return [types.TextContent(
        type="text",
        text=f"Google '{query}' ({len(results.get('results', []))} hits):\n{payload}",
    )]


async def _handle_generic_form_fill(args: dict[str, Any]) -> list[types.TextContent]:
    """generic_form_fill: フィールド名推定でフォーム自動入力"""
    data = args.get("data") or {}
    submit = bool(args.get("submit", False))
    if not isinstance(data, dict) or not data:
        raise ValueError("data は1件以上の {field: value} 辞書で指定してください")

    _ctx, page = await _ensure_browser()
    filled: list[str] = []
    skipped: list[str] = []

    for field_name, value in data.items():
        if value is None:
            continue
        text_value = str(value)
        # 推定: name → id → placeholder → aria-label → label[for] → ラベルテキスト隣接
        candidates = [
            f'input[name="{field_name}"]',
            f'textarea[name="{field_name}"]',
            f'select[name="{field_name}"]',
            f'input[id="{field_name}"]',
            f'textarea[id="{field_name}"]',
            f'input[placeholder*="{field_name}" i]',
            f'textarea[placeholder*="{field_name}" i]',
            f'input[aria-label*="{field_name}" i]',
            f'textarea[aria-label*="{field_name}" i]',
        ]
        target = None
        for sel in candidates:
            loc = page.locator(sel).first
            try:
                if await loc.count() > 0:
                    target = loc
                    break
            except Exception:
                continue

        if target is None:
            # label[for=...] または get_by_label のフォールバック
            try:
                lab = page.get_by_label(field_name).first
                if await lab.count() > 0:
                    target = lab
            except Exception:
                pass

        if target is None:
            skipped.append(field_name)
            continue

        try:
            tag = (await target.evaluate("el => el.tagName")).lower()
            if tag == "select":
                await target.select_option(text_value)
            else:
                await target.fill(text_value)
            filled.append(field_name)
        except Exception as e:
            skipped.append(f"{field_name} ({e})")

    submitted = False
    if submit:
        for sel in [
            'button[type="submit"]',
            'input[type="submit"]',
            'button:has-text("送信")',
            'button:has-text("Submit")',
            'button:has-text("検索")',
        ]:
            btn = page.locator(sel).first
            try:
                if await btn.count() > 0:
                    await btn.click()
                    submitted = True
                    break
            except Exception:
                continue

    return [types.TextContent(
        type="text",
        text=(
            f"Filled: {filled}\n"
            f"Skipped: {skipped}\n"
            f"Submitted: {submitted}"
        ),
    )]


# 既定のGemini Gem / Grok Project URL。自分専用のGem/Projectを使う場合は
# 環境変数 MARURU_GEMINI_URL / MARURU_GROK_URL で上書きする（個人URLをコードに
# 書かないため）。指定が無ければ各サービスのトップページにフォールバック。
DEFAULT_GEMINI_GEM = os.environ.get("MARURU_GEMINI_URL", "https://gemini.google.com/")
DEFAULT_GROK_PROJECT = os.environ.get("MARURU_GROK_URL", "https://grok.com/")


async def _wait_for_gemini_answer(page, timeout: int = GEMINI_TIMEOUT) -> str:
    """Gemini Gemの回答完了を待ち、回答テキストを返す"""
    deadline = time.time() + timeout / 1000

    # 最低5秒は生成開始を待つ
    await asyncio.sleep(5)

    while time.time() < deadline:
        try:
            # 「停止」ボタンが居る = 生成中
            stop_btn = page.locator(
                'button[aria-label*="停止"], button[aria-label*="Stop"], '
                'button:has-text("停止"), button:has-text("Stop")'
            )
            if await stop_btn.count() > 0:
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        # 入力ボックスが編集可能 = 生成完了
        try:
            input_box = page.get_by_role("textbox").first
            if await input_box.count() > 0 and await input_box.is_editable():
                break
        except Exception:
            pass
        await asyncio.sleep(2)

    await asyncio.sleep(2)

    # Geminiは <model-response> 要素または .markdown / message-content
    selectors = [
        "model-response .markdown",
        "model-response",
        "message-content",
        '[data-test-id*="response"]',
    ]
    for sel in selectors:
        try:
            answers = page.locator(sel)
            count = await answers.count()
            if count > 0:
                text = await answers.last.inner_text()
                if text.strip():
                    return text.strip()
        except Exception:
            pass

    text = await page.inner_text("body")
    if len(text) > 15000:
        text = text[:15000] + "\n... (truncated)"
    return text


async def _handle_gemini_ask(args: dict[str, Any]) -> list[types.TextContent]:
    """gemini_ask: Gemini Gem質問ワークフロー"""
    question = args["question"]
    gem_url = (args.get("gem_url") or "").strip() or DEFAULT_GEMINI_GEM
    _ctx, page = await _ensure_browser()

    await page.goto(gem_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Quill エディタ（contenteditable）の textbox に入力
    input_box = page.get_by_role("textbox").first
    await input_box.wait_for(timeout=ELEMENT_WAIT_TIMEOUT)
    try:
        await input_box.fill(question, timeout=5000)
    except Exception:
        await input_box.click(timeout=5000)
        await input_box.press_sequentially(question, delay=20)
    await asyncio.sleep(0.5)

    # 送信
    send_btn = page.locator(
        'button[aria-label*="送信"], button[aria-label*="Send"], '
        'button[mat-icon-button]:has(mat-icon[fonticon="send"])'
    )
    if await send_btn.count() > 0:
        await send_btn.first.click()
    else:
        await input_box.press("Enter")

    answer = await _wait_for_gemini_answer(page)
    return [types.TextContent(type="text", text=f"Question: {question}\n\n{answer}")]


async def _wait_for_grok_answer(page, timeout: int = GROK_TIMEOUT) -> str:
    """Grok Projectの回答完了を待ち、回答テキストを返す"""
    deadline = time.time() + timeout / 1000

    await asyncio.sleep(5)

    while time.time() < deadline:
        try:
            # 停止ボタン: aria-label="Stop generating" / svg with stop icon
            stop_btn = page.locator(
                'button[aria-label*="Stop"], button[aria-label*="停止"], '
                'button[aria-label*="生成"][aria-label*="止"]'
            )
            if await stop_btn.count() > 0:
                await asyncio.sleep(2)
                continue
        except Exception:
            pass
        try:
            # tiptap入力欄が編集可能 = 生成完了
            input_box = page.locator(
                '.tiptap.ProseMirror, div[contenteditable="true"][class*="ProseMirror"]'
            ).first
            if await input_box.count() > 0 and await input_box.is_editable():
                break
        except Exception:
            pass
        await asyncio.sleep(2)

    await asyncio.sleep(2)

    # Grokの回答セレクタ
    selectors = [
        '[data-testid="message-bubble"][data-author="agent"]',
        '[data-testid*="response"]',
        '.response-content-markdown',
        '.message-bubble.agent',
        'div.message-content[data-author="agent"]',
        'article [data-author="agent"]',
    ]
    for sel in selectors:
        try:
            answers = page.locator(sel)
            count = await answers.count()
            if count > 0:
                text = await answers.last.inner_text()
                if text.strip():
                    return text.strip()
        except Exception:
            pass

    # フォールバック: メッセージリストの最後の要素
    try:
        text = await page.locator("main").last.inner_text()
        if text.strip():
            if len(text) > 15000:
                text = text[:15000] + "\n... (truncated)"
            return text.strip()
    except Exception:
        pass

    text = await page.inner_text("body")
    if len(text) > 15000:
        text = text[:15000] + "\n... (truncated)"
    return text


async def _handle_grok_ask(args: dict[str, Any]) -> list[types.TextContent]:
    """grok_ask: Grok Project質問ワークフロー"""
    question = args["question"]
    project_url = (args.get("project_url") or "").strip() or DEFAULT_GROK_PROJECT
    _ctx, page = await _ensure_browser()

    await page.goto(project_url, timeout=PAGE_LOAD_TIMEOUT, wait_until="domcontentloaded")
    await asyncio.sleep(2)

    # Grok の入力欄は tiptap ProseMirror（contenteditable=true、role属性なし）
    input_box = page.locator(
        '.tiptap.ProseMirror, div[contenteditable="true"].tiptap, '
        'div[contenteditable="true"][class*="ProseMirror"]'
    ).first
    await input_box.wait_for(timeout=ELEMENT_WAIT_TIMEOUT)
    await input_box.click(timeout=5000)
    await input_box.press_sequentially(question, delay=15)
    await asyncio.sleep(0.5)

    # 送信ボタン or Enter
    send_btn = page.locator(
        'button[aria-label*="送信"], button[aria-label*="Send"], '
        'button[type="submit"], button:has(svg[class*="send"])'
    )
    if await send_btn.count() > 0:
        await send_btn.first.click()
    else:
        await input_box.press("Enter")

    answer = await _wait_for_grok_answer(page)
    return [types.TextContent(type="text", text=f"Question: {question}\n\n{answer}")]


async def _handle_iframe_evaluate(args: dict[str, Any]) -> list[types.TextContent]:
    """iframe_evaluate: iframe内でJavaScriptを実行 / 指定なしならフレーム一覧"""
    expression = args.get("expression")
    frame_url = args.get("frame_url")
    frame_name = args.get("frame_name")
    frame_selector = args.get("frame_selector")
    frame_index = args.get("frame_index")

    _ctx, page = await _ensure_browser()

    # メインフレームを除く子フレームのみを対象にする
    child_frames = [f for f in page.frames if f.parent_frame is not None]

    no_selector = (
        frame_url is None
        and frame_name is None
        and frame_selector is None
        and frame_index is None
    )

    # 指定がない or expression未指定 → 一覧返却（探索用途）
    if no_selector or expression is None:
        listing = [
            {"index": i, "url": f.url, "name": f.name}
            for i, f in enumerate(child_frames)
        ]
        header = f"Child frames: {len(child_frames)}"
        if expression is not None and no_selector:
            header = (
                "expression指定だけではiframeを特定できません。"
                "frame_url / frame_name / frame_selector / frame_index のいずれかを併用してください。\n"
                + header
            )
        return [types.TextContent(
            type="text",
            text=header + "\n" + json.dumps(listing, ensure_ascii=False, indent=2)
        )]

    # iframe特定
    target_frame = None

    if frame_index is not None:
        if not (0 <= frame_index < len(child_frames)):
            return [types.TextContent(
                type="text",
                text=f"frame_index={frame_index} is out of range (0..{len(child_frames) - 1})"
            )]
        target_frame = child_frames[frame_index]
    elif frame_selector is not None:
        element = await page.query_selector(frame_selector)
        if element is None:
            return [types.TextContent(
                type="text",
                text=f"iframe element not found: {frame_selector}"
            )]
        target_frame = await element.content_frame()
        if target_frame is None:
            return [types.TextContent(
                type="text",
                text=f"element {frame_selector} has no content frame"
            )]
    else:
        matched = []
        for f in child_frames:
            if frame_url is not None and frame_url not in f.url:
                continue
            if frame_name is not None and f.name != frame_name:
                continue
            matched.append(f)
        if not matched:
            return [types.TextContent(
                type="text",
                text=f"No frame matched (frame_url={frame_url!r}, frame_name={frame_name!r})"
            )]
        if len(matched) > 1:
            listing = [{"index": i, "url": f.url, "name": f.name} for i, f in enumerate(matched)]
            return [types.TextContent(
                type="text",
                text=(
                    f"Multiple frames matched ({len(matched)}). frame_indexで絞り込んでください。\n"
                    + json.dumps(listing, ensure_ascii=False, indent=2)
                )
            )]
        target_frame = matched[0]

    # 実行
    result = await target_frame.evaluate(expression)
    result_str = (
        json.dumps(result, ensure_ascii=False, default=str)
        if result is not None else "undefined"
    )
    if len(result_str) > 15000:
        result_str = result_str[:15000] + "\n... (truncated)"
    return [types.TextContent(
        type="text",
        text=f"Frame: {target_frame.url} (name={target_frame.name!r})\nResult: {result_str}"
    )]


# --- 手動記録モード (record / replay) ---

def _safe_recording_path(name: str) -> Path:
    """recording name から安全なファイルパスを返す（path traversal防止）"""
    # 英数字・ハイフン・アンダースコアのみ許可
    safe = "".join(c for c in name if c.isalnum() or c in ("_", "-"))
    if not safe:
        raise ValueError(f"Invalid recording name: {name!r}")
    RECORDINGS_DIR.mkdir(parents=True, exist_ok=True)
    return RECORDINGS_DIR / f"{safe}.json"


async def _handle_browser_record_start(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_record_start: クリック/キー/スクロール記録開始"""
    global _active_recording

    name = args.get("name")
    if not name:
        raise ValueError("name is required")

    variables_template = args.get("variables_template") or []

    if _active_recording is not None:
        return [types.TextContent(
            type="text",
            text=f"既に記録中です: name={_active_recording['name']!r}。先に browser_record_stop を呼んでください。"
        )]

    _ctx, page = await _ensure_browser()

    # 注入実行（既に注入済みなら 'already-recording' が返るが、上の active チェックで普通は来ない）
    inject_result = await page.evaluate(_RECORD_INJECT_JS)

    _active_recording = {
        "name": name,
        "started_at": time.time(),
        "variables_template": variables_template,
        "page_url": page.url,
    }

    return [types.TextContent(
        type="text",
        text=(
            f"Recording started: name={name!r}\n"
            f"  page: {page.url}\n"
            f"  inject: {inject_result}\n"
            f"  variables_template: {variables_template}\n"
            f"  → ブラウザ上で手動操作してください。停止は browser_record_stop。"
        )
    )]


async def _handle_browser_record_stop(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_record_stop: 記録停止 + JSON保存"""
    global _active_recording

    if _active_recording is None:
        return [types.TextContent(type="text", text="記録中ではありません。先に browser_record_start を呼んでください。")]

    _ctx, page = await _ensure_browser()

    actions = await page.evaluate("window.__maruruRecord || []")
    # 記録停止: 注入変数を消す
    await page.evaluate("delete window.__maruruRecord; delete window.__maruruRecordStartedAt;")

    name = _active_recording["name"]
    save_path = _safe_recording_path(name)

    payload = {
        "name": name,
        "created_at": _active_recording["started_at"],
        "stopped_at": time.time(),
        "page_url_at_start": _active_recording["page_url"],
        "page_url_at_stop": page.url,
        "variables_template": _active_recording["variables_template"],
        "actions": actions,
    }
    save_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    # サマリ
    by_type: dict[str, int] = {}
    selectors_sample = []
    for a in actions:
        t = a.get("type", "?")
        by_type[t] = by_type.get(t, 0) + 1
        if t == "click" and len(selectors_sample) < 3:
            sel = (a.get("target") or {}).get("selector")
            if sel:
                selectors_sample.append(sel)

    _active_recording = None

    summary_lines = [
        f"Recording stopped: name={name!r}",
        f"  saved: {save_path}",
        f"  total actions: {len(actions)}",
        f"  by_type: {by_type}",
    ]
    if selectors_sample:
        summary_lines.append(f"  click selectors (first 3): {selectors_sample}")
    return [types.TextContent(type="text", text="\n".join(summary_lines))]


def _apply_variables(action: dict[str, Any], variables: dict[str, Any]) -> dict[str, Any]:
    """action 内の selector 文字列に対して {{var}} 置換を適用した複製を返す"""
    if not variables:
        return action
    new_action = json.loads(json.dumps(action))
    target = new_action.get("target")
    if isinstance(target, dict):
        sel = target.get("selector")
        if isinstance(sel, str):
            for k, v in variables.items():
                sel = sel.replace("{{" + str(k) + "}}", str(v))
            target["selector"] = sel
    return new_action


async def _replay_click(page: Any, action: dict[str, Any], jitter: int) -> str:
    """click action を再生（selector優先、座標フォールバック、±jitterで bot検知緩和）"""
    rx = float(action.get("x", 0))
    ry = float(action.get("y", 0))
    target = action.get("target") or {}
    selector = target.get("selector")

    # 1. selector優先: bounding_box取得 → 中心 ±jitter で物理クリック
    if selector:
        try:
            loc = page.locator(selector).first
            await loc.scroll_into_view_if_needed(timeout=3000)
            box = await loc.bounding_box()
            if box and box["width"] > 0 and box["height"] > 0:
                cx = box["x"] + box["width"] / 2 + random.uniform(-jitter, jitter)
                cy = box["y"] + box["height"] / 2 + random.uniform(-jitter, jitter)
                # 要素境界内にクランプ（はみ出し防止）
                cx = max(box["x"] + 1, min(box["x"] + box["width"] - 1, cx))
                cy = max(box["y"] + 1, min(box["y"] + box["height"] - 1, cy))
                await page.mouse.click(cx, cy)
                return f"click via selector ok ({selector!r}) at ({cx:.1f}, {cy:.1f})"
        except Exception as e:
            # selector失敗 → 座標フォールバックへ
            pass

    # 2. 座標フォールバック
    jx = rx + random.uniform(-jitter, jitter)
    jy = ry + random.uniform(-jitter, jitter)
    try:
        await page.mouse.click(jx, jy)
        return f"click via coords ok ({jx:.1f}, {jy:.1f}) [selector={selector!r} miss]"
    except Exception as e:
        raise RuntimeError(f"click failed both via selector ({selector!r}) and coords ({rx},{ry}): {e}")


async def _replay_keydown(page: Any, action: dict[str, Any]) -> str:
    """keydown action を再生（modifier対応）"""
    key = action.get("key")
    if not key:
        return "keydown skipped (no key)"
    mods = []
    if action.get("ctrl"): mods.append("Control")
    if action.get("shift"): mods.append("Shift")
    if action.get("alt"): mods.append("Alt")
    if action.get("meta"): mods.append("Meta")
    combo = "+".join(mods + [key]) if mods else key
    await page.keyboard.press(combo)
    return f"keydown {combo!r}"


async def _replay_scroll(page: Any, action: dict[str, Any]) -> str:
    """scroll action を再生"""
    x = action.get("x", 0)
    y = action.get("y", 0)
    await page.evaluate(f"window.scrollTo({float(x)}, {float(y)})")
    return f"scroll to ({x}, {y})"


async def _replay_wait_max_lv(page: Any, action: dict[str, Any], jitter: int) -> str:
    """wait_max_lv: 長尺アニメ抜け用 — fixed wait + 1クリック（連打NG、連打防止オーバーレイ向け）"""
    ms = int(action.get("ms", 5000))
    await asyncio.sleep(ms / 1000.0)
    rx = float(action.get("x", 0))
    ry = float(action.get("y", 0))
    if rx > 0 or ry > 0:
        jx = rx + random.uniform(-jitter, jitter)
        jy = ry + random.uniform(-jitter, jitter)
        await page.mouse.click(jx, jy)
        return f"wait_max_lv: slept {ms}ms + 1 click at ({jx:.1f}, {jy:.1f})"
    return f"wait_max_lv: slept {ms}ms (no click coord)"


async def _handle_browser_record_replay(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_record_replay: 保存記録の再生（selector優先・座標フォールバック・±jitter・ランダム待機）"""
    name = args.get("name")
    if not name:
        raise ValueError("name is required")

    variables = args.get("variables") or []
    jitter = int(args.get("jitter", 10))
    wait_min = float(args.get("wait_min", 2))
    wait_max = float(args.get("wait_max", 5))

    if wait_max < wait_min:
        wait_max = wait_min

    save_path = _safe_recording_path(name)
    if not save_path.exists():
        raise FileNotFoundError(f"Recording not found: {save_path}")

    payload = json.loads(save_path.read_text(encoding="utf-8"))
    raw_actions = payload.get("actions", [])
    if not raw_actions:
        return [types.TextContent(type="text", text=f"Recording {name!r} has no actions; nothing to replay.")]

    # 安全網: 旧フォーマットや SPA 由来の重複を再生側でも除去（記録側 dedup と同等ロジック）
    actions: list[dict[str, Any]] = []
    for a in raw_actions:
        if actions:
            last = actions[-1]
            if a.get("type") == last.get("type") and (a.get("ts", 0) - last.get("ts", 0)) <= 100:
                sa = (a.get("target") or {}).get("selector")
                sb = (last.get("target") or {}).get("selector")
                if sa == sb:
                    if a.get("type") == "click" and a.get("x") == last.get("x") and a.get("y") == last.get("y"):
                        continue
                    if a.get("type") == "keydown" and a.get("key") == last.get("key"):
                        continue
                    if a.get("type") == "scroll" and a.get("x") == last.get("x") and a.get("y") == last.get("y"):
                        continue
        actions.append(a)
    deduped_count = len(raw_actions) - len(actions)

    _ctx, page = await _ensure_browser()

    # variablesが空なら1ループだけ（{}）
    iterations: list[dict[str, Any]] = variables if variables else [{}]

    log: list[str] = []
    total_executed = 0
    errors: list[str] = []

    for i, var_set in enumerate(iterations):
        log.append(f"--- iteration {i + 1}/{len(iterations)} (vars={var_set}) ---")
        for j, action in enumerate(actions):
            a = _apply_variables(action, var_set)
            atype = a.get("type", "?")
            try:
                if atype == "click":
                    msg = await _replay_click(page, a, jitter)
                elif atype == "keydown":
                    msg = await _replay_keydown(page, a)
                elif atype == "scroll":
                    msg = await _replay_scroll(page, a)
                elif atype == "wait_max_lv":
                    msg = await _replay_wait_max_lv(page, a, jitter)
                elif atype == "wait":
                    ms = int(a.get("ms", 1000))
                    await asyncio.sleep(ms / 1000.0)
                    msg = f"wait {ms}ms"
                else:
                    msg = f"skipped unknown type: {atype}"
                log.append(f"[{j}] {atype}: {msg}")
                total_executed += 1
            except Exception as e:
                err = f"[{j}] {atype} FAILED: {type(e).__name__}: {e}"
                log.append(err)
                errors.append(err)
                # 失敗時は中断（連打事故防止のためfail-fast）
                break

            # アクション間ランダム待機
            sleep_s = random.uniform(wait_min, wait_max)
            await asyncio.sleep(sleep_s)

        if errors:
            log.append(f"Iteration {i + 1} aborted due to error(s).")
            break

    summary = (
        f"Replay finished: name={name!r}, iterations={len(iterations)}, "
        f"executed={total_executed}, errors={len(errors)}, "
        f"deduped_at_load={deduped_count}"
    )
    detail = "\n".join(log[-80:])  # 直近80行のみ
    if len(log) > 80:
        detail = f"... ({len(log) - 80} more lines truncated) ...\n" + detail
    return [types.TextContent(type="text", text=summary + "\n" + detail)]


async def _handle_browser_popup_flow(args: dict[str, Any]) -> list[types.TextContent]:
    """browser_popup_flow: OAuthポップアップ一括フロー（クリック→ポップアップ→閉鎖待ち→元タブURL変化待ち）"""
    global _page

    trigger = args["trigger_selector"]
    expected_sub = args.get("expected_url_substring", "")
    popup_to = args.get("popup_timeout", 10000)
    close_to = args.get("close_timeout", 120000)
    redirect_to = args.get("redirect_timeout", 30000)

    _ctx, original_page = await _ensure_browser()
    original_url = original_page.url

    # 1. トリガークリック + ポップアップ追従
    locator, label = await _resolve_clickable_locator(original_page, trigger)
    async with original_page.expect_popup(timeout=popup_to) as popup_info:
        await locator.click(timeout=ELEMENT_WAIT_TIMEOUT)
    popup = await popup_info.value
    initial_popup_url = popup.url
    messages = [f"Popup opened from {label} (initial url: {initial_popup_url!r})"]

    # 2. ポップアップ閉鎖待ち。
    #    一部OAuthは popup を即閉じてくるため、wait_for_load_state より先に閉鎖待ちを開始する。
    #    （wait_for_load_state を先に呼ぶと、すでに閉じていた場合の挙動が読みにくいため）
    if not popup.is_closed():
        try:
            await popup.wait_for_event("close", timeout=close_to)
        except Exception as e:
            raise RuntimeError(
                f"Popup did not close within {close_to}ms (current url: {popup.url})"
            ) from e
    messages.append("Popup closed")

    # 3. 元タブを active に戻す
    if original_page.is_closed():
        raise RuntimeError("Original tab was closed unexpectedly during popup flow")
    _page = original_page
    await _page.bring_to_front()

    # 4. URL遷移待ち（expected_url_substring 指定時のみ）
    if expected_sub:
        try:
            await original_page.wait_for_url(
                lambda url: expected_sub in str(url),
                timeout=redirect_to,
                wait_until="commit",  # 完全loadではなくcommit時点で十分（OAuthリダイレクト後の遅延緩和）
            )
            messages.append(f"Original tab redirected: {original_url!r} -> {original_page.url!r}")
        except Exception as e:
            raise RuntimeError(
                f"Original tab did not navigate to URL containing {expected_sub!r} "
                f"within {redirect_to}ms (current: {original_page.url!r})"
            ) from e
    else:
        messages.append(f"Original tab url: {original_page.url!r}")

    return [types.TextContent(type="text", text="\n".join(messages))]


# --- メイン ---

SERVER_INSTRUCTIONS = """\
maruru-browser: Playwright MCP using a persistent Chrome profile (40+ tools).

# Tool categories (consider easily-missed ones)
- Core: browser_navigate / browser_evaluate / browser_click / browser_type / browser_wait_for / browser_tabs
- AI bridges (direct prompt, no browser ops): chatgpt_ask / gemini_ask / grok_ask / perplexity_search / x_search / google_search
- Specialized (prefer over manual composition):
  * browser_popup_flow: one-call OAuth/share popup flow (click -> popup -> close -> origin URL change)
  * browser_tabs action=latest: rescue when a new tab opened but focus wasn't followed
  * browser_tabs action=wait_close: wait for popup tab to close
  * wait_for_navigation: SPA route change wait (better than browser_wait_for)
  * iframe_evaluate: run JS inside an iframe (browser_evaluate cannot reach)
  * record_replay: repeat an action sequence with jitter (mitigates bot detection)
  * browser_handle_dialog: auto-response policy for alert/confirm/prompt
  * generic_form_fill: fill forms by inferring fields from label/placeholder

# Gotchas
- browser_snapshot may raise AttributeError in some envs; fall back to browser_evaluate (document.body.innerText etc.)
- Clicks/keys that open a new tab: always set follow_new_tab=true (safer than manual tabs.switch)
- x_search returns 0 hits when not logged in; fall back to google_search / perplexity_search
- Auth-gated PDF/binary download: browser_evaluate -> fetch(url, {credentials:'include'}) -> ArrayBuffer -> btoa(), or cookies_get and hand off to an external HTTP client
- Popups and new tabs are equivalent in Playwright; DoH filters (e.g. NextDNS) may block OAuth popups

# AI bridge selection
- Knowledge / fresh info with sources: perplexity_search
- Realtime / breaking / social context: x_search (login required) + google_search
- Long-context / code gen: chatgpt_ask (project instructions applied) / gemini_ask
- Candid / contrarian take: grok_ask
"""


async def main():
    """MCPサーバーのメインエントリポイント"""
    try:
        async with stdio_server() as (read_stream, write_stream):
            await server.run(
                read_stream, write_stream,
                InitializationOptions(
                    server_name="maruru-browser",
                    server_version="0.9.2",
                    capabilities=server.get_capabilities(
                        notification_options=NotificationOptions(),
                        experimental_capabilities={}
                    ),
                    instructions=SERVER_INSTRUCTIONS,
                )
            )
    finally:
        await _cleanup()


if __name__ == "__main__":
    asyncio.run(main())
