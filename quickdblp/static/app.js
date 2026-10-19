"use strict";

// ---------- helpers ----------

const $ = (selector) => document.querySelector(selector);

// Build DOM nodes without innerHTML: every string from the server ends up as text.
function el(tag, props = {}, ...children) {
  const node = document.createElement(tag);
  for (const [key, value] of Object.entries(props)) {
    if (value === false || value == null) continue;
    if (key === "class") node.className = value;
    else if (key === "text") node.textContent = value;
    else if (key.startsWith("on")) node.addEventListener(key.slice(2), value);
    else node.setAttribute(key, value === true ? "" : value);
  }
  for (const child of children.flat(Infinity)) if (child != null) node.append(child);
  return node;
}

function link(url, text) {
  if (!/^https?:\/\//.test(url || "")) return document.createTextNode(text ?? url ?? "");
  return el("a", { href: url, target: "_blank", rel: "noopener noreferrer", text: text ?? url });
}

// Disclosure triangle for table rows: inline SVG, so it does not depend on any stylesheet rule.
function triangle(open) {
  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 12 12");
  svg.setAttribute("width", "13");
  svg.setAttribute("height", "13");
  svg.setAttribute("aria-hidden", "true");
  const shape = document.createElementNS(ns, "polygon");
  shape.setAttribute("points", "1.8,0 12,6 1.8,12");
  shape.setAttribute("fill", "currentColor");
  svg.append(shape);
  setTriangle(svg, open);
  return svg;
}

function setTriangle(svg, open) {
  svg.firstChild.setAttribute("transform", open ? "rotate(90 6 6)" : "");
}

const dblpLabel = (url) => url.replace("https://dblp.org/pid/", "dblp:");
const orcidLabel = (url) => url.replace(/^https?:\/\/orcid.org\//, "");

function authorLinks(author) {
  return [
    link(author.dblp, dblpLabel(author.dblp)),
    author.orcid ? [" · ", link(author.orcid, "ORCID " + orcidLabel(author.orcid))] : null,
  ];
}

const plural = (n, word) => `${n.toLocaleString()} ${word}${n === 1 ? "" : "s"}`;

function debounce(fn, ms) {
  let timer;
  return (...args) => { clearTimeout(timer); timer = setTimeout(() => fn(...args), ms); };
}

// A text box that starts small and grows with its content, up to a limit (then it scrolls)
function autoGrow(box, maxLines = 12) {
  const resize = () => {
    box.style.height = "auto";
    const style = getComputedStyle(box);
    const line = parseFloat(style.lineHeight) || parseFloat(style.fontSize) * 1.4;
    const limit = line * maxLines + parseFloat(style.paddingTop) + parseFloat(style.paddingBottom);
    const wanted = box.scrollHeight + parseFloat(style.borderTopWidth) + parseFloat(style.borderBottomWidth);
    box.style.height = `${Math.min(wanted, limit)}px`;
    box.style.overflowY = wanted > limit ? "auto" : "hidden";
  };
  box.addEventListener("input", resize);
  return resize;
}

function splitList(text) {
  const seen = new Set();
  return text.split(/[;\n]/).map((s) => s.trim()).filter((s) => s && !seen.has(s) && seen.add(s));
}

// Enter in a text box presses its button; Shift+Enter inserts a line break.
function submitOnEnter(box, button) {
  box.addEventListener("keydown", (event) => {
    if (event.key !== "Enter" || event.shiftKey || event.isComposing) return;
    event.preventDefault();
    button.click();
  });
}

// ---------- API ----------

function showError(message) {
  const box = $("#error");
  box.replaceChildren(message ? el("div", { text: message }) : "");
  box.hidden = !message;
}

async function api(path, body) {
  let response;
  try {
    response = await fetch(path, body === undefined ? {} : {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
    });
  } catch (e) {
    throw new Error("Could not reach the server.");
  }
  let data = null;
  try { data = await response.json(); } catch (e) { /* not JSON */ }
  if (!response.ok) {
    let detail = data && data.detail;
    if (Array.isArray(detail)) detail = detail.map((d) => d.msg).join("; ");
    throw new Error(detail || `Request failed (${response.status})`);
  }
  return data;
}

// Run an async action with the button disabled and errors shown in the banner.
async function busy(button, action) {
  const label = button.textContent;
  button.disabled = true;
  showError("");
  try { return await action(); }
  catch (e) { showError(e.message); }
  finally { button.disabled = false; button.textContent = label; }
}

// ---------- state ----------

const state = {
  paperTypes: {},           // id -> name, from the server
  paperTypeHelp: {},        // id -> short explanation (tooltip)
  defaultExcludedTypes: [], // types that start unticked
  selected: new Map(),      // author id -> author
  conflicts: null,          // last /api/conflicts response
  expanded: new Set(),      // co-author ids with open paper lists
  sort: { key: "name", dir: 1 },
  reviewerQueries: [],
  reviewersChecked: false,
  hasAuthors: false,
  tab: "conflicts",         // "conflicts" | "reviewers"
};
let refreshCounter = 0;     // drop responses of outdated requests

// ---------- meta ----------

async function loadMeta() {
  try {
    const meta = await api("/api/meta");
    if (!meta.ready) {
      $("#snapshot-info").textContent = "No data loaded yet.";
      return;
    }
    state.paperTypes = meta.paper_types;
    state.paperTypeHelp = meta.paper_type_help || {};
    state.defaultExcludedTypes = meta.default_excluded_types || [];
    const info = $("#snapshot-info");
    const date = (meta.snapshot_time || "").slice(0, 10);
    info.textContent = `DBLP data from ${date} (${plural(meta.age_days, "day")} old) · ` +
      `${plural(meta.total_authors, "author")}, ${plural(meta.total_papers, "paper")}`;
    if (meta.age_days > 10) {
      info.classList.add("stale");
      info.textContent += " – the snapshot is outdated, consider refreshing it.";
    }
    const bad = Object.entries(meta.integrity || {}).filter(([key, n]) => n && key !== "duplicate_links_removed");
    if (bad.length) {
      info.classList.add("stale");
      info.textContent += ` – DATA WARNING: the snapshot lost records during parsing (${bad.map(([key, n]) => `${n} ${key.replaceAll("_", " ")}`).join(", ")}); regenerate it with the current ponder_dblp.`;
    }
    buildTypeBoxes();
  } catch (e) {
    $("#snapshot-info").textContent = "";
    showError(e.message);
  }
}

// The paper types as round tags that toggle: filled and ticked when included, outlined and faded when not. They have
// the colour of the same tag in the paper lists (see style.css). Each is still a real checkbox (invisible, on top).
function buildTypeBoxes() {
  $("#type-boxes").replaceChildren(...Object.entries(state.paperTypes).map(([id, name]) => {
    const input = el("input", { type: "checkbox", value: id, checked: !state.defaultExcludedTypes.includes(Number(id)) });
    const pill = el("label", { class: "tag type-pill", "data-type": id, title: state.paperTypeHelp[id] }, input, name);
    const sync = () => pill.classList.toggle("on", input.checked);
    input.addEventListener("change", () => { sync(); refresh(); });
    sync();
    return pill;
  }));
}

// ---------- step 1: authors ----------

$("#guess-btn").addEventListener("click", (event) => busy(event.target, async () => {
  const problem = $("#guess-error");
  problem.hidden = true;
  let authors;
  try {
    ({ authors } = await api("/api/guess", { source: $("#guess-source").value, html: $("#guess-html").value }));
    if (!authors.length) throw new Error("The page was recognised but contains no authors.");
  } catch (e) {
    problem.textContent = e.message;  // shown in the window itself, where the user is looking
    problem.hidden = false;
    return;
  }
  $("#author-input").value = authors.join("\n");
  growAuthors();
  closeGuessWindow();
  await searchAuthors(event.target);
}));

$("#author-search").addEventListener("click", (event) => searchAuthors(event.target));

function searchAuthors(button) {
  const queries = splitList($("#author-input").value);
  if (!queries.length) return showError("Enter at least one author name or DBLP URL.");
  return busy(button, async () => {
    const { results } = await api("/api/authors/search", { queries, matcher: $("#author-matcher").value });
    state.selected.clear();
    $("#author-results").replaceChildren(...results.map(renderGroup));
    updateSelection();
  });
}

function renderGroup(result) {
  const { query, authors, truncated } = result;
  const group = el("div", { class: "group" }, el("h3", {}, query,
    truncated ? el("span", { class: "badge truncated", text: "more matches exist – refine the search" }) : null));
  if (!authors.length) {
    group.append(el("p", { class: "muted" }, el("span", { class: "badge not_found", text: "No match" }),
      " Try accent-insensitive or fuzzy matching, or a DBLP URL."));
    return group;
  }
  const preselect = authors.length === 1 && !truncated;
  for (const author of authors) {
    if (preselect) state.selected.set(author.id, author);
    const box = el("input", {
      type: "checkbox", id: `cand-${author.id}`, checked: preselect,
      onchange: (event) => {
        if (event.target.checked) state.selected.set(author.id, author); else state.selected.delete(author.id);
        updateSelection();
      },
    });
    const recent = el("div");
    group.append(el("div", { class: "cand" }, box, el("div", {},
      el("label", { for: `cand-${author.id}` }, author.name),
      el("span", { class: "meta" }, " · ", authorLinks(author), ` · ${plural(author.paper_count, "paper")}`,
        author.latest_year ? `, latest ${author.latest_year}` : "", " · ",
        el("button", { type: "button", class: "link", onclick: (event) => toggleRecent(event.target, author, recent) },
          "recent papers")),
      recent)));
  }
  return group;
}

async function toggleRecent(button, author, container) {
  if (container.childNodes.length) return container.replaceChildren();
  try {
    const { papers } = await api(`/api/authors/${author.id}/recent?limit=3`);
    container.replaceChildren(el("ul", {}, papers.map((p) =>
      el("li", {}, `${p.year}: `, link(p.dblp, p.title)))));
  } catch (e) { showError(e.message); }
}

function updateSelection() {
  const names = [...state.selected.values()].map((a) => `${a.name} (${dblpLabel(a.dblp)})`);
  $("#selected-summary").textContent = names.length
    ? `Selected: ${names.join("; ")}`
    : "Tick the matching DBLP profiles to compute their conflicts.";
  refresh();
}

// ---------- tabs (steps 2 and 3) ----------

const TABS = ["conflicts", "reviewers"];

function setTabLabel(tab, number, text) {
  $(`#tab-${tab}`).replaceChildren(el("span", { class: "step", text: String(number) }), " " + text);
}

function applyTabs() {
  $("#tabs").hidden = !state.hasAuthors;
  $("#shared-filters").hidden = !state.hasAuthors;
  $("#right-empty").hidden = state.hasAuthors;
  for (const tab of TABS) {
    const active = tab === state.tab;
    $(`#step-${tab}`).hidden = !(state.hasAuthors && active);
    const button = $(`#tab-${tab}`);
    button.setAttribute("aria-selected", String(active));
    button.tabIndex = active ? 0 : -1;
  }
}

function showTab(tab, focus = false) {
  state.tab = tab;
  applyTabs();
  if (focus) $(`#tab-${tab}`).focus();
}

for (const tab of TABS) {
  $(`#tab-${tab}`).addEventListener("click", () => showTab(tab));
  $(`#tab-${tab}`).addEventListener("keydown", (event) => {
    const i = TABS.indexOf(tab);
    const target = { ArrowRight: TABS[(i + 1) % TABS.length], ArrowLeft: TABS[(i + TABS.length - 1) % TABS.length],
                     Home: TABS[0], End: TABS[TABS.length - 1] }[event.key];
    if (target) { event.preventDefault(); showTab(target, true); }
  });
}

// ---------- step 2: conflicts ----------

function filterParams() {
  return {
    author_ids: [...state.selected.keys()],
    years_back: $("#any-year").checked ? null : Number($("#years").value),
    exclude_self: $("#exclude-self").checked,
    exclude_types: [...document.querySelectorAll("#type-boxes input")].filter((b) => !b.checked).map((b) => Number(b.value)),
  };
}

function updateYearsLabel() {
  const anyYear = $("#any-year").checked;
  $("#years").disabled = anyYear;
  const years = Number($("#years").value);
  $("#years-label").textContent = anyYear ? "all years"
    : `${plural(years, "year")} (since ${new Date().getFullYear() - years})`;
}

const refresh = debounce(async () => {
  updateYearsLabel();
  const ticket = ++refreshCounter;
  const hasAuthors = state.selected.size > 0;
  state.hasAuthors = hasAuthors;
  applyTabs();
  if (!hasAuthors) { state.conflicts = null; return; }
  try {
    const params = filterParams();
    const conflicts = await api("/api/conflicts", params);
    if (ticket !== refreshCounter) return;
    state.conflicts = conflicts;
    renderConflicts();
    if (state.reviewersChecked) await runReviewerCheck(ticket);
  } catch (e) { showError(e.message); }
}, 250);

function conflictText(entry) {
  return [entry.name, entry.dblp, entry.orcid, ...entry.papers.map((p) => p.title)].join("\n").toLowerCase();
}

function authorShortName(id) {
  const a = state.selected.get(id);
  return a ? a.name : String(id);
}

const COLUMNS = [
  { key: "name", label: "Co-author", value: (e) => e.name.toLowerCase() },
  { key: "orcid", label: "Links", value: (e) => e.dblp },
  { key: "paper_count", label: "Papers", num: true, value: (e) => e.paper_count },
  { key: "latest_year", label: "Latest", num: true, value: (e) => e.latest_year },
];

function renderConflicts() {
  if (!state.conflicts) return;
  const needle = $("#conflict-filter").value.trim().toLowerCase();
  const multi = state.selected.size > 1;
  const columns = COLUMNS;
  const col = COLUMNS.find((c) => c.key === state.sort.key) || COLUMNS[0];
  const rows = state.conflicts.coauthors.filter((e) => !needle || conflictText(e).includes(needle));
  rows.sort((a, b) => {
    const [x, y] = [col.value(a), col.value(b)];
    return (x < y ? -1 : x > y ? 1 : 0) * state.sort.dir || a.name.localeCompare(b.name);
  });

  setTabLabel("conflicts", 2, `Conflicts (${state.conflicts.coauthors.length.toLocaleString()})`);
  const papers = rows.reduce((n, e) => n + e.paper_count, 0);
  const since = state.conflicts.since_year;
  $("#conflict-summary").textContent =
    `${plural(rows.length, "co-author")}` +
    (needle ? ` of ${state.conflicts.coauthors.length} match the filter` : "") +
    `, ${plural(papers, "paper")}` + (since ? ` since ${since}` : " (all years)") + "." +
    (state.conflicts.truncated ? " Only the first 5000 co-authors are shown; narrow the filters." : "");

  const head = el("tr", {}, el("th"), columns.map((c) =>
    el("th", { scope: "col", "aria-sort": c.key === state.sort.key ? (state.sort.dir > 0 ? "ascending" : "descending") : "none",
               class: c.num ? "num" : null },
      el("button", { type: "button", onclick: () => {
        state.sort = { key: c.key, dir: state.sort.key === c.key ? -state.sort.dir : 1 };
        renderConflicts();
      } }, c.label))));
  const body = rows.flatMap((e) => conflictRows(e, columns.length + 1, multi));
  $("#conflict-table").replaceChildren(el("thead", {}, head), el("tbody", {}, body));
}

// Two <tr>s per co-author: the summary row and the (optional) paper list. Shared with the reviewer check.
function conflictRows(entry, colspan, showVia) {
  const open = state.expanded.has(entry.id);
  const detail = el("tr", { class: "detail", hidden: !open }, el("td", { colspan }, paperList(entry.papers)));
  const arrow = triangle(open);
  const toggle = el("button", {
    type: "button", class: "toggle", "aria-expanded": String(open), "aria-label": `Papers shared with ${entry.name}`,
    onclick: () => {
      const nowOpen = detail.hidden;
      detail.hidden = !nowOpen;
      toggle.setAttribute("aria-expanded", String(nowOpen));
      setTriangle(arrow, nowOpen);
      if (nowOpen) state.expanded.add(entry.id); else state.expanded.delete(entry.id);
    },
  }, arrow);
  const main = el("tr", {},
    el("td", {}, toggle),
    el("td", {}, entry.name,
      showVia && entry.via.length ? el("div", { class: "muted small-text", text: "with " + entry.via.map(authorShortName).join(", ") }) : null),
    el("td", { class: "links" }, link(entry.dblp, dblpLabel(entry.dblp)),
      entry.orcid ? el("div", {}, link(entry.orcid, "ORCID " + orcidLabel(entry.orcid))) : null),
    el("td", { class: "num", text: String(entry.paper_count) }),
    el("td", { class: "num", text: String(entry.latest_year) }));
  return [main, detail];
}

function paperList(papers) {
  return el("ul", {}, papers.map((p) =>
    el("li", {}, `${p.year} `, link(p.dblp, p.title), el("span", { class: "tag", "data-type": p.type_id, text: p.type }))));
}

$("#years").addEventListener("input", refresh);
$("#any-year").addEventListener("change", refresh);
$("#exclude-self").addEventListener("change", refresh);
$("#conflict-filter").addEventListener("input", debounce(renderConflicts, 150));
$("#expand-all").addEventListener("click", () => {
  (state.conflicts?.coauthors ?? []).forEach((e) => state.expanded.add(e.id));
  renderConflicts();
});
$("#collapse-all").addEventListener("click", () => { state.expanded.clear(); renderConflicts(); });

// ---------- step 3: reviewers ----------

$("#reviewer-check").addEventListener("click", (event) => {
  state.reviewerQueries = splitList($("#reviewer-input").value);
  if (!state.reviewerQueries.length) return showError("Enter at least one reviewer.");
  state.reviewersChecked = true;
  return busy(event.target, () => runReviewerCheck());
});

const STATUS_LABEL = {
  conflict: "Conflict", no_conflict: "No conflict found", ambiguous: "Several profiles match", not_found: "Not found in DBLP",
};

async function runReviewerCheck(ticket = refreshCounter) {
  const { reviewers } = await api("/api/reviewers/check", {
    ...filterParams(), reviewers: state.reviewerQueries, matcher: $("#reviewer-matcher").value,
  });
  if (ticket !== refreshCounter) return;
  const count = (status) => reviewers.filter((r) => r.status === status).length;
  setTabLabel("reviewers", 3, `Check reviewers (${count("conflict")} of ${reviewers.length} in conflict)`);
  $("#reviewer-summary").textContent =
    `${count("conflict")} conflict, ${count("no_conflict")} no conflict found, ` +
    `${count("ambiguous")} ambiguous, ${count("not_found")} not found.`;
  $("#reviewer-results").replaceChildren(el("table", {},
    el("thead", {}, el("tr", {}, ["Reviewer", "Result"].map((h) => el("th", { scope: "col", text: h })))),
    el("tbody", {}, reviewers.map((r, index) => reviewerRow(r, index)))));
}

function reviewerRow(result, index) {
  const badge = el("span", { class: `badge ${result.status}`, text: STATUS_LABEL[result.status] });
  let details = [];
  if (result.status === "conflict") {
    details = [candidateLine(result.candidates[0], false)];
  } else if (result.status === "no_conflict") {
    details = [el("span", { class: "muted" }, result.candidates[0].name, " (", authorLinks(result.candidates[0]), ")")];
  } else if (result.status === "ambiguous") {
    const note = result.truncated ? "Too many matches – refine the name or use a DBLP URL." : "Pick the right profile:";
    details = [el("div", { class: "muted", text: note }),
      el("ul", { class: "cands" }, result.candidates.map((c) => el("li", {}, candidateLine(c, true, index))))];
  }
  return el("tr", {}, el("td", { text: result.query }), el("td", {}, badge, el("div", {}, details)));
}

function candidateLine(candidate, withChoice, index) {
  const status = candidate.conflict
    ? el("span", { class: "badge conflict", text: "Conflict" })
    : el("span", { class: "badge no_conflict", text: "No conflict found" });
  const line = el("div", {}, withChoice ? [status, " "] : null,
    `${candidate.name} (`, authorLinks(candidate), `) · ${plural(candidate.paper_count, "paper")}`);
  if (withChoice) {
    line.append(" · ", el("button", { type: "button", class: "link", onclick: () => {
      state.reviewerQueries[index] = candidate.dblp;
      $("#reviewer-input").value = state.reviewerQueries.join("\n");
      growReviewers();
      busy($("#reviewer-check"), () => runReviewerCheck());
    } }, "use this profile"));
  }
  if (candidate.conflict) {
    const c = candidate.conflict;
    const via = c.via.length ? ` with ${c.via.map(authorShortName).join(", ")}` : "";
    line.append(el("details", {},
      el("summary", { class: "muted", text: `${plural(c.paper_count, "shared paper")}${via}, latest ${c.latest_year}` }),
      paperList(c.papers)));
  }
  return line;
}

// ---------- data updates (the server refreshes its data in the background) ----------

const update = { timer: null, wasRunning: false, flash: "", flashUntil: 0 };

const formatDate = (value) => {
  const date = new Date(typeof value === "number" ? value * 1000 : value);
  return isNaN(date) ? "?" : date.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
};

function describeUpdate(status) {
  const job = status.job;
  if (job && job.state === "running") {
    const what = status.ready ? "Updating the data" : "Importing the data for the first time";
    return `${what} (${job.phase}${job.detail ? ": " + job.detail : ""})`;
  }
  if (Date.now() < update.flashUntil) return update.flash;
  if (job && job.state === "failed" && status.last_error) {
    return `The last update failed: ${status.last_error.message}`;
  }
  if (!status.ready) return "There is no data yet.";
  let text;
  if (status.remote && status.remote.update_available) {
    text = `A newer DBLP dump is available (${formatDate(status.remote.last_modified)}).`;
  } else if (status.remote) {
    text = `Up to date with dblp.org (checked ${formatDate(status.remote.checked_at)}).`;
  } else {
    text = "Not checked for updates yet.";
  }
  if (status.next_check_at) text += ` Next automatic check: ${formatDate(status.next_check_at)}.`;
  return text;
}

function renderUpdate(status) {
  $("#update-panel").hidden = !status.enabled;
  if (!status.enabled) return;
  const job = status.job;
  const running = job && job.state === "running";
  $("#update-status").textContent = describeUpdate(status);
  $("#update-status").classList.toggle("failed", !running && job && job.state === "failed" && !!status.last_error);
  const button = $("#update-btn");
  button.disabled = running;
  button.textContent = running ? "Updating…" : (status.remote && status.remote.update_available ? "Update now" : "Check for updates");
  const progress = $("#update-progress");
  progress.hidden = !running;
  if (running) progress.value = job.progress;
  const lines = job ? job.log : [];
  $("#update-log-btn").hidden = !lines.length;
  setUpdateLog(lines);
}

function setUpdateLog(lines) {
  const pre = $("#update-log");
  const atEnd = pre.scrollHeight - pre.scrollTop - pre.clientHeight < 20;
  pre.textContent = lines.join(String.fromCharCode(10));
  if (atEnd) pre.scrollTop = pre.scrollHeight;  // follows a running update, unless the reader scrolled up
}

// Floating windows (popovers): opened by a button, placed under it; Esc, a click outside or the cross closes them.
// Returns a function that closes the window.
function setupFloatingWindow(win, openButton, closeButton, onOpen = () => {}) {
  const place = () => {
    const anchor = openButton.getBoundingClientRect();
    const width = win.offsetWidth || Math.min(window.innerWidth - 16, 900);
    win.style.top = `${anchor.bottom + 6}px`;
    win.style.left = `${Math.max(8, Math.min(anchor.left, window.innerWidth - width - 8))}px`;
  };
  if ("popover" in HTMLElement.prototype) {
    win.addEventListener("beforetoggle", (event) => { if (event.newState === "open") place(); });
    win.addEventListener("toggle", (event) => {
      if (event.newState !== "open") return;
      place();  // now that it is visible its real width is known
      onOpen();
    });
    return () => win.hidePopover();
  }
  // browsers without the popover API: the same window, opened and closed by the buttons
  win.removeAttribute("popover");
  win.hidden = true;
  for (const button of [openButton, closeButton]) button.removeAttribute("popovertarget");
  openButton.addEventListener("click", () => { win.hidden = !win.hidden; if (!win.hidden) { place(); onOpen(); } });
  closeButton.addEventListener("click", () => { win.hidden = true; });
  return () => { win.hidden = true; };
}

$("#update-log-copy").addEventListener("click", async (event) => {
  const button = event.target;
  try { await navigator.clipboard.writeText($("#update-log").textContent); button.textContent = "Copied"; }
  catch (e) { button.textContent = "Copy failed"; }  // clipboard access needs https or localhost
  setTimeout(() => { button.textContent = "Copy"; }, 1500);
});

async function pollUpdate() {
  clearTimeout(update.timer);
  let delay = 30000;
  try {
    const status = await api("/api/refresh");
    renderUpdate(status);
    const running = !!status.job && status.job.state === "running";
    if (update.wasRunning && !running && status.job && status.job.state === "succeeded") {
      update.flash = "The data was updated; new searches use it.";
      update.flashUntil = Date.now() + 20000;
      renderUpdate(status);
      loadMeta();
    }
    update.wasRunning = running;
    if (running) delay = 2000;
  } catch (e) { /* the server is busy or restarting: try again later */ }
  update.timer = setTimeout(pollUpdate, delay);
}

$("#update-btn").addEventListener("click", async (event) => {
  event.target.disabled = true;
  try {
    const answer = await api("/api/refresh", {});
    const reasons = {
      up_to_date: "Already up to date.",
      already_running: "An update is already running.",
      recent_failure: `The last attempt failed; try again after ${answer.retry_at ? new Date(answer.retry_at * 1000).toLocaleTimeString() : "a while"}.`,
      check_failed: `Could not ask dblp.org: ${answer.message || "no answer"}`,
    };
    update.flash = reasons[answer.reason] || "";
    update.flashUntil = answer.started ? 0 : Date.now() + 10000;
  } catch (e) {
    showError(e.message);
  }
  pollUpdate();
});

// ---------- init ----------

const growAuthors = autoGrow($("#author-input"));
const growReviewers = autoGrow($("#reviewer-input"));
const closeGuessWindow = setupFloatingWindow($("#guess-window"), $("#guess-open"), $("#guess-close"), () => $("#guess-html").focus());
setupFloatingWindow($("#update-log-window"), $("#update-log-btn"), $("#update-log-close"), () => {
  $("#update-log").scrollTop = $("#update-log").scrollHeight;
});
submitOnEnter($("#author-input"), $("#author-search"));
submitOnEnter($("#reviewer-input"), $("#reviewer-check"));
submitOnEnter($("#guess-html"), $("#guess-btn"));
applyTabs();
updateYearsLabel();
loadMeta();
pollUpdate();
