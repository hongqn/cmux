"use strict";
// cmux web remote: a Claude-app-like client for cmux. The drawer lists cmux
// workspaces; each holds its agent sessions, terminals, browsers and documents.
// The main column shows one of them at a time with a view made for its kind.

const $ = (id) => document.getElementById(id);
const PHONE = matchMedia("(max-width: 900px)");
const saved = (key, fallback) => { try { return JSON.parse(localStorage.getItem(key)) ?? fallback; } catch (e) { return fallback; } };
const save = (key, value) => localStorage.setItem(key, JSON.stringify(value));

const state = {
  tree: null,
  notifications: [],
  sel: saved("cmuxSel", null),          // {wsId, surfaceId} or {wsId, view: "new"}
  expanded: new Set(saved("cmuxExpanded", [])),
  viewBySurface: saved("cmuxViews", {}), // agent surfaces: "chat" | "terminal"
  mounted: "",                          // key of what #view shows
  screens: {},
  fromBottom: {},
  fontMode: saved("cmuxFontMode", "fit"),
  chats: {},
  chatPolls: {},
  poll: null,
  browser: null,
  file: null,
  pendingNew: null,                     // {wsId, until}
  newCwd: null,                         // the folder input on the New screen
};

async function api(path, body, signal) {
  const opts = body === undefined ? { signal } : { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body), signal };
  const res = await fetch(path, opts);
  if (res.status === 401) { location.href = "/"; throw new Error("unauthorized"); }
  const data = await res.json();
  if (!res.ok) throw new Error(data.message || data.error || res.statusText);
  return data;
}

function el(tag, props, ...children) {
  const node = document.createElement(tag);
  Object.assign(node, props || {});
  for (const c of children) if (c != null) node.append(c);
  return node;
}
const sleep = (ms) => new Promise((ok) => setTimeout(ok, ms));

// cmux renders Markdown in descriptions and strips it from notification previews.
// One readable line: drop XML-ish wrappers agents put in prompts (<task-notification>, <pasted_content>).
function oneLine(text) {
  return plainMarkdown(String(text || "").replace(/<\/?[a-zA-Z_][\w-]*(\s[^>]*)?>/g, " ")).replace(/\s+/g, " ").trim();
}
function plainMarkdown(text) {
  return String(text || "").replace(/\x1b\[[0-9;?]*[ -\/]*[@-~]/g, "").replace(/\[([^\]]+)\]\([^)]+\)/g, "$1").replace(/(\*\*|__|`)/g, "");
}

// ---- model ----------------------------------------------------------------------
function allWorkspaces() {
  return ((state.tree && state.tree.windows) || []).flatMap((w) => w.workspaces.map((ws) => Object.assign(ws, { windowId: w.id })));
}
function findWorkspace(id) { return allWorkspaces().find((w) => w.id === id) || null; }
function findSurface(surfaceId) {
  for (const ws of allWorkspaces()) for (const pane of ws.panes) for (const s of pane.surfaces)
    if (s.id === surfaceId) return { ws, pane, surface: s };
  return null;
}
function isAgent(s) { return !!(s && s.agent); }
const KIND_RANK = (s) => (isAgent(s) ? 0 : s.type === "terminal" ? 1 : s.type === "browser" ? 2 : 3);
function wsItems(ws) {
  return ws.panes.flatMap((p) => p.surfaces).map((s, i) => ({ s, i }))
    .sort((a, b) => KIND_RANK(a.s) - KIND_RANK(b.s) || a.i - b.i).map((x) => x.s);
}
function cleanTitle(t) { return String(t || "").replace(/^[\s✳✻✽✶✢·⏺●◐◓◑◒⠁-⣿]+/u, "").trim(); }
function itemLabel(s) {
  if (s.type === "browser") {
    const title = cleanTitle(s.title);
    if (title) return title;
    try { return new URL(s.url).host; } catch (e) { return s.url || "Browser"; }
  }
  return cleanTitle(s.title) || s.ref;
}
function itemIcon(s) {
  if (isAgent(s)) return { t: "✳", c: "var(--acc)" };
  return { terminal: { t: "›_" }, browser: { t: "◍" }, markdown: { t: "¶" }, filepreview: { t: "▤" } }[s.type] || { t: "•" };
}
function agentState(s) { return s && s.agent ? s.agent.state : ""; }
function viewFor(s) {
  if (!s) return "none";
  if (s.type === "terminal") return s.agent && s.agent.chat && (state.viewBySurface[s.id] || "chat") === "chat" ? "chat" : "terminal";
  if (s.type === "browser") return "browser";
  if (s.type === "markdown" || s.type === "filepreview") return "file";
  return "unsupported";
}
function unreadFor(ws) {
  const listed = state.notifications.filter((n) => !n.is_read && n.workspace_id === ws.id).length;
  return Math.max(ws.unread_count || 0, listed);
}
function wsAttention(ws) {
  const items = wsItems(ws);
  // Live agent sessions are authoritative; sidebar status pills can be stale.
  if (items.some(isAgent)) {
    return { needsInput: items.some((s) => agentState(s) === "needs_input"), working: items.some((s) => agentState(s) === "working") };
  }
  const work = ((ws.sidebar && ws.sidebar.statuses) || []).map((x) => x.work);
  return {
    needsInput: items.some((s) => agentState(s) === "needs_input") || work.includes("waiting"),
    working: items.some((s) => agentState(s) === "working") || work.includes("running") || work.includes("subagents"),
  };
}
function selected() {
  const sel = state.sel;
  if (!sel) return { ws: null, surface: null };
  const ws = findWorkspace(sel.wsId);
  const hit = sel.surfaceId && findSurface(sel.surfaceId);
  return { ws: hit ? hit.ws : ws, surface: hit ? hit.surface : null };
}

// ---- data -----------------------------------------------------------------------
async function loadTree() {
  try {
    state.tree = await api("/api/tree");
  } catch (e) {
    $("wslist").replaceChildren(el("div", { className: "empty", textContent: "Error: " + e.message }));
    return;
  }
  if (state.pendingNew) {
    const ws = findWorkspace(state.pendingNew.wsId);
    const agent = ws && wsItems(ws).find(isAgent);
    if (agent) { state.pendingNew = null; select(ws.id, agent.id); return; }
    if (Date.now() > state.pendingNew.until) state.pendingNew = null;
  }
  const { ws, surface } = selected();
  if (!state.sel || !ws) {
    const onMac = allWorkspaces().find((w) => w.selected) || allWorkspaces()[0];
    if (onMac) { openWorkspace(onMac.id); return; }
  } else if (state.sel.surfaceId && !surface) {
    openWorkspace(ws.id);  // the surface closed
    return;
  }
  render();
}
let treeTimer = null;
function scheduleTree(ms = 400) { clearTimeout(treeTimer); treeTimer = setTimeout(loadTree, ms); }
setInterval(() => { if (!document.hidden) loadTree(); }, 10000);  // agent states change without events

// ---- navigation -----------------------------------------------------------------
function select(wsId, surfaceId, view) {
  state.sel = view ? { wsId, view } : { wsId, surfaceId };
  save("cmuxSel", state.sel);
  if (wsId && !state.expanded.has(wsId)) { state.expanded.add(wsId); save("cmuxExpanded", [...state.expanded]); }
  document.body.classList.remove("show-nav");
  render();
  markRead();
}
function openWorkspace(wsId) {
  const ws = findWorkspace(wsId);
  const first = ws && wsItems(ws)[0];
  select(wsId, first ? first.id : null);
}

function render() {
  renderNav();
  renderMain();
}

function renderNav() {
  const list = $("wslist");
  const scroll = list.scrollTop;
  const needle = $("filter").value.trim().toLowerCase();
  list.replaceChildren();
  const sel = state.sel || {};
  for (const ws of allWorkspaces()) {
    const items = wsItems(ws);
    const hay = [ws.title, ws.directory, ws.branch, ...items.map(itemLabel)].join(" ").toLowerCase();
    if (needle && !hay.includes(needle)) continue;
    const open = !!needle || state.expanded.has(ws.id);
    const attn = wsAttention(ws);
    const unread = unreadFor(ws);
    const subText = attn.needsInput ? "Needs your input"
      : oneLine(ws.latest_notification_text || (/^\s*</.test(ws.latest_message || "") ? "" : ws.latest_message) || ws.description || "")
        || [ws.directory, ws.branch].filter(Boolean).join(" · ");
    const head = el("div", { className: "ws-head" },
      ws.custom_color ? el("span", { className: "rail", style: "background:" + ws.custom_color }) : null,
      el("span", { className: "chev", textContent: "▸" }),
      el("div", { className: "meta" },
        el("div", { className: "name", textContent: ws.title || ws.ref }),
        subText ? el("div", { className: "sub" + (attn.needsInput ? " attn" : ""), textContent: subText }) : null),
      attn.working && !attn.needsInput ? el("span", { className: "spin" }) : null,
      unread ? el("span", { className: "badge", textContent: unread }) : null);
    head.onclick = () => {
      if (state.expanded.has(ws.id)) state.expanded.delete(ws.id); else state.expanded.add(ws.id);
      save("cmuxExpanded", [...state.expanded]);
      renderNav();
    };
    const box = el("div", { className: "ws" + (open ? " open" : "") + (ws.selected ? " mac" : "") }, head);
    const itemsBox = el("div", { className: "ws-items" });
    for (const s of items) {
      const icon = itemIcon(s);
      const st = agentState(s);
      const unreadItem = state.notifications.some((n) => !n.is_read && n.surface_id === s.id);
      const row = el("div", { className: "item" + (sel.surfaceId === s.id ? " on" : "") },
        el("span", { className: "ic", textContent: icon.t, style: icon.c ? "color:" + icon.c : "" }),
        el("span", { className: "lbl", textContent: itemLabel(s) }),
        st === "working" ? el("span", { className: "st working" })
          : st === "needs_input" ? el("span", { className: "st needs_input", textContent: "input" }) : null,
        unreadItem ? el("span", { className: "dot" }) : null);
      row.onclick = () => select(ws.id, s.id);
      itemsBox.append(row);
    }
    if (!items.length) itemsBox.append(el("div", { className: "item", textContent: "(empty)", style: "color:var(--dim)" }));
    box.append(itemsBox);
    list.append(box);
  }
  if (!list.children.length) list.append(el("div", { className: "empty", textContent: needle ? "No matches." : "No workspaces." }));
  list.scrollTop = scroll;
}

// ---- main column ------------------------------------------------------------------
function renderMain() {
  const sel = state.sel || {};
  const { ws, surface } = selected();
  const view = sel.view === "new" ? "new" : state.pendingNew && sel.wsId === state.pendingNew.wsId ? "starting" : viewFor(surface);
  const key = view + ":" + (surface ? surface.id : sel.wsId || "");
  renderHeader(ws, surface, view);
  if (key === state.mounted) { updateComposer(view, surface); return; }
  unmountView();
  state.mounted = key;
  const root = $("view");
  root.replaceChildren();
  $("urlbar").hidden = view !== "browser";
  if (view === "chat") mountChat(surface, root);
  else if (view === "terminal") mountTerminal(surface, root);
  else if (view === "browser") mountBrowser(surface, root);
  else if (view === "file") mountFile(surface, root);
  else if (view === "new") mountNew(root);
  else if (view === "starting") root.append(el("div", { className: "empty" }, el("div", { className: "spin", style: "margin:0 auto 10px" }), "Starting Claude…"));
  else if (view === "unsupported") root.append(el("div", { className: "empty", textContent: `${surface.type} panels are not shown here.` }));
  else root.append(el("div", { className: "empty", textContent: ws ? "This workspace is empty." : "Pick a workspace." }));
  updateComposer(view, surface);
  restartPolling();
  restartChatPolls();
}

function unmountView() {
  if (state.browser) {
    state.browser.abort.abort();
    api("/api/browser/release", { surface_id: state.browser.sid }).catch(() => {});
    state.browser = null;
  }
  if (state.file) { state.file.abort.abort(); state.file = null; }
}

function renderHeader(ws, surface, view) {
  $("title").textContent = view === "new" ? "New Claude session" : surface ? itemLabel(surface) : ws ? ws.title || ws.ref : "cmux";
  $("subtitle").textContent = view === "new" ? "" : ws ? [surface ? ws.title : null, ws.directory, ws.branch && ws.branch + (ws.git_dirty ? "*" : "")].filter(Boolean).join(" · ") : "";
  const actions = $("actions");
  actions.replaceChildren();
  if (surface && surface.agent && surface.agent.chat) {
    const toChat = view !== "chat";
    const b = el("button", { textContent: toChat ? "Chat" : "Terminal", title: toChat ? "Show the conversation" : "Show the terminal screen" });
    b.onclick = () => { state.viewBySurface[surface.id] = toChat ? "chat" : "terminal"; save("cmuxViews", state.viewBySurface); renderMain(); };
    actions.append(b);
  }
  if (view === "terminal" && PHONE.matches) {
    const b = el("button", { textContent: state.fontMode === "fit" ? "Aa+" : "Aa−", title: "Text size" });
    b.onclick = () => { state.fontMode = state.fontMode === "fit" ? "read" : "fit"; save("cmuxFontMode", state.fontMode); renderHeader(ws, surface, view); document.querySelectorAll("#view pre.term").forEach(restoreScroll); };
    actions.append(b);
  }
  if (ws) {
    const more = el("button", { className: "icon", textContent: "⋯", title: "More" });
    more.onclick = (e) => {
      e.stopPropagation();
      const entries = [["Show on Mac", async () => { await api("/api/workspace/select", { workspace_id: ws.id }); scheduleTree(100); }]];
      if (surface && surface.type === "browser" && surface.url) entries.push(["Open on this device", () => window.open(surface.url, "_blank", "noopener")]);
      showMenu(more, entries);
    };
    actions.append(more);
  }
}

function showMenu(anchor, entries) {
  const pop = $("menuPop");
  pop.replaceChildren(...entries.map(([label, fn]) => { const b = el("button", { textContent: label }); b.onclick = () => { pop.hidden = true; fn(); }; return b; }));
  const r = anchor.getBoundingClientRect();
  pop.style.top = r.bottom + 4 + "px";
  pop.style.right = Math.max(8, innerWidth - r.right) + "px";
  pop.hidden = false;
}
document.addEventListener("click", () => { $("menuPop").hidden = true; document.body.classList.remove("show-notifs"); });

// Opening an item marks its notifications read, as focusing it in cmux does.
function markRead() {
  const { ws, surface } = selected();
  if (!ws) return;
  const unread = state.notifications.filter((n) => !n.is_read && n.workspace_id === ws.id);
  const forItem = surface && unread.some((n) => n.surface_id === surface.id);
  const forWs = unread.filter((n) => !n.surface_id);
  if (!forItem && !forWs.length) return;
  for (const n of unread) if (!n.surface_id || (surface && n.surface_id === surface.id)) n.is_read = true;
  renderNotifications();
  renderNav();
  const calls = [];
  if (forItem) calls.push(api("/api/notifications/read", { workspace_id: ws.id, surface_id: surface.id }));
  for (const n of forWs) calls.push(api("/api/notifications/read", { id: n.id }));
  Promise.allSettled(calls).then(() => scheduleTree());
}

// ---- views: chat and terminal ------------------------------------------------------
function mountChat(surface, root) {
  const chat = chatState(surface.agent.session_id);
  chat.surfaceId = surface.id;
  chat.root.dataset.session = surface.agent.session_id;
  chat.scroll.dataset.surface = surface.id + ":chat";
  if (!chat.loaded) chat.status.textContent = "Loading conversation…";
  root.append(chat.root);
  restoreScroll(chat.scroll);
}

function mountTerminal(surface, root) {
  const pre = el("pre", { className: "term" });
  pre.dataset.surface = surface.id;
  pre.addEventListener("scroll", () => rememberScroll(pre), { passive: true });
  root.append(pre);
  const cached = state.screens[surface.id];
  if (cached) paintScreen(pre, cached); else pre.textContent = "Loading…";
  restoreScroll(pre);
}

// ---- view: browser ------------------------------------------------------------------
function mountBrowser(surface, root) {
  const img = el("img", { className: "shot", alt: "", draggable: false });
  const note = el("div", { className: "shot-note", textContent: "Loading page…" });
  const wrap = el("div", { className: "shot-wrap" }, img, note);
  root.append(wrap);
  $("url").value = surface.url || "";
  const b = state.browser = { sid: surface.id, img, wrap, note, hash: "", fast: 0, abort: new AbortController() };
  img.onload = () => { note.hidden = true; };
  attachBrowserGestures(b);
  browserLoop(b);
}

async function browserLoop(b) {
  while (state.browser === b) {
    await untilVisible(b);
    if (state.browser !== b) return;
    const box = b.wrap.getBoundingClientRect();
    try {
      const r = await api("/api/browser/shot", { surface_id: b.sid, hash: b.hash, width: Math.round(box.width), height: Math.round(box.height) }, b.abort.signal);
      if (state.browser !== b) return;
      if (r.png) { b.img.src = "data:image/png;base64," + r.png; b.hash = r.hash; }
      if (r.url && document.activeElement !== $("url")) $("url").value = r.url;
    } catch (e) {
      if (state.browser !== b || e.name === "AbortError") return;
      b.note.hidden = false;
      b.note.textContent = "Browser unavailable: " + e.message;
      await sleep(3000);
    }
    await sleep(Date.now() < b.fast ? 350 : 1500);
  }
}

function browserAct(body) {
  const b = state.browser;
  if (!b) return Promise.resolve();
  b.fast = Date.now() + 4000;
  return api("/api/browser/act", Object.assign({ surface_id: b.sid }, body)).catch((e) => { b.note.hidden = false; b.note.textContent = e.message; });
}

// Tap = click at that spot; drag or wheel = scroll the element under the pointer.
function attachBrowserGestures(b) {
  let start = null;
  const frac = (e) => {
    const r = b.img.getBoundingClientRect();
    return { fx: (e.clientX - r.left) / r.width, fy: (e.clientY - r.top) / r.height, r };
  };
  b.wrap.addEventListener("pointerdown", (e) => { start = { x: e.clientX, y: e.clientY, moved: false, lastY: e.clientY, lastX: e.clientX }; b.wrap.setPointerCapture(e.pointerId); });
  b.wrap.addEventListener("pointermove", (e) => {
    if (!start) return;
    if (Math.abs(e.clientY - start.y) > 8 || Math.abs(e.clientX - start.x) > 8) start.moved = true;
  });
  b.wrap.addEventListener("pointerup", (e) => {
    if (!start) return;
    const s = start; start = null;
    const { fx, fy, r } = frac(e);
    if (!r.width || !r.height) return;  // no screenshot yet
    if (!s.moved) {
      if (fx < 0 || fx > 1 || fy < 0 || fy > 1) return;
      const ring = el("div", { className: "tap-ring" });
      ring.style.left = e.clientX - b.wrap.getBoundingClientRect().left + "px";
      ring.style.top = e.clientY - b.wrap.getBoundingClientRect().top + "px";
      b.wrap.append(ring);
      setTimeout(() => ring.remove(), 600);
      browserAct({ action: "tap", fx, fy });
    } else {
      const sx = frac({ clientX: s.x, clientY: s.y });
      browserAct({ action: "scroll", fx: sx.fx, fy: sx.fy, dx: -(e.clientX - s.x) / r.width, dy: -(e.clientY - s.y) / r.height });
    }
  });
  let wheel = 0, wheelTimer = null, wheelAt = null;
  b.wrap.addEventListener("wheel", (e) => {
    e.preventDefault();
    wheel += e.deltaY;
    wheelAt = frac(e);
    if (!wheelAt.r.height) return;
    clearTimeout(wheelTimer);
    wheelTimer = setTimeout(() => { const d = wheel; wheel = 0; browserAct({ action: "scroll", fx: wheelAt.fx, fy: wheelAt.fy, dx: 0, dy: d / wheelAt.r.height }); }, 120);
  }, { passive: false });
}

for (const btn of document.querySelectorAll("#urlbar [data-nav]")) btn.onclick = () => browserAct({ action: btn.dataset.nav });
$("url").addEventListener("keydown", (e) => {
  if (e.key === "Enter") { e.preventDefault(); browserAct({ action: "navigate", url: $("url").value }); $("url").blur(); }
});

// ---- view: Markdown and file previews ---------------------------------------------------
function mountFile(surface, root) {
  const doc = el("div", { className: "doc" }, el("div", { className: "empty", textContent: "Loading…" }));
  root.append(doc);
  const f = state.file = { sid: surface.id, doc, stamp: "", abort: new AbortController(), markdown: surface.type === "markdown" };
  fileLoop(f);
}

async function fileLoop(f) {
  while (state.file === f) {
    await untilVisible(f);
    try {
      const r = await api("/api/file", { surface_id: f.sid, stamp: f.stamp }, f.abort.signal);
      if (state.file !== f) return;
      if (!r.unchanged) {
        f.stamp = r.stamp;
        const markdown = f.markdown || /\.(md|markdown|mdx)$/i.test(r.name || "");
        const top = f.doc.scrollTop;
        f.doc.replaceChildren(markdown
          ? el("div", { className: "msg bot", innerHTML: md(r.text) })
          : el("pre", { className: "code whole", textContent: r.text }));
        if (r.truncated) f.doc.append(el("div", { className: "empty", textContent: "(file truncated)" }));
        f.doc.scrollTop = top;
        $("subtitle").textContent = r.path || $("subtitle").textContent;
      }
    } catch (e) {
      if (state.file !== f || e.name === "AbortError") return;
      f.doc.replaceChildren(el("div", { className: "empty", textContent: e.message }));
    }
    await sleep(2500);
  }
}

// ---- view: new Claude session -------------------------------------------------------------
function mountNew(root) {
  const dirs = [...new Set(allWorkspaces().map((w) => w.directory).filter(Boolean))].slice(0, 12);
  const cwd = el("input", { placeholder: "~/path/to/project", value: saved("cmuxLastDir", dirs[0] || "~"), autocapitalize: "off", spellcheck: false });
  const chips = el("div", { className: "dirs" });
  const mark = () => chips.querySelectorAll("button").forEach((b) => b.classList.toggle("on", b.textContent === cwd.value));
  for (const d of dirs) {
    const b = el("button", { textContent: d });
    b.onclick = () => { cwd.value = d; mark(); };
    chips.append(b);
  }
  cwd.addEventListener("input", mark);
  mark();
  root.append(el("div", { className: "newchat" },
    el("h1", { textContent: "What should Claude work on?" }),
    el("label", { textContent: "Folder" }), cwd, chips));
  state.newCwd = cwd;
  $("input").focus();
}

async function startSession(prompt) {
  const cwd = state.newCwd && state.newCwd.value.trim();
  if (!cwd) return;
  save("cmuxLastDir", cwd);
  const r = await api("/api/session/new", { cwd, prompt });
  const wsId = r.workspace_id;
  state.pendingNew = { wsId, until: Date.now() + 45000 };
  select(wsId, null);
  for (let i = 0; i < 30 && state.pendingNew; i++) { await sleep(1500); await loadTree(); }
}
$("newChat").onclick = () => select(state.sel && state.sel.wsId, null, "new");

// ---- composer -------------------------------------------------------------------------------
const TERMINAL_KEYS = [["Enter", "enter"], ["Esc", "escape"], ["Ctrl-C", "ctrl+c"], ["Tab", "tab"], ["⇧Tab", "shift+tab"], ["↑", "up"], ["↓", "down"], ["←", "left"], ["→", "right"], ["Ctrl-D", "ctrl+d"], ["PgUp", "pageup"], ["PgDn", "pagedown"]];
const BROWSER_KEYS = [["Enter", "Enter"], ["Tab", "Tab"], ["⌫", "Backspace"], ["Esc", "Escape"], ["↑", "ArrowUp"], ["↓", "ArrowDown"]];
let composerMode = "";

function updateComposer(view, surface) {
  const mode = view === "chat" || view === "terminal" || view === "browser" || view === "new" ? view : "";
  const box = $("composer");
  box.hidden = !mode;
  document.body.classList.toggle("term-mode", mode === "terminal");
  const send = $("sendBtn");
  const chat = mode === "chat" && state.chats[surface.agent.session_id];
  const working = chat && chat.state === "working";
  const stop = working && !$("input").value.trim();
  send.classList.toggle("stop", !!stop);
  send.textContent = stop ? "■" : "↑";
  send.title = stop ? "Stop Claude" : "Send";
  if (mode === composerMode) return;
  composerMode = mode;
  $("input").placeholder = { chat: "Reply to Claude", terminal: "Type a command", browser: "Type into the page", new: "Describe a task (optional)" }[mode] || "";
  $("input").enterKeyHint = mode === "chat" && PHONE.matches ? "enter" : mode === "new" ? "go" : "send";
  const keys = $("keys");
  keys.replaceChildren();
  const list = mode === "terminal" ? TERMINAL_KEYS : mode === "browser" ? BROWSER_KEYS : [];
  for (const [label, key] of list) {
    const b = el("button", { textContent: label });
    b.addEventListener("pointerdown", (e) => { if (document.activeElement === $("input")) e.preventDefault(); });
    b.onclick = () => {
      const { surface: s } = selected();
      if (!s) return;
      if (composerMode === "browser") browserAct({ action: "key", key });
      else api("/api/key", { surface_id: s.id, key }).catch(() => {});
    };
    keys.append(b);
  }
  if (mode === "new") { $("sendBtn").textContent = "↑"; $("sendBtn").classList.remove("stop"); }
}

async function submit() {
  const input = $("input");
  const text = input.value;
  const { surface } = selected();
  if (composerMode === "new") {
    input.value = ""; growInput();
    await startSession(text.trim()).catch((e) => alert(e.message));
    return;
  }
  if (!surface) return;
  if (composerMode === "chat") {
    const chat = state.chats[surface.agent.session_id];
    if (!text.trim()) {
      if (chat && chat.state === "working") api("/api/chat/interrupt", { session_id: surface.agent.session_id }).catch(() => {});
      return;
    }
    input.value = ""; growInput();
    await sendChat(surface, text).catch(() => { input.value = text; growInput(); });
  } else if (composerMode === "terminal") {
    input.value = ""; growInput();
    await api("/api/send", { surface_id: surface.id, text, enter: true }).catch(() => { input.value = text; });
  } else if (composerMode === "browser") {
    if (!text) return;
    input.value = ""; growInput();
    await browserAct({ action: "type", text });
  }
  renderMain();
}

function growInput() {
  const box = $("input");
  box.style.height = "auto";
  box.style.height = Math.min(box.scrollHeight, 220) + "px";
  const { surface } = selected();
  if (composerMode === "chat") updateComposer("chat", surface);
}
$("input").addEventListener("input", growInput);
$("input").addEventListener("keydown", (e) => {
  if (e.key !== "Enter" || e.shiftKey || e.isComposing) return;
  if (composerMode === "chat" && PHONE.matches) return;  // Return adds a line; the button sends
  e.preventDefault();
  submit();
});
$("sendBtn").addEventListener("pointerdown", (e) => { if (document.activeElement === $("input")) e.preventDefault(); });
$("sendBtn").onclick = submit;

// ---- notifications ----------------------------------------------------------------------------
async function loadNotifications() {
  try {
    const r = await api("/api/notifications");
    state.notifications = r.notifications.slice().sort((a, b) => (b.created_at || "").localeCompare(a.created_at || ""));
    renderNotifications();
    if (state.tree) renderNav();
  } catch (e) { /* the status dot shows connectivity */ }
}

function jumpTo(n) {
  const hit = n.surface_id && findSurface(n.surface_id);
  if (hit) select(hit.ws.id, hit.surface.id);
  else if (n.workspace_id) openWorkspace(n.workspace_id);
}

function renderNotifications() {
  const list = $("nlist");
  list.replaceChildren();
  const unread = state.notifications.filter((n) => !n.is_read).length;
  $("unreadCount").textContent = unread;
  $("unreadCount").hidden = !unread;
  document.title = (unread ? "(" + unread + ") " : "") + "cmux";
  $("jumpUnread").hidden = $("clearAll").hidden = !state.notifications.length;
  $("jumpUnread").disabled = !unread;
  if (!state.notifications.length) {
    list.append(el("div", { className: "n-empty" }, el("b", { textContent: "No notifications yet" }), "Desktop notifications will appear here for quick review."));
    return;
  }
  for (const n of state.notifications) {
    const clear = el("button", { className: "x", textContent: "✕", title: "Clear notification" });
    clear.onclick = async (e) => {
      e.stopPropagation();
      state.notifications = state.notifications.filter((x) => x.id !== n.id);
      renderNotifications();
      try { await api("/api/notifications/dismiss", { id: n.id }); } catch (err) {}
      loadNotifications();
      scheduleTree();
    };
    const time = n.created_at ? new Date(n.created_at).toLocaleTimeString([], { hour: "numeric", minute: "2-digit" }) : "";
    const item = el("div", { className: "n" + (n.is_read ? "" : " unread") },
      el("span", { className: "dot" }),
      el("div", { className: "main" },
        el("div", { className: "top" }, el("span", { className: "nt", textContent: n.title || "(no title)" }), el("span", { className: "time", textContent: time })),
        n.body || n.subtitle ? el("div", { className: "nb", textContent: plainMarkdown([n.subtitle, n.body].filter(Boolean).join("\n")).trim() }) : null,
        n.tab_title ? el("div", { className: "tab", textContent: n.tab_title }) : null),
      clear);
    item.onclick = () => { jumpTo(n); document.body.classList.remove("show-notifs"); };
    list.append(item);
  }
}

$("bell").onclick = (e) => { e.stopPropagation(); document.body.classList.toggle("show-notifs"); };
$("notifs").onclick = (e) => e.stopPropagation();
$("jumpUnread").onclick = () => {
  const n = state.notifications.find((x) => !x.is_read);
  if (n) { jumpTo(n); document.body.classList.remove("show-notifs"); }
};
$("clearAll").onclick = async () => {
  state.notifications = [];
  renderNotifications();
  try { await api("/api/notifications/clear_all", {}); } catch (e) {}
  loadNotifications();
  scheduleTree();
};
function showBrowserNotification(n) {
  if (!n || !("Notification" in window) || Notification.permission !== "granted") return;
  if (document.visibilityState === "visible" && document.hasFocus()) return;
  const notice = new Notification(n.title || "cmux", { body: [n.subtitle, n.body].filter(Boolean).join("\n"), tag: n.id });
  notice.onclick = () => { window.focus(); jumpTo(n); };
}
$("enableNotify").onclick = async () => {
  if (!("Notification" in window)) { alert("This browser has no Notification API (on iOS, add the page to the Home Screen first)."); return; }
  const p = await Notification.requestPermission();
  if (p !== "granted") alert("Browser notifications need HTTPS (or localhost) and permission.");
};

// ---- live events -------------------------------------------------------------------------------
function connectEvents() {
  const es = new EventSource("/api/events");
  es.onmessage = (msg) => {
    const m = JSON.parse(msg.data);
    if (m.type === "status") {
      $("status").className = m.connected ? "ok" : "bad";
      $("status").title = m.connected ? "connected to cmux" : "cmux offline";
      if (m.connected) { loadTree(); loadNotifications(); }
      return;
    }
    if (m.type === "sidebar") {
      const ws = findWorkspace(m.workspace_id);
      if (ws) { ws.sidebar = m.sidebar; renderNav(); }
      return;
    }
    if (m.category === "notification") {
      if (m.name === "notification.created") showBrowserNotification(m.notification);
      loadNotifications();
      scheduleTree();
    } else if (m.category === "workspace" || m.category === "surface") {
      scheduleTree();
    }
  };
  es.onerror = () => { $("status").className = "bad"; $("status").title = "reconnecting"; };
}

// ---- shell ---------------------------------------------------------------------------------------
$("menu").onclick = (e) => { e.stopPropagation(); document.body.classList.toggle("show-nav"); };
$("scrim").onclick = () => document.body.classList.remove("show-nav");
$("filter").addEventListener("input", renderNav);
PHONE.addEventListener("change", () => { composerMode = ""; state.mounted = ""; render(); });

// iOS keeps the layout viewport when the keyboard opens; size the page to what is visible.
if (window.visualViewport) {
  const fitViewport = () => {
    document.documentElement.style.setProperty("--vvh", visualViewport.height + "px");
    if (visualViewport.offsetTop) window.scrollTo(0, 0);
    document.querySelectorAll("#view pre.term, #view .chat-scroll").forEach(restoreScroll);
  };
  visualViewport.addEventListener("resize", fitViewport);
  visualViewport.addEventListener("scroll", fitViewport);
  fitViewport();
}

// ---- agent chat view ----------------------------------------------------------
// Terminals running Claude Code can show their conversation instead of the
// screen: read from the session's transcript, so text wraps to the page rather
// than to the terminal's columns. Input still goes to the same terminal.

function esc(s) {
  return s.replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Markdown subset: fences, headings, lists, quotes, tables, rules, inline code,
// bold, italics, http(s) links. Text is escaped before any markup is added.
function mdInline(raw) {
  const codes = [];
  let s = esc(raw).replace(/`([^`\n]+)`/g, (m, c) => { codes.push(c); return `\u0000${codes.length - 1}\u0000`; });
  s = s.replace(/\[([^\]\n]+)\]\((https?:\/\/[^\s)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  s = s.replace(/(^|[\s(])(https?:\/\/[^\s<)]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
  // Relative links point into the Mac's filesystem; keep the label only.
  s = s.replace(/\[([^\]\n]+)\]\(([^)\s]+)\)/g, '<span class="rel" title="$2">$1</span>');
  s = s.replace(/\*\*([^*\n]+)\*\*/g, "<b>$1</b>").replace(/(^|[^*\w])\*([^*\n]+)\*(?!\w)/g, "$1<i>$2</i>");
  return s.replace(/\u0000(\d+)\u0000/g, (m, i) => `<code>${codes[+i]}</code>`);
}

function md(text) {
  const lines = text.replace(/\r/g, "").split("\n");
  const out = [];
  const isList = (l) => /^\s*([-*+]|\d+[.)])\s+/.test(l);
  const isBlockStart = (l) => /^\s*(```|#{1,6}\s|>|([-*+]|\d+[.)])\s+|\|)/.test(l);
  let i = 0;
  while (i < lines.length) {
    const l = lines[i];
    if (/^\s*```/.test(l)) {
      const buf = [];
      i++;
      while (i < lines.length && !/^\s*```/.test(lines[i])) buf.push(lines[i++]);
      i++;
      out.push(`<pre class="code">${esc(buf.join("\n"))}</pre>`);
    } else if (/^\s*\|.*\|\s*$/.test(l) && i + 1 < lines.length && /^\s*\|[\s:|-]+\|\s*$/.test(lines[i + 1])) {
      const row = (r) => r.trim().replace(/^\||\|$/g, "").split("|").map((c) => mdInline(c.trim()));
      const head = row(l);
      i += 2;
      const body = [];
      while (i < lines.length && /^\s*\|.*\|\s*$/.test(lines[i])) body.push(row(lines[i++]));
      out.push(`<div class="tablewrap"><table><tr>${head.map((c) => `<th>${c}</th>`).join("")}</tr>` +
        body.map((r) => `<tr>${r.map((c) => `<td>${c}</td>`).join("")}</tr>`).join("") + "</table></div>");
    } else if (/^#{1,6}\s/.test(l)) {
      const level = l.match(/^#+/)[0].length;
      out.push(`<div class="h h${Math.min(level, 3)}">${mdInline(l.replace(/^#+\s*/, ""))}</div>`);
      i++;
    } else if (isList(l)) {
      const ordered = /^\s*\d/.test(l);
      const items = [];
      while (i < lines.length && (isList(lines[i]) || (/^\s{2,}\S/.test(lines[i]) && items.length))) {
        const m = lines[i].match(/^(\s*)([-*+]|\d+[.)])\s+(.*)$/);
        if (m) items.push({ indent: m[1].length, text: m[3] });
        else items[items.length - 1].text += " " + lines[i].trim();
        i++;
      }
      const tag = ordered ? "ol" : "ul";
      out.push(`<${tag}>` + items.map((it) => `<li style="margin-left:${Math.min(it.indent, 8) * 0.6}em">${mdInline(it.text)}</li>`).join("") + `</${tag}>`);
    } else if (/^\s*>/.test(l)) {
      const buf = [];
      while (i < lines.length && /^\s*>/.test(lines[i])) buf.push(lines[i++].replace(/^\s*>\s?/, ""));
      out.push(`<blockquote>${md(buf.join("\n"))}</blockquote>`);
    } else if (/^\s*(-{3,}|\*{3,}|_{3,})\s*$/.test(l)) {
      out.push("<hr>");
      i++;
    } else if (!l.trim()) {
      i++;
    } else {
      const buf = [];
      while (i < lines.length && lines[i].trim() && !(buf.length && isBlockStart(lines[i]))) buf.push(lines[i++]);
      out.push(`<p>${buf.map(mdInline).join("<br>")}</p>`);
    }
  }
  return out.join("");
}

const TOOL_ICONS = { Bash: "$", Edit: "✎", MultiEdit: "✎", Write: "✎", NotebookEdit: "✎", Read: "📄", Grep: "🔍", Glob: "🔍",
                     WebFetch: "🌐", WebSearch: "🌐", Task: "◆", Agent: "◆", TodoWrite: "☑", AskUserQuestion: "?" };

function chatState(sid) {
  if (!state.chats[sid]) {
    const scroll = el("div", { className: "chat-scroll" });
    const older = el("button", { className: "chat-older", textContent: "Load earlier messages", hidden: true });
    const msgs = el("div", { className: "msgs" });
    const status = el("div", { className: "chat-status" });
    scroll.append(older, msgs);
    const root = el("div", { className: "chat" }, scroll, status);
    const chat = state.chats[sid] = { sid, root, scroll, older, msgs, status, results: {}, tools: {}, questions: {},
                                      start: 0, end: 0, state: "", loaded: false, peek: "", surfaceId: null };
    scroll.addEventListener("scroll", () => {
      rememberScroll(scroll);
      if (scroll.scrollTop < 80 && chat.start > 0 && !chat.loadingOlder) loadOlder(chat);
    }, { passive: true });
    older.onclick = () => loadOlder(chat);
  }
  return state.chats[sid];
}

function toolRow(item) {
  const row = el("details", { className: "step" });
  row.dataset.call = item.id;
  const icon = TOOL_ICONS[item.tool] || "⚙";
  const name = item.tool.startsWith("mcp__") ? item.tool.split("__").slice(-1)[0] : item.tool;
  const head = el("summary", {},
    el("span", { className: "st" }),
    el("span", { className: "ti", textContent: icon }),
    el("b", { textContent: name }),
    el("span", { className: "ts", textContent: item.tool === "Bash" ? (item.note || item.summary) : item.summary || "" }));
  if (item.additions || item.deletions) {
    head.append(el("span", { className: "diffstat", innerHTML: `<i>+${item.additions}</i> <u>−${item.deletions}</u>` }));
  }
  row.append(head);
  const body = el("div", { className: "sb" });
  if (item.command) body.append(el("pre", { className: "code", textContent: "$ " + item.command }));
  if (item.diff) {
    const pre = el("pre", { className: "code diff" });
    for (const line of item.diff.split("\n")) {
      pre.append(el("span", { className: line[0] === "+" ? "add" : line[0] === "-" ? "del" : "", textContent: line + "\n" }));
    }
    body.append(pre);
  }
  if (item.todos) {
    body.append(el("div", { className: "todos", innerHTML: item.todos.map((t) =>
      `<div class="${t.status}">${t.status === "completed" ? "☑" : t.status === "in_progress" ? "◐" : "☐"} ${esc(t.text)}</div>`).join("") }));
  }
  if (item.detail) body.append(el("pre", { className: "code", textContent: item.detail }));
  body.append(el("pre", { className: "code out", hidden: true }));
  row.append(body);
  return row;
}

function applyResult(chat, result) {
  const row = chat.tools[result.call_id];
  if (row) {
    row.classList.add(result.is_error ? "err" : "ok");
    const out = row.querySelector("pre.out");
    if (result.text) { out.hidden = false; out.textContent = result.text; }
    refreshGroup(row.closest(".steps"));
  }
  for (const card of chat.questions[result.call_id] || []) {
    card.classList.add("answered");
    const ans = card.querySelector(".answer");
    ans.textContent = result.text.replace(/^User has answered[^:]*:\s*/i, "").slice(0, 400);
  }
}

function refreshGroup(group) {
  if (!group) return;
  const rows = group.querySelectorAll(".step");
  const errors = group.querySelectorAll(".step.err").length;
  const running = [...rows].filter((r) => !r.classList.contains("ok") && !r.classList.contains("err")).length;
  const last = rows[rows.length - 1];
  const lastText = last ? last.querySelector("b").textContent + " " + last.querySelector(".ts").textContent : "";
  group.querySelector(":scope > summary .gl").textContent =
    `${rows.length} step${rows.length === 1 ? "" : "s"}` + (errors ? ` · ${errors} failed` : "") + (running ? " · running" : "");
  group.querySelector(":scope > summary .gs").textContent = lastText;
}

function itemNode(chat, item) {
  if (item.kind === "user") return el("div", { className: "msg user", textContent: item.text });
  if (item.kind === "assistant") return el("div", { className: "msg bot", innerHTML: md(item.text) });
  if (item.kind === "note") return el("div", { className: "msg note", textContent: item.text });
  if (item.kind === "question") {
    const card = el("div", { className: "msg question" },
      el("div", { className: "qh", textContent: item.header || "Question" }),
      el("div", { className: "qp", innerHTML: md(item.prompt) }));
    const opts = el("div", { className: "qo" });
    item.options.forEach((o, i) => {
      const b = el("button", {}, el("b", { textContent: `${i + 1}. ${o.label}` }), o.detail ? el("span", { textContent: o.detail }) : null);
      b.onclick = () => answer(chat, i);
      opts.append(b);
    });
    card.append(opts, el("div", { className: "answer" }));
    (chat.questions[item.call_id] = chat.questions[item.call_id] || []).push(card);
    return card;
  }
  return null;
}

// Append items in transcript order; consecutive tool calls share one collapsed group.
function addItems(chat, items, prepend) {
  const frag = document.createDocumentFragment();
  let group = prepend ? null : (chat.msgs.lastElementChild && chat.msgs.lastElementChild.classList.contains("steps") ? chat.msgs.lastElementChild : null);
  const results = [];
  for (const item of items) {
    if (item.kind === "result") { results.push(item); continue; }
    if (item.kind === "tool") {
      if (!group) {
        group = el("details", { className: "steps" }, el("summary", {}, el("span", { className: "gl" }), el("span", { className: "gs" })));
        frag.append(group);
      }
      const row = toolRow(item);
      chat.tools[item.id] = row;
      group.append(row);
      refreshGroup(group);
      continue;
    }
    if (item.kind === "user") chat.msgs.querySelectorAll(".msg.pending").forEach((p) => p.remove());
    const node = itemNode(chat, item);
    if (node) { group = null; frag.append(node); }
  }
  if (prepend) chat.msgs.prepend(frag); else chat.msgs.append(frag);
  for (const r of results) { chat.results[r.call_id] = r; applyResult(chat, r); }
  // Results that arrived before their call (loaded older pages later).
  if (prepend) for (const r of Object.values(chat.results)) applyResult(chat, r);
}

async function loadOlder(chat) {
  chat.loadingOlder = true;
  try {
    const page = await api("/api/chat/history", { session_id: chat.sid, before: chat.start });
    const fromBottom = chat.scroll.scrollHeight - chat.scroll.scrollTop;
    addItems(chat, page.items, true);
    chat.start = page.start;
    chat.older.hidden = chat.start <= 0;
    chat.scroll.scrollTop = chat.scroll.scrollHeight - fromBottom;
  } catch (e) { /* try again on the next scroll */ }
  chat.loadingOlder = false;
}

function setChatState(chat, value) {
  chat.state = value;
  const cur = selected().surface;
  if (cur && cur.agent && cur.agent.session_id === chat.sid) { cur.agent.state = value; updateComposer("chat", cur); renderNav(); }
  const st = chat.status;
  st.replaceChildren();
  st.className = "chat-status " + value;
  if (value === "working") {
    const stop = el("button", { textContent: "Stop" });
    stop.onclick = () => api("/api/chat/interrupt", { session_id: chat.sid }).catch(() => {});
    st.append(el("span", { className: "spin" }), el("span", { textContent: "Claude is working…" }), stop);
  } else if (value === "needs_input") {
    // Permission prompts are drawn in the terminal and reach the transcript only
    // once answered, so show the bottom of the screen with number keys.
    const keys = el("div", { className: "peek-keys" });
    for (const n of ["1", "2", "3"]) {
      const b = el("button", { textContent: n });
      b.onclick = () => chat.surfaceId && api("/api/send", { surface_id: chat.surfaceId, text: n, enter: false }).then(() => refreshPeek(chat));
      keys.append(b);
    }
    const escB = el("button", { textContent: "Esc" });
    escB.onclick = () => chat.surfaceId && api("/api/key", { surface_id: chat.surfaceId, key: "escape" }).then(() => refreshPeek(chat));
    const term = el("button", { textContent: "Terminal" });
    term.onclick = () => { state.viewBySurface[chat.surfaceId] = "terminal"; save("cmuxViews", state.viewBySurface); renderMain(); };
    keys.append(escB, term);
    st.append(el("div", { className: "peek-head", textContent: "Claude needs your input" }), el("pre", { className: "peek", textContent: chat.peek }), keys);
    refreshPeek(chat);
  } else if (value === "ended") {
    st.append(el("span", { textContent: "This session has ended." }));
  }
}

async function refreshPeek(chat) {
  if (!chat.surfaceId) return;
  try {
    const r = await api("/api/peek", { surface_id: chat.surfaceId });
    const lines = (r.text || "").replace(/\s+$/, "").split("\n");
    chat.peek = lines.slice(-14).join("\n");
    const pre = chat.status.querySelector(".peek");
    if (pre) pre.textContent = chat.peek;
  } catch (e) { /* keep the last one */ }
}

function answer(chat, index) {
  if (!chat.surfaceId) return;
  api("/api/send", { surface_id: chat.surfaceId, text: String(index + 1), enter: false }).then(() => refreshPeek(chat)).catch(() => {});
}

async function chatLoop(poll, sid) {
  const chat = chatState(sid);
  while (state.chatPolls[sid] === poll) {
    await untilVisible(poll);
    if (state.chatPolls[sid] !== poll) return;
    try {
      if (!chat.loaded) {
        const page = await api("/api/chat/history", { session_id: sid }, poll.abort.signal);
        chat.msgs.replaceChildren();
        Object.assign(chat, { results: {}, tools: {}, questions: {}, start: page.start, end: page.end, loaded: true });
        addItems(chat, page.items, false);
        chat.older.hidden = chat.start <= 0;
        setChatState(chat, page.state);
        restoreScroll(chat.scroll);
        continue;
      }
      const upd = await api("/api/chat/poll", { session_id: sid, after: chat.end, state: chat.state }, poll.abort.signal);
      if (state.chatPolls[sid] !== poll) return;
      if (upd.reset) { chat.loaded = false; continue; }
      chat.end = upd.end;
      if (upd.items.length) { addItems(chat, upd.items, false); restoreScroll(chat.scroll); }
      if (upd.state !== chat.state) setChatState(chat, upd.state);
      else if (upd.state === "needs_input") refreshPeek(chat);
    } catch (e) {
      if (state.chatPolls[sid] !== poll || e.name === "AbortError") return;
      chat.status.className = "chat-status error";
      chat.status.textContent = "Chat unavailable: " + e.message;
      await new Promise((ok) => setTimeout(ok, 3000));
    }
  }
}

function restartChatPolls() {
  const want = new Set([...document.querySelectorAll("#view .chat")].map((c) => c.dataset.session));
  for (const [sid, poll] of Object.entries(state.chatPolls)) {
    if (!want.has(sid)) { poll.abort.abort(); delete state.chatPolls[sid]; }
  }
  for (const sid of want) {
    if (state.chatPolls[sid]) continue;
    const poll = state.chatPolls[sid] = { abort: new AbortController() };
    chatLoop(poll, sid);
  }
}

async function sendChat(s, text) {
  const chat = chatState(s.agent.session_id);
  const pending = el("div", { className: "msg user pending", textContent: text });
  chat.msgs.append(pending);
  delete state.fromBottom[chat.scroll.dataset.surface];
  restoreScroll(chat.scroll);
  try {
    await api("/api/chat/send", { session_id: s.agent.session_id, text });
  } catch (e) {
    pending.classList.add("failed");
    pending.title = e.message;
    throw e;
  }
}

// ---- terminal painting ------------------------------------------------------
function fitFont(pre, columns) {
  const width = pre.clientWidth - 10;
  if (!columns || width <= 0) return;
  if (PHONE.matches && state.fontMode === "read") { pre.style.fontSize = "11px"; return; }
  // Menlo / SF Mono advance is about 0.6em. Phones go smaller so the whole width fits.
  const floor = PHONE.matches ? 4.5 : 7;
  pre.style.fontSize = Math.max(floor, Math.min(13, width / (columns * 0.6))) + "px";
}

// Scroll position lives in state, not in the <pre>: the tree refresh rebuilds
// every pane, and a fresh <pre> would otherwise start at the top.
function rememberScroll(pre) {
  if (!pre.isConnected || pre.restoring) return;
  const up = pre.scrollHeight - pre.scrollTop - pre.clientHeight;
  if (up < 40) delete state.fromBottom[pre.dataset.surface];
  else state.fromBottom[pre.dataset.surface] = up;
}

function restoreScroll(pre) {
  if (!pre.isConnected) return;
  const s = state.screens[pre.dataset.surface];
  if (s && s.grid) fitFont(pre, s.grid.columns);
  const up = state.fromBottom[pre.dataset.surface] || 0;
  pre.restoring = true;
  pre.scrollTop = Math.max(0, pre.scrollHeight - pre.clientHeight - up);
  requestAnimationFrame(() => { pre.restoring = false; });
}

function paintScreen(pre, screen) {
  if (screen.grid) paintGrid(pre, screen.grid);
  else { pre.style.color = ""; pre.style.background = ""; pre.textContent = screen.text || (screen.error ? "[" + screen.error + "]" : ""); }
  restoreScroll(pre);
}

function paintGrid(pre, grid) {
  pre.style.color = grid.foreground;
  pre.style.background = grid.background;
  fitFont(pre, grid.columns);
  const frag = document.createDocumentFragment();
  const cur = grid.cursor || {};
  grid.lines.forEach((spans, lineNo) => {
    let col = 0;
    let pending = "";
    const flush = () => { if (pending) { frag.append(pending); pending = ""; } };
    const styled = (text, sid, extraClass) => {
      const css = grid.styles[String(sid)] || "";
      if (!css && !extraClass) { pending += text; return; }
      flush();
      const span = document.createElement("span");
      if (css) span.style.cssText = css;
      if (extraClass) span.className = extraClass;
      span.textContent = text;
      frag.append(span);
    };
    const cursorCol = cur.visible && cur.line === lineNo ? cur.column : -1;
    for (const [c, w, text, sid] of spans) {
      if (c > col) { pending += " ".repeat(c - col); col = c; }
      if (cursorCol >= col && cursorCol < c + w) {
        // Split the span around the cursor cell.
        const chars = [...text];
        const cw = chars.length ? w / chars.length : 1;
        const at = Math.min(chars.length - 1, Math.floor((cursorCol - c) / cw));
        if (at > 0) styled(chars.slice(0, at).join(""), sid);
        styled(chars[at] || " ", sid, "cur");
        if (at + 1 < chars.length) styled(chars.slice(at + 1).join(""), sid);
      } else {
        styled(text, sid);
      }
      col = c + w;
    }
    if (cursorCol >= col) { pending += " ".repeat(cursorCol - col); styled(" ", 0, "cur"); }
    pending += "\n";
    flush();
  });
  pre.replaceChildren(frag);
}

// ---- polling every visible terminal in one long-poll -------------------------
function visibleTerminals() {
  return [...document.querySelectorAll("#view pre.term")].map((p) => p.dataset.surface);
}

function restartPolling() {
  const ids = visibleTerminals();
  const key = ids.join(",");
  if (state.poll && state.poll.key === key) return;
  if (state.poll) state.poll.abort.abort();
  if (!ids.length) { state.poll = null; return; }
  state.poll = { key, abort: new AbortController() };
  pollLoop(state.poll, ids);
}

// Agents animate (spinners, timers, a blinking dot), so a busy terminal
// changes every few hundred ms. Redraw at most once a second, and not at all
// while the tab is in the background.
const MIN_POLL_INTERVAL_MS = 1000;

function untilVisible(poll) {
  if (!document.hidden) return Promise.resolve();
  return new Promise((ok) => {
    const done = () => { document.removeEventListener("visibilitychange", check); poll.abort.signal.removeEventListener("abort", done); ok(); };
    const check = () => { if (!document.hidden) done(); };
    document.addEventListener("visibilitychange", check);
    poll.abort.signal.addEventListener("abort", done);
  });
}

async function pollLoop(poll, ids) {
  let lastStart = 0;
  while (state.poll === poll) {
    await untilVisible(poll);
    const wait = lastStart + MIN_POLL_INTERVAL_MS - Date.now();
    if (wait > 0) await new Promise((ok) => setTimeout(ok, wait));
    if (state.poll !== poll) return;
    lastStart = Date.now();
    const surfaces = Object.fromEntries(ids.map((id) => [id, (state.screens[id] || {}).revision || ""]));
    try {
      const r = await api("/api/screens", { surfaces }, poll.abort.signal);
      if (state.poll !== poll) return;
      for (const [id, screen] of Object.entries(r.screens)) {
        if (!screen.changed) continue;
        state.screens[id] = screen;
        document.querySelectorAll(`pre.term[data-surface="${id}"]`).forEach((pre) => paintScreen(pre, screen));
      }
    } catch (e) {
      if (state.poll !== poll || e.name === "AbortError") return;
      await new Promise((ok) => setTimeout(ok, 3000));
    }
  }
}

new ResizeObserver(() => {
  // A new size or font changes the content height; keep the same distance from the bottom.
  for (const pre of document.querySelectorAll("#view pre.term, #view .chat-scroll")) restoreScroll(pre);
}).observe($("view"));


// ---- start ----------------------------------------------------------------------------------------
loadTree();
loadNotifications();
connectEvents();
