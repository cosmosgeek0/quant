"use strict";

/* QUANT owner room — core engine, built from zero 2026-08-14.
   Owns: poll loop, owner-language truth, cockpit strip, nav, page
   band, keyed tape engine, shared components, command palette,
   appearance prefs, clock. Tabs register window.QuantTabs.<name> =
   {needs, init, render} and build from window.QuantUI only. The
   browser computes no money value; absence renders as a calm em dash
   with its reason — never zero, never a sample. Motion signals
   arrival, change, or state; money digits never tween. */

var API = {
  strip: "/api/strip", health: "/api/health", position: "/api/position",
  momentum: "/api/momentum", failures: "/api/failures",
  stream: "/api/stream", candidates: "/api/candidates",
  decisions: "/api/decisions", pnl: "/api/pnl", fees: "/api/fees",
  events: "/api/events"
};
var REFRESH_MS = 2000;
var TABS = ["overview", "basis", "volume", "quant", "journal"];
var PAGE_ICONS = { overview: "gauge", basis: "scale",
  volume: "chart-line", quant: "activity", journal: "file-text" };
var NAV_SECTIONS = [
  { name: "Overview", tabs: ["overview"] },
  { name: "Engine", tabs: ["basis", "volume"] },
  { name: "Control", tabs: ["quant"] },
  { name: "Records", tabs: ["journal"] }
];

/* ---------- pure helpers (exported for offline tests) ---------- */

function fmtNum(raw, maxDp) {
  if (raw === null || raw === undefined || raw === "") return null;
  var value = Number(raw);
  if (!Number.isFinite(value)) return null;
  var text = value.toFixed(maxDp === undefined ? 6 : maxDp);
  if (text.indexOf(".") >= 0) {
    text = text.replace(/0+$/, "").replace(/\.$/, "");
  }
  return text.replace("-", "−");
}
function fmtPct(raw) {
  var text = fmtNum(raw, 3);
  return text === null ? null : text + "%";
}
function fmtUsdt(raw) {
  var text = fmtNum(raw, 4);
  return text === null ? null : text + " USDT";
}
function ageText(seconds) {
  if (seconds === null || seconds === undefined ||
      !Number.isFinite(Number(seconds))) return "age unknown";
  var s = Math.max(0, Number(seconds));
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return (s / 60).toFixed(1) + "m ago";
  return (s / 3600).toFixed(1) + "h ago";
}
function shortTime(iso) {
  if (typeof iso !== "string" || iso.length < 19) return "";
  return iso.slice(11, 19) + "Z";
}
function shortDay(iso) {
  if (typeof iso !== "string" || iso.length < 10) return "";
  return iso.slice(0, 10);
}
function ownerStopLine(stopPresent, stopJobsPresent) {
  if (stopPresent) return "Paused — exposure management remains protected";
  if (stopJobsPresent) return "Watching only — new entries paused";
  return "";
}
function ownerLockLine(lockPresent) {
  return lockPresent ? "Another entry currently owns the account"
    : "Entry lock clear";
}
/* Contract §2 precedence. Evaluating is never inferred from a healthy
   feed; Authority is never inferred from machine flags ("Unavailable"
   until the operator contract exists). */
function executionPhase(facts) {
  var states = (facts.positionStates || []).map(function (s) {
    return String(s || "").toUpperCase();
  });
  var i;
  for (i = 0; i < states.length; i++) {
    if (states[i].indexOf("UNKNOWN") >= 0 ||
        states[i].indexOf("ONE_LEG") >= 0 ||
        states[i].indexOf("FROZEN") >= 0) return "Attention";
  }
  if (facts.reconciling) return "Reconciling";
  for (i = 0; i < states.length; i++) {
    if (states[i].indexOf("CLOS") >= 0) return "Exiting";
  }
  for (i = 0; i < states.length; i++) {
    if (states[i].indexOf("OPENING") >= 0) return "Entering";
  }
  for (i = 0; i < states.length; i++) {
    if (states[i] === "OPEN" || states[i].indexOf("HOLD") >= 0) {
      return "Holding";
    }
  }
  if (facts.runtimeFresh) return "Watching";
  return "Offline";
}
function ownerPhaseForState(raw) {
  var st = String(raw || "").toUpperCase();
  if (st.indexOf("UNKNOWN") >= 0 || st.indexOf("ONE_LEG") >= 0 ||
      st.indexOf("FROZEN") >= 0) return "Attention";
  if (st.indexOf("CLOS") >= 0) return "Exiting";
  if (st.indexOf("OPENING") >= 0) return "Entering";
  if (st === "OPEN" || st.indexOf("HOLD") >= 0) return "Holding";
  return raw ? String(raw) : "Unknown";
}

/* ---------- state + dom ---------- */

var state = { data: {}, lastGoodAt: null, connected: true,
  tab: "overview", eventsLoaded: false };

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}
function ic(name, cls) {
  var node = el("span", "ic" + (cls ? " " + cls : ""));
  node.style.setProperty("--ic", "url(vendor/icons/" + name + ".svg)");
  return node;
}
function clearTo(id) {
  var root = document.getElementById(id);
  root.textContent = "";
  return root;
}

/* ---------- prefs (appearance; browser-side only) ---------- */

var ACCENTS = {
  red: ["#e5484d", "229,72,77", "#b42318"],
  orange: ["#f2760c", "242,118,12", "#b45309"],
  yellow: ["#eab308", "234,179,8", "#8a6d00"],
  green: ["#12a150", "18,161,80", "#0a7c3d"],
  blue: ["#2f6bff", "47,107,255", "#1d4ed8"],
  indigo: ["#5b5bef", "91,91,239", "#4338ca"],
  violet: ["#9333ea", "147,51,234", "#7c22ce"]
};
function getPrefs() {
  var defaults = { theme: "light", accent: "blue", zoom: 1,
    clockZone: "UTC", motion: "on" };
  try {
    var raw = window.localStorage.getItem("quant-ui-prefs");
    if (!raw) return defaults;
    var got = JSON.parse(raw);
    Object.keys(defaults).forEach(function (key) {
      if (got[key] === undefined) got[key] = defaults[key];
    });
    return got;
  } catch (err) { return defaults; }
}
function setPrefs(next) {
  try {
    window.localStorage.setItem("quant-ui-prefs", JSON.stringify(next));
  } catch (err) { /* prefs stay session-local */ }
  applyPrefs();
}
function applyPrefs() {
  var prefs = getPrefs();
  var rootEl = document.documentElement;
  rootEl.dataset.theme = prefs.theme === "dark" ? "dark" : "light";
  rootEl.dataset.motion = prefs.motion === "off" ? "off" : "on";
  rootEl.style.fontSize = (prefs.zoom * 100) + "%";
  var accent = ACCENTS[prefs.accent] || ACCENTS.blue;
  rootEl.style.setProperty("--accent", accent[0]);
  rootEl.style.setProperty("--accent-ink", accent[2]);
  rootEl.style.setProperty("--accent-soft",
    "rgba(" + accent[1] + ", 0.13)");
  rootEl.style.setProperty("--accent-line",
    "rgba(" + accent[1] + ", 0.42)");
}

/* ---------- shared components ---------- */

function tile(opts) {
  var box = el("div", "tile");
  box.appendChild(el("div", "tile-l", opts.label));
  var absent = opts.absent === true || opts.value === null ||
    opts.value === undefined || opts.value === "";
  var v = el("div", "tile-v" +
    (absent ? " absent" : (opts.tone ? " " + opts.tone : "")),
    absent ? "—" : opts.value);
  box.appendChild(v);
  box.appendChild(el("div", "tile-s",
    absent ? (opts.absentWhy || "") : (opts.sub || "")));
  if (opts.title) box.title = opts.title;
  return box;
}
function tiles(list) {
  var row = el("div", "tiles band");
  list.forEach(function (t) { row.appendChild(t); });
  return row;
}
function chip(text, tone, sub) {
  var node = el("span", "chip" + (tone ? " " + tone : ""));
  node.appendChild(el("span", null, text));
  if (sub) node.appendChild(el("span", "chip-sub", sub));
  return node;
}
function sevMark(level) {
  var node = el("span", "sev " + (level || ""));
  node.appendChild(el("i")); node.appendChild(el("i"));
  node.appendChild(el("i"));
  return node;
}
function c2(primary, sub, opts) {
  var o = opts || {};
  var box = el("div", "c2" + (o.num ? " num" : ""));
  var p = el("div", "c2p" + (o.tone ? " " + o.tone : ""));
  if (o.unit && primary !== "—") {
    p.appendChild(document.createTextNode(primary));
    p.appendChild(el("span", "unit", o.unit));
  } else { p.textContent = primary; }
  box.appendChild(p);
  if (sub) box.appendChild(el("div", "c2s", sub));
  return box;
}
function band(title, meta) {
  var box = el("section", "band");
  var head = el("div", "band-head");
  head.appendChild(el("span", "band-title", title));
  if (meta) head.appendChild(el("span", "band-meta", meta));
  box.appendChild(head);
  return box;
}
function stateBlock(kind, title, detail) {
  var box = el("div", "stateblock");
  var inner = el("div");
  inner.appendChild(el("div", "sb-t" + (kind === "error" ? " bad" : ""),
    title));
  if (detail) inner.appendChild(el("div", "sb-d", detail));
  box.appendChild(inner);
  return box;
}
function facts(pairs) {
  var grid = el("div", "facts");
  pairs.forEach(function (pair) {
    var row = el("div", "fact");
    row.appendChild(el("div", "fl", pair[0]));
    row.appendChild(el("div", "fv", pair[1]));
    grid.appendChild(row);
  });
  return grid;
}
function feedRow(evt) {
  var row = el("div", "feedrow" +
    (typeof evt.onExpand === "function" ? " click" : ""));
  var t = el("span", "fr-t" + (evt.timeText ? "" : " absent"),
    evt.timeText || "—");
  if (!evt.timeText) t.title = "time unrecorded";
  row.appendChild(t);
  row.appendChild(el("span", "fr-mark " + (evt.tone || "")));
  var e = el("span", "fr-e");
  e.appendChild(el("b", null, evt.title));
  if (evt.caption) {
    e.appendChild(el("span", null, " — " + evt.caption));
  }
  row.appendChild(e);
  row.appendChild(el("span", "fr-sym", evt.symbol || ""));
  var chev = el("span", "fr-ch");
  if (typeof evt.onExpand === "function") {
    chev.appendChild(ic("chevron-down"));
    chev.firstChild.classList.add("fr-ch");
  }
  row.appendChild(chev);
  if (typeof evt.onExpand === "function") {
    var open = null;
    row.addEventListener("click", function () {
      if (open) { open.remove(); open = null;
        row.classList.remove("open"); return; }
      open = el("div", "feedx settle");
      open.appendChild(evt.onExpand());
      row.classList.add("open");
      row.after(open);
    });
  }
  return row;
}
function groupHead(dateText, summaryText) {
  var head = el("div", "grouphead");
  head.appendChild(el("span", "gh-d", dateText));
  head.appendChild(el("span", "gh-s", summaryText));
  return head;
}
function pillBar(items, activeKey, onSelect) {
  var bar = el("div", "pills band");
  items.forEach(function (item) {
    var p = el("button", "pill" + (item.key === activeKey ? " on" : ""),
      item.label);
    p.type = "button";
    if (item.count !== undefined && item.count !== null) {
      p.appendChild(el("span", "n", String(item.count)));
    }
    p.addEventListener("click", function () { onSelect(item.key); });
    bar.appendChild(p);
  });
  return bar;
}
function dayStrip(days, activeIso, onSelect) {
  var strip = el("div", "daystrip band");
  days.forEach(function (day) {
    var card = el("button", "daycard" +
      (day.iso === activeIso ? " on" : "") +
      (day.isToday ? " today" : ""));
    card.type = "button";
    card.appendChild(el("div", "dc-day", day.label));
    var r2 = el("div", "dc-row");
    r2.appendChild(el("span", "l", "Events"));
    r2.appendChild(el("span", "v",
      day.total === null ? "—" : String(day.total)));
    card.appendChild(r2);
    var r3 = el("div", "dc-row");
    if (day.iso === activeIso) {
      r3.appendChild(el("span", "l", "Current page"));
      if (day.counts) {
        var cc = el("span", "dc-counts");
        cc.appendChild(el("span", "cc", "C " + day.counts.c));
        cc.appendChild(el("span", "cw", "W " + day.counts.w));
        cc.appendChild(el("span", "ci", "I " + day.counts.i));
        cc.appendChild(el("span", "cd", "D " + day.counts.d));
        r3.appendChild(cc);
      }
    } else {
      r3.appendChild(el("span", "l", "Select day"));
      r3.appendChild(el("span", "v up", String(day.total || 0)));
    }
    card.appendChild(r3);
    card.addEventListener("click", function () { onSelect(day.iso); });
    strip.appendChild(card);
  });
  return strip;
}
/* Live trail: pinned now-line + keyed history; new keys arrive with a
   slide exactly once. */
function liveTrail(container) {
  var seen = new Set();
  var now = el("div", "now");
  var lamp = el("span", "now-lamp");
  lamp.appendChild(el("span", "glow"));
  now.appendChild(lamp);
  var text = el("div");
  var phase = el("div", "now-phase", "");
  var detail = el("div", "now-detail", "");
  text.appendChild(phase); text.appendChild(detail);
  now.appendChild(text);
  container.appendChild(now);
  var list = el("div");
  container.appendChild(list);
  var rendered = new Map();
  return {
    update: function (nowState, events) {
      lamp.className = "now-lamp " + (nowState.lamp || "off") +
        (nowState.breathing === "calm" ? " breathe-calm" :
          (nowState.breathing === "urgent" ? " breathe-urgent" : ""));
      if (phase.textContent !== nowState.phase) {
        phase.textContent = nowState.phase;
      }
      if (detail.textContent !== nowState.detail) {
        detail.textContent = nowState.detail;
      }
      var keys = new Set();
      var prev = null;
      events.slice(0, 40).forEach(function (evt) {
        keys.add(evt.key);
        var row = rendered.get(evt.key);
        if (!row) {
          row = feedRow(evt);
          if (!seen.has(evt.key) && seen.size > 0) {
            row.classList.add("arrive");
          }
          seen.add(evt.key);
          rendered.set(evt.key, row);
          if (prev) prev.after(row); else list.prepend(row);
        }
        prev = row;
      });
      rendered.forEach(function (row, key) {
        if (!keys.has(key)) { row.remove(); rendered.delete(key); }
      });
      if (seen.size === 0 && events.length === 0) seen.add("__primed");
    }
  };
}

/* ---------- keyed tape engine (no-flicker tables) ---------- */

function Tape(options) {
  this.table = options.table;
  this.tbody = this.table.querySelector("tbody");
  this.columns = options.columns;
  this.expandRender = options.expandRender;
  this.sortKey = options.sortKey;
  this.sortAsc = false;
  this.rows = new Map();
  this.order = [];
  this.expanded = new Set();
  this.hovering = false;
  var self = this;
  this.tbody.addEventListener("mouseenter",
    function () { self.hovering = true; }, true);
  this.tbody.addEventListener("mouseleave",
    function () { self.hovering = false; }, true);
  var thead = this.table.querySelector("thead");
  thead.addEventListener("click", function (ev) {
    var th = ev.target.closest("th[data-sort]");
    if (!th) return;
    var key = th.getAttribute("data-sort");
    if (self.sortKey === key) self.sortAsc = !self.sortAsc;
    else { self.sortKey = key; self.sortAsc = false; }
    thead.querySelectorAll("th").forEach(function (node) {
      node.classList.toggle("sorted", node === th);
      node.classList.toggle("asc", node === th && self.sortAsc);
    });
    self.apply(self.computeOrder(self.lastRows || []));
  });
}
Tape.prototype.computeOrder = function (rows) {
  var key = this.sortKey, asc = this.sortAsc;
  return rows.slice().sort(function (a, b) {
    var av = a[key], bv = b[key];
    var an = av === null || av === undefined;
    var bn = bv === null || bv === undefined;
    if (an && bn) return a.key < b.key ? -1 : 1;
    if (an) return 1;
    if (bn) return -1;
    var aN = Number(av), bN = Number(bv), cmp;
    if (Number.isFinite(aN) && Number.isFinite(bN)) cmp = aN - bN;
    else cmp = String(av) < String(bv) ? -1 :
      (String(av) > String(bv) ? 1 : 0);
    if (cmp === 0) cmp = a.key < b.key ? -1 : 1;
    return asc ? cmp : -cmp;
  }).map(function (row) { return row.key; });
};
Tape.prototype.apply = function (orderKeys) {
  var self = this;
  this.order = orderKeys;
  orderKeys.forEach(function (key) {
    var entry = self.rows.get(key);
    if (!entry) return;
    self.tbody.appendChild(entry.tr);
    if (entry.xtr) self.tbody.appendChild(entry.xtr);
  });
};
Tape.prototype.update = function (rows) {
  var self = this;
  this.lastRows = rows;
  var seen = new Set();
  rows.forEach(function (row) {
    seen.add(row.key);
    var entry = self.rows.get(row.key);
    if (!entry) {
      var tr = el("tr", "rowbtn enter");
      var cells = {};
      self.columns.forEach(function (col) {
        var td = el("td", col.cls || "");
        cells[col.key] = td;
        tr.appendChild(td);
      });
      tr.addEventListener("click", function () { self.toggle(row.key); });
      entry = { tr: tr, cells: cells, xtr: null, lastX: "" };
      self.rows.set(row.key, entry);
      self.tbody.appendChild(tr);
    }
    entry.row = row;
    self.columns.forEach(function (col) {
      var out = col.render(row);
      var td = entry.cells[col.key];
      if (out.node) {
        var html = out.node.outerHTML;
        if (entry["h" + col.key] !== html) {
          entry["h" + col.key] = html;
          td.textContent = "";
          td.appendChild(out.node);
        }
      } else if (td.textContent !== out.text) {
        var had = td.textContent !== "";
        td.textContent = out.text;
        if (had) {
          td.classList.remove("flash", "flash-up", "flash-down");
          void td.offsetWidth;
          td.classList.add(out.dir === "up" ? "flash-up" :
            (out.dir === "down" ? "flash-down" : "flash"));
        }
      }
      td.className = (col.cls || "") + (out.cls ? " " + out.cls : "");
    });
    if (self.expanded.has(row.key)) self.renderX(entry);
  });
  Array.from(this.rows.keys()).forEach(function (key) {
    if (seen.has(key)) return;
    var entry = self.rows.get(key);
    entry.tr.remove();
    if (entry.xtr) entry.xtr.remove();
    self.rows.delete(key);
    self.expanded.delete(key);
  });
  var next = this.computeOrder(rows);
  if (this.hovering || this.expanded.size > 0) {
    var current = this.order.filter(function (key) {
      return seen.has(key);
    });
    next.forEach(function (key) {
      if (current.indexOf(key) < 0) current.push(key);
    });
    this.apply(current);
  } else {
    this.apply(next);
  }
};
Tape.prototype.toggle = function (key) {
  var entry = this.rows.get(key);
  if (!entry) return;
  if (this.expanded.has(key)) {
    this.expanded.delete(key);
    if (entry.xtr) { entry.xtr.remove(); entry.xtr = null; }
    entry.lastX = "";
    return;
  }
  this.expanded.add(key);
  this.renderX(entry);
};
Tape.prototype.renderX = function (entry) {
  var json = JSON.stringify(entry.row);
  if (entry.xtr && entry.lastX === json) return;
  var body = this.expandRender(entry.row);
  if (!entry.xtr) {
    entry.xtr = el("tr", "exprow settle");
    var td = el("td");
    td.colSpan = this.columns.length;
    entry.xtr.appendChild(td);
    entry.tr.after(entry.xtr);
  }
  entry.lastX = json;
  entry.xtr.firstChild.textContent = "";
  entry.xtr.firstChild.appendChild(body);
};
function tapeTable(minWidth, columns) {
  var wrap = el("div", "tapewrap band");
  var table = el("table", "tape");
  if (minWidth) table.style.minWidth = minWidth + "px";
  var thead = el("thead");
  var tr = el("tr");
  columns.forEach(function (col) {
    var th = el("th", col.cls || "", col.label);
    if (col.sort) {
      th.setAttribute("data-sort", col.sort);
      th.appendChild(el("span", "sortic"));
    }
    tr.appendChild(th);
  });
  thead.appendChild(tr);
  table.appendChild(thead);
  table.appendChild(el("tbody"));
  wrap.appendChild(table);
  return { wrap: wrap, table: table };
}

/* ---------- shared data derivations ---------- */

function collectPositionStates() {
  var out = [];
  var pos = state.data.position;
  if (pos && pos.position && pos.position.state) {
    out.push(pos.position.state);
  }
  if (pos && pos.supervision && pos.supervision.status) {
    out.push(pos.supervision.status);
  }
  var mom = state.data.momentum;
  if (mom && mom.state && mom.state.positions) {
    Object.keys(mom.state.positions).forEach(function (key) {
      var record = mom.state.positions[key];
      if (record && record.state) out.push(record.state);
    });
  }
  return out;
}
function exposureLines() {
  var lines = [];
  var pos = state.data.position;
  if (pos && pos.position && typeof pos.position === "object") {
    lines.push({ strategy: "Basis",
      symbol: pos.position.symbol || "(symbol unrecorded)",
      st: pos.position.state || "(state unrecorded)",
      record: pos.position, tab: "basis" });
  }
  var mom = state.data.momentum;
  if (mom && mom.state && mom.state.positions) {
    Object.keys(mom.state.positions).forEach(function (key) {
      var record = mom.state.positions[key];
      if (!record) return;
      var st = String(record.state || "").toUpperCase();
      if (!st || st === "CLOSED" || st === "NONE") return;
      lines.push({ strategy: "Volume", symbol: record.symbol || key,
        st: record.state, record: record, tab: "volume" });
    });
  }
  return lines;
}
function runtimeFresh() {
  var strip = state.data.strip;
  if (!strip || !strip.pull) return false;
  return strip.pull.level === "green" || strip.pull.level === "amber";
}
function currentPhase() {
  return executionPhase({ positionStates: collectPositionStates(),
    reconciling: false, runtimeFresh: runtimeFresh() });
}
function buildEventList() {
  var out = [];
  var mom = state.data.momentum;
  if (mom && mom.receipts && Array.isArray(mom.receipts.records)) {
    mom.receipts.records.forEach(function (record) {
      out.push({ ts: record.utc || record.decision_utc || "",
        sev: "info", strat: "Volume", sym: record.symbol || "",
        text: (record.action || "action") + " recorded",
        money: null, raw: record });
    });
  }
  if (mom && mom.realized && Array.isArray(mom.realized.records)) {
    mom.realized.records.forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      out.push({ ts: record.utc || "", sev: "trade", strat: "Volume",
        sym: record.symbol || "",
        text: "closed: realized net " + (fmtUsdt(net) || "not recorded"),
        money: fmtUsdt(net), raw: record });
    });
  }
  var pnl = state.data.pnl;
  if (pnl && Array.isArray(pnl.records)) {
    pnl.records.forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      out.push({ ts: record.utc || "", sev: "trade", strat: "Basis",
        sym: record.symbol || "",
        text: (record.verdict || "record") + ": net " +
          (fmtUsdt(net) || "not recorded"),
        money: fmtUsdt(net), raw: record });
    });
  }
  var failures = state.data.failures;
  if (failures && Array.isArray(failures.failures)) {
    failures.failures.forEach(function (record) {
      out.push({ ts: record.utc || "",
        sev: String(record.verdict || "").indexOf("UNKNOWN") >= 0 ?
          "attention" : "refused",
        strat: record.op && String(record.op).indexOf("BASIS") >= 0 ?
          "Basis" : "Volume",
        sym: record.symbol || "",
        text: (record.verdict || "refused") +
          (record.reason ? ": " + record.reason : ""),
        money: null, raw: record });
    });
  }
  var dec = state.data.decisions;
  if (dec && Array.isArray(dec.decisions)) {
    dec.decisions.forEach(function (record) {
      var action = String(record.action || "");
      if (!action || action === "WATCH") return;
      out.push({ ts: record.decision_utc || "", sev: "info",
        strat: "Volume",
        sym: record.symbol ||
          (record.lifecycle_state && record.lifecycle_state.symbol) || "",
        text: "decision: " + action +
          (record.job_reason ? " — " + record.job_reason : ""),
        money: null, raw: record });
    });
  }
  out.sort(function (a, b) { return a.ts < b.ts ? 1 : -1; });
  return out;
}
function eventTone(event) {
  return event.sev === "attention" ? "bad" :
    (event.sev === "refused" ? "warn" :
      (event.sev === "trade" ? "ok" : "info"));
}
function techDrawer(payload) {
  var details = el("details");
  details.appendChild(el("summary", null, "Technical evidence"));
  var pre = el("pre");
  pre.style.cssText =
    "font:11px/1.5 var(--mono);color:var(--ink-2);overflow:auto;" +
    "max-height:260px;margin:6px 0 0";
  pre.textContent = JSON.stringify(payload, null, 1);
  details.appendChild(pre);
  details.style.cssText =
    "margin:8px 12px;font-size:11.5px;color:var(--muted)";
  return details;
}

/* ---------- cockpit strip (contract §4) ---------- */

/* Global truth lives as compact topbar chips (owner order: no big
   status bar). Execution phase, data freshness, seals — owner words
   only; the raw machine word never renders (§4, #26). */
function renderStrip() {
  var strip = state.data.strip;
  if (!strip) return;
  var chips = clearTo("statuschips");
  var phase = currentPhase();
  var phaseTone = phase === "Attention" ? "bad" :
    (phase === "Offline" ? "" :
      (phase === "Watching" || phase === "Evaluating" ? "ok" : "warn"));
  var stopLine = ownerStopLine(strip.stop_present,
    strip.stop_jobs_present);
  chips.appendChild(chip(phase, phaseTone, stopLine ? "sealed" : null));
  var expo = exposureLines();
  if (expo.length > 0) {
    chips.appendChild(chip(expo[0].symbol, "warn",
      expo.length > 1 ? "+" + (expo.length - 1) + " more" : "open"));
  }
  var pull = strip.pull || {};
  var pullTone = !pull.present ? "" :
    (pull.level === "green" ? "ok" :
      (pull.level === "amber" ? "warn" : "bad"));
  chips.appendChild(chip(!pull.present ? "No data" :
    (pull.level === "green" ? "Live" :
      (pull.level === "amber" ? "Delayed" : "Stale")), pullTone,
    pull.present ? ageText(pull.age_seconds) : null));
}

/* ---------- nav + page band + router ---------- */

function buildNav() {
  /* Nav lives statically in index.html (labels exist before JS runs);
     this only wires the clicks. */
  TABS.forEach(function (tab) {
    var item = document.getElementById("tab-" + tab);
    if (item) {
      item.addEventListener("click", function () { selectTab(tab); });
    }
  });
}
function renderPageBand() {
  var bandEl = document.getElementById("pageband");
  var strip = state.data.strip || {};
  var pull = strip.pull || {};
  var liveCls = "", liveText = "No data delivery yet";
  if (pull.present) {
    if (pull.ok === false || pull.level === "red") {
      liveCls = "bad"; liveText = "Stale · " + ageText(pull.age_seconds);
    } else if (pull.level === "amber") {
      liveCls = "warn";
      liveText = "Delayed · " + ageText(pull.age_seconds);
    } else {
      liveCls = "ok"; liveText = "Live · " + ageText(pull.age_seconds);
    }
  }
  var title = state.tab.charAt(0).toUpperCase() + state.tab.slice(1);
  var sig = title + "|" + liveCls + "|" + liveText;
  if (bandEl.getAttribute("data-r") === sig) return;
  bandEl.setAttribute("data-r", sig);
  bandEl.textContent = "";
  bandEl.appendChild(ic(PAGE_ICONS[state.tab], "pb-ic"));
  bandEl.appendChild(el("span", "pb-title", title));
  bandEl.appendChild(el("span",
    "pb-live" + (liveCls ? " " + liveCls : ""), liveText));
  bandEl.appendChild(el("span", "pb-meta",
    "reads only — cannot trade, pause, or arm"));
}
function selectTab(name) {
  if (TABS.indexOf(name) < 0) name = "overview";
  state.tab = name;
  TABS.forEach(function (tab) {
    var item = document.getElementById("tab-" + tab);
    if (item) item.setAttribute("aria-selected",
      tab === name ? "true" : "false");
  });
  if (window.location.hash !== "#" + name) {
    window.history.replaceState(null, "", "#" + name);
  }
  var root = document.getElementById("pageroot");
  root.setAttribute("data-tab", name);
  root.textContent = "";
  renderCurrent(true);
}
function renderCurrent(fresh) {
  renderStrip();
  renderPageBand();
  var module = window.QuantTabs[state.tab];
  if (module && typeof module.render === "function") {
    module.render(fresh === true);
  }
}

/* ---------- palette + clock ---------- */

function paletteIndex() {
  var out = [];
  TABS.forEach(function (tab) {
    out.push({ group: "Pages",
      label: tab.charAt(0).toUpperCase() + tab.slice(1),
      sub: null, icon: PAGE_ICONS[tab],
      action: function () { selectTab(tab); } });
  });
  var seenSyms = new Set();
  function addSymbol(symbol, sub, tab) {
    if (!symbol || seenSyms.has(symbol)) return;
    seenSyms.add(symbol);
    out.push({ group: "Symbols", label: symbol, sub: sub, icon: null,
      action: function () { selectTab(tab); } });
  }
  exposureLines().forEach(function (line) {
    addSymbol(line.symbol, line.strategy + " · open", line.tab);
  });
  var stream = state.data.stream;
  if (stream && Array.isArray(stream.records)) {
    stream.records.slice(0, 300).forEach(function (record) {
      var sel = Array.isArray(record.selection) ? record.selection : [];
      var isBasis = sel.indexOf("basis_gap") >= 0;
      addSymbol(record.symbol,
        isBasis ? "Basis gap delivered" : "Volume surge delivered",
        isBasis ? "basis" : "volume");
    });
  }
  return out;
}
function buildPalette() {
  var overlay = el("div");
  overlay.id = "palette";
  var pal = el("div", "pal");
  var inRow = el("div", "pal-in");
  inRow.appendChild(ic("search"));
  var input = el("input");
  input.type = "text";
  input.placeholder = "Search commands";
  inRow.appendChild(input);
  pal.appendChild(inRow);
  var list = el("div", "pal-list");
  pal.appendChild(list);
  var foot = el("div", "pal-foot");
  foot.appendChild(el("span", null, "↑↓ navigate"));
  foot.appendChild(el("span", null, "enter open"));
  foot.appendChild(el("span", null, "esc close"));
  pal.appendChild(foot);
  overlay.appendChild(pal);
  document.body.appendChild(overlay);

  var items = [], sel = 0;
  function renderList() {
    list.textContent = "";
    var q = input.value.trim().toLowerCase();
    var all = paletteIndex();
    items = all.filter(function (item) {
      if (!q) return true;
      return (item.label + " " + (item.sub || ""))
        .toLowerCase().indexOf(q) >= 0;
    }).slice(0, 40);
    if (sel >= items.length) sel = Math.max(0, items.length - 1);
    var lastGroup = null;
    items.forEach(function (item, index) {
      if (item.group !== lastGroup) {
        lastGroup = item.group;
        list.appendChild(el("div", "pal-g", item.group));
      }
      var row = el("div", "pal-item" + (index === sel ? " sel" : ""));
      if (item.icon) row.appendChild(ic(item.icon));
      row.appendChild(el("span", "pl", item.label));
      if (item.sub) row.appendChild(el("span", "ps", item.sub));
      row.addEventListener("click", function () {
        item.action(); close();
      });
      list.appendChild(row);
    });
    if (items.length === 0) {
      list.appendChild(el("div", "pal-g", "Nothing matches"));
    }
  }
  function open() {
    overlay.classList.add("on");
    input.value = ""; sel = 0; renderList();
    input.focus();
  }
  function close() { overlay.classList.remove("on"); }
  input.addEventListener("input", function () { sel = 0; renderList(); });
  input.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown") { sel = Math.min(sel + 1,
      items.length - 1); renderList(); ev.preventDefault(); }
    else if (ev.key === "ArrowUp") { sel = Math.max(sel - 1, 0);
      renderList(); ev.preventDefault(); }
    else if (ev.key === "Enter" && items[sel]) {
      items[sel].action(); close();
    } else if (ev.key === "Escape") close();
  });
  overlay.addEventListener("click", function (ev) {
    if (ev.target === overlay) close();
  });
  document.addEventListener("keydown", function (ev) {
    if ((ev.metaKey || ev.ctrlKey) && ev.key.toLowerCase() === "k") {
      ev.preventDefault();
      if (overlay.classList.contains("on")) close(); else open();
    } else if (ev.key === "/" &&
        !/INPUT|TEXTAREA/.test(document.activeElement.tagName)) {
      ev.preventDefault(); open();
    }
  });
  return { open: open, close: close };
}
function buildAppearance() {
  var slot = document.getElementById("sidebottom");
  var trigger = el("button");
  trigger.type = "button";
  trigger.id = "appearance-trigger";
  trigger.appendChild(ic("settings"));
  trigger.appendChild(el("span", "lbl", "Appearance"));
  slot.appendChild(trigger);
  var panel = el("div");
  panel.id = "appearance";
  document.body.appendChild(panel);
  function segRow(label, options, activeKey, onPick) {
    var row = el("div", "ap-row");
    row.appendChild(el("span", "ap-l", label));
    var seg = el("span", "ap-seg");
    options.forEach(function (opt) {
      var p = el("button",
        "ap-pill" + (opt[0] === activeKey ? " on" : ""), opt[1]);
      p.type = "button";
      p.addEventListener("click", function () { onPick(opt[0]); });
      seg.appendChild(p);
    });
    row.appendChild(seg);
    return row;
  }
  function renderPanel() {
    var prefs = getPrefs();
    panel.textContent = "";
    panel.appendChild(segRow("Theme",
      [["light", "Light"], ["dark", "Dark"]], prefs.theme,
      function (v) { prefs.theme = v; setPrefs(prefs); renderPanel(); }));
    var accentRow = el("div", "ap-row");
    accentRow.appendChild(el("span", "ap-l", "Accent"));
    var seg = el("span", "ap-seg");
    Object.keys(ACCENTS).forEach(function (name) {
      var dot = el("button",
        "ap-dot" + (name === prefs.accent ? " on" : ""));
      dot.type = "button";
      dot.style.background = ACCENTS[name][0];
      dot.title = name;
      dot.addEventListener("click", function () {
        prefs.accent = name; setPrefs(prefs); renderPanel();
      });
      seg.appendChild(dot);
    });
    accentRow.appendChild(seg);
    panel.appendChild(accentRow);
    panel.appendChild(segRow("Zoom",
      [[0.9, "90%"], [1, "100%"], [1.1, "110%"], [1.25, "125%"]],
      prefs.zoom,
      function (v) { prefs.zoom = v; setPrefs(prefs); renderPanel(); }));
    panel.appendChild(segRow("Clock",
      [["UTC", "UTC"], ["IST", "IST"]], prefs.clockZone,
      function (v) { prefs.clockZone = v; setPrefs(prefs);
        renderPanel(); tickClock(); }));
    panel.appendChild(segRow("Motion",
      [["on", "On"], ["off", "Off"]], prefs.motion,
      function (v) { prefs.motion = v; setPrefs(prefs); renderPanel(); }));
  }
  trigger.addEventListener("click", function (ev) {
    ev.stopPropagation();
    if (panel.classList.contains("on")) {
      panel.classList.remove("on"); return;
    }
    renderPanel();
    var rect = trigger.getBoundingClientRect();
    panel.style.left = rect.left + "px";
    panel.style.bottom = (window.innerHeight - rect.top + 8) + "px";
    panel.classList.add("on");
  });
  document.addEventListener("click", function (ev) {
    if (!panel.contains(ev.target) && ev.target !== trigger) {
      panel.classList.remove("on");
    }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "Escape") panel.classList.remove("on");
  });
}
function tickClock() {
  var timeEl = document.getElementById("clocktime");
  var zoneEl = document.getElementById("clockzone");
  if (!timeEl) return;
  var prefs = getPrefs();
  var now = new Date();
  var ms = now.getTime();
  if (prefs.clockZone === "IST") ms += 330 * 60000;
  var d = new Date(ms);
  var pad = function (v) { return String(v).padStart(2, "0"); };
  timeEl.textContent = pad(d.getUTCHours()) + ":" +
    pad(d.getUTCMinutes()) + ":" + pad(d.getUTCSeconds());
  zoneEl.textContent = prefs.clockZone === "IST" ? "IST" : "UTC";
}

/* ---------- fetch + poll ---------- */

function getJson(url) {
  return fetch(url).then(function (response) {
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  });
}
function setConnection(ok) {
  var bar = document.getElementById("connbar");
  state.connected = ok;
  if (ok) { bar.className = ""; bar.textContent = ""; return; }
  bar.className = "on";
  bar.textContent = "Connection to the dashboard server lost — " +
    "showing the last good data" + (state.lastGoodAt ?
      " from " + shortTime(state.lastGoodAt) : "") + ".";
}
function coreUrls() {
  var urls = ["strip", "position", "momentum", "failures"];
  var module = window.QuantTabs[state.tab];
  if (module && Array.isArray(module.needs)) {
    module.needs.forEach(function (name) {
      if (urls.indexOf(name) < 0 && API[name]) urls.push(name);
    });
  }
  return urls;
}
function pollOnce() {
  Promise.all(coreUrls().map(function (name) {
    return getJson(API[name]).then(function (payload) {
      return { name: name, payload: payload };
    });
  })).then(function (results) {
    results.forEach(function (result) {
      state.data[result.name] = result.payload;
    });
    state.lastGoodAt = new Date().toISOString();
    setConnection(true);
    renderCurrent(false);
  }).catch(function () { setConnection(false); });
  if (!state.eventsLoaded) {
    getJson(API.events).then(function (payload) {
      state.data.events = payload;
      state.eventsLoaded = true;
      renderCurrent(false);
    }).catch(function () { /* archived history stays pending */ });
  }
}

/* ---------- shared surface ---------- */

window.QuantTabs = window.QuantTabs || {};
window.QuantUI = {
  state: state, el: el, ic: ic, clearTo: clearTo,
  tile: tile, tiles: tiles, chip: chip, sevMark: sevMark, c2: c2,
  band: band, stateBlock: stateBlock, facts: facts, feedRow: feedRow,
  groupHead: groupHead, pillBar: pillBar, dayStrip: dayStrip,
  liveTrail: liveTrail, Tape: Tape, tapeTable: tapeTable,
  techDrawer: techDrawer,
  fmtNum: fmtNum, fmtPct: fmtPct, fmtUsdt: fmtUsdt, ageText: ageText,
  shortTime: shortTime, shortDay: shortDay,
  ownerStopLine: ownerStopLine, ownerLockLine: ownerLockLine,
  ownerPhaseForState: ownerPhaseForState,
  executionPhase: executionPhase, currentPhase: currentPhase,
  exposureLines: exposureLines,
  collectPositionStates: collectPositionStates,
  buildEventList: buildEventList, eventTone: eventTone,
  getPrefs: getPrefs, selectTab: selectTab,
  renderCurrent: renderCurrent
};

/* ---------- boot ---------- */

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("sidenav")) return;
    applyPrefs();
    buildNav();
    buildAppearance();
    var pal = buildPalette();
    document.getElementById("searchfield")
      .addEventListener("click", pal.open);
    TABS.forEach(function (tab) {
      var module = window.QuantTabs[tab];
      if (module && typeof module.init === "function") module.init();
    });
    var initial = window.location.hash.replace("#", "");
    selectTab(TABS.indexOf(initial) >= 0 ? initial : "overview");
    tickClock();
    window.setInterval(tickClock, 1000);
    pollOnce();
    window.setInterval(pollOnce, REFRESH_MS);
  });
}

/* Pure helpers exported for offline tests. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { fmtNum: fmtNum, fmtPct: fmtPct, fmtUsdt: fmtUsdt,
    ageText: ageText, ownerStopLine: ownerStopLine,
    ownerLockLine: ownerLockLine, executionPhase: executionPhase };
}
