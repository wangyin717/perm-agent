---
name: computer-use
description: Drive a native app on this desktop. Web pages still use browser-use.
---

# computer-use

Use this skill for a native app already on this machine: open it, read a window, click, or type. Public pages and logged-in Chrome stay on `browser_exec` / `browser_screenshot`. Do not `bash` `cua-driver`.

Call **`computer`** with the tool `name` and an `arguments` object. Names and parameters are in `tools.md` next to this file. Grep one heading, then read that section. Do not read the whole file.

```
grep pattern="^# click$" path="<directory of this skill>/tools.md"
```

If `tools.md` is missing, the plugin did not start. Say so. Do not invent parameters.

## Which call

- Already have `pid` and `window_id`: `get_window_state` for that pair, then `click` or `type_text`.
- The app is not running: `launch_app`. It returns `pid` and window ids. Then `get_window_state`.
- The app is already running and you need a window id: `list_windows`, then `get_window_state`. Without `pid`, the result is one on-screen titled window per app. Pass `pid` to see that app's other windows.
- `list_apps` only answers whether an app is installed or running. It is not required before `launch_app`.

`get_window_state` must use the same `pid` and `window_id` as the later click or type. The element numbers belong to that snapshot.

Prefer `element_index` from that snapshot. That click does not move the real mouse.

Use `x` and `y` only when the control is not in the tree (self-drawn apps such as WeChat). A pixel click follows the real pointer. Before it, move the real pointer onto that control with `move_cursor` and `scope: "desktop"`, then `click`. Tell the user not to touch the mouse for those few seconds. Afterwards you may move the pointer back. Window scope only moves a fake overlay, so it does not count.

`move_cursor` with `scope: "desktop"` takes screen coordinates. `click` x, y are window-screenshot pixels. Convert with the window origin from `list_windows`: screen = origin + screenshot point × (window size / screenshot size).

Do not use the `page` tool. Web pages stay on `browser_exec`.

To check the screen, call `bring_to_front` for that window, then `get_window_state`. A screenshot often fails while the window is not in front. Use that window's screenshot. Do not call `get_desktop_state` to capture the whole desktop. Do not write an OCR program or a shell script to read the image.

## Permissions

If macOS has not granted CuaDriver yet, the plugin runs `cua-driver permissions grant` and the system permission dialogs appear. Tell the user to click Allow on those dialogs. Accessibility and Screen Recording both have to be allowed. The same `computer` call continues after they click. Do not send them into System Settings to find the checkboxes. Do not click the dialogs yourself.

## Do not

- Put image bytes or base64 in the conversation
- Launch the app from bash
- Drive Chrome pages with `computer`
