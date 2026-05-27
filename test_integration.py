#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration test for mcp-maruru-browser
Directly tests handler functions without MCP protocol.
Requires Chrome to NOT be using the profile already.
"""

import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_maruru_browser_server as srv


async def test_navigate():
    """Test browser_navigate with example.com"""
    print("\n=== Test: browser_navigate ===")
    result = await srv._handle_browser_navigate({"url": "https://example.com"})
    text = result[0].text
    print(text)
    assert "example.com" in text.lower() or "Example" in text
    assert "Status:" in text
    print("PASS: browser_navigate")


async def test_snapshot():
    """Test browser_snapshot on current page"""
    print("\n=== Test: browser_snapshot ===")
    result = await srv._handle_browser_snapshot({})
    text = result[0].text
    print(text[:500])
    assert "URL:" in text
    assert "Title:" in text
    print("PASS: browser_snapshot")


async def test_click():
    """Test browser_click on example.com link"""
    print("\n=== Test: browser_click ===")
    # First navigate to example.com
    await srv._handle_browser_navigate({"url": "https://example.com"})
    result = await srv._handle_browser_click({"selector": "Learn more"})
    text = result[0].text
    print(text)
    assert "Clicked" in text
    print("PASS: browser_click")


async def test_type():
    """Test browser_type on a search page"""
    print("\n=== Test: browser_type ===")
    await srv._handle_browser_navigate({"url": "https://www.google.com"})
    await asyncio.sleep(1)
    result = await srv._handle_browser_type({
        "selector": "textarea",
        "text": "hello world",
        "submit": False,
    })
    text = result[0].text
    print(text)
    assert "Typed" in text
    print("PASS: browser_type")


async def main():
    print("Starting integration tests...")
    print("NOTE: This will open a Chrome browser window.")

    try:
        await test_navigate()
        await test_snapshot()
        await test_click()
        await test_type()
        print("\n=== All integration tests PASSED! ===")
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
    finally:
        print("\nCleaning up browser...")
        await srv._cleanup()
        print("Done.")


if __name__ == "__main__":
    asyncio.run(main())
