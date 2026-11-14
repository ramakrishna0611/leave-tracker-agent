// Away. — single-page frontend for the Leave Tracker Agent API (vanilla JS, no build step).
const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const state = { me: null, month: null, sel: { start: null, end: null }, type: "casual", checkTimer: null };

// ---------- helpers ----------
async function api(path, opts = {}) {
  const res = await fetch(path, {
    method: opts.method || (opts.body ? "POST" : "GET"),
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    body: opts.body ? JSON.stringify(opts.body) : undefined,
    credentials: "same-origin",
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok) {
    const err = new Error(typeof data.detail === "string" ? data.detail : "Something went wrong.");
    err.status = res.status;
    throw err;
  }
  return data;
}

function esc(s) {
  return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// Small, safe markdown subset for assistant replies: **bold**, lists, paragraphs.
function md(text) {
  const lines = esc(String(text).replace(/[   ]/g, " ")).split(/\n/);
  let html = "", inList = false;
  for (const raw of lines) {
    const line = raw.replace(/\*\*(.+?)\*\*/g, "<b>$1</b>").replace(/(^|[^*])\*(?!\s)(.+?)\*(?!\*)/g, "$1<i>$2</i>");
    const item = line.match(/^\s*(?:[-•]|\d+\.)\s+(.*)/);
    if (item) { if (!inList) { html += "<ul>"; inList = true; } html += `<li>${item[1]}</li>`; continue; }
    if (inList) { html += "</ul>"; inList = false; }
    if (line.trim()) html += `<p>${line}</p>`;
  }
  return html + (inList ? "</ul>" : "");
}

function toast(msg, bad = false) {
  const t = document.createElement("div");
  t.className = "toast" + (bad ? " bad" : "");
  t.textContent = msg;
  $("#toasts").appendChild(t);
  setTimeout(() => t.remove(), 4200);
}

const iso = d => `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, "0")}-${String(d.getDate()).padStart(2, "0")}`;
const parse = s => { const [y, m, d] = s.split("-").map(Number); return new Date(y, m - 1, d); };
const nice = s => parse(s).toLocaleDateString("en-GB", { weekday: "short", day: "numeric", month: "short" });
const initials = name => name.split(" ").map(p => p[0]).join("").slice(0, 2).toUpperCase();

// ---------- login ----------
function showLogin() {
  $("#app").hidden = true;
  $("#login").hidden = false;
  $("#org-form").hidden = false;
  $("#user-form").hidden = true;
  $("#step1-dot").classList.add("on");
  $("#step2-dot").classList.remove("on");
  $("#org-form [name=code]").focus();
}

$("#org-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    const r = await api("/api/login/org", { body: { code: f.get("code"), password: f.get("password") } });
    $("#org-chosen").textContent = r.org;
    $("#seat").textContent = String(f.get("code")).toUpperCase();
    $("#org-form").hidden = true;
    $("#user-form").hidden = false;
    $("#step2-dot").classList.add("on");
    $("#user-form [name=email]").focus();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#user-form").addEventListener("submit", async e => {
  e.preventDefault();
  const f = new FormData(e.target);
  $("#login-error").textContent = "";
  try {
    await api("/api/login/user", { body: { email: f.get("email"), password: f.get("password") } });
    e.target.reset();
    await boot();
  } catch (err) { $("#login-error").textContent = err.message; }
});

$("#back-org").addEventListener("click", showLogin);

$("#logout").addEventListener("click", async () => {
  await api("/api/logout", { body: {} });
  $("#messages").innerHTML = "";
  state.me = null;
  showLogin();
});

// ---------- boot ----------
async function boot() {
  try { state.me = await api("/api/me"); }
  catch (err) { if (err.status === 401) return showLogin(); throw err; }
  const me = state.me;
  $("#login").hidden = true;
  $("#app").hidden = false;
  $("#org-name").textContent = me.org;
  $("#project-name").textContent = me.project;
  $("#user-name").textContent = me.name;
  $("#role-stamp").textContent = me.role;
  $("#acting").hidden = !me.acting_pm;
  $("#today").textContent = parse(me.today).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" });
  setBell(me.unread);
  if (!state.month) state.month = me.today.slice(0, 7);
  renderChips();
  if (!$("#messages").children.length) {
    addMsg("bot", `Hi ${me.name.split(" ")[0]}. I can check your balance, show who's away, apply for leave` +
      (me.can_manage ? ", and handle approvals for your team" : "") + ". What do you need?");
  }
  renderTypes();
  await Promise.all([loadCalendar(), loadBoard()]);
  showCoverNotice();
}

async function refreshAll() {
  state.me = await api("/api/me");
  setBell(state.me.unread);
  $("#acting").hidden = !state.me.acting_pm;
  renderTypes();
  await Promise.all([loadCalendar(), loadBoard()]);
}

// ---------- notifications ----------
function setBell(n) {
  $("#bell-count").hidden = !n;
  $("#bell-count").textContent = n;
}

$("#bell").addEventListener("click", async e => {
  e.stopPropagation();
  const panel = $("#notif-panel");
  if (!panel.hidden) { panel.hidden = true; return; }
  const items = await api("/api/notifications");
  panel.innerHTML = items.length ? items.map(n =>
    `<div class="notif ${n.read ? "" : "unread"}"><div>${esc(n.message)}</div><div class="at">${esc(n.at)}</div></div>`).join("")
    : `<div class="notif empty">No notifications yet.</div>`;
  panel.hidden = false;
  await api("/api/notifications/read-all", { body: {} });
  setBell(0);
});
document.addEventListener("click", e => { if (!e.target.closest("#notif-panel")) $("#notif-panel").hidden = true; });

// ---------- cover notice (BA) ----------
function showCoverNotice() {
  const notices = state.me.cover_notices || [];
  if (!notices.length) return;
  const c = notices[0];
  $("#cover-title").textContent = `${c.pm} is out of office`;
  $("#cover-text").innerHTML = `From <b>${esc(c.start)}</b> to <b>${esc(c.end)}</b> you are the <b>acting project manager</b> for ${esc(state.me.project)}` +
    (c.now ? " — starting today." : ".");
  $("#cover-modal").dataset.id = c.id;
  $("#cover-modal").showModal();
}

$("#cover-ack").addEventListener("click", async () => {
  await api(`/api/cover/${$("#cover-modal").dataset.id}/ack`, { body: {} });
  $("#cover-modal").close();
  state.me.cover_notices.shift();
  toast("Thanks — cover acknowledged.");
  showCoverNotice();
});

// ---------- chat ----------
function addMsg(kind, text) {
  const el = document.createElement("div");
  el.className = `msg ${kind}`;
  if (kind === "bot") el.innerHTML = md(text); else el.textContent = text;
  $("#messages").appendChild(el);
  $("#messages").scrollTop = $("#messages").scrollHeight;
  return el;
}

function renderChips() {
  const me = state.me;
  const chips = ["I want to take leave", "What's my balance?", "Who is away next week?"];
  if (me.can_manage) chips.push("Any requests waiting for me?", "How much leave has my team used?");
  else chips.push("Show my leave requests");
  chips.push("What's the sick leave policy?");
  $("#chips").innerHTML = chips.map(c => `<button type="button">${esc(c)}</button>`).join("");
  $$("#chips button").forEach(b => b.addEventListener("click", () => sendChat(b.textContent)));
}

async function sendChat(text) {
  text = text.trim();
  if (!text) return;
  addMsg("user", text);
  $("#chat-input").value = "";
  $("#send").disabled = true;
  const typing = addMsg("sys", "");
  typing.innerHTML = '<span class="typing"><span></span><span></span><span></span></span>';
  try {
    const r = await api("/api/chat", { body: { message: text } });
    typing.remove();
    addMsg("bot", r.reply || "…");
    setPending(r.pending);
    await refreshAll();
  } catch (err) {
    typing.remove();
    addMsg("sys", err.message);
  } finally {
    $("#send").disabled = false;
    $("#chat-input").focus();
  }
}

function setPending(summary) {
  $("#pending").hidden = !summary;
  $("#pending-text").textContent = summary || "";
}

$("#chat-form").addEventListener("submit", e => { e.preventDefault(); sendChat($("#chat-input").value); });

$("#confirm").addEventListener("click", async () => {
  const r = await api("/api/chat/confirm", { body: {} });
  setPending(null);
  addMsg(r.ok ? "bot" : "sys", r.message);
  if (r.ok) toast(r.message);
  await refreshAll();
});

$("#discard").addEventListener("click", async () => {
  await api("/api/chat/discard", { body: {} });
  setPending(null);
  addMsg("sys", "Discarded.");
});

// ---------- calendar ----------
async function loadCalendar() {
  const data = await api(`/api/calendar?month=${state.month}`);
  const [y, m] = state.month.split("-").map(Number);
  $("#month-title").textContent = new Date(y, m - 1, 1).toLocaleDateString("en-GB", { month: "long", year: "numeric" });
  const grid = $("#cal-grid");
  grid.innerHTML = "";
  for (let i = 0; i < data.first_weekday; i++) grid.insertAdjacentHTML("beforeend", '<div class="day blank"></div>');
  for (const d of data.days) {
    const b = document.createElement("button");
    b.type = "button";
    b.className = "day";
    b.dataset.date = d.date;
    if (d.weekend) b.classList.add("weekend");
    if (d.holiday) b.classList.add("holiday");
    if (d.past) b.classList.add("past");
    if (d.today) b.classList.add("today");
    if (d.blocked_by.length) b.classList.add("blocked");
    b.disabled = d.past;
    const tip = [d.holiday, ...d.leaves.map(l => `${l.name} (${l.status.toLowerCase()} ${l.type})`)].filter(Boolean);
    if (d.blocked_by.length) tip.push(`Blocked: ${d.blocked_by.join(", ")} is away`);
    b.title = tip.join("\n");
    b.innerHTML = `<span class="num">${Number(d.date.slice(8))}</span>` +
      (d.holiday ? `<span class="hol">${esc(d.holiday)}</span>` : "") +
      `<span class="people">${d.leaves.map(l =>
        `<span class="person ${l.status} ${l.mine ? "me" : ""}" title="${esc(l.name)}">${esc(initials(l.name))}</span>`).join("")}</span>`;
    b.addEventListener("click", () => pickDay(d.date));
    grid.appendChild(b);
  }
  paintRange();
}

function pickDay(date) {
  const s = state.sel;
  if (!s.start || (s.start && s.end) || date < s.start) { s.start = date; s.end = null; }
  else s.end = date;
  paintRange();
  scheduleCheck();
}

function paintRange() {
  const { start, end } = state.sel;
  $$(".day[data-date]").forEach(el => {
    const d = el.dataset.date;
    el.classList.toggle("in-range", !!start && d >= start && d <= (end || start));
    el.classList.toggle("range-edge", d === start || d === end);
  });
  $("#range-label").textContent = start
    ? (end && end !== start ? `${nice(start)} → ${nice(end)}` : `${nice(start)}${end ? "" : " · pick an end day or request one day"}`)
    : "Pick a start and an end day";
}

$("#prev-month").addEventListener("click", () => shiftMonth(-1));
$("#next-month").addEventListener("click", () => shiftMonth(1));
function shiftMonth(delta) {
  const [y, m] = state.month.split("-").map(Number);
  const d = new Date(y, m - 1 + delta, 1);
  state.month = iso(d).slice(0, 7);
  loadCalendar();
}

function renderTypes() {
  const bal = state.me.balance;
  $("#types").innerHTML = state.me.leave_types.map(t =>
    `<button type="button" class="type ${t === state.type ? "on" : ""}" data-type="${t}">
       <b>${bal[t].remaining}</b><small>${t[0].toUpperCase() + t.slice(1)} · of ${bal[t].allowed}</small></button>`).join("");
  $$(".type").forEach(b => b.addEventListener("click", () => {
    state.type = b.dataset.type;
    renderTypes();
    scheduleCheck();
  }));
}

function scheduleCheck() {
  clearTimeout(state.checkTimer);
  state.checkTimer = setTimeout(runCheck, 150);
}

async function runCheck() {
  const line = $("#check-line");
  const { start, end } = state.sel;
  $("#apply-btn").disabled = true;
  if (!start) { line.className = "check-line"; line.textContent = ""; return; }
  const r = await api("/api/check", { body: { leave_type: state.type, start, end: end || start } });
  if (r.ok) {
    line.className = "check-line ok";
    line.textContent = `✓ ${r.days} working day${r.days === 1 ? "" : "s"} · ${r.remaining - r.days} ${state.type} left after this`;
    $("#apply-btn").disabled = false;
  } else {
    line.className = "check-line bad";
    line.innerHTML = esc(r.problems.join(" "));
    if (r.suggestion) {
      const btn = document.createElement("button");
      btn.className = "btn ghost small";
      btn.type = "button";
      btn.textContent = `Use ${r.suggestion_label}`;
      btn.addEventListener("click", () => {
        state.sel = { start: r.suggestion[0], end: r.suggestion[1] };
        state.month = r.suggestion[0].slice(0, 7);
        loadCalendar().then(scheduleCheck);
      });
      line.appendChild(btn);
    }
  }
}

$("#apply-form").addEventListener("submit", async e => {
  e.preventDefault();
  const reason = $("#reason").value.trim();
  if (!reason) { toast("Please add a reason.", true); $("#reason").focus(); return; }
  const { start, end } = state.sel;
  try {
    const r = await api("/api/leaves", { body: { leave_type: state.type, start, end: end || start, reason } });
    toast(r.message);
    state.sel = { start: null, end: null };
    $("#reason").value = "";
    $("#check-line").textContent = "";
    $("#apply-btn").disabled = true;
    await refreshAll();
  } catch (err) { toast(err.message, true); }
});

// ---------- team board ----------
function gauge(type, b) {
  const r = 26, c = 2 * Math.PI * r;
  const usedFrac = Math.min(1, (b.used + b.pending) / b.allowed);
  return `<div class="gauge"><svg width="70" height="70" viewBox="0 0 70 70" aria-hidden="true">
    <circle cx="35" cy="35" r="${r}" fill="none" stroke="var(--paper-2)" stroke-width="7"/>
    <circle cx="35" cy="35" r="${r}" fill="none" stroke="var(--brand)" stroke-width="7" stroke-linecap="round"
      stroke-dasharray="${c * (1 - usedFrac)} ${c}" transform="rotate(-90 35 35)"/>
    <text x="35" y="41" text-anchor="middle" class="val">${b.remaining}</text></svg>${type} left</div>`;
}

async function loadBoard() {
  const me = state.me;
  $("#balances").innerHTML = me.leave_types.map(t => gauge(t, me.balance[t])).join("");
  const [board, mine] = await Promise.all([api("/api/dashboard"), api("/api/leaves/mine")]);

  // timeline swimlanes
  const n = board.axis.length;
  const cols = `repeat(${n}, 1fr)`;
  const cells = board.axis.map(a => `<i class="${a.off ? "off" : ""}"></i>`).join("");
  let html = `<div class="tl-row tl-axis"><span></span><div class="tl-cells" style="grid-template-columns:${cols}">${
    board.axis.map((a, i) => `<i class="${a.off ? "off" : ""}" title="${a.date}">${a.dow}<br>${
      i === 0 || a.dow === "M" ? a.dom : "&nbsp;"}</i>`).join("")}</div></div>`;
  for (const lane of board.lanes) {
    html += `<div class="tl-row"><span class="tl-name ${lane.me ? "me" : ""}">${esc(lane.name.split(" ")[0])}<small>${lane.role}</small></span>
      <div class="tl-track"><div class="tl-cells" style="grid-template-columns:${cols}">${cells}</div>${
      lane.bars.map(b => `<span class="tl-bar ${b.status}" style="left:${b.offset / n * 100}%;width:${b.length / n * 100}%"
        title="${esc(b.label)} · ${b.status.toLowerCase()} ${b.type}">${b.length >= 3 ? b.type : ""}</span>`).join("")}</div></div>`;
  }
  $("#timeline").innerHTML = html;
  $("#board-hint").textContent = `${nice(board.axis[0].date)} → ${nice(board.axis[n - 1].date)}`;

  // capacity
  $("#capacity-block").hidden = !board.capacity.length;
  $("#capacity").innerHTML = board.capacity.map(c =>
    `<div class="cap"><span>${esc(c.date)} · ${esc(c.away.join(", "))} away</span><b>${c.percent}% capacity</b></div>`).join("");

  // approvals and usage (managers)
  const canDecide = board.manager || board.approvals.length > 0;
  $("#approvals-block").hidden = !canDecide;
  $("#usage-block").hidden = !board.manager;
  if (canDecide) {
    $("#approvals-count").textContent = board.approvals.length || "";
    $("#approvals-count").hidden = !board.approvals.length;
    $("#approvals").innerHTML = board.approvals.length ? board.approvals.map(a => `
      <div class="card-row"><div class="top"><b>${esc(a.name)}</b><span class="meta">${a.days}d ${esc(a.type)}</span></div>
        <div class="meta">${esc(a.label)}</div><div class="reason">“${esc(a.reason)}”</div>
        <div class="row-btns"><button class="btn stamp reject" data-id="${a.id}" data-ok="0">Reject</button>
        <button class="btn stamp" data-id="${a.id}" data-ok="1">Approve</button></div></div>`).join("")
      : '<p class="empty">Nothing waiting. Nice.</p>';
    $$("#approvals .btn").forEach(b => b.addEventListener("click", () => decide(b.dataset.id, b.dataset.ok === "1")));
  }
  if (board.manager) {

    const max = Math.max(...board.usage.map(u => u.total_used + u.total_pending + u.total_remaining), 1);
    $("#usage").innerHTML = board.usage.map(u => `
      <div class="usage-row"><span>${esc(u.name.split(" ")[0])}${u.no_leave_yet ? '<span class="chip-warn" title="No leave taken or booked this year">rest?</span>' : ""}</span>
        <span class="bar"><i class="u" style="width:${u.total_used / max * 100}%"></i><i class="p" style="width:${u.total_pending / max * 100}%"></i></span>
        <span class="n">${u.total_used}${u.total_pending ? "+" + u.total_pending : ""}/${u.total_used + u.total_pending + u.total_remaining}</span></div>`).join("");
  }

  // my leaves
  $("#my-leaves").innerHTML = mine.length ? mine.map(l => `
    <div class="card-row"><div class="top"><span><b>${esc(l.type)}</b> · ${l.days}d</span><span class="status ${l.status}">${l.status}</span></div>
      <div class="meta">${esc(l.label)}</div><div class="reason">${esc(l.reason)}${l.note ? ` — <i>${esc(l.note)}</i>` : ""}</div>
      ${["PENDING", "APPROVED"].includes(l.status) && l.start >= state.me.today
        ? `<div class="row-btns"><button class="btn ghost small" data-cancel="${l.id}">Cancel</button></div>` : ""}</div>`).join("")
    : '<p class="empty">No leave yet. Pick days on the calendar or ask the assistant.</p>';
  $$("[data-cancel]").forEach(b => b.addEventListener("click", () => cancelLeave(b.dataset.cancel)));
}

async function decide(id, approve) {
  const note = approve ? "" : (prompt("Reason for rejecting (optional):") || "");
  try {
    const r = await api(`/api/approvals/${id}`, { body: { approve, note } });
    toast(r.message);
    await refreshAll();
  } catch (err) { toast(err.message, true); }
}

async function cancelLeave(id) {
  if (!confirm("Cancel this leave?")) return;
  try {
    const r = await api(`/api/leaves/${id}/cancel`, { body: {} });
    toast(r.message);
    await refreshAll();
  } catch (err) { toast(err.message, true); }
}

// ---------- dark / light mode ----------
function currentTheme() {
  return document.documentElement.dataset.theme ||
    (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}
$$(".theme-btn").forEach(btn => btn.addEventListener("click", () => {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.documentElement.dataset.theme = next;
  try { localStorage.setItem("away-theme", next); } catch (e) { /* private mode: still switches */ }
}));
// Without a saved choice, follow the system setting so the sun/moon icon matches what is shown
if (!document.documentElement.dataset.theme) document.documentElement.dataset.theme = currentTheme();

// ---------- mobile tabs ----------
function showTab(tab) {
  $$("#tabs button").forEach(b => b.classList.toggle("on", b.dataset.tab === tab));
  $$(".grid > .panel").forEach(p => p.classList.toggle("show", p.dataset.pane === tab));
}
$$("#tabs button").forEach(b => b.addEventListener("click", () => showTab(b.dataset.tab)));
showTab("chat");

boot().catch(err => toast(err.message, true));
