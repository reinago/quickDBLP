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

function buildTypeBoxes() {
  $("#type-boxes").replaceChildren(...Object.entries(state.paperTypes).map(([id, name]) =>
    el("label", { title: state.paperTypeHelp[id] },
      el("input", { type: "checkbox", value: id, checked: !state.defaultExcludedTypes.includes(Number(id)), onchange: refresh }),
      " " + name)));
}

// ---------- step 1: authors ----------

$("#guess-btn").addEventListener("click", (event) => busy(event.target, async () => {
  const { authors } = await api("/api/guess", { source: $("#guess-source").value, html: $("#guess-html").value });
  if (!authors.length) throw new Error("The page was recognised but contains no authors.");
  $("#author-input").value = authors.join("\n");
  $("#guess-box").open = false;
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
    `, ${plural(papers, "shared paper")}` + (since ? ` since ${since}` : " (all years)") + "." +
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
    el("li", {}, `${p.year} `, link(p.dblp, p.title), el("span", { class: "tag", text: p.type }))));
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

// ---------- init ----------

submitOnEnter($("#author-input"), $("#author-search"));
submitOnEnter($("#reviewer-input"), $("#reviewer-check"));
submitOnEnter($("#guess-html"), $("#guess-btn"));
applyTabs();
updateYearsLabel();
loadMeta();
