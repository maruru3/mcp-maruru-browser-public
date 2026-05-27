# mcp-maruru-browser

> [!WARNING]
> This server gives an MCP client control over a real Chrome profile.
> If that profile is logged in to websites, the client may access
> authenticated pages, cookies, localStorage, and sensitive DOM content.
> Use only on your own local machine with trusted MCP clients.
> Do not expose this server to remote agents, shared hosts, or public
> automation services.
> See [SECURITY.md](SECURITY.md) before installing.

`mcp-maruru-browser` is a local Model Context Protocol server for controlling a real Chrome browser through Playwright.

The main difference from a disposable browser automation setup is that this server launches Chrome with a **persistent user profile**, so login state, cookies, localStorage, extensions, and site preferences are reused across MCP tool calls and server restarts.

This is a personal, local-first browser MCP server for tasks where an agent needs to work in the same browser environment the user already uses.

## What makes it different from official Playwright MCP

Official Playwright MCP is a general-purpose browser automation server. `maruru-browser` is intentionally more personal and stateful.

Key differences:

- **Persistent Chrome profile**: reuses existing login sessions, cookies, localStorage, and extensions.
- **Real Chrome channel**: launches persistent Chrome in headed mode instead of a throwaway browser context.
- **AI service shortcuts**: includes `perplexity_search`, `chatgpt_ask`, `gemini_ask`, and `grok_ask`.
- **Search extraction helpers**: `x_search` and `google_search` parse live DOM.
- **New-window / popup support**: `follow_new_tab`, `browser_tabs(action=wait_close)`, and `browser_popup_flow` handle sites that open separate windows (OAuth flows, share popups, `target="_blank"` links).
- **Multi-client safety**: a process-wide profile lock prevents two clients from racing on the same Chrome profile.

This server is not designed as a sandbox. It is designed as a trusted local bridge into your own browser profile.

## Requirements

- Python 3.11+
- Chrome installed (real Chrome, not Chromium)
- Playwright Chromium browser support (for the Playwright driver)
- MCP-compatible client such as Claude Code or Claude Desktop

## Install

Replace `<path-to-this-repo>` with the directory where you cloned this repository.

### Option A: uv

```powershell
cd <path-to-this-repo>

uv venv
.venv\Scripts\Activate.ps1

uv pip install -e .
uv run playwright install chromium
```

### Option B: venv + pip

```powershell
cd <path-to-this-repo>

py -3.11 -m venv .venv
.venv\Scripts\Activate.ps1

python -m pip install --upgrade pip
pip install -e .
python -m playwright install chromium
```

## Run directly

```powershell
python mcp_maruru_browser_server.py
```

The server speaks MCP over stdio, so normally it is started by an MCP client rather than run manually.

## Environment variables

All paths and private AI URLs are env-overridable so personal data does not need to live in the source tree.

| Variable | Default | Purpose |
| --- | --- | --- |
| `MARURU_BROWSER_PROFILE` | `~/.maruru-browser/chrome-profile` | Chrome user-data directory. Set to a different path per client when running Claude Code and Claude Desktop side by side. |
| `MARURU_BROWSER_RECORDINGS` | `~/.maruru-browser/recordings` | Where `browser_record_*` saves JSON recordings. |
| `MARURU_BROWSER_ARTIFACTS` | `~/.maruru-browser/artifacts` | Where `browser_take_screenshot` and `browser_pdf_save` write output files. Filenames passed by tool calls are sanitized to stay inside this directory. |
| `MARURU_CHATGPT_URL` | `https://chatgpt.com/` | Override to point `chatgpt_ask` at your own Project URL. |
| `MARURU_GEMINI_URL` | `https://gemini.google.com/` | Override to point `gemini_ask` at your own Gem URL. |
| `MARURU_GROK_URL` | `https://grok.com/` | Override to point `grok_ask` at your own Project URL. |
| `PYTHONIOENCODING` | (unset) | Set to `utf-8` for non-ASCII output on Windows. |

## Claude Code setup (`.mcp.json`)

```json
{
  "mcpServers": {
    "maruru-browser": {
      "command": "<path-to-this-repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<path-to-this-repo>\\mcp_maruru_browser_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

Or with `uv`:

```json
{
  "mcpServers": {
    "maruru-browser": {
      "command": "uv",
      "args": [
        "--directory",
        "<path-to-this-repo>",
        "run",
        "python",
        "mcp_maruru_browser_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8"
      }
    }
  }
}
```

## Claude Desktop setup

Edit `%APPDATA%\Claude\claude_desktop_config.json` (Windows) and add the same `mcpServers` entry shape. The block under `mcpServers` is identical to the Claude Code form above.

### Running Claude Code and Claude Desktop side by side

A persistent Chrome profile can only be opened by **one** process at a time. The server enforces this with a process-level lock — if two MCP clients try to launch Chrome against the same `MARURU_BROWSER_PROFILE`, the second one errors out immediately rather than hanging for 60 seconds.

To run Claude Code and Claude Desktop simultaneously, give each client its own profile via `MARURU_BROWSER_PROFILE`:

```json
{
  "mcpServers": {
    "maruru-browser": {
      "command": "<path-to-this-repo>\\.venv\\Scripts\\python.exe",
      "args": [
        "<path-to-this-repo>\\mcp_maruru_browser_server.py"
      ],
      "env": {
        "PYTHONIOENCODING": "utf-8",
        "MARURU_BROWSER_PROFILE": "C:\\path\\to\\chrome-profile-desktop"
      }
    }
  }
}
```

Caveats:

- Each profile holds its own login state. You will need to log in once per profile to whatever sites the agent needs.
- Recording paths, console buffers, and AI-service tools are per-server-process. They do not cross between Code and Desktop instances.
- If you do not need true simultaneity, sharing one profile (and only running one client at a time) is simpler.

## Tool catalog

Version `0.9.0` exposes 39 tools.

### Basic browser tools

| Tool | Purpose |
| --- | --- |
| `browser_navigate` | Navigate the current page to a URL. |
| `browser_snapshot` | Return an accessibility snapshot of the current page. |
| `browser_click` | Click an element by CSS selector or visible text. Pass `follow_new_tab=true` to auto-switch when the click opens a new tab/window. |
| `browser_type` | Fill/type text into an element, optionally pressing Enter. With `submit=true` and `follow_new_tab=true`, only the Enter press is wrapped in the new-tab waiter. |
| `browser_press_key` | Press a keyboard key such as `Enter`, `Tab`, `Escape`, or `Control+a`. Also supports `follow_new_tab`. |
| `browser_evaluate` | Execute JavaScript in the current page and return the result. |
| `browser_select_option` | Select an option in a `<select>` element by value or label. |
| `browser_navigate_back` | Go back in browser history. |
| `browser_navigate_forward` | Go forward in browser history. |
| `browser_resize` | Resize the viewport for responsive checks. |

### Wait, scroll, and view tools

| Tool | Purpose |
| --- | --- |
| `browser_wait_for` | Wait for a selector, page text, or fixed time in milliseconds. |
| `browser_scroll` | Scroll up, down, top, bottom, or to a selector. |
| `browser_take_screenshot` | Save a PNG screenshot. |
| `browser_pdf_save` | Save the current page as a PDF through Chrome DevTools Protocol. |

### Tabs, windows, and popups

| Tool | Purpose |
| --- | --- |
| `browser_tabs` | List, switch, open, close, jump to the latest tab (`action=latest`), or wait for a popup to close (`action=wait_close`). |
| `browser_popup_flow` | High-level OAuth helper: click a trigger → follow the popup → wait for it to close → optionally wait for the original tab to reach an expected URL. One call covers a typical OAuth round-trip. |
| `browser_hover` | Hover an element to open menus or reveal tooltips. |

### Debug and monitoring tools

| Tool | Purpose |
| --- | --- |
| `browser_console_messages` | Read buffered console logs and page errors. |
| `browser_network_requests` | Read buffered request, response, and failed network events. |
| `browser_handle_dialog` | Configure automatic handling for `alert`, `confirm`, and `prompt` dialogs. |

### Advanced interaction tools

| Tool | Purpose |
| --- | --- |
| `browser_mouse_xy` | Move, click, double-click, press, or release the mouse by viewport coordinates. |
| `browser_drag_drop` | Drag one element onto another element. |
| `browser_file_upload` | Set files on an `<input type="file">` element. |

### AI integrations

| Tool | Purpose |
| --- | --- |
| `perplexity_search` | Ask Perplexity and extract the answer from the browser session. |
| `chatgpt_ask` | Ask ChatGPT and extract the answer. Defaults to `https://chatgpt.com/`; override with the `project_url` argument or `MARURU_CHATGPT_URL` env var to target a specific Project. |
| `gemini_ask` | Ask Gemini and extract the answer. Defaults to `https://gemini.google.com/`; override with the `gem_url` argument or `MARURU_GEMINI_URL` env var to target a specific Gem. |
| `grok_ask` | Ask Grok and extract the answer. Defaults to `https://grok.com/`; override with the `project_url` argument or `MARURU_GROK_URL` env var to target a specific Project. |

### Cookies, storage, navigation, search, and forms

| Tool | Purpose |
| --- | --- |
| `cookies_get` | Read browser cookies, optionally filtered by URL and cookie name. |
| `cookies_set` | Add or overwrite browser cookies. |
| `local_storage_get` | Read one localStorage key or dump all localStorage for the current origin. |
| `local_storage_set` | Set or remove a localStorage key for the current origin. |
| `wait_for_navigation` | Wait for a URL pattern and/or load state after navigation. |
| `x_search` | Search X/Twitter and extract tweet-like results from the DOM. |
| `google_search` | Search Google and extract result titles, URLs, snippets, and best-effort AI Overview text. |
| `generic_form_fill` | Fill a form by matching field names against name, id, placeholder, aria-label, or label text. |

### Iframes

| Tool | Purpose |
| --- | --- |
| `iframe_evaluate` | Run JavaScript inside a child iframe. Locate by `frame_url` (substring), `frame_name` (exact), `frame_selector` (CSS for the host `<iframe>`), or `frame_index` (0-based). Calling with no selector or with `expression` omitted returns the list of child frames. |

### Manual recording mode

| Tool | Purpose |
| --- | --- |
| `browser_record_start` | Start recording manual interactions on the active page. Click, keydown, and scroll events are intercepted at the document's capture phase via an injected listener; each event is stamped with a CSS path of its target so replays can survive cosmetic DOM shifts. |
| `browser_record_stop` | Stop the active recording and persist it to `<MARURU_BROWSER_RECORDINGS>/<name>.json`. |
| `browser_record_replay` | Replay a saved recording. Selector-based clicks are tried first, with the click point jittered inside the element's bounding box for bot-detection mitigation; the recorded raw coordinates serve as a fallback when the selector is missing. Action intervals are randomized between `wait_min` and `wait_max` seconds. Pass a `variables` list to iterate the same script with `{{var}}` substitutions in selectors. The special action type `wait_max_lv` performs a fixed-duration wait followed by a single dismiss click, intended for animated overlays whose end state is hard to detect in the DOM. |

Recordings are stored under the directory set by `MARURU_BROWSER_RECORDINGS` and contain raw cursor coordinates plus full DOM-text snippets. Treat them as sensitive: they should not be committed.

## Reliability features (v0.8.0)

- **Tool call serialization**: an `asyncio.Lock` queues all tool calls so concurrent MCP requests do not race on the shared browser state.
- **Bounded waits**: per-call timeout (300s), lock-wait timeout (200s), and browser-launch timeout (60s) eliminate indefinite hangs.
- **Profile cross-process lock**: a Windows `msvcrt` file lock on the Chrome user-data directory prevents two server processes from sharing a profile and corrupting it.
- **Tab-count notifications**: every tool response appends a `[tabs: N]` footer and warns when the count changes between calls, surfacing unintended popups immediately.

## Notes on specific tools

- `cookies_set`: each cookie must specify either `url` **or** `domain`+`path`, not both. Playwright's `add_cookies` rejects a `url`+`path` combination with `Cookie should have either url or path`. Prefer `url` when targeting a single origin.
- `local_storage_*`: operates on the current page's origin. Navigate to the target site first.
- `wait_for_navigation`: combines `page.wait_for_url(predicate)` (substring match) with `wait_for_load_state(state)`. Pass `state="networkidle"` for SPAs.
- `x_search` / `google_search`: scrape live DOM. Markup changes on those sites will break extraction.
- `browser_pdf_save`: bypasses headed-mode limitations by calling `Page.printToPDF` over CDP directly.
- `browser_popup_flow`: holds the global tool lock for the entire flow (up to `popup_timeout + close_timeout + redirect_timeout`, default 160s). Other tools will block during the wait. Keep the total under the 300s per-call timeout.

## Security notes

This MCP server uses a persistent Chrome profile. Treat it as equivalent to giving an MCP client access to your logged-in browser.

Important risks:

- The Chrome profile contains active login state for sites you use.
- Any tool call can interact with authenticated pages as you.
- `cookies_get` and `local_storage_get` can expose session tokens and site data.
- `browser_evaluate`, `x_search`, and `google_search` run JavaScript or scrape DOM content in authenticated browser contexts.

Do not expose this MCP server to remote agents, shared agent hosts, multi-tenant systems, or public automation services.

Use it only locally, in a single-user environment, with an isolated Chrome profile when testing risky workflows.

Do not commit the Chrome profile directory.

See [SECURITY.md](SECURITY.md) for the full security policy.

## Known limits

- Vision-based operations are not implemented.
- `x_search` and `google_search` depend on live DOM structures and can break when X or Google changes markup.
- `browser_record_*` injects a capture-phase listener into the live document. Navigations replace the document and discard the listener, so recordings should be limited to a single page lifecycle.
- The persistent profile may fail to launch if another Chrome process (outside this server) is already using the same profile.
- Sites with bot detection, CAPTCHA, region-specific layouts, or login prompts may require manual intervention.

## License

MIT — see [LICENSE](LICENSE).
