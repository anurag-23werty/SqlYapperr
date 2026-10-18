// SQLYapper web client. No framework, no inline scripts (strict CSP).
// All server data is rendered with textContent, never innerHTML.

const state = { user: null, csrf: null, datasets: [], current: null, conversation: null, mode: "signin" };
const $ = (sel) => document.querySelector(sel);

async function api(path, { method = "GET", body, form } = {}) {
  const headers = {};
  if (method !== "GET") headers["X-CSRF-Token"] = state.csrf || "";
  let payload;
  if (form) payload = form;
  else if (body !== undefined) { headers["Content-Type"] = "application/json"; payload = JSON.stringify(body); }
  const resp = await fetch(path, { method, headers, body: payload, credentials: "same-origin" });
  let data = null;
  try { data = await resp.json(); } catch { /* empty body */ }
  if (!resp.ok) {
    const err = new Error(data?.error?.message || `Request failed (${resp.status})`);
    err.status = resp.status;
    err.code = data?.error?.code;
    throw err;
  }
  return data;
}

function el(tag, attrs = {}, text) {
  const node = document.createElement(tag);
  for (const [k, v] of Object.entries(attrs)) {
    if (k === "class") node.className = v;
    else node.setAttribute(k, v);
  }
  if (text !== undefined) node.textContent = text;
  return node;
}

function showError(node, message) {
  node.textContent = message;
  node.hidden = !message;
}

// ------------------------------------------------------------------ auth

function setMode(mode) {
  state.mode = mode;
  const signup = mode === "signup";
  $("#tab-signin").setAttribute("aria-selected", String(!signup));
  $("#tab-signup").setAttribute("aria-selected", String(signup));
  $("#name-field").hidden = !signup;
  $("#pw-hint").hidden = !signup;
  $("#auth-submit").textContent = signup ? "Create account" : "Sign in";
  $("#auth-form").password.autocomplete = signup ? "new-password" : "current-password";
  showError($("#auth-error"), "");
}

async function onAuthSubmit(event) {
  event.preventDefault();
  const f = event.currentTarget;
  const button = $("#auth-submit");
  button.disabled = true;
  try {
    const body = { email: f.email.value, password: f.password.value };
    if (state.mode === "signup") body.name = f.name.value;
    const data = await api(`/api/auth/${state.mode}`, { method: "POST", body });
    f.password.value = "";
    await enterApp(data);
  } catch (err) {
    showError($("#auth-error"), err.message);
  } finally {
    button.disabled = false;
  }
}

async function enterApp(session) {
  state.user = session.user;
  state.csrf = session.csrf_token;
  $("#auth-view").hidden = true;
  $("#app-view").hidden = false;
  $("#who").textContent = `${session.user.name} (${session.user.email})`;
  await loadDatasets();
}

async function signOut() {
  try { await api("/api/auth/signout", { method: "POST" }); } catch { /* already signed out */ }
  Object.assign(state, { user: null, csrf: null, datasets: [], current: null, conversation: null });
  $("#thread").replaceChildren();
  $("#app-view").hidden = true;
  $("#auth-view").hidden = false;
}

// -------------------------------------------------------------- datasets

async function loadDatasets(selectId) {
  state.datasets = await api("/api/datasets");
  const list = $("#dataset-list");
  list.replaceChildren();
  for (const ds of state.datasets) {
    const visible = ds.tables.filter((t) => !t.hidden);
    const rows = visible.reduce((n, t) => n + t.row_count, 0);
    const btn = el("button", { type: "button" });
    btn.append(el("span", {}, ds.name), el("span", { class: "meta" }, `${visible.length} table${visible.length === 1 ? "" : "s"}, ${rows.toLocaleString()} rows`));
    btn.addEventListener("click", () => selectDataset(ds.id));
    const li = el("li");
    li.append(btn);
    list.append(li);
  }
  if (!state.datasets.length) list.append(el("li", { class: "muted" }, "No datasets yet. Upload a CSV below."));
  const target = selectId || state.current?.id || state.datasets[0]?.id;
  if (target) selectDataset(target);
}

function selectDataset(id) {
  const ds = state.datasets.find((d) => d.id === id);
  if (!ds) return;
  state.current = ds;
  state.conversation = null;
  for (const [i, li] of [...$("#dataset-list").children].entries()) {
    li.firstChild?.setAttribute?.("aria-current", String(state.datasets[i]?.id === id));
  }
  $("#ds-title").textContent = ds.name;
  const visible = ds.tables.filter((t) => !t.hidden).map((t) => t.name);
  $("#ds-sub").textContent = visible.length ? `Tables: ${visible.join(", ")}` : "No visible tables";
  $("#question").disabled = false;
  $("#ask").disabled = false;
  renderSchema();
  renderEmpty();
}

function renderSchema() {
  const panel = $("#schema-panel");
  panel.replaceChildren();
  const ds = state.current;
  if (!ds) return;
  panel.append(el("p", { class: "note" },
    "Hidden columns are never shown to the AI and can never be queried. Columns that look personal (emails, phone numbers, passwords) start hidden."));
  for (const t of ds.tables) {
    const h = el("h4", {}, `${t.name}, ${t.row_count.toLocaleString()} rows${t.hidden ? ", hidden table" : ""}`);
    const cols = el("div", { class: "cols" });
    for (const c of t.columns) {
      const chip = el("span", { class: `chip${c.hidden || t.hidden ? " hidden-col" : ""}`, title: c.original || c.name });
      chip.append(document.createTextNode(`${c.name} (${c.type.toLowerCase()})`));
      if (!t.hidden) {
        const toggle = el("button", { type: "button", "aria-label": `${c.hidden ? "Show" : "Hide"} ${c.name}` }, c.hidden ? "show" : "hide");
        toggle.addEventListener("click", () => setHidden(t.name, c.name, !c.hidden));
        chip.append(toggle);
      }
      cols.append(chip);
    }
    panel.append(h, cols);
  }
}

async function setHidden(table, column, hidden) {
  try {
    const updated = await api(`/api/datasets/${state.current.id}/visibility`, { method: "PATCH", body: { table, column, hidden } });
    state.datasets = state.datasets.map((d) => (d.id === updated.id ? updated : d));
    state.current = updated;
    renderSchema();
  } catch (err) {
    alertTurn("error", err.message);
  }
}

async function onUpload(event) {
  event.preventDefault();
  const f = event.currentTarget;
  const files = f.files.files;
  if (!files.length) return showError($("#upload-error"), "Choose at least one CSV file.");
  const form = new FormData();
  form.append("name", f.name.value || files[0].name.replace(/\.[^.]+$/, ""));
  for (const file of files) form.append("files", file);
  const button = f.querySelector("button[type=submit]");
  button.disabled = true;
  button.textContent = "Uploading…";
  try {
    const ds = await api("/api/datasets", { method: "POST", form });
    f.reset();
    $("#drop-label").textContent = "Choose CSV files (each file becomes a table)";
    showError($("#upload-error"), "");
    await loadDatasets(ds.id);
  } catch (err) {
    showError($("#upload-error"), err.message);
  } finally {
    button.disabled = false;
    button.textContent = "Upload";
  }
}

// ------------------------------------------------------------------ chat

function renderEmpty() {
  const box = el("div", { class: "empty" });
  box.append(el("p", {}, "Ask a question about this dataset. Try one of these:"));
  const ul = el("ul");
  for (const q of exampleQuestions()) {
    const b = el("button", { type: "button" }, q);
    b.addEventListener("click", () => { $("#question").value = q; $("#question").focus(); });
    const li = el("li");
    li.append(b);
    ul.append(li);
  }
  box.append(ul);
  $("#thread").replaceChildren(box);
}

function exampleQuestions() {
  const ds = state.current;
  if (ds?.name.startsWith("Sample shop")) {
    return ["What are the top 10 customers by revenue?", "Show monthly sales for 2025.", "Which products have the highest return rate?"];
  }
  const out = [];
  for (const t of ds?.tables || []) {
    if (t.hidden) continue;
    const cols = t.columns.filter((c) => !c.hidden);
    const num = cols.find((c) => ["INTEGER", "REAL"].includes(c.type) && !c.name.endsWith("id"));
    const cat = cols.find((c) => c.type === "TEXT" && !c.semantic && c.distinct > 1 && c.distinct <= 30);
    const date = cols.find((c) => c.semantic);
    if (num && cat) out.push(`What is the total ${num.name.replaceAll("_", " ")} by ${cat.name.replaceAll("_", " ")}?`);
    if (date) out.push(`How many ${t.name.replaceAll("_", " ")} per month?`);
    out.push(`How many rows are in ${t.name}?`);
  }
  return out.slice(0, 3);
}

const STAGES = [
  ["Read question", () => true],
  ["Drafted SQL", (r) => r.attempts.some((a) => a.stage !== "generation")],
  ["Checked every node", (r) => r.attempts.some((a) => a.stage === "execution")],
  ["Rewrote from the checked tree", (r) => r.attempts.some((a) => a.stage === "execution")],
  ["Ran read-only", (r) => r.kind === "answer"],
];

function renderTrail(ol, r) {
  ol.replaceChildren();
  const rejected = r.attempts.filter((a) => a.stage === "validation").length;
  for (const [label, done] of STAGES) {
    let text = label;
    if (label === "Checked every node" && rejected) text += ` (${rejected} draft${rejected > 1 ? "s" : ""} rejected)`;
    if (label === "Ran read-only" && r.duration_ms != null) text += ` in ${r.duration_ms} ms`;
    const ok = done(r);
    ol.append(el("li", { class: ok ? "" : (r.kind === "answer" ? "skip" : "stop") }, text));
    if (!ok) break;
  }
}

function renderTurn(question, r) {
  const node = $("#tpl-turn").content.firstElementChild.cloneNode(true);
  node.classList.add(r.kind);
  node.querySelector(".q").textContent = question;
  node.querySelector(".a").textContent = r.kind === "answer" ? (r.answer || "Done.") : (r.message || "Something went wrong.");
  if (["answer", "rejected", "error"].includes(r.kind) && r.attempts.length) renderTrail(node.querySelector(".trail"), r);
  else node.querySelector(".trail").remove();

  if (r.sql) {
    const box = node.querySelector(".sql-box");
    box.hidden = false;
    box.querySelector("code").textContent = r.sql;
    const notes = [r.explanation, ...(r.assumptions || []).map((a) => `Assumption: ${a}`)].filter(Boolean);
    box.querySelector(".assumptions").textContent = notes.join(" ");
    const plan = box.querySelector(".plan");
    if (r.plan?.length) for (const p of r.plan) plan.querySelector("ul").append(el("li", {}, p));
    else plan.remove();
  }
  if (r.columns?.length) {
    const wrap = node.querySelector(".table-wrap");
    wrap.hidden = false;
    const headRow = el("tr");
    const numeric = r.columns.map((_, i) => r.rows.length > 0 && r.rows.every((row) => row[i] === null || typeof row[i] === "number"));
    r.columns.forEach((c, i) => headRow.append(el("th", numeric[i] ? { scope: "col", class: "num" } : { scope: "col" }, c)));
    wrap.querySelector("thead").append(headRow);
    const tbody = wrap.querySelector("tbody");
    for (const row of r.rows) {
      const tr = el("tr");
      for (const v of row) {
        const isNum = typeof v === "number";
        tr.append(el("td", isNum ? { class: "num" } : {}, v === null ? "—" : isNum ? v.toLocaleString() : String(v)));
      }
      tbody.append(tr);
    }
  }
  const foot = [];
  if (r.kind === "answer") foot.push(`${r.row_count} row${r.row_count === 1 ? "" : "s"}${r.truncated ? " (truncated)" : ""}`);
  if (r.applied_default_limit) foot.push("default row limit applied");
  foot.push(`request ${r.request_id.slice(0, 8)}`);
  node.querySelector(".foot").textContent = foot.join(", ");
  return node;
}

function alertTurn(kind, message) {
  const r = { kind, message, attempts: [], request_id: "--------" };
  $("#thread").append(renderTurn("", r));
}

async function onAsk(event) {
  event.preventDefault();
  const input = $("#question");
  const question = input.value.trim();
  if (!question || !state.current) return;
  const thread = $("#thread");
  thread.querySelector(".empty")?.remove();
  const pending = el("p", { class: "thinking" }, "Thinking…");
  thread.append(pending);
  $("#ask").disabled = true;
  try {
    const r = await api(`/api/datasets/${state.current.id}/chat`, {
      method: "POST", body: { question, conversation_id: state.conversation || undefined },
    });
    state.conversation = r.conversation_id;
    pending.replaceWith(renderTurn(question, r));
    input.value = "";
  } catch (err) {
    pending.remove();
    if (err.status === 401) return signOut();
    alertTurn("error", err.message);
  } finally {
    $("#ask").disabled = false;
    thread.lastElementChild?.scrollIntoView({ behavior: "smooth", block: "start" });
  }
}

// ------------------------------------------------------------------ boot

function wire() {
  $("#tab-signin").addEventListener("click", () => setMode("signin"));
  $("#tab-signup").addEventListener("click", () => setMode("signup"));
  $("#auth-form").addEventListener("submit", onAuthSubmit);
  $("#signout").addEventListener("click", signOut);
  $("#upload-form").addEventListener("submit", onUpload);
  $("#upload-form").files.addEventListener("change", (e) => {
    const n = e.target.files.length;
    $("#drop-label").textContent = n ? [...e.target.files].map((f) => f.name).join(", ") : "Choose CSV files (each file becomes a table)";
  });
  $("#ask-form").addEventListener("submit", onAsk);
  $("#question").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); $("#ask-form").requestSubmit(); }
  });
  $("#new-chat").addEventListener("click", () => { state.conversation = null; renderEmpty(); });
  $("#toggle-schema").addEventListener("click", (e) => {
    const panel = $("#schema-panel");
    panel.hidden = !panel.hidden;
    e.currentTarget.setAttribute("aria-expanded", String(!panel.hidden));
  });
}

async function boot() {
  wire();
  try {
    await enterApp(await api("/api/auth/me"));
  } catch {
    $("#auth-view").hidden = false;
    setMode("signin");
  }
}

boot();
