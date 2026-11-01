# cmux web remote (prototype)

A small web server that lets a browser (phone, tablet, another computer) drive
the cmux app on your Mac:

- see every window and workspace, with each workspace's split layout: all
  panes side by side as in cmux, each with its tab bar
- watch terminals live and in color (cmux's own render grid), with the cursor;
  zoom one pane to read it full size
- type text and send keys (Enter, Esc, Ctrl-C, Tab, arrows, Ctrl-D)
- create a workspace, or switch the Mac's window to one
- a sidebar that mirrors cmux's own rows: unread badge, latest notification
  preview, description, color rail, agent status pills (Working / Done / Idle),
  newest log line, progress, branch, directory, PRs and ports
- a bell with the unread count opening a notifications list laid out like
  cmux's (Jump to Latest Unread, Clear All, per-notification clear), plus
  browser notifications; picking a pane marks its notifications read, as
  focusing it in cmux does

It uses only the public cmux control socket (v2 JSON lines) and the
[`events.stream`](../../docs/events.md) subscription. It does not change cmux.
Python standard library only, so the macOS system `python3` (3.9+) runs it.

It is a prototype. Terminals are redrawn from snapshots when their text
changes (checked every 0.5 s on cmux builds without `surface.wait_text_change`),
not streamed byte by byte, and keyboard input goes through the text box and key
buttons rather than straight from the browser keyboard. That is fine for
watching agents and answering them; full-screen TUIs such as vim are readable
but clumsy to drive. Browser and Markdown tabs are listed but not mirrored.

## Run

Start it **from a terminal inside cmux**. The default socket mode, `cmuxOnly`,
trusts processes started from cmux terminals, so no socket password is needed:

```bash
python3 examples/cmux-web-remote/server.py
```

Keep it in the foreground of that terminal (or a child of it, such as an
agent's background task). `cmuxOnly` checks the process ancestry, so `nohup
... &`, `launchd` or a detached `screen` reparent the server away from cmux and
the socket answers `Access denied`; the server then exits with that message. To
run it detached, switch cmux's socket to `password` mode and export
`CMUX_SOCKET_PASSWORD`.

It prints a one-time login URL such as
`http://127.0.0.1:8787/login?token=...`. Opening it sets an HttpOnly cookie for
30 days. Set `CMUX_WEB_TOKEN` to keep the same token across restarts.

Options:

| Option / env | Meaning |
| --- | --- |
| `--host`, `--port` | Bind address, default `127.0.0.1:8787`. Keep loopback; see below. |
| `--socket` / `CMUX_SOCKET_PATH` | cmux socket. Defaults to the one the surrounding cmux terminal exports. |
| `CMUX_SOCKET_PASSWORD` | Socket password, when cmux's socket mode is `password`. |
| `CMUX_WEB_TOKEN` | Fixed access token instead of a random one. |
| `--access-log` | Log every request. Off by default: only errors are logged, because a log line per poll would keep changing the server's own terminal, and a browser watching that workspace would poll again at once, forever. |
| `--allow-origin <url>` | Extra origin allowed to POST, e.g. your Tailscale HTTPS name if a proxy rewrites `Host`. |

## Use it from your phone

Do not bind it to `0.0.0.0` on an open network: whoever has the token can type
into your terminals, which means running any command as you. Put an
authenticated, encrypted tunnel in front of it instead. With
[Tailscale](https://tailscale.com):

```bash
tailscale serve --bg --https=443 http://127.0.0.1:8787
```

Then open `https://<mac-name>.<tailnet>.ts.net/login?token=...` on the phone.
HTTPS also lets the page show browser notifications. On iOS, browser
notifications work only after you add the page to the Home Screen (iOS 16.4+),
and only while the page is running. For pushes while the page is closed, see
[Next steps](#next-steps).

## How it works

```
browser ──HTTP/SSE──▶ server.py ──unix socket──▶ cmux.app
                         ├─ one long-lived events.stream (notification, workspace, surface)
                         └─ one short connection per API call
```

| HTTP | cmux v2 method |
| --- | --- |
| `GET /api/tree` | `system.tree` (split layout, panes, tabs), `extension.sidebar.snapshot` (unread count, latest notification, description, color, branch, PRs, ports) and cached `sidebar_state` (status pills, log, progress) |
| `POST /api/screens {surfaces: {id: revision}}` | Waits until any listed terminal changes, then returns its text and colored grid. Change detection: `surface.wait_text_change` for one terminal; `surface.read_text` polled every 0.5 s for several, or on cmux builds without it (0.65.x). Colors: `terminal.replay` |
| `POST /api/send {surface_id, text, enter}` | `surface.send_text`, then `surface.send_key enter` |
| `POST /api/key {surface_id, key}` | `surface.send_key` |
| `POST /api/workspace/select {workspace_id}` | `workspace.select` (switches the Mac's screen too) |
| `POST /api/workspace/create {cwd?}` | `workspace.create` |
| `GET /api/notifications` | `notification.list` |
| `POST /api/notifications/read {id \| all \| workspace_id, surface_id}` | `notification.mark_read` |
| `POST /api/notifications/clear_all` | `notification.dismiss` for each notification |
| `POST /api/notifications/dismiss {id}` | `notification.dismiss` |
| `GET /api/events` (SSE) | `events.stream` |

`terminal.replay` returns cmux's `cmux.render-grid.v1` frame: styled text spans
with resolved colors, the cursor and up to 240 rows of scrollback. The server
turns it into a compact `{styles, lines}` form; with gzip a 111×81 terminal is
about 30 KB per redraw. The page polls at most once a second (agents animate spinners, timers and a
blinking dot, so a busy terminal changes several times a second) and stops
polling while its tab is in the background. One `/api/screens` request covers every visible pane,
because browsers allow only six connections per host. Viewing a hibernated agent
wakes it, as the iOS app does.

Status pills, the newest log line and progress have no v2 read, so the server
uses the v1 `sidebar_state --tab=<id>` command the `cmux sidebar-state` CLI
uses: every workspace once at startup (eight at a time, about 1 s for 136
workspaces), then only the workspace named by a `sidebar.*` or
`program_status.changed` event. Browsers get a `sidebar` message and redraw
that row.

The event stream redacts notification titles and bodies, so on
`notification.created` the server looks the notification up with
`notification.list` before forwarding it. On reconnect it resumes from the last
`seq`. When cmux reports a resume gap or restarts, browsers reload their
snapshot.

API calls also accept `Authorization: Bearer <token>`, so scripts can use them:

```bash
curl -H "Authorization: Bearer $CMUX_WEB_TOKEN" http://127.0.0.1:8787/api/tree
```

## Security notes

- Every API route needs the token (cookie or bearer), compared in constant time.
- The cookie is `HttpOnly; SameSite=Strict`. State-changing requests must be
  JSON POSTs from the same origin (or carry no `Origin`, as from curl).
- The token never appears in the server log.
- `surface.send_text` from the API skips the CLI's "don't type over an agent
  prompt draft" check that `cmux send` does. Check the screen before you send.

## Tests

The tests run the server against a fake cmux socket, so they need neither macOS
nor cmux:

```bash
python3 examples/cmux-web-remote/test_server.py
```

## Next steps

- Feed approval buttons (`feed.permission.reply` and friends) for agent
  permission requests.
- Web Push (VAPID) or ntfy/Bark forwarding, for alerts while the page is closed.
- An xterm.js view with colors, if cmux exposes a PTY byte stream to socket clients.
