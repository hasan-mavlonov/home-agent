"use strict";

// ---------------------------------------------------------------------------
// basics

const state = { csrf: null, tz: undefined, settings: null, timers: [], overview: null, lastNotifCount: 0 };

class ApiError extends Error {
  constructor(message, status) { super(message); this.status = status; }
}

function h(tag, attrs, ...children) {
  const el = document.createElement(tag);
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value === undefined || value === null || value === false) continue;
    if (key === "class") el.className = value;
    else if (key === "text") el.textContent = value;
    else if (key === "html") el.innerHTML = value; // only for HTML produced by the server's safe renderer
    else if (key.startsWith("on") && typeof value === "function") el.addEventListener(key.slice(2), value);
    else if (value === true) el.setAttribute(key, "");
    else el.setAttribute(key, String(value));
  }
  for (const child of children.flat(Infinity)) {
    if (child === undefined || child === null || child === false) continue;
    el.append(child instanceof Node ? child : document.createTextNode(String(child)));
  }
  return el;
}

async function api(method, path, body) {
  const headers = { Accept: "application/json" };
  const init = { method, headers, credentials: "same-origin" };
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";
  if (body instanceof FormData) init.body = body;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; init.body = JSON.stringify(body); }
  const res = await fetch(path, init);
  let data = null;
  if ((res.headers.get("content-type") || "").includes("application/json")) data = await res.json();
  if (res.status === 401 && !path.startsWith("/api/auth/")) { showLogin(); throw new ApiError("Please sign in again", 401); }
  if (!res.ok) throw new ApiError((data && data.detail) || res.statusText || "Request failed", res.status);
  return data;
}

function toast(message, isError) {
  const el = document.getElementById("toast");
  el.textContent = message;
  el.className = "toast" + (isError ? " error" : "");
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, isError ? 6000 : 3000);
}

async function guarded(fn, okMessage) {
  try {
    const result = await fn();
    if (okMessage) toast(okMessage);
    return result;
  } catch (err) {
    if (err.status !== 401) toast(err.message || String(err), true);
    return undefined;
  }
}

function every(ms, fn) {
  const id = setInterval(fn, ms);
  state.timers.push(id);
  return id;
}

function clearTimers() {
  for (const id of state.timers) clearInterval(id);
  state.timers = [];
}

function fmtTime(iso, withDate = true) {
  if (!iso) return "";
  const date = new Date(iso);
  try {
    return new Intl.DateTimeFormat(undefined, withDate
      ? { timeZone: state.tz, dateStyle: "medium", timeStyle: "short" }
      : { timeZone: state.tz, timeStyle: "medium" }).format(date);
  } catch { return date.toLocaleString(); }
}

function fmtAgo(iso) {
  if (!iso) return "";
  const seconds = (Date.now() - new Date(iso).getTime()) / 1000;
  if (seconds < 60) return "just now";
  if (seconds < 3600) return `${Math.round(seconds / 60)} min ago`;
  if (seconds < 86400) return `${Math.round(seconds / 3600)} h ago`;
  return fmtTime(iso);
}

function money(value) {
  if (value === null || value === undefined) return "-";
  return value > 0 && value < 0.01 ? "<$0.01" : `$${Number(value).toFixed(2)}`;
}

function statusChip(status, label) {
  return h("span", { class: `status ${status}` }, label || String(status || "").replace(/_/g, " "));
}

function setTitle(text) { document.title = text ? `${text} - Home Agent` : "Home Agent"; }

function view() { return document.getElementById("view"); }

function render(...nodes) {
  view().replaceChildren(...nodes.flat(Infinity).filter((n) => n !== null && n !== undefined && n !== false));
}

function linkTo(url, label) {
  if (!url) return null;
  return h("a", { href: url, target: "_blank", rel: "noopener noreferrer" }, label || url);
}

function truncate(text, n) {
  text = text || "";
  return text.length > n ? text.slice(0, n - 1) + "…" : text;
}

function isActive(task) { return task && (task.status === "queued" || task.status === "running"); }

// ---------------------------------------------------------------------------
// auth

function showLogin(hint) {
  clearTimers();
  document.getElementById("app").hidden = true;
  document.getElementById("login").hidden = false;
  if (hint) document.getElementById("login-hint").textContent = hint;
  document.getElementById("password").focus();
}

async function boot() {
  let info;
  try { info = await api("GET", "/api/auth/session"); }
  catch { showLogin("Cannot reach Home Agent."); return; }
  if (!info.authenticated) {
    showLogin(info.password_set ? "Sign in to your agent." : "No password is set yet. On the Mac, run: uv run home-agent set-password");
    return;
  }
  state.csrf = info.csrf_token;
  await startApp();
}

document.getElementById("login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const error = document.getElementById("login-error");
  error.textContent = "";
  try {
    const res = await api("POST", "/api/auth/login", { password: document.getElementById("password").value });
    state.csrf = res.csrf_token;
    document.getElementById("password").value = "";
    await startApp();
  } catch (err) { error.textContent = err.message; }
});

async function startApp() {
  document.getElementById("login").hidden = true;
  document.getElementById("app").hidden = false;
  await loadSettings();
  refreshOverview();
  if (!state.overviewTimer) state.overviewTimer = setInterval(refreshOverview, 15000);
  route();
}

async function loadSettings() {
  try {
    state.settings = await api("GET", "/api/settings");
    state.tz = state.settings.timezone;
    const provider = state.settings.providers.find((p) => p.name === state.settings.model_provider);
    const badge = document.getElementById("mode-badge");
    if (provider) {
      badge.textContent = (provider.local ? "Local · " : "Cloud · ") + provider.label;
      badge.className = "badge " + (provider.local ? "local" : "cloud");
      badge.title = provider.local ? "Prompts stay on this computer or your private network" : "Prompts are sent to the cloud model provider";
    }
  } catch { /* shown elsewhere */ }
}

async function signOut() {
  await guarded(() => api("POST", "/api/auth/logout"));
  state.csrf = null;
  showLogin("Signed out.");
}

// ---------------------------------------------------------------------------
// overview + notifications

async function refreshOverview() {
  if (document.getElementById("app").hidden) return;
  let data;
  try { data = await api("GET", "/api/overview"); } catch { return; }
  state.overview = data;
  const count = document.getElementById("bell-count");
  count.hidden = !data.unread_notifications;
  count.textContent = data.unread_notifications > 99 ? "99+" : String(data.unread_notifications);
  const active = document.getElementById("active-indicator");
  active.hidden = !data.active_tasks.length;
  active.textContent = data.active_tasks.length === 1 ? "1 task working" : `${data.active_tasks.length} tasks working`;
  const opps = document.getElementById("nav-new-opps");
  opps.hidden = !data.new_opportunities;
  opps.textContent = `${data.new_opportunities} new`;
  const scheds = document.getElementById("nav-pending-schedules");
  scheds.hidden = !data.pending_schedules;
  scheds.textContent = `${data.pending_schedules} to confirm`;
  if (data.unread_notifications > state.lastNotifCount && state.lastNotifCount !== 0) toast("New notification");
  state.lastNotifCount = data.unread_notifications;
}

async function openNotifications() {
  const panel = document.getElementById("notifications");
  if (!panel.hidden) { panel.hidden = true; return; }
  panel.hidden = false;
  const list = document.getElementById("notif-list");
  list.replaceChildren(h("p", { class: "empty" }, "Loading..."));
  const data = await guarded(() => api("GET", "/api/notifications"));
  if (!data) return;
  if (!data.items.length) { list.replaceChildren(h("p", { class: "empty" }, "No notifications yet.")); return; }
  list.replaceChildren(...data.items.map((n) => h("div", {
    class: "notif" + (n.read_at ? "" : " unread"),
    onclick: async () => {
      await guarded(() => api("POST", "/api/notifications/read", { ids: [n.id] }));
      panel.hidden = true;
      refreshOverview();
      if (n.link) location.hash = n.link;
    },
  }, h("div", { class: "title" }, n.title), n.body ? h("div", { class: "small" }, n.body) : null,
  h("div", { class: "small muted" }, fmtAgo(n.created_at)))));
}

document.getElementById("bell").addEventListener("click", openNotifications);
document.getElementById("notif-close").addEventListener("click", () => { document.getElementById("notifications").hidden = true; });
document.getElementById("notif-read-all").addEventListener("click", async () => {
  await guarded(() => api("POST", "/api/notifications/read", {}));
  document.getElementById("notifications").hidden = true;
  refreshOverview();
});

// ---------------------------------------------------------------------------
// router

const routes = {
  chat: renderChat, tasks: renderTasks, opportunities: renderOpportunities, drafts: renderDrafts,
  schedules: renderSchedules, profile: renderProfile, files: renderFiles, settings: renderSettings, more: renderMore,
};

function route() {
  if (document.getElementById("app").hidden) return;
  clearTimers();
  const parts = location.hash.replace(/^#\/?/, "").split("/").filter(Boolean);
  const name = routes[parts[0]] ? parts[0] : "chat";
  for (const a of document.querySelectorAll("[data-nav]")) a.classList.toggle("active", a.dataset.nav === name);
  document.getElementById("notifications").hidden = true;
  routes[name](parts.slice(1)).catch((err) => {
    if (err.status !== 401) render(h("div", { class: "notice danger" }, err.message || String(err)));
  });
}

window.addEventListener("hashchange", route);

// ---------------------------------------------------------------------------
// task progress (shared by chat and task pages)

const EVENT_ICONS = { tool_call: "→", tool_result: "✓", tool_error: "!", status: "·", assistant: "✎", check: "⚠" };

function eventItem(event) {
  return h("li", { class: event.type },
    h("span", { class: "t" }, fmtTime(event.ts, false)),
    `${EVENT_ICONS[event.type] || "·"} ${event.summary}`);
}

function taskMetrics(task) {
  const parts = [
    task.provider ? `${task.provider}${task.model ? " / " + task.model : ""}` : null,
    `${task.steps} steps`, `${task.tool_calls} tool calls`, `${task.searches} searches`, `${task.fetches} pages`,
    `${(task.input_tokens + task.output_tokens).toLocaleString()} tokens`, money(task.cost_usd),
  ];
  return h("div", { class: "metrics" }, parts.filter(Boolean).map((p) => h("span", {}, p)));
}

function liveTaskCard(task, { onFinish, compact } = {}) {
  const list = h("ul", { class: "timeline" });
  const statusEl = h("span", {}, statusChip(task.status));
  const detail = h("span", { class: "small muted" }, task.status_detail || "");
  const cancel = h("button", {
    class: "small danger", hidden: !isActive(task),
    onclick: async () => {
      if (!confirm("Cancel this task?")) return;
      await guarded(() => api("POST", `/api/tasks/${task.id}/cancel`), "Cancellation requested");
    },
  }, "Cancel");
  const metrics = h("div", {}, taskMetrics(task));
  const body = compact && !isActive(task)
    ? h("details", {}, h("summary", { class: "small muted" }, "Show what the agent did"), list, metrics)
    : h("div", {}, list, metrics);
  const card = h("div", { class: "task-card" },
    h("div", { class: "head" }, statusEl, h("a", { href: `#/tasks/${task.id}`, class: "small" }, `Task #${task.id}`), detail,
      h("span", { class: "spacer" }), cancel),
    body);
  const warnings = h("div", { class: "small check-warn" });
  card.append(warnings);
  let after = 0;
  let current = task;
  async function poll() {
    let data;
    try { data = await api("GET", `/api/tasks/${task.id}/events?after=${after}`); } catch { return; }
    for (const event of data.events) {
      list.append(eventItem(event));
      after = Math.max(after, event.id);
      if (event.type === "check") warnings.append(h("div", {}, `⚠ ${event.summary} - see task #${task.id}`));
    }
    const was = current.status;
    current = data.task;
    statusEl.replaceChildren(statusChip(current.status));
    detail.textContent = current.status_detail || "";
    metrics.replaceChildren(taskMetrics(current));
    cancel.hidden = !isActive(current);
    if (isActive({ status: was }) && !isActive(current)) {
      clearInterval(timer);
      refreshOverview();
      if (onFinish) onFinish(current);
    }
  }
  poll();
  const timer = isActive(task) ? every(1500, poll) : null;
  return card;
}

// ---------------------------------------------------------------------------
// chat

const EXAMPLES = [
  "Find AI and robotics hackathons I can enter in the next three months.",
  "Find startup competitions, grants, and accelerator opportunities relevant to my projects.",
  "Research this company and summarize what it does: ",
  "Draft an email asking whether international students can participate.",
  "Check these opportunities every week and show me newly discovered ones.",
];

async function renderChat(params) {
  setTitle("Chat");
  const id = params[0] ? Number(params[0]) : null;
  const convs = await api("GET", "/api/conversations");
  const listEl = h("div", { class: "chat-list card" },
    h("div", { class: "row" }, h("strong", {}, "Chats"), h("span", { class: "spacer" }),
      h("button", { class: "small primary", onclick: newChat }, "New chat")),
    convs.items.length ? convs.items.map((c) => h("a", { href: `#/chat/${c.id}`, class: c.id === id ? "active" : "" },
      h("div", { class: "title" }, c.title), h("div", { class: "preview" }, c.last_message || fmtAgo(c.updated_at))))
      : h("p", { class: "muted small" }, "No chats yet."));
  const main = h("section", { class: "chat-main" });
  render(h("div", { class: "chat" + (id ? " has-conversation" : "") }, listEl, main));
  if (!id) {
    main.append(h("div", { class: "empty" }, h("h2", {}, "What should your agent work on?"),
      h("p", {}, "Tasks keep running on your Mac after you close this page.")),
    h("div", { class: "examples" }, EXAMPLES.map((text) => h("button", { onclick: () => newChat(text) }, text))));
    return;
  }
  await renderConversation(id, main);
}

async function newChat(prefill) {
  const conv = await guarded(() => api("POST", "/api/conversations", {}));
  if (!conv) return;
  if (typeof prefill === "string") sessionStorage.setItem(`draft-${conv.id}`, prefill);
  location.hash = `#/chat/${conv.id}`;
}

async function renderConversation(id, main) {
  const data = await api("GET", `/api/conversations/${id}`);
  setTitle(data.conversation.title);
  const tasksById = Object.fromEntries(data.tasks.map((t) => [t.id, t]));
  const active = data.tasks.find(isActive);
  const header = h("div", { class: "row" },
    h("a", { href: "#/chat", class: "button small", "aria-label": "All chats" }, "‹ Chats"),
    h("strong", {}, data.conversation.title), h("span", { class: "spacer" }),
    h("button", { class: "small", onclick: async () => {
      const title = prompt("Rename chat", data.conversation.title);
      if (title) { await guarded(() => api("PATCH", `/api/conversations/${id}`, { title })); route(); }
    } }, "Rename"),
    h("button", { class: "small danger", onclick: async () => {
      if (!confirm("Delete this chat and its history? Saved opportunities and drafts are kept.")) return;
      await guarded(() => api("DELETE", `/api/conversations/${id}`));
      location.hash = "#/chat";
    } }, "Delete"));
  const messages = h("div", { class: "messages" });
  const shownTasks = new Set();
  for (const m of data.messages) {
    if (m.role === "user") {
      messages.append(h("div", { class: "msg user" }, m.content));
      const task = tasksById[m.task_id];
      if (task) {
        shownTasks.add(task.id);
        messages.append(liveTaskCard(task, { compact: true, onFinish: route }));
      }
    } else if (m.role === "assistant") {
      messages.append(h("div", { class: "msg assistant md", html: m.html }));
    } else {
      messages.append(h("div", { class: "msg notice" }, m.content));
    }
  }
  for (const t of data.tasks) {
    if (!shownTasks.has(t.id) && isActive(t)) messages.append(liveTaskCard(t, { onFinish: route }));
  }
  if (!data.messages.length) {
    messages.append(h("div", { class: "examples" }, EXAMPLES.map((text) => h("button", { onclick: () => {
      input.value = text; input.focus();
    } }, text))));
  }
  const input = h("textarea", { rows: 2, placeholder: active ? "The agent is working on your last request..." : "Ask your agent to research something", "aria-label": "Message" });
  input.value = sessionStorage.getItem(`draft-${id}`) || "";
  input.addEventListener("input", () => sessionStorage.setItem(`draft-${id}`, input.value));
  const send = h("button", { class: "primary", disabled: !!active }, "Send");
  async function submit() {
    const content = input.value.trim();
    if (!content) return;
    send.disabled = true;
    const result = await guarded(() => api("POST", `/api/conversations/${id}/messages`, { content }));
    if (result) {
      sessionStorage.removeItem(`draft-${id}`);
      input.value = "";
      refreshOverview();
      route();
    } else {
      send.disabled = false;
    }
  }
  send.addEventListener("click", submit);
  input.addEventListener("keydown", (e) => { if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) submit(); });
  const hint = active ? h("p", { class: "small muted" }, "Working in the background. You can close this page; you will get a notification when it finishes.") : null;
  main.replaceChildren(...[header, messages, hint, h("div", { class: "composer" }, input, send)].filter(Boolean));
  messages.scrollTop = messages.scrollHeight;
  if (window.innerWidth > 820) input.focus();
}

// ---------------------------------------------------------------------------
// tasks

async function renderTasks(params) {
  if (params[0]) return renderTaskDetail(Number(params[0]));
  setTitle("Tasks");
  const filter = sessionStorage.getItem("task-filter") || "";
  const data = await api("GET", `/api/tasks${filter ? "?status=" + filter : ""}`);
  const tabs = [["", "All"], ["active", "Active"], ["succeeded", "Done"], ["failed", "Failed"], ["cancelled", "Cancelled"]];
  render(
    h("div", { class: "page-head" }, h("h2", {}, "Tasks"), h("span", { class: "spacer" }),
      tabs.map(([value, label]) => h("button", { class: "small" + (value === filter ? " primary" : ""), onclick: () => {
        sessionStorage.setItem("task-filter", value); route();
      } }, label))),
    data.items.length ? h("ul", { class: "list card" }, data.items.map((t) => h("li", {},
      h("a", { class: "item", href: `#/tasks/${t.id}` },
        h("div", { class: "row" }, statusChip(t.status), h("strong", {}, t.title), t.kind === "scheduled" ? h("span", { class: "chip" }, "scheduled") : null,
          t.new_opportunities ? h("span", { class: "status new" }, `${t.new_opportunities} new`) : null),
        h("div", { class: "small muted" }, `#${t.id} · ${fmtAgo(t.created_at)} · ${t.steps} steps · ${money(t.cost_usd)}${t.status_detail ? " · " + t.status_detail : ""}`)))))
      : h("p", { class: "empty" }, "No tasks here."));
  if (data.items.some(isActive)) every(4000, () => { if (location.hash.startsWith("#/tasks") && !location.hash.match(/tasks\/\d/)) route(); });
}

async function renderTaskDetail(id) {
  const task = await api("GET", `/api/tasks/${id}`);
  setTitle(task.title);
  const sources = task.sources.filter((s) => s.kind !== "search_result" || !task.sources.some((o) => o.url === s.url && o.kind !== "search_result"));
  const read = sources.filter((s) => s.kind !== "search_result");
  const seen = sources.filter((s) => s.kind === "search_result");
  const checks = task.events.filter((e) => e.type === "check");
  const actions = h("div", { class: "row" },
    task.conversation_id ? h("a", { class: "button small", href: `#/chat/${task.conversation_id}` }, "Open chat") : null,
    !isActive(task) ? h("button", { class: "small", onclick: async () => {
      const t = await guarded(() => api("POST", `/api/tasks/${id}/retry`), "Started again");
      if (t) location.hash = `#/tasks/${t.id}`;
    } }, "Run again") : null);
  render(
    h("div", { class: "page-head" }, h("a", { class: "button small", href: "#/tasks" }, "‹ Tasks"), h("h2", {}, task.title)),
    h("div", { class: "card stack" },
      h("div", { class: "small muted" }, `Created ${fmtTime(task.created_at)}${task.finished_at ? " · finished " + fmtTime(task.finished_at) : ""}${task.kind === "scheduled" ? " · scheduled run" : ""}`),
      actions,
      liveTaskCard(task, { onFinish: () => renderTaskDetail(id) }),
      task.error ? h("div", { class: "notice danger" }, task.error) : null),
    task.result_html ? h("div", { class: "card" }, h("h3", {}, "Result"), h("div", { class: "md", html: task.result_html })) : null,
    checks.length ? h("div", { class: "card" }, h("h3", {}, "Checks"), checks.map((c) => h("div", { class: "notice warn" },
      h("p", {}, c.summary), h("ul", {}, (c.data && c.data.urls || []).map((u) => h("li", {}, h("code", {}, u))))))) : null,
    task.opportunities.length || task.drafts.length ? h("div", { class: "card" }, h("h3", {}, "Saved by this task"),
      h("ul", {}, task.opportunities.map((o) => h("li", {}, h("a", { href: "#/opportunities" }, o.name), " ", statusChip(o.verification_status), o.is_new ? [" ", statusChip("new")] : null)),
        task.drafts.map((d) => h("li", {}, h("a", { href: `#/drafts/${d.id}` }, `Draft: ${d.subject || "(no subject)"}`), " ", statusChip(d.status))))) : null,
    h("div", { class: "card sources" }, h("h3", {}, `Sources consulted (${read.length} read, ${seen.length} seen in search)`),
      read.length ? h("ul", {}, read.map((s) => h("li", {}, linkTo(s.final_url || s.url, s.title || s.final_url || s.url), " ",
        s.ok ? h("span", { class: "small muted" }, `${s.kind === "rendered" ? "rendered" : "read"} ${fmtTime(s.accessed_at)}`)
          : h("span", { class: "small check-bad" }, `not accessible: ${s.error || "error"}`)))) : h("p", { class: "muted" }, "No pages were read."),
      seen.length ? h("details", {}, h("summary", { class: "small" }, "Search results seen (not opened)"),
        h("ul", {}, seen.map((s) => h("li", { class: "small" }, linkTo(s.url, s.title || s.url))))) : null));
}

// ---------------------------------------------------------------------------
// opportunities

const FACT_MARKS = { verified: "✓", inferred: "~", unverified: "!", unknown: "?" };
const FACT_LABELS = { verified: "verified on a fetched page", inferred: "inferred", unverified: "not confirmed", unknown: "not found" };
const FACT_TITLES = {
  verified: "Verified: quoted from a page the agent fetched",
  inferred: "Inferred by the agent, not stated on a page",
  unverified: "Claimed but not confirmed on a fetched page",
  unknown: "Not found",
};

function factMark(opp, field) {
  const status = (opp.field_status || {})[field] || "unknown";
  const check = (opp.field_checks || {})[field];
  const title = check && status !== "verified" ? `${FACT_TITLES[status]} (${check})` : FACT_TITLES[status];
  return h("span", { class: `fact-mark ${status}`, title, "aria-label": title }, FACT_MARKS[status]);
}

const NOTICE_LABELS = { past_edition: "May be a past edition" };

function oppWarnings(o, compact) {
  return (o.warnings || []).map((n) => compact
    ? h("details", { class: "small opp-warn" }, h("summary", { class: "check-warn" }, `⚠ ${NOTICE_LABELS[n.kind] || "Check needed"}`), n.message)
    : h("div", { class: "small check-warn opp-warn" }, `⚠ ${n.message}`));
}

function deadlineText(o) {
  if (!o.deadline_date) return o.deadline_text || "unknown";
  let text = o.deadline_date + (o.deadline_time ? ` ${o.deadline_time}` : "");
  if (o.deadline_timezone) text += ` (${o.deadline_timezone.replace("Etc/GMT+12", "AoE, UTC-12").replace(/^Etc\/GMT([+-])(\d+)$/, (_, s, n) => `UTC${s === "+" ? "-" : "+"}${n}`)})`;
  return text;
}

function formatText(o) {
  const f = { online: "Online", in_person: "In person", hybrid: "Hybrid", unknown: "" }[o.format] || "";
  return [o.location, f].filter(Boolean).join(" · ") || "unknown";
}

const OPP_COLUMNS = [
  ["name", "Name"], ["category", "Category"], ["relevance_score", "Relevance"], ["eligibility_status", "Eligibility"],
  ["deadline_date", "Deadline"], ["location", "Where"], ["cost", "Cost"], ["prize", "Prize/funding"],
  ["application_url", "Apply"], ["sources", "Sources"], ["verification_status", "Verification"],
];

async function renderOpportunities() {
  setTitle("Opportunities");
  const prefs = JSON.parse(localStorage.getItem("opp-prefs") || '{"sort":"deadline_date","asc":true,"hideExpired":false,"bookmarked":false,"dismissed":false}');
  const savePrefs = () => localStorage.setItem("opp-prefs", JSON.stringify(prefs));
  const query = h("input", { type: "search", placeholder: "Filter", "aria-label": "Filter opportunities", value: sessionStorage.getItem("opp-q") || "" });
  const tableWrap = h("div", { class: "table-wrap" });
  const cards = h("div", { class: "opp-cards" });
  let items = [];

  async function load() {
    const data = await api("GET", `/api/opportunities?include_dismissed=${prefs.dismissed}`);
    items = data.items;
    draw();
  }

  function visible() {
    const q = query.value.trim().toLowerCase();
    let list = items.filter((o) => (!prefs.hideExpired || !o.expired) && (!prefs.bookmarked || o.bookmarked));
    if (q) list = list.filter((o) => [o.name, o.organizer, o.summary, o.category, o.location].join(" ").toLowerCase().includes(q));
    const key = prefs.sort;
    list.sort((a, b) => {
      let x = a[key]; let y = b[key];
      if (key === "sources") { x = a.sources.length; y = b.sources.length; }
      if (x === null || x === undefined || x === "") return 1;
      if (y === null || y === undefined || y === "") return -1;
      const result = typeof x === "number" ? x - y : String(x).localeCompare(String(y));
      return prefs.asc ? result : -result;
    });
    return list;
  }

  async function patch(o, body) {
    const updated = await guarded(() => api("PATCH", `/api/opportunities/${o.id}`, body));
    if (updated) { Object.assign(o, updated); if (body.dismissed !== undefined && !prefs.dismissed) items = items.filter((x) => x.id !== o.id); draw(); refreshOverview(); }
  }

  function actionButtons(o) {
    return h("div", { class: "row" },
      h("button", { class: "small", title: o.bookmarked ? "Remove bookmark" : "Bookmark", onclick: () => patch(o, { bookmarked: !o.bookmarked }) }, o.bookmarked ? "★" : "☆"),
      h("button", { class: "small", onclick: () => patch(o, { dismissed: !o.dismissed }) }, o.dismissed ? "Restore" : "Dismiss"),
      h("button", { class: "small", onclick: () => editNotes(o) }, o.notes ? "Notes ✎" : "Notes"));
  }

  function editNotes(o) {
    const notes = prompt(`Notes for ${o.name}`, o.notes || "");
    if (notes !== null) patch(o, { notes });
  }

  function badges(o) {
    return [o.is_new ? statusChip("new") : null, o.expired ? statusChip("expired") : null, o.bookmarked ? h("span", { class: "chip" }, "★ saved") : null, o.dismissed ? h("span", { class: "chip" }, "dismissed") : null];
  }

  function sourcesList(o) {
    if (!o.sources.length) return h("span", { class: "muted" }, "none");
    return h("details", {}, h("summary", { class: "small" }, `${o.sources.length} source${o.sources.length > 1 ? "s" : ""}`),
      h("ul", { class: "small" }, o.sources.map((s) => h("li", {}, linkTo(s.url, s.title || s.url), h("span", { class: "muted" }, ` checked ${fmtTime(s.accessed_at)}`)))));
  }

  function draw() {
    const list = visible();
    const head = h("tr", {}, OPP_COLUMNS.map(([key, label]) => h("th", {
      class: key === prefs.sort ? "sorted" + (prefs.asc ? " asc" : "") : "",
      onclick: () => { prefs.asc = prefs.sort === key ? !prefs.asc : true; prefs.sort = key; savePrefs(); draw(); },
    }, label)));
    const rows = list.map((o) => h("tr", { class: (o.is_new ? "is-new " : "") + (o.expired ? "is-expired" : "") },
      h("td", { class: "name" }, h("strong", {}, o.official_url ? linkTo(o.official_url, o.name) : o.name), h("div", { class: "small muted" }, o.organizer || ""), h("div", { class: "row" }, badges(o)), oppWarnings(o, true), o.notes ? h("div", { class: "small" }, `Note: ${o.notes}`) : null, actionButtons(o)),
      h("td", {}, o.category),
      h("td", { title: o.relevance_reason || "" }, h("strong", {}, o.relevance_score ?? "-")),
      h("td", { title: o.eligibility || "" }, statusChip(o.eligibility_status || "unclear"), " ", factMark(o, "eligibility"), h("div", { class: "small" }, truncate(o.eligibility, 90))),
      h("td", {}, factMark(o, "deadline"), " ", deadlineText(o), o.expired ? h("div", { class: "small check-bad" }, "Closed") : null),
      h("td", {}, factMark(o, "location"), " ", formatText(o)),
      h("td", {}, factMark(o, "cost"), " ", o.cost || "unknown"),
      h("td", {}, factMark(o, "prize"), " ", o.prize || "unknown"),
      h("td", {}, o.application_url ? [factMark(o, "application_url"), " ", linkTo(o.application_url, "Apply")] : h("span", { class: "muted" }, "unknown")),
      h("td", {}, sourcesList(o)),
      h("td", {}, statusChip(o.verification_status), h("div", { class: "small muted" }, `checked ${fmtAgo(o.last_checked_at)}`))));
    tableWrap.replaceChildren(list.length ? h("table", { class: "data" }, h("thead", {}, head), h("tbody", {}, rows))
      : h("p", { class: "empty" }, items.length ? "Nothing matches the filters." : "No opportunities saved yet. Ask the agent to find some in Chat."));
    cards.replaceChildren(...(list.length ? list.map((o) => h("div", { class: "card opp-card" + (o.expired ? " is-expired" : "") },
      h("div", { class: "row" }, badges(o)),
      h("h3", {}, o.official_url ? linkTo(o.official_url, o.name) : o.name),
      h("div", { class: "small muted" }, [o.organizer, o.category].filter(Boolean).join(" · ")),
      oppWarnings(o),
      h("dl", {},
        h("dt", {}, "Relevance"), h("dd", {}, `${o.relevance_score ?? "-"} - ${o.relevance_reason || ""}`),
        h("dt", {}, "Eligibility"), h("dd", {}, statusChip(o.eligibility_status || "unclear"), " ", factMark(o, "eligibility"), " ", o.eligibility || ""),
        h("dt", {}, "Deadline"), h("dd", {}, factMark(o, "deadline"), " ", deadlineText(o)),
        h("dt", {}, "Where"), h("dd", {}, factMark(o, "location"), " ", formatText(o)),
        h("dt", {}, "Cost"), h("dd", {}, factMark(o, "cost"), " ", o.cost || "unknown"),
        h("dt", {}, "Prize/funding"), h("dd", {}, factMark(o, "prize"), " ", o.prize || "unknown"),
        h("dt", {}, "Apply"), h("dd", {}, o.application_url ? linkTo(o.application_url) : "unknown"),
        h("dt", {}, "Verification"), h("dd", {}, statusChip(o.verification_status), ` checked ${fmtAgo(o.last_checked_at)}`),
        h("dt", {}, "Sources"), h("dd", {}, sourcesList(o)),
        o.notes ? [h("dt", {}, "Notes"), h("dd", {}, o.notes)] : null),
      actionButtons(o))) : [h("p", { class: "empty" }, "No opportunities to show.")]));
  }

  const toggle = (key, label) => h("label", { class: "inline" }, h("input", { type: "checkbox", checked: prefs[key], onchange: (e) => {
    prefs[key] = e.target.checked; savePrefs(); key === "dismissed" ? load() : draw();
  } }), label);
  query.addEventListener("input", () => { sessionStorage.setItem("opp-q", query.value); draw(); });
  render(
    h("div", { class: "page-head" }, h("h2", {}, "Opportunities"), h("span", { class: "spacer" }),
      h("button", { class: "small", onclick: async () => { await guarded(() => api("POST", "/api/opportunities/mark-seen")); refreshOverview(); load(); } }, "Mark all as seen"),
      h("a", { class: "button small", href: "/api/opportunities/export.csv" }, "Export CSV")),
    h("div", { class: "toolbar" }, query, toggle("hideExpired", "Hide expired"), toggle("bookmarked", "Bookmarked only"), toggle("dismissed", "Show dismissed")),
    h("div", { class: "legend" }, Object.entries(FACT_MARKS).map(([k, mark]) => h("span", { title: FACT_TITLES[k] }, h("span", { class: `fact-mark ${k}` }, mark), " ", FACT_LABELS[k]))),
    tableWrap, cards);
  await load();
}

// ---------------------------------------------------------------------------
// drafts

function splitAddresses(text) {
  return text.split(/[,;\n]/).map((s) => s.trim()).filter(Boolean);
}

async function renderDrafts(params) {
  setTitle("Email drafts");
  const id = params[0] ? Number(params[0]) : null;
  const data = await api("GET", "/api/drafts");
  const list = h("div", { class: "card" },
    h("div", { class: "row" }, h("strong", {}, "Drafts"), h("span", { class: "spacer" }),
      h("button", { class: "small primary", onclick: async () => {
        const d = await guarded(() => api("POST", "/api/drafts", { subject: "", body: "" }));
        if (d) location.hash = `#/drafts/${d.id}`;
      } }, "New draft")),
    data.items.length ? h("ul", { class: "list" }, data.items.map((d) => h("li", {},
      h("a", { class: "item", href: `#/drafts/${d.id}` }, h("div", { class: "row" }, statusChip(d.status), h("strong", {}, d.subject || "(no subject)")),
        h("div", { class: "small muted" }, `${d.to_addrs.join(", ") || "no recipient"} · ${fmtAgo(d.updated_at)}`)))))
      : h("p", { class: "muted small" }, "No drafts yet. Ask the agent to draft an email, or create one."));
  const editor = h("div", {});
  render(h("div", { class: "page-head" }, h("h2", {}, "Email drafts")),
    h("div", { class: "draft-layout" }, list, editor));
  if (!data.gmail.connected) {
    editor.append(h("div", { class: "notice small" }, "Drafts live in Home Agent. Copy them or open them in your mail app. To create Gmail drafts, connect Gmail in Settings (optional)."));
  }
  if (id) await renderDraftEditor(id, editor, data.gmail);
  else editor.append(h("div", { class: "card empty" }, "Select a draft."));
}

async function renderDraftEditor(id, container, gmail) {
  const d = await api("GET", `/api/drafts/${id}`);
  const locked = ["sending", "sent", "send_unknown"].includes(d.status);
  const to = h("input", { value: d.to_addrs.join(", "), disabled: locked, placeholder: "name@example.org" });
  const cc = h("input", { value: d.cc_addrs.join(", "), disabled: locked });
  const bcc = h("input", { value: d.bcc_addrs.join(", "), disabled: locked });
  const subject = h("input", { value: d.subject, disabled: locked });
  const body = h("textarea", { rows: 14, disabled: locked });
  body.value = d.body;
  const files = await api("GET", "/api/files");
  const attached = new Set(d.attachments.map((a) => a.path));
  const attachBoxes = files.items.map((f) => h("label", { class: "inline small" }, h("input", { type: "checkbox", value: f.path, checked: attached.has(f.path), disabled: locked }), f.path));
  let dirty = false;
  for (const el of [to, cc, bcc, subject, body, ...attachBoxes.map((l) => l.querySelector("input"))]) el.addEventListener("input", () => { dirty = true; reviewBox.replaceChildren(); });

  async function save(quiet) {
    const payload = {
      to: splitAddresses(to.value), cc: splitAddresses(cc.value), bcc: splitAddresses(bcc.value), subject: subject.value, body: body.value,
      attachments: attachBoxes.map((l) => l.querySelector("input")).filter((i) => i.checked).map((i) => i.value),
    };
    const updated = await guarded(() => api("PUT", `/api/drafts/${id}`, payload), quiet ? null : "Saved");
    if (updated) { dirty = false; if (!quiet) renderDraftEditor(id, container, gmail); }
    return updated;
  }

  const mailto = () => {
    const params = new URLSearchParams();
    if (cc.value.trim()) params.set("cc", cc.value);
    if (bcc.value.trim()) params.set("bcc", bcc.value);
    params.set("subject", subject.value);
    params.set("body", body.value);
    return `mailto:${encodeURIComponent(to.value.trim()).replace(/%2C/g, ",").replace(/%40/g, "@")}?${params.toString().replace(/\+/g, "%20")}`;
  };

  const reviewBox = h("div", {});
  function showReview(fresh) {
    const confirmBox = h("input", { type: "checkbox", id: "approve-check" });
    const approve = h("button", { class: "primary", disabled: true, onclick: async () => {
      const res = await guarded(() => api("POST", `/api/drafts/${id}/approve`, { content_hash: fresh.content_hash }), "Approved for 30 minutes");
      if (res) renderDraftEditor(id, container, gmail);
    } }, "Approve this exact email");
    confirmBox.addEventListener("change", () => { approve.disabled = !confirmBox.checked; });
    reviewBox.replaceChildren(h("div", { class: "review stack" },
      h("h3", {}, "Review before approving"),
      h("p", { class: "small" }, "This is exactly what will be sent. Any later edit cancels the approval."),
      h("dl", { class: "kv" },
        h("dt", {}, "To"), h("dd", {}, fresh.to_addrs.join(", ") || "(none)"),
        h("dt", {}, "Cc"), h("dd", {}, fresh.cc_addrs.join(", ") || "-"),
        h("dt", {}, "Bcc"), h("dd", {}, fresh.bcc_addrs.join(", ") || "-"),
        h("dt", {}, "Subject"), h("dd", {}, fresh.subject || "(none)"),
        h("dt", {}, "Attachments"), h("dd", {}, fresh.attachments.map((a) => a.path).join(", ") || "none")),
      h("pre", {}, fresh.body),
      fresh.warnings.length ? h("div", { class: "notice warn" }, fresh.warnings.map((w) => h("div", {}, w))) : null,
      h("label", { class: "inline", for: "approve-check" }, confirmBox, "I approve sending exactly this email from my Gmail account."),
      h("div", { class: "row" }, approve, h("button", { onclick: () => reviewBox.replaceChildren() }, "Close"))));
  }

  const sendSection = [];
  if (!d.send_enabled) {
    sendSection.push(h("p", { class: "small muted" }, "Sending from Home Agent is off (drafts only). To allow approved sending, set GMAIL_ALLOW_SEND=true in .env and connect Gmail."));
  } else if (!d.gmail_connected) {
    sendSection.push(h("p", { class: "small muted" }, "Connect Gmail in Settings to send approved emails."));
  } else if (d.status === "approved" && d.approval_valid) {
    sendSection.push(h("div", { class: "notice ok" }, `Approved ${fmtAgo(d.approved_at)}; valid until ${fmtTime(d.approval_expires_at)}.`),
      h("div", { class: "row" },
        h("button", { class: "primary", onclick: async () => {
          if (!confirm(`Send this email to ${d.to_addrs.join(", ")} now?`)) return;
          const res = await guarded(() => api("POST", `/api/drafts/${id}/send`, { content_hash: d.content_hash }), "Sent");
          renderDraftEditor(id, container, gmail);
          if (res) route();
        } }, "Send now"),
        h("button", { onclick: async () => { await guarded(() => api("POST", `/api/drafts/${id}/revoke`), "Approval removed"); renderDraftEditor(id, container, gmail); } }, "Remove approval")));
  } else if (!locked) {
    sendSection.push(h("button", { onclick: async () => {
      if (dirty && !(await save(true))) return;
      const fresh = await guarded(() => api("GET", `/api/drafts/${id}`));
      if (fresh) showReview(fresh);
    } }, "Review & approve for sending"));
  }
  if (d.status === "send_unknown") {
    sendSection.push(h("div", { class: "notice warn" }, d.last_error || "The send outcome is unknown. Check your Gmail Sent folder."),
      h("div", { class: "row" },
        h("button", { onclick: async () => { await guarded(() => api("POST", `/api/drafts/${id}/resolve`, { outcome: "sent" })); renderDraftEditor(id, container, gmail); } }, "It was sent"),
        h("button", { onclick: async () => { await guarded(() => api("POST", `/api/drafts/${id}/resolve`, { outcome: "not_sent" })); renderDraftEditor(id, container, gmail); } }, "It was not sent")));
  }

  container.replaceChildren(h("div", { class: "card stack" },
    h("div", { class: "row" }, statusChip(d.status), d.sent_at ? h("span", { class: "small" }, `Sent ${fmtTime(d.sent_at)}`) : null,
      d.gmail_draft_id ? h("span", { class: "chip" }, d.gmail_synced_hash === d.content_hash ? "In Gmail drafts" : "Gmail draft is out of date") : null,
      d.source_task_id ? h("a", { class: "small", href: `#/tasks/${d.source_task_id}` }, `from task #${d.source_task_id}`) : null),
    d.warnings.length ? h("div", { class: "notice warn" }, h("strong", {}, "Check before sending: "), d.warnings.map((w) => h("div", {}, w))) : null,
    d.last_error && d.status !== "send_unknown" ? h("div", { class: "notice danger" }, d.last_error) : null,
    d.attachment_error ? h("div", { class: "notice danger" }, d.attachment_error) : null,
    h("div", {}, h("label", {}, "To"), to, h("label", {}, "Cc"), cc, h("label", {}, "Bcc"), bcc, h("label", {}, "Subject"), subject, h("label", {}, "Message"), body),
    files.items.length ? h("details", {}, h("summary", {}, `Attachments (${attached.size})`), h("div", { class: "stack" }, attachBoxes)) : null,
    h("div", { class: "row" },
      !locked ? h("button", { class: "primary", onclick: () => save(false) }, "Save") : null,
      h("button", { onclick: async () => { await navigator.clipboard.writeText(`Subject: ${subject.value}\n\n${body.value}`); toast("Copied"); } }, "Copy"),
      h("a", { class: "button", href: "#", onclick: (e) => { e.preventDefault(); location.href = mailto(); } }, "Open in mail app"),
      gmail.connected && !locked ? h("button", { onclick: async () => { if (dirty && !(await save(true))) return; const r = await guarded(() => api("POST", `/api/drafts/${id}/gmail`), "Saved to Gmail drafts"); if (r) renderDraftEditor(id, container, gmail); } }, "Save to Gmail drafts") : null,
      h("span", { class: "spacer" }),
      d.status !== "sending" ? h("button", { class: "danger", onclick: async () => {
        if (!confirm("Delete this draft?")) return;
        await guarded(() => api("DELETE", `/api/drafts/${id}`));
        location.hash = "#/drafts";
      } }, "Delete") : null),
    h("div", { class: "stack" }, h("h3", {}, "Sending"), sendSection),
    reviewBox));
}

// ---------------------------------------------------------------------------
// schedules

const WEEKDAYS = ["Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday"];

function scheduleForm(existing, onDone) {
  const rec = existing ? existing.recurrence : { freq: "weekly", weekday: 0, time: "09:00" };
  const name = h("input", { value: existing ? existing.name : "", placeholder: "Weekly AI hackathon scan" });
  const promptEl = h("textarea", { rows: 4, placeholder: "Find new AI and robotics hackathons and competitions I can enter, and re-check saved ones." });
  promptEl.value = existing ? existing.prompt : "";
  const freq = h("select", {}, ["daily", "weekly", "monthly"].map((f) => h("option", { value: f, selected: rec.freq === f }, f)));
  const weekday = h("select", {}, WEEKDAYS.map((d, i) => h("option", { value: i, selected: rec.weekday === i }, d)));
  const day = h("input", { type: "number", min: 1, max: 31, value: rec.day || 1 });
  const time = h("input", { type: "time", value: rec.time || "09:00" });
  const tz = h("input", { value: existing ? existing.timezone : state.tz || "Asia/Shanghai" });
  const weekdayWrap = h("div", {}, h("label", {}, "Day of week"), weekday);
  const dayWrap = h("div", {}, h("label", {}, "Day of month"), day);
  const sync = () => { weekdayWrap.hidden = freq.value !== "weekly"; dayWrap.hidden = freq.value !== "monthly"; };
  freq.addEventListener("change", sync);
  sync();
  return h("div", { class: "card stack" },
    h("h3", {}, existing ? "Edit schedule" : "New schedule"),
    h("div", {}, h("label", {}, "Name"), name, h("label", {}, "What to research each time"), promptEl),
    h("div", { class: "grid2" }, h("div", {}, h("label", {}, "Repeat"), freq), weekdayWrap, dayWrap, h("div", {}, h("label", {}, "Time"), time), h("div", {}, h("label", {}, "Time zone"), tz)),
    h("div", { class: "row" },
      h("button", { class: "primary", onclick: async () => {
        const recurrence = { freq: freq.value, time: time.value };
        if (freq.value === "weekly") recurrence.weekday = Number(weekday.value);
        if (freq.value === "monthly") recurrence.day = Number(day.value);
        const body = { name: name.value, prompt: promptEl.value, recurrence, timezone: tz.value.trim() };
        const res = await guarded(() => existing ? api("PUT", `/api/schedules/${existing.id}`, body) : api("POST", "/api/schedules", body), "Saved");
        if (res) onDone();
      } }, "Save"),
      h("button", { onclick: onDone }, "Cancel")));
}

async function renderSchedules() {
  setTitle("Schedules");
  const data = await api("GET", "/api/schedules");
  const formSlot = h("div", {});
  const reload = () => route();
  const action = (label, fn, cls) => h("button", { class: "small" + (cls ? " " + cls : ""), onclick: fn }, label);
  render(
    h("div", { class: "page-head" }, h("h2", {}, "Schedules"), h("span", { class: "spacer" }),
      h("button", { class: "primary small", onclick: () => formSlot.replaceChildren(scheduleForm(null, reload)) }, "New schedule")),
    h("p", { class: "muted small" }, `Recurring research runs only when you set it up or confirm a schedule the agent proposed. Times use each schedule's time zone (default ${state.tz}).`),
    formSlot,
    data.items.length ? data.items.map((s) => h("div", { class: "card stack" },
      h("div", { class: "row" }, h("strong", {}, s.name), statusChip(s.status, s.status === "pending_confirmation" ? "needs your confirmation" : s.status),
        s.created_by === "agent" ? h("span", { class: "chip" }, "proposed by agent") : null),
      h("div", { class: "small" }, `${s.description} (${s.timezone})`),
      s.next_run_at ? h("div", { class: "small muted" }, `Next run: ${fmtTime(s.next_run_at)}`) : null,
      s.last_task ? h("div", { class: "small" }, "Last run: ", h("a", { href: `#/tasks/${s.last_task.id}` }, `task #${s.last_task.id}`), " ", statusChip(s.last_task.status),
        s.last_task.new_opportunities ? [" ", statusChip("new", `${s.last_task.new_opportunities} new`)] : null) : null,
      h("details", {}, h("summary", { class: "small" }, "Prompt"), h("pre", {}, s.prompt)),
      h("div", { class: "row" },
        s.status !== "active" ? action(s.status === "pending_confirmation" ? "Confirm & activate" : "Resume", async () => { await guarded(() => api("POST", `/api/schedules/${s.id}/activate`), "Activated"); reload(); }, "primary") : null,
        s.status === "active" ? action("Pause", async () => { await guarded(() => api("POST", `/api/schedules/${s.id}/pause`), "Paused"); reload(); }) : null,
        action("Run now", async () => { const t = await guarded(() => api("POST", `/api/schedules/${s.id}/run-now`), "Started"); if (t) location.hash = `#/tasks/${t.id}`; }),
        action("Edit", () => formSlot.replaceChildren(scheduleForm(s, reload))),
        action("Delete", async () => { if (confirm(`Delete schedule "${s.name}"?`)) { await guarded(() => api("DELETE", `/api/schedules/${s.id}`)); reload(); } }, "danger"))))
      : h("p", { class: "empty" }, "No schedules. Ask in chat (\"check these opportunities every week\") or create one here."));
}

// ---------------------------------------------------------------------------
// profile + memory

async function renderProfile() {
  setTitle("Profile");
  const [profile, memories] = await Promise.all([api("GET", "/api/profile"), api("GET", "/api/memories")]);
  const fields = {};
  const field = (key, label, multiline, hint) => {
    const el = multiline ? h("textarea", { rows: 3 }) : h("input", {});
    el.value = Array.isArray(profile[key]) ? profile[key].join(", ") : (profile[key] || "");
    fields[key] = el;
    return h("div", {}, h("label", {}, label), el, hint ? h("div", { class: "small muted" }, hint) : null);
  };
  const projectsBox = h("div", { class: "stack" });
  const addProject = (p) => {
    const name = h("input", { value: p.name || "", placeholder: "Project name" });
    const desc = h("textarea", { rows: 2, placeholder: "What it does, stage, tech, team (helps the agent judge relevance)" });
    desc.value = p.description || "";
    const row = h("div", { class: "card" }, name, desc, h("button", { class: "small link", onclick: () => row.remove() }, "Remove"));
    row.dataset.project = "1";
    projectsBox.append(row);
  };
  (profile.projects || []).forEach(addProject);

  const memList = h("ul", { class: "list" }, memories.items.map((m) => {
    const li = h("li", {}, h("div", {}, m.content), h("div", { class: "row small muted" },
      m.source === "agent" ? h("span", { class: "chip" }, "saved by agent") : h("span", { class: "chip" }, "added by you"), fmtAgo(m.updated_at),
      h("span", { class: "spacer" }),
      h("button", { class: "small", onclick: async () => {
        const text = prompt("Edit note", m.content);
        if (text) { await guarded(() => api("PUT", `/api/memories/${m.id}`, { content: text })); route(); }
      } }, "Edit"),
      h("button", { class: "small danger", onclick: async () => { await guarded(() => api("DELETE", `/api/memories/${m.id}`)); route(); } }, "Delete")));
    return li;
  }));
  const newMemory = h("input", { placeholder: "e.g. I prefer online events; I can travel within China" });
  render(
    h("div", { class: "page-head" }, h("h2", {}, "Profile & memory")),
    h("div", { class: "card stack" },
      h("p", { class: "small muted" }, "The agent uses this to judge relevance and eligibility. It is included in every task's instructions."),
      h("div", { class: "grid2" }, field("name", "Name"), field("location", "Based in"), field("citizenship", "Citizenship"), field("education", "Education"), field("languages", "Languages")),
      field("interests", "Interests", false, "Comma separated"),
      h("div", {}, h("label", {}, "Projects"), projectsBox, h("button", { class: "small", onclick: () => addProject({}) }, "Add project")),
      field("goals", "Goals", true), field("constraints", "Constraints (budget, travel, time)", true), field("notes", "Other notes", true),
      h("div", {}, h("button", { class: "primary", onclick: async () => {
        const payload = {};
        for (const [key, el] of Object.entries(fields)) payload[key] = key === "interests" ? el.value.split(",").map((s) => s.trim()).filter(Boolean) : el.value;
        payload.projects = [...projectsBox.querySelectorAll("[data-project]")].map((row) => ({ name: row.querySelector("input").value, description: row.querySelector("textarea").value })).filter((p) => p.name.trim());
        await guarded(() => api("PUT", "/api/profile", payload), "Profile saved");
      } }, "Save profile"))),
    h("div", { class: "card stack" }, h("h3", {}, "Saved notes (memory)"),
      h("p", { class: "small muted" }, "Preferences the agent should remember. You can edit or delete any of them."),
      memories.items.length ? memList : h("p", { class: "muted" }, "No saved notes."),
      h("div", { class: "row" }, newMemory, h("button", { class: "small primary", onclick: async () => {
        if (!newMemory.value.trim()) return;
        await guarded(() => api("POST", "/api/memories", { content: newMemory.value }), "Saved");
        route();
      } }, "Add"))));
}

// ---------------------------------------------------------------------------
// files

async function renderFiles() {
  setTitle("Files");
  const data = await api("GET", "/api/files");
  const upload = h("input", { type: "file", accept: ".pdf,.md,.txt,.csv,.json,.docx,.png,.jpg,.jpeg" });
  const viewer = h("div", {});
  upload.addEventListener("change", async () => {
    if (!upload.files.length) return;
    const form = new FormData();
    form.append("file", upload.files[0]);
    await guarded(() => api("POST", "/api/files", form), "Uploaded");
    route();
  });
  render(
    h("div", { class: "page-head" }, h("h2", {}, "Files")),
    h("div", { class: "card stack" },
      h("p", { class: "small muted" }, "The agent can only read and write files in this workspace. Upload a CV or project description to let it use them."),
      upload,
      data.items.length ? h("ul", { class: "list" }, data.items.map((f) => h("li", { class: "row" }, h("code", {}, f.path), h("span", { class: "small muted" }, `${(f.size / 1024).toFixed(1)} KB`), h("span", { class: "spacer" }),
        h("button", { class: "small", onclick: async () => {
          const content = await guarded(() => api("GET", `/api/files/content?path=${encodeURIComponent(f.path)}`));
          if (content) viewer.replaceChildren(h("div", { class: "card" }, h("h3", {}, f.path), h("pre", {}, content.text)));
        } }, "View"),
        h("button", { class: "small danger", onclick: async () => { if (confirm(`Delete ${f.path}?`)) { await guarded(() => api("DELETE", `/api/files?path=${encodeURIComponent(f.path)}`)); route(); } } }, "Delete"))))
        : h("p", { class: "muted" }, "No files yet.")),
    viewer);
}

// ---------------------------------------------------------------------------
// settings

function statusLine(ok, text) {
  return h("div", { class: ok === true ? "check-ok" : ok === false ? "check-bad" : "check-warn" }, (ok === true ? "✓ " : ok === false ? "✗ " : "! ") + text);
}

async function renderSettings() {
  setTitle("Settings");
  const [settings, integ] = await Promise.all([api("GET", "/api/settings"), api("GET", "/api/integrations")]);
  state.settings = settings;
  const provider = h("select", {}, settings.providers.map((p) => h("option", { value: p.name, selected: p.name === settings.model_provider, disabled: !p.allowed },
    `${p.label} - ${p.local ? "local" : "cloud"}${p.configured ? "" : " (not configured)"}${p.allowed ? "" : " (blocked by LOCAL_ONLY)"}`)));
  const effort = h("select", {}, ["low", "medium", "high", "xhigh", "max"].map((e) => h("option", { value: e, selected: e === settings.anthropic_effort }, e)));
  const effortBox = h("div", { class: "stack" }, h("label", {}, "Claude effort (cost vs. thoroughness)"), effort);
  const showEffort = () => { effortBox.hidden = provider.value !== "anthropic"; };
  provider.addEventListener("change", showEffort);
  showEffort();
  const tz = h("input", { value: settings.timezone, list: "tz-list" });
  const tzList = h("datalist", { id: "tz-list" }, ["Asia/Shanghai", "Asia/Tashkent", "Asia/Hong_Kong", "Asia/Singapore", "Asia/Tokyo", "Europe/London", "Europe/Berlin", "America/New_York", "America/Los_Angeles", "UTC"].map((z) => h("option", { value: z })));
  const taskCap = h("input", { type: "number", step: "0.1", min: "0.1", value: settings.task_max_cost_usd });
  const dailyCap = h("input", { type: "number", step: "0.5", min: "0.5", value: settings.daily_max_cost_usd });
  const testOut = (id) => h("div", { class: "small", id });

  const modelOut = testOut("model-test");
  const searchOut = testOut("search-test");
  const browserOut = testOut("browser-test");
  const runTest = (path, out) => async () => {
    out.textContent = "Testing...";
    const res = await guarded(() => api("POST", path));
    if (res) out.replaceChildren(statusLine(res.ok, res.message));
  };

  const gmail = integ.gmail;
  const pasteUrl = h("input", { placeholder: "Paste the full address from the browser after approving (starts with http://127.0.0.1...)" });
  const gmailBox = h("div", { class: "stack" },
    !gmail.configured ? h("p", { class: "small muted" }, "Optional. To create Gmail drafts, add GMAIL_CLIENT_ID and GMAIL_CLIENT_SECRET to .env (see README). Home Agent asks only for the gmail.compose permission.")
      : gmail.connected ? [statusLine(true, `Connected${gmail.account ? " as " + gmail.account : ""}`), h("div", { class: "small muted" }, `Permission: ${gmail.scopes}. Sending ${gmail.allow_send ? "enabled (each email needs your approval)" : "disabled (drafts only)"}.`),
        h("button", { class: "small danger", onclick: async () => { if (confirm("Disconnect Gmail and revoke Home Agent's access?")) { await guarded(() => api("POST", "/api/integrations/gmail/disconnect"), "Disconnected"); route(); } } }, "Disconnect")]
        : [h("p", { class: "small" }, `Redirect URI registered in Google Cloud must be: `, h("code", {}, gmail.redirect_uri)),
          h("button", { class: "primary small", onclick: async () => {
            const res = await guarded(() => api("POST", "/api/integrations/gmail/start"));
            if (res) window.open(res.auth_url, "_blank", "noopener");
          } }, "Connect Gmail"),
          h("p", { class: "small muted" }, "If you are not on the Mac itself, the browser ends on an address starting with http://127.0.0.1 that fails to load. Copy that whole address and paste it here:"),
          h("div", { class: "row" }, pasteUrl, h("button", { class: "small", onclick: async () => {
            const res = await guarded(() => api("POST", "/api/integrations/gmail/complete", { redirect_url: pasteUrl.value }), "Gmail connected");
            if (res) route();
          } }, "Finish"))],
    gmail.last_error ? h("div", { class: "notice warn small" }, gmail.last_error) : null);

  render(
    h("div", { class: "page-head" }, h("h2", {}, "Settings"), h("span", { class: "spacer" }), h("button", { class: "small", onclick: signOut }, "Sign out")),
    h("div", { class: "status-grid" },
      h("div", { class: "card stack" }, h("h3", {}, "Model"),
        integ.model.ok ? statusLine(true, `${integ.model.provider} / ${integ.model.model || ""} (${integ.model.local ? "local: prompts stay on your machine/network" : "cloud API"})`) : statusLine(false, integ.model.error),
        settings.local_only ? h("div", { class: "notice ok small" }, "LOCAL_ONLY is on: cloud model providers are disabled.") : null,
        h("label", {}, "Provider for new tasks"), provider,
        effortBox,
        h("div", { class: "row" }, h("button", { class: "small", onclick: runTest("/api/integrations/test-model", modelOut) }, "Test model")), modelOut),
      h("div", { class: "card stack" }, h("h3", {}, "Web research"),
        integ.search.ok ? statusLine(true, `Search: ${integ.search.name}`) : statusLine(false, integ.search.error),
        h("div", { class: "small muted" }, "Only search queries go to the search provider. Pages are fetched directly from their websites."),
        h("div", { class: "row" }, h("button", { class: "small", onclick: runTest("/api/integrations/test-search", searchOut) }, "Test search (uses 1 query)")), searchOut,
        integ.browser.enabled ? statusLine(integ.browser.last_error ? false : true, integ.browser.last_error || "JavaScript rendering available on demand") : statusLine(null, "JavaScript rendering disabled"),
        h("div", { class: "row" }, h("button", { class: "small", onclick: runTest("/api/integrations/test-browser", browserOut) }, "Test browser")), browserOut,
        integ.outbound_proxy ? statusLine(null, "Outbound proxy is configured") : null),
      h("div", { class: "card stack" }, h("h3", {}, "Gmail (optional)"), gmailBox),
      h("div", { class: "card stack" }, h("h3", {}, "Time & budget"),
        h("label", {}, "Time zone"), tz, tzList,
        h("div", { class: "grid2" }, h("div", {}, h("label", {}, "Max cost per task (USD)"), taskCap), h("div", {}, h("label", {}, "Max cost per day (USD)"), dailyCap)),
        h("div", { class: "small muted" }, `Spent today ${money(settings.spend.today_usd)} · this month ${money(settings.spend.month_usd)}`),
        h("div", { class: "small muted" }, `Per task: ${settings.limits.max_steps} steps, ${settings.limits.max_tool_calls} tool calls, ${settings.limits.max_searches} searches, ${settings.limits.max_fetches} page reads, ${settings.limits.max_minutes} min. Change these in .env.`)),
      h("div", { class: "card stack" }, h("h3", {}, "This installation"),
        h("dl", { class: "kv small" }, h("dt", {}, "Data"), h("dd", {}, h("code", {}, settings.data_dir)), h("dt", {}, "Workspace"), h("dd", {}, h("code", {}, settings.workspace_dir)),
          h("dt", {}, "Parallel tasks"), h("dd", {}, String(settings.limits.max_concurrent_tasks))))),
    h("div", { class: "row mt" }, h("button", { class: "primary", onclick: async () => {
      const res = await guarded(() => api("PUT", "/api/settings", { model_provider: provider.value, anthropic_effort: effort.value, timezone: tz.value.trim(), task_max_cost_usd: Number(taskCap.value), daily_max_cost_usd: Number(dailyCap.value) }), "Settings saved");
      if (res) { await loadSettings(); route(); }
    } }, "Save settings")));
}

async function renderMore() {
  setTitle("More");
  render(h("div", { class: "page-head" }, h("h2", {}, "More")),
    h("ul", { class: "list card" },
      [["#/schedules", "Schedules"], ["#/profile", "Profile & memory"], ["#/files", "Files"], ["#/settings", "Settings & integrations"]]
        .map(([href, label]) => h("li", {}, h("a", { class: "item", href }, label))),
      h("li", {}, h("button", { class: "link", onclick: signOut }, "Sign out"))));
}

boot();
