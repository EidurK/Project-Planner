const $ = (id) => document.getElementById(id);
let busy = false;
function setBusy(v) { busy = v; $("stop").hidden = !v; }
const sessionId = localStorage.getItem("pm-session") || crypto.randomUUID();
localStorage.setItem("pm-session", sessionId);

function addMsg(cls, text) {
  const el = document.createElement("div");
  el.className = cls.startsWith("tool") ? cls : `msg ${cls}`;
  el.textContent = text;
  $("messages").appendChild(el);
  $("messages").scrollTop = $("messages").scrollHeight;
  return el;
}

let projects = [];

// top-level projects first, each followed by its subtasks: [[project, depth], ...]
function orderedTree() {
  const ids = new Set(projects.map((p) => p.id));
  const out = [];
  const walk = (p, depth) => {
    out.push([p, depth]);
    projects.filter((c) => c.parent_id === p.id).forEach((c) => walk(c, depth + 1));
  };
  projects.filter((p) => !p.parent_id || !ids.has(p.parent_id)).forEach((p) => walk(p, 0));
  return out;
}

async function loadProjects() {
  projects = await (await fetch("/api/projects")).json();
  renderTable();
  renderGantt();
  loadOverview();
}

function renderTable() {
  const t = $("projects");
  t.replaceChildren();
  const head = t.insertRow();
  for (const h of ["Id", "Name", "Status", "Who", "Est h", "Logged", "Needs"]) {
    const th = document.createElement("th"); th.textContent = h; head.appendChild(th);
  }
  for (const [p, depth] of orderedTree()) {
    const r = t.insertRow();
    r.insertCell().textContent = p.id;
    const nameCell = r.insertCell();
    nameCell.textContent = p.name;
    nameCell.style.paddingLeft = `${8 + depth * 16}px`;
    if (depth) nameCell.className = "sub";
    const s = r.insertCell(); const badge = document.createElement("span");
    badge.className = `status ${p.status}`; badge.textContent = p.status.replace("_", " "); s.appendChild(badge);
    r.insertCell().textContent = (p.assignees || []).join(", ");
    r.insertCell().textContent = p.estimated_hours;
    r.insertCell().textContent = p.hours_logged;
    r.insertCell().textContent = (p.depends_on || []).join(", ");
  }
  if (!projects.length) t.insertRow().insertCell().textContent = "No projects yet.";
}

// ---- Gantt ----

// Earliest-start schedule in hours: a task starts when all its (and its ancestors') prerequisites end.
function schedule() {
  const byId = new Map(projects.map((p) => [p.id, p]));
  const kids = new Map();
  for (const p of projects) if (byId.has(p.parent_id)) kids.set(p.parent_id, [...(kids.get(p.parent_id) || []), p]);
  const memo = new Map();
  const visiting = new Set();
  const needs = (p) => {  // own prerequisites plus those inherited from ancestors
    const deps = new Set(p.depends_on || []);
    for (let a = byId.get(p.parent_id), n = 0; a && n < 50; a = byId.get(a.parent_id), n++)
      (a.depends_on || []).forEach((d) => deps.add(d));
    return [...deps];
  };
  const calc = (p) => {
    if (memo.has(p.id)) return memo.get(p.id);
    if (visiting.has(p.id)) return { start: 0, end: 0 };  // cycle guard
    visiting.add(p.id);
    const ready = Math.max(0, ...needs(p).filter((d) => byId.has(d) && d !== p.id).map((d) => calc(byId.get(d)).end));
    let r;
    if (kids.has(p.id)) {
      const spans = kids.get(p.id).map(calc);
      r = { start: Math.min(...spans.map((x) => x.start)), end: Math.max(...spans.map((x) => x.end)) };
    } else {
      r = { start: ready, end: ready + (p.estimated_hours || 0) };
    }
    visiting.delete(p.id);
    memo.set(p.id, r);
    return r;
  };
  projects.forEach(calc);
  return { memo, byId, kids };
}

function niceStep(total) {
  const raw = total / 8;
  const mag = 10 ** Math.floor(Math.log10(raw));
  return [1, 2, 5, 10].map((m) => m * mag).find((s) => s >= raw) || mag * 10;
}

function renderGantt() {
  const root = $("gantt");
  root.replaceChildren();
  if (!projects.length) { root.textContent = "No projects yet."; root.style.padding = "12px"; return; }
  root.style.padding = "";
  const { memo, kids } = schedule();
  const total = Math.max(1, ...[...memo.values()].map((x) => x.end));
  const step = niceStep(total);
  const span = Math.ceil((total * 1.04) / step) * step;  // headroom so the last bar/label isn't flush with the edge
  const pct = (h) => `${(h / span) * 100}%`;

  const row = (cls = "") => { const r = document.createElement("div"); r.className = `g-row ${cls}`; root.appendChild(r); return r; };
  const head = row("head");
  head.append(Object.assign(document.createElement("div"), { className: "g-label", textContent: "Task (in-game hours →)" }));
  const axis = Object.assign(document.createElement("div"), { className: "g-area" });
  for (let h = 0; h <= span; h += step) {
    const tick = Object.assign(document.createElement("div"), { className: "g-tick", textContent: `${h}h` });
    tick.style.left = pct(h);
    axis.appendChild(tick);
  }
  head.appendChild(axis);

  for (const [p, depth] of orderedTree()) {
    const { start, end } = memo.get(p.id);
    const isParent = kids.has(p.id);
    const r = row();
    const glyph = p.status === "done" ? "✓ " : p.status === "blocked" ? "⛔ " : "";
    const label = Object.assign(document.createElement("div"), {
      className: `g-label${isParent ? " parent" : ""}`, textContent: `${glyph}${p.name}`, title: p.name });
    label.style.paddingLeft = `${10 + depth * 14}px`;
    const area = Object.assign(document.createElement("div"), { className: "g-area" });
    for (let h = step; h <= span; h += step) {  // gridlines
      const g = Object.assign(document.createElement("div"), { className: "g-tick" });
      g.style.left = pct(h);
      area.appendChild(g);
    }
    const bar = document.createElement("div");
    bar.className = `g-bar ${isParent ? "parent " : ""}${p.status}`;
    bar.style.left = pct(start);
    bar.style.width = pct(end - start);
    const done = Math.min(1, (p.hours_logged || 0) / (p.estimated_hours || Infinity));
    bar.appendChild(Object.assign(document.createElement("i"), {}));
    bar.firstChild.style.width = `${done * 100}%`;
    const tipText = `${p.name}\n${p.status.replace("_", " ")} · ${start}h → ${end}h` +
      `\n${p.hours_logged || 0} / ${p.estimated_hours || 0}h logged` +
      ((p.assignees || []).length ? `\nWho: ${p.assignees.join(", ")}` : "") +
      ((p.depends_on || []).length ? `\nNeeds: ${p.depends_on.join(", ")}` : "");
    for (const el of [bar, label]) {
      el.addEventListener("mousemove", (e) => showTip(tipText, e));
      el.addEventListener("mouseleave", () => ($("tip").hidden = true));
    }
    area.appendChild(bar);
    r.append(label, area);
  }
}

function showTip(text, e) {
  const tip = $("tip");
  tip.textContent = text;
  tip.hidden = false;
  tip.style.left = `${Math.min(e.clientX + 14, window.innerWidth - tip.offsetWidth - 8)}px`;
  tip.style.top = `${Math.min(e.clientY + 14, window.innerHeight - tip.offsetHeight - 8)}px`;
}

// ---- Overview ----

function el(tag, props = {}, ...kids) {
  const e = Object.assign(document.createElement(tag), props);
  e.append(...kids);
  return e;
}

function workCell(projects) {
  const cell = el("td");
  if (!projects.length) { cell.append(el("small", { textContent: "Nothing assigned" })); return cell; }
  for (const p of projects) {
    const label = p.parent ? `${p.name} ` : p.name;
    cell.append(el("div", { className: `work${p.status === "done" ? " done" : ""}` },
      el("span", { className: `status ${p.status}`, textContent: p.status.replace("_", " ") }),
      el("span", { textContent: label }),
      ...(p.parent ? [el("span", { className: "via", textContent: `(in ${p.parent})` })] : [])));
  }
  return cell;
}

function fillPeopleTable(table, people, { removable }) {
  table.replaceChildren();
  const head = table.insertRow();
  for (const h of ["Name", ...(removable ? ["Position"] : []), "Working on", "Hours left", ""]) head.append(el("th", { textContent: h }));
  for (const person of people) {
    const r = table.insertRow();
    r.append(el("td", { textContent: person.name }));
    if (removable) r.append(el("td", { textContent: person.position }));
    r.append(workCell(person.projects));
    r.append(el("td", { className: "num", textContent: person.remaining_hours }));
    const act = el("td");
    if (removable) {
      act.append(el("button", { className: "x", title: `Remove ${person.name}`, textContent: "✕", onclick: async () => {
        if (!confirm(`Remove ${person.name} from the employee list? Their project assignments stay.`)) return;
        await fetch(`/api/employees/${encodeURIComponent(person.name)}`, { method: "DELETE" });
        loadOverview();
      } }));
    } else {
      act.append(el("button", { title: "Add to employee list", textContent: "Add as employee", onclick: async () => {
        const position = prompt(`Position of ${person.name}?`);
        if (!position) return;
        await fetch("/api/employees", { method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ name: person.name, position }) });
        loadOverview();
      } }));
    }
    r.append(act);
  }
}

async function loadOverview() {
  const data = await (await fetch("/api/overview")).json();
  const t = $("employees");
  if (data.employees.length) fillPeopleTable(t, data.employees, { removable: true });
  else t.replaceChildren(el("tr", {}, el("td", { textContent: "No employees yet. Add them below or ask the project manager." })));
  $("unlisted-box").hidden = !data.unlisted.length;
  fillPeopleTable($("unlisted"), data.unlisted, { removable: false });
  const org = await (await fetch("/api/organization")).json();
  if (document.activeElement !== $("org-text")) $("org-text").value = org.content;
}

$("add-employee").addEventListener("submit", async (e) => {
  e.preventDefault();
  const res = await fetch("/api/employees", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ name: $("emp-name").value, position: $("emp-position").value }) });
  $("emp-status").textContent = res.ok ? "" : (await res.json()).detail;
  if (res.ok) { $("emp-name").value = ""; $("emp-position").value = ""; loadOverview(); }
});
$("save-org").addEventListener("click", async () => {
  const res = await fetch("/api/organization", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content: $("org-text").value }) });
  $("org-status").textContent = res.ok ? "Saved." : (await res.json()).detail;
});

// ---- Settings ----

function applyTheme(choice) {
  const dark = choice === "dark" || (choice === "system" && matchMedia("(prefers-color-scheme: dark)").matches);
  document.documentElement.dataset.theme = dark ? "dark" : "light";
}
$("theme-select").value = localStorage.getItem("pm-theme") || "system";
$("theme-select").addEventListener("change", (e) => {
  localStorage.setItem("pm-theme", e.target.value);
  applyTheme(e.target.value);
});
matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
  if (($("theme-select").value) === "system") applyTheme("system");
});

async function loadSettings() {
  const data = await (await fetch("/api/settings")).json();
  const sel = $("model-select");
  sel.replaceChildren(...data.models.map((m) => new Option(m.price ? `${m.label} — ${m.price}` : m.label, m.id)));
  sel.value = data.model;
  showKeyStatus(data.api_key);
}

function showKeyStatus(k) {
  $("key-status").textContent = !k.configured ? "No API key set. The agents can't answer until you add one."
    : k.source === "app" ? `Using the key saved in this app (${k.hint}).`
    : `Using ANTHROPIC_API_KEY from the server environment (${k.hint}).`;
  $("key-remove").hidden = k.source !== "app";
}
$("key-form").addEventListener("submit", async (e) => {
  e.preventDefault();
  $("key-msg").textContent = "Checking key…";
  const res = await fetch("/api/settings/api-key", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ key: $("key-input").value }) });
  const data = await res.json();
  if (!res.ok) { $("key-msg").textContent = data.detail; return; }
  $("key-input").value = "";
  $("key-msg").textContent = data.warning || "Key saved.";
  showKeyStatus(data.status);
});
$("key-remove").addEventListener("click", async () => {
  if (!confirm("Remove the saved API key?")) return;
  const data = await (await fetch("/api/settings/api-key", { method: "DELETE" })).json();
  $("key-msg").textContent = "Saved key removed.";
  showKeyStatus(data.status);
});
$("model-select").addEventListener("change", async (e) => {
  const res = await fetch("/api/settings", { method: "PUT", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ model: e.target.value }) });
  $("model-status").textContent = res.ok ? `Saved. Now using ${e.target.value}.` : (await res.json()).detail;
  if (!res.ok) loadSettings();
});

// ---- Tabs ----

function showTab(name) {
  if (!$(`tab-${name}`)) name = "chat";
  document.querySelectorAll(".panel").forEach((el) => el.classList.toggle("active", el.id === `tab-${name}`));
  document.querySelectorAll(".tabs button").forEach((b) => b.classList.toggle("active", b.dataset.tab === name));
  history.replaceState(null, "", `#${name}`);
  if (name === "gantt") renderGantt();
  if (name === "overview") loadOverview();
  if (name === "chat") $("input").focus();
}
document.querySelectorAll(".tabs button").forEach((b) => b.addEventListener("click", () => showTab(b.dataset.tab)));

const currentAgent = () => $("agent-select").value;
const agentQuery = () => (currentAgent() ? `?agent=${encodeURIComponent(currentAgent())}` : "");

async function loadAgents() {
  const sel = $("agent-select");
  const keep = sel.value;
  const list = await (await fetch("/api/agents")).json();
  sel.replaceChildren(new Option("Project manager (main agent)", ""));
  for (const a of list) sel.appendChild(new Option(`${a.label}${a.builtin ? "" : " (custom)"} — ${a.description}`, a.id));
  sel.value = [...sel.options].some((o) => o.value === keep) ? keep : "";
}

async function loadPrompt() {
  const res = await fetch(`/api/prompt${agentQuery()}`);
  if (!res.ok) { $("agent-select").value = ""; return loadPrompt(); }  // agent was deleted
  const data = await res.json();
  $("prompt").value = data.content;
  const sel = $("versions");
  sel.replaceChildren(new Option("Previous versions…", ""));
  for (const v of data.versions) sel.appendChild(new Option(v.replace(/^(prompt|agent-[a-z0-9_]+)-/, "").replace(".md", ""), v));
}

let pendingQuestion = null;

async function send(text) {
  setBusy(true);
  addMsg("user", text);
  let bubble = null;
  const res = await fetch("/api/chat", {
    method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId, message: text }),
  });
  const reader = res.body.getReader();
  const dec = new TextDecoder();
  let buf = "";
  while (true) {
    const { value, done } = await reader.read();
    if (done) break;
    buf += dec.decode(value, { stream: true });
    let i;
    while ((i = buf.indexOf("\n\n")) >= 0) {
      const ev = JSON.parse(buf.slice(5, i).trim()); buf = buf.slice(i + 2);
      if (ev.type === "text") {
        if (!bubble) bubble = addMsg("assistant", "");
        bubble.textContent += ev.text;
        $("messages").scrollTop = $("messages").scrollHeight;
      } else if (ev.type === "tool") {
        bubble = null;
        const who = ev.agent && ev.agent !== "pm" ? `${ev.agent}: ` : "";
        const detail = ev.name === "web_search" && ev.input.query ? ` “${ev.input.query}”` : "";
        addMsg(ev.error ? "tool err" : "tool", `🔧 ${who}${ev.name}${detail}`);
        if (ev.writes) { loadProjects(); loadAgents().then(loadPrompt); loadSettings(); }
      } else if (ev.type === "subagent") {
        bubble = null;
        addMsg("tool agent", ev.status === "start" ? `▶ ${ev.name} started` : `■ ${ev.name} finished`);
        if (ev.writes) loadProjects();
      } else if (ev.type === "question") {
        bubble = null;
        pendingQuestion = ev.id;
        addMsg("assistant question", `❓ ${ev.agent} asks:\n${ev.question}`);
        $("input").placeholder = "Answer the question above…";
        $("input").focus();
      } else if (ev.type === "stopped") {
        bubble = null;
        addMsg("error", "⏹ Stopped.");
      } else if (ev.type === "error") {
        addMsg("error", ev.text);
      }
    }
  }
  setBusy(false);
  pendingQuestion = null;
  $("input").placeholder = "Talk to your project manager…";
  $("input").focus();
}

$("form").addEventListener("submit", (e) => {
  e.preventDefault();
  const text = $("input").value.trim();
  if (!text) return;
  if (pendingQuestion) {  // reply goes to the waiting subagent, not a new chat turn
    const id = pendingQuestion;
    pendingQuestion = null;
    $("input").value = "";
    $("input").placeholder = "Talk to your project manager…";
    addMsg("user", text);
    fetch("/api/answer", { method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ question_id: id, answer: text }) });
    return;
  }
  if (busy) return;
  $("input").value = "";
  send(text);
});
$("input").addEventListener("keydown", (e) => {
  if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("form").requestSubmit(); }
});
$("stop").addEventListener("click", () => {
  $("stop").disabled = true;
  fetch("/api/stop", { method: "POST", headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ session_id: sessionId }) }).finally(() => { $("stop").disabled = false; });
});
$("reset").addEventListener("click", async () => {
  await fetch("/api/reset", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ session_id: sessionId }) });
  $("messages").replaceChildren();
  pendingQuestion = null;
  $("input").placeholder = "Talk to your project manager…";
});
$("save-prompt").addEventListener("click", async () => {
  const res = await fetch("/api/prompt", { method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ content: $("prompt").value, agent: currentAgent() || null }) });
  $("prompt-status").textContent = res.ok ? "Saved." : (await res.json()).detail;
  if (res.ok) loadPrompt();
});
$("versions").addEventListener("change", async (e) => {
  if (!e.target.value) return;
  const data = await (await fetch(`/api/prompt/versions/${e.target.value}${agentQuery()}`)).json();
  $("prompt").value = data.content;
  $("prompt-status").textContent = "Loaded old version into editor — click Save to restore it.";
});

$("agent-select").addEventListener("change", loadPrompt);
loadProjects(); loadAgents().then(loadPrompt); loadSettings();
showTab(location.hash.slice(1) || "chat");
