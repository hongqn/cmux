# cmux web remote (prototype)

A small web server that lets a browser (phone, tablet, another computer) drive
the cmux app on your Mac:

- see every window, workspace and terminal
- read a terminal's screen, live (long-poll on `surface.wait_text_change`)
- type text and send keys (Enter, Esc, Ctrl-C, Tab, arrows, Ctrl-D)
- create a workspace, or switch the Mac's window to one
- get cmux notifications live, with browser notifications and an unread count

It uses only the public cmux control socket (v2 JSON lines) and the
[`events.stream`](../../docs/events.md) subscription. It does not change cmux.
Python standard library only, so the macOS system `python3` (3.9+) runs it.

It is a prototype. The terminal view is a plain-text snapshot (no colors, no
cursor). That is fine for watching agents and answering them, but not for
full-screen TUIs such as vim.

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
| `GET /api/tree` | `window.list`, `workspace.list`, `surface.list` |
| `GET /api/screen?surface_id=&after_revision=` | `surface.wait_text_change` (scrollback, last 400 lines); on cmux builds without it (0.65.x), `surface.read_text` polled every 0.5 s |
| `POST /api/send {surface_id, text, enter}` | `surface.send_text`, then `surface.send_key enter` |
| `POST /api/key {surface_id, key}` | `surface.send_key` |
| `POST /api/workspace/select {workspace_id}` | `workspace.select` (switches the Mac's screen too) |
| `POST /api/workspace/create {cwd?}` | `workspace.create` |
| `GET /api/notifications` | `notification.list` |
| `POST /api/notifications/read {id \| all}` | `notification.mark_read` |
| `POST /api/notifications/dismiss {id}` | `notification.dismiss` |
| `GET /api/events` (SSE) | `events.stream` |

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
