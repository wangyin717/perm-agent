---
name: browser-use
description: Drive a real browser when a page needs clicks, login, or JS rendering. Public static docs still use web_search and web_fetch.
---

# browser-use

Use this skill when `web_fetch` is an empty JS shell, or the user needs clicks, forms, a logged-in Chrome, or a screenshot of a live page. Do not use it for public HTML that fetch can already read.

Use **`browser_exec`** and **`browser_screenshot`**. Do not `bash` the `browser-use` CLI. Do not launch Chrome.

## Attach only

The plugin attaches to the Chrome that is **already running**. Do not pick a profile. Do not copy cookies. Do not launch a second browser.

If Chrome is not connected yet, the plugin opens `chrome://inspect/#remote-debugging` and ticks **Allow remote debugging for this browser instance**. **Wait.** The user only clicks Allow on Chrome's **"Allow remote debugging?"** popup. Do not tick that checkbox yourself and do not click Allow. Do not wait for them to type in the TUI — the same `browser_exec` call continues when they click Allow.

Do **not** start Chrome yourself (`Google Chrome`, `--headless`, `--user-data-dir`, `--remote-debugging-port`, `nohup`).
Do **not** read or copy `~/Library/Application Support/Google/Chrome`.

Login walls: stop and ask. Do not fill passwords or MFA. If Chrome is already signed in, use that session after attach works.

## browser_exec

Pass Python in `code`. Helpers are pre-imported. The namespace persists across calls.

```
new_tab("https://example.com")
print(page_info())
```

First navigation is `new_tab(url)`, not `goto_url`. After navigation, `wait_for_load()`. Click with a screenshot then `click_at_xy(x, y)`.

Do **not** `print(list_tabs())` for the whole browser. Chrome may have dozens of tabs; that dump floods the session and can kill the plugin. Filter first, or `switch_tab` to a URL you already have:

```
tabs = list_tabs()
print([t for t in tabs if "mail.google.com" in str(t.get("url") or "")])
```

If a matching tab exists, `switch_tab` to it. Do not open a second Gmail window.

## browser_screenshot

Prefer this over `capture_screenshot()` inside `browser_exec`. The image is saved under the session; jsonl stores the path, not pixels.

## Do not

- Invent a core `browser` tool
- Put base64 in tool results (never print image bytes)
- Use the browser for pages `web_search` / `web_fetch` already handle
- Launch Chrome / Chromium / Brave from bash
- Read or copy the user's Chrome profile directory
