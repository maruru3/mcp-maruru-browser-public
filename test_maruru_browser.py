#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
mcp-maruru-browser: Minimal tests
"""

import sys
import os
import asyncio

# サーバーモジュールをインポートできるようにパスを追加
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))


def test_import():
    """モジュールがインポートできることを確認"""
    import mcp_maruru_browser_server as mod
    assert hasattr(mod, "server"), "server object not found"
    assert hasattr(mod, "_ensure_browser"), "_ensure_browser not found"
    assert hasattr(mod, "_cleanup"), "_cleanup not found"
    assert hasattr(mod, "PAGE_LOAD_TIMEOUT"), "PAGE_LOAD_TIMEOUT not found"
    assert hasattr(mod, "ELEMENT_WAIT_TIMEOUT"), "ELEMENT_WAIT_TIMEOUT not found"
    assert mod.PAGE_LOAD_TIMEOUT == 30000
    assert mod.ELEMENT_WAIT_TIMEOUT == 10000
    print("  test_import: OK")


def test_list_tools():
    """中核ツール群が登録されていることを確認（個別ツール追加で増えるためサブセット検証）"""
    import mcp_maruru_browser_server as mod

    tools = asyncio.run(mod.list_tools())
    assert len(tools) > 0, f"No tools returned"

    # コア機能のサブセット検証（追加で増えても通る）
    must_have = {
        "browser_navigate",
        "browser_snapshot",
        "browser_click",
        "browser_type",
        "browser_press_key",
        "browser_evaluate",
        "browser_tabs",
        "browser_popup_flow",
    }
    actual_names = {t.name for t in tools}
    missing = must_have - actual_names
    assert not missing, f"Missing core tools: {missing}"
    print(f"  test_list_tools: OK ({len(tools)} tools)")


if __name__ == "__main__":
    print("Running tests...")
    test_import()
    test_list_tools()
    print("All tests passed!")
