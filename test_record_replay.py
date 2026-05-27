#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Integration test for browser_record_start / browser_record_stop / browser_record_replay.

Strategy:
  1. Navigate to a self-contained data: URL with 2 buttons + counter
  2. Start recording
  3. Programmatically click both buttons (Playwright dispatches real DOM events,
     so our document-level capture listener will see them)
  4. Stop recording
  5. Inspect the saved JSON: should contain 2 click actions with valid selectors
  6. Reset page (re-navigate)
  7. Replay the recording
  8. Verify counter == 2 (replay actually performed clicks)
"""

import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import mcp_maruru_browser_server as srv


TEST_NAME = "_pytest_record_replay_minimal"
# Reuse the same recordings directory as the server (env-overridable).
RECORDINGS_DIR = srv.RECORDINGS_DIR
RECORDING_PATH = RECORDINGS_DIR / f"{TEST_NAME}.json"


# 2 buttons, counter increments on click; survives navigation reset
TEST_HTML = """
<!doctype html><html><head><title>record-replay test</title></head>
<body>
  <h1 id="title">record-replay test</h1>
  <div>Count: <span id="counter">0</span></div>
  <button id="btn-a" data-role="primary">Button A</button>
  <button id="btn-b" data-role="secondary">Button B</button>
  <script>
    const c = document.getElementById('counter');
    document.querySelectorAll('button').forEach(b => {
      b.addEventListener('click', () => {
        c.textContent = String(parseInt(c.textContent) + 1);
      });
    });
  </script>
</body></html>
""".strip()

DATA_URL = "data:text/html;charset=utf-8," + TEST_HTML.replace("\n", "").replace("  ", "").replace('"', '%22').replace("#", "%23").replace(" ", "%20")


async def _read_counter() -> int:
    _ctx, page = await srv._ensure_browser()
    val = await page.evaluate("document.getElementById('counter').textContent")
    return int(val)


async def test_record_then_replay() -> None:
    print("\n=== Test: record_start -> click x2 -> record_stop -> replay ===")

    # Pre-cleanup
    if RECORDING_PATH.exists():
        RECORDING_PATH.unlink()

    # 1. Open test page
    await srv._handle_browser_navigate({"url": DATA_URL})
    await asyncio.sleep(0.3)

    # 2. Start recording
    res = await srv._handle_browser_record_start({"name": TEST_NAME})
    print("record_start:", res[0].text[:200])

    # 3. Click both buttons via Playwright (dispatches real DOM events)
    _ctx, page = await srv._ensure_browser()
    await page.click("#btn-a")
    await asyncio.sleep(0.2)
    await page.click("#btn-b")
    await asyncio.sleep(0.2)

    count_after_record = await _read_counter()
    assert count_after_record == 2, f"Counter should be 2 after manual clicks, got {count_after_record}"

    # 4. Stop recording
    res = await srv._handle_browser_record_stop({})
    print("record_stop:", res[0].text[:300])

    # 5. Inspect JSON
    assert RECORDING_PATH.exists(), f"Recording file not saved: {RECORDING_PATH}"
    data = json.loads(RECORDING_PATH.read_text(encoding="utf-8"))
    actions = data.get("actions", [])
    clicks = [a for a in actions if a.get("type") == "click"]
    assert len(clicks) >= 2, f"Expected >=2 click actions, got {len(clicks)}: {actions}"

    # Each click should carry both target selector and coords
    for c in clicks[:2]:
        assert "target" in c and c["target"].get("selector"), f"Missing selector: {c}"
        assert "x" in c and "y" in c, f"Missing coords: {c}"
    print(f"  recorded clicks: {len(clicks)} (selectors: {[c['target']['selector'] for c in clicks[:2]]})")

    # 6. Reset page (counter back to 0)
    await srv._handle_browser_navigate({"url": DATA_URL})
    await asyncio.sleep(0.3)
    assert await _read_counter() == 0, "Counter should be reset to 0"

    # 7. Replay (no jitter, fast wait for testing)
    res = await srv._handle_browser_record_replay({
        "name": TEST_NAME,
        "jitter": 0,
        "wait_min": 0.1,
        "wait_max": 0.2,
    })
    print("record_replay:", res[0].text[:300])

    # 8. Verify counter
    final = await _read_counter()
    assert final >= 2, f"Replay should have produced >=2 clicks, counter={final}"
    print(f"  PASS: counter={final} after replay")


async def main() -> None:
    print("Starting record_replay tests...")
    print("NOTE: opens Chrome window via persistent profile.")
    try:
        await test_record_then_replay()
        print("\n=== record_replay tests PASSED ===")
    except Exception as e:
        print(f"\nFAILED: {type(e).__name__}: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
    finally:
        # Cleanup recording artifact
        if RECORDING_PATH.exists():
            RECORDING_PATH.unlink()
        print("\nClosing browser...")
        await srv._cleanup()


if __name__ == "__main__":
    asyncio.run(main())
