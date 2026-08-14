"use strict";

/* Quant owner dashboard — Iteration 1 (Milestones 1-4).
   Read-only product over the current factual GET endpoints. Policy,
   authority and execution stay separate everywhere. Missing data renders
   as Unavailable — never zero, never a sample. The browser calculates no
   money value; it displays what the backend supplies. */

var API = {
  strip: "/api/strip",
  health: "/api/health",
  position: "/api/position",
  momentum: "/api/momentum",
  failures: "/api/failures",
  stream: "/api/stream",
  candidates: "/api/candidates",
  decisions: "/api/decisions",
  pnl: "/api/pnl",
  fees: "/api/fees",
  events: "/api/events"
};
var REFRESH_MS = 2000;

/* ---------- pure formatting helpers ---------- */

function fmtNum(raw, maxDp) {
  if (raw === null || raw === undefined || raw === "") return null;
  var value = Number(raw);
  if (!Number.isFinite(value)) return null;
  var dp = maxDp === undefined ? 6 : maxDp;
  var text = value.toFixed(dp);
  if (text.indexOf(".") >= 0) {
    text = text.replace(/0+$/, "").replace(/\.$/, "");
  }
  return text;
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
      !Number.isFinite(Number(seconds))) {
    return "age unknown";
  }
  var s = Math.max(0, Number(seconds));
  if (s < 60) return Math.round(s) + "s ago";
  if (s < 3600) return (s / 60).toFixed(1) + "m ago";
  return (s / 3600).toFixed(1) + "h ago";
}

function shortTime(iso) {
  if (typeof iso !== "string" || iso.length < 16) return "";
  return iso.slice(11, 19) + " UTC";
}

function secondsUntil(iso) {
  if (typeof iso !== "string") return null;
  var t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  return Math.round((t - Date.now()) / 1000);
}

/* ---------- owner-language mappings (contract section 4) ---------- */

function ownerStopLine(stopPresent, stopJobsPresent) {
  if (stopPresent) return "Paused — exposure management remains protected";
  if (stopJobsPresent) return "Watching only — new entries paused";
  return "";
}

function ownerLockLine(lockPresent) {
  return lockPresent ? "Another entry currently owns the account"
    : "Entry lock clear";
}

/* Execution phase from provable sources only. Precedence:
   Attention > Reconciling > Exiting > Entering > Holding > Evaluating >
   Watching > Offline. Evaluating is not provable in Iteration 1. */
function executionPhase(facts) {
  var states = facts.positionStates || [];
  var i;
  for (i = 0; i < states.length; i++) {
    var s = String(states[i] || "").toUpperCase();
    if (s.indexOf("UNKNOWN") >= 0 || s.indexOf("ONE_LEG") >= 0 ||
        s.indexOf("FROZEN") >= 0) {
      return "Attention";
    }
  }
  if (facts.reconciling) return "Reconciling";
  for (i = 0; i < states.length; i++) {
    if (String(states[i]).toUpperCase().indexOf("CLOS") >= 0) return "Exiting";
  }
  for (i = 0; i < states.length; i++) {
    if (String(states[i]).toUpperCase().indexOf("OPENING") >= 0) {
      return "Entering";
    }
  }
  for (i = 0; i < states.length; i++) {
    var st = String(states[i]).toUpperCase();
    if (st === "OPEN" || st.indexOf("HOLD") >= 0) return "Holding";
  }
  if (facts.runtimeFresh) return "Watching";
  return "Offline";
}

/* ---------- state ---------- */

var state = {
  data: {},
  lastGoodAt: null,
  connected: true,
  tab: "overview",
  eventsLoaded: false
};

/* ---------- dom helpers ---------- */

function el(tag, cls, text) {
  var node = document.createElement(tag);
  if (cls) node.className = cls;
  if (text !== undefined && text !== null) node.textContent = text;
  return node;
}

function setText(node, text) {
  if (node.textContent !== text) {
    node.textContent = text;
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");
  }
}

function clearTo(id) {
  var root = document.getElementById(id);
  root.textContent = "";
  return root;
}

function techDrawer(payload) {
  var details = el("details", "tech");
  details.appendChild(el("summary", null, "Technical"));
  var pre = el("pre");
  pre.textContent = JSON.stringify(payload, null, 1);
  details.appendChild(pre);
  return details;
}

/* ---------- fetch layer ---------- */

function getJson(url) {
  return fetch(url).then(function (response) {
    if (!response.ok) throw new Error("HTTP " + response.status);
    return response.json();
  });
}

function setConnection(ok) {
  var bar = document.getElementById("connbar");
  if (ok) {
    state.connected = true;
    bar.className = "";
    bar.textContent = "";
    return;
  }
  state.connected = false;
  bar.className = "on red";
  bar.textContent = "Connection to the dashboard server lost — showing " +
    "the last good data" + (state.lastGoodAt ?
      " from " + shortTime(state.lastGoodAt) : "") + ".";
}

/* ---------- derived facts ---------- */

function collectPositionStates() {
  var out = [];
  var pos = state.data.position;
  if (pos && pos.position && typeof pos.position === "object") {
    if (pos.position.state) out.push(pos.position.state);
  }
  if (pos && pos.supervision && typeof pos.supervision === "object") {
    if (pos.supervision.status) out.push(pos.supervision.status);
  }
  var mom = state.data.momentum;
  if (mom && mom.state && mom.state.positions &&
      typeof mom.state.positions === "object") {
    Object.keys(mom.state.positions).forEach(function (key) {
      var record = mom.state.positions[key];
      if (record && typeof record === "object" && record.state) {
        out.push(record.state);
      }
    });
  }
  return out;
}

function exposureLines() {
  var lines = [];
  var pos = state.data.position;
  if (pos && pos.position && typeof pos.position === "object") {
    lines.push({
      strategy: "Basis",
      symbol: pos.position.symbol || "(symbol unrecorded)",
      st: pos.position.state || "(state unrecorded)",
      record: pos.position,
      tab: "basis"
    });
  }
  var mom = state.data.momentum;
  if (mom && mom.state && mom.state.positions &&
      typeof mom.state.positions === "object") {
    Object.keys(mom.state.positions).forEach(function (key) {
      var record = mom.state.positions[key];
      if (!record || typeof record !== "object") return;
      var st = String(record.state || "").toUpperCase();
      if (!st || st === "CLOSED" || st === "NONE") return;
      lines.push({
        strategy: "Volume",
        symbol: record.symbol || key,
        st: record.state,
        record: record,
        tab: "volume"
      });
    });
  }
  return lines;
}

function runtimeFresh() {
  var strip = state.data.strip;
  if (!strip) return false;
  if (strip.pull && strip.pull.level === "green") return true;
  if (strip.pull && strip.pull.level === "amber") return true;
  return false;
}

/* ---------- global strip ---------- */

function setCell(id, value, cls, sub, title) {
  var cell = document.getElementById(id);
  var valueNode = cell.querySelector(".gvalue");
  valueNode.classList.remove("skel", "s-green", "s-amber", "s-red", "s-gray");
  if (cls) valueNode.classList.add(cls);
  setText(valueNode, value);
  var subNode = cell.querySelector(".gsub");
  if (subNode.textContent !== (sub || "")) subNode.textContent = sub || "";
  cell.title = title || "";
}

function renderStrip() {
  var strip = state.data.strip;
  if (!strip) return;

  var states = collectPositionStates();
  var phase = executionPhase({
    positionStates: states,
    reconciling: false,
    runtimeFresh: runtimeFresh()
  });
  var phaseCls = phase === "Attention" ? "s-red" :
    (phase === "Offline" ? "s-gray" :
      (phase === "Holding" || phase === "Entering" || phase === "Exiting" ?
        "s-amber" : ""));
  setCell("g-exec", phase, phaseCls,
    ownerStopLine(strip.stop_present, strip.stop_jobs_present));

  setCell("g-auth", "Unavailable", "s-gray",
    "No operator contract yet",
    "Entry authority cannot be read until the operator state contract " +
    "exists. It is never inferred from machine flags.");

  setCell("g-equity", "Unavailable", "s-gray", "No account contract yet");
  setCell("g-net", "Unavailable", "s-gray", "Ledger incomplete");

  var expo = exposureLines();
  if (expo.length === 0) {
    setCell("g-expo", "None", "", "Flat");
  } else {
    var first = expo[0];
    setCell("g-expo", first.symbol, "s-amber",
      first.strategy + " · " + first.st +
      (expo.length > 1 ? " · +" + (expo.length - 1) + " more" : ""));
  }

  var gov = strip.governor || {};
  if (gov.present) {
    var riskCls = gov.entries_used >= gov.entries_cap ? "s-red" :
      (gov.entries_used > 0 ? "s-amber" : "");
    setCell("g-risk", gov.entries_used + " of " + gov.entries_cap, riskCls,
      "loss+fees " + gov.loss_fees_used_usdt + " of " +
      gov.loss_fees_cap_usdt + " USDT");
  } else {
    setCell("g-risk", "0 of 5", "",
      "No entries recorded today",
      "The governor day ledger has no rows today; caps are code law.");
  }

  var pull = strip.pull || {};
  var market = strip.market_pass || {};
  var dataValue, dataCls, dataSub;
  if (!pull.present) {
    dataValue = "Unavailable"; dataCls = "s-gray";
    dataSub = "No data delivery yet";
  } else if (pull.ok === false || pull.level === "red") {
    dataValue = "Stale"; dataCls = "s-red";
    dataSub = "Delivery stopped " + ageText(pull.age_seconds);
  } else if (pull.level === "amber") {
    dataValue = "Delayed"; dataCls = "s-amber";
    dataSub = "Updated " + ageText(pull.age_seconds);
  } else {
    dataValue = "Live"; dataCls = "s-green";
    dataSub = market.state === "sealed" ?
      "Market feed sealed while paused" :
      "Updated " + ageText(pull.age_seconds);
  }
  setCell("g-data", dataValue, dataCls, dataSub,
    "Delivery age: " + ageText(pull.age_seconds) +
    " · market pass: " + (market.state || "unknown"));
}

/* ---------- keyed table engine ---------- */

function KeyedTable(options) {
  this.table = document.getElementById(options.tableId);
  this.tbody = this.table.querySelector("tbody");
  this.columns = options.columns;
  this.rankchip = document.getElementById(options.rankchipId);
  this.expandRender = options.expandRender;
  this.sortKey = options.sortKey;
  this.sortAsc = false;
  this.rows = new Map();
  this.order = [];
  this.expanded = new Set();
  this.hovering = false;
  this.pendingOrder = null;

  var self = this;
  this.tbody.addEventListener("mouseenter", function () {
    self.hovering = true;
  }, true);
  this.tbody.addEventListener("mouseleave", function () {
    self.hovering = false;
  }, true);
  this.table.querySelector("thead").addEventListener("click", function (ev) {
    var th = ev.target.closest("th[data-sort]");
    if (!th) return;
    var key = th.getAttribute("data-sort");
    if (self.sortKey === key) { self.sortAsc = !self.sortAsc; }
    else { self.sortKey = key; self.sortAsc = false; }
    self.table.querySelectorAll("th").forEach(function (node) {
      node.classList.toggle("sorted", node === th);
      node.classList.toggle("asc", node === th && self.sortAsc);
    });
    self.applyOrder(self.computeOrder(self.lastRows || []));
  });
  if (this.rankchip) {
    this.rankchip.querySelector("button").addEventListener("click",
      function () {
        if (self.pendingOrder) self.applyOrder(self.pendingOrder);
        self.pendingOrder = null;
        self.rankchip.classList.remove("on");
      });
  }
}

KeyedTable.prototype.computeOrder = function (rows) {
  var key = this.sortKey;
  var asc = this.sortAsc;
  var sorted = rows.slice().sort(function (a, b) {
    var av = a[key], bv = b[key];
    var an = av === null || av === undefined;
    var bn = bv === null || bv === undefined;
    if (an && bn) return a.symbol < b.symbol ? -1 : 1;
    if (an) return 1;
    if (bn) return -1;
    var aNum = Number(av), bNum = Number(bv);
    var cmp;
    if (Number.isFinite(aNum) && Number.isFinite(bNum)) {
      cmp = aNum - bNum;
    } else {
      cmp = String(av) < String(bv) ? -1 : (String(av) > String(bv) ? 1 : 0);
    }
    if (cmp === 0) cmp = a.symbol < b.symbol ? -1 : 1;
    return asc ? cmp : -cmp;
  });
  return sorted.map(function (row) { return row.key; });
};

KeyedTable.prototype.applyOrder = function (orderKeys) {
  var self = this;
  this.order = orderKeys;
  orderKeys.forEach(function (key) {
    var entry = self.rows.get(key);
    if (!entry) return;
    self.tbody.appendChild(entry.tr);
    if (entry.exprTr) self.tbody.appendChild(entry.exprTr);
  });
};

KeyedTable.prototype.update = function (rows) {
  var self = this;
  this.lastRows = rows;
  var seen = new Set();

  rows.forEach(function (row) {
    seen.add(row.key);
    var entry = self.rows.get(row.key);
    if (!entry) {
      var tr = el("tr", "rowbtn");
      tr.setAttribute("data-key", row.key);
      var cells = {};
      self.columns.forEach(function (col) {
        var td = el("td", col.cls || "");
        cells[col.key] = td;
        tr.appendChild(td);
      });
      tr.addEventListener("click", function () { self.toggle(row.key); });
      entry = { tr: tr, cells: cells, exprTr: null, lastExpandJson: "" };
      self.rows.set(row.key, entry);
      self.tbody.appendChild(tr);
    }
    entry.row = row;
    self.columns.forEach(function (col) {
      var value = col.render(row);
      var td = entry.cells[col.key];
      if (td.textContent !== value.text) {
        td.textContent = value.text;
        td.classList.remove("flash");
        void td.offsetWidth;
        td.classList.add("flash");
      }
      td.className = (col.cls || "") + (value.cls ? " " + value.cls : "");
    });
    if (self.expanded.has(row.key)) self.renderExpansion(entry);
  });

  Array.from(this.rows.keys()).forEach(function (key) {
    if (seen.has(key)) return;
    var entry = self.rows.get(key);
    entry.tr.remove();
    if (entry.exprTr) entry.exprTr.remove();
    self.rows.delete(key);
    self.expanded.delete(key);
  });

  var next = this.computeOrder(rows);
  var frozen = this.hovering || this.expanded.size > 0;
  if (!frozen) {
    this.applyOrder(next);
    this.pendingOrder = null;
    if (this.rankchip) this.rankchip.classList.remove("on");
    return;
  }
  var current = this.order.filter(function (key) { return seen.has(key); });
  next.forEach(function (key) {
    if (current.indexOf(key) < 0) current.push(key);
  });
  this.applyOrder(current);
  if (JSON.stringify(current) !== JSON.stringify(next)) {
    this.pendingOrder = next;
    if (this.rankchip) this.rankchip.classList.add("on");
  }
};

KeyedTable.prototype.toggle = function (key) {
  var entry = this.rows.get(key);
  if (!entry) return;
  if (this.expanded.has(key)) {
    this.expanded.delete(key);
    if (entry.exprTr) { entry.exprTr.remove(); entry.exprTr = null; }
    entry.lastExpandJson = "";
    return;
  }
  this.expanded.add(key);
  this.renderExpansion(entry);
};

KeyedTable.prototype.renderExpansion = function (entry) {
  var body = this.expandRender(entry.row);
  var json = JSON.stringify(entry.row);
  if (entry.exprTr && entry.lastExpandJson === json) return;
  if (!entry.exprTr) {
    entry.exprTr = el("tr", "exprow");
    var td = el("td");
    td.colSpan = this.columns.length;
    entry.exprTr.appendChild(td);
    entry.tr.after(entry.exprTr);
  }
  entry.lastExpandJson = json;
  var cell = entry.exprTr.firstChild;
  cell.textContent = "";
  cell.appendChild(body);
};

/* ---------- overview ---------- */

function ownerPhaseForState(raw) {
  var st = String(raw || "").toUpperCase();
  if (st.indexOf("UNKNOWN") >= 0 || st.indexOf("ONE_LEG") >= 0 ||
      st.indexOf("FROZEN") >= 0) return "Attention";
  if (st.indexOf("CLOS") >= 0) return "Exiting";
  if (st.indexOf("OPENING") >= 0) return "Entering";
  if (st === "OPEN" || st.indexOf("HOLD") >= 0) return "Holding";
  return raw ? String(raw) : "Unknown";
}

function renderOverview() {
  var root = clearTo("ov-exposure");
  var expo = exposureLines();
  if (expo.length === 0) {
    root.appendChild(el("p", "empty",
      "No standing position. The account is flat."));
  } else {
    expo.forEach(function (line) {
      var p = el("p", null);
      var phase = ownerPhaseForState(line.st);
      var badge = el("span", "badge " +
        (phase === "Attention" ? "b-red" : "b-amber"), phase);
      p.appendChild(badge);
      p.appendChild(document.createTextNode(
        " " + line.strategy + " · " + line.symbol));
      root.appendChild(p);
      root.appendChild(techDrawer(line.record));
    });
  }

  root = clearTo("ov-capital");
  [["Total equity", "Unavailable — no account contract yet"],
   ["Free Spot USDT", "Unavailable — no account contract yet"],
   ["Free Futures USDT", "Unavailable — no account contract yet"],
   ["Capital exposed", "Unavailable — no account contract yet"]
  ].forEach(function (pair) {
    var p = el("p", "unavail");
    p.appendChild(el("strong", null, pair[0] + ": "));
    p.appendChild(document.createTextNode(pair[1]));
    root.appendChild(p);
  });

  root = clearTo("ov-basis-realized");
  var pnl = state.data.pnl;
  if (!pnl || !pnl.present || !pnl.records || pnl.records.length === 0) {
    root.appendChild(el("p", "empty",
      "No completed Basis accounting records in the delivered data."));
  } else {
    pnl.records.slice(0, 6).forEach(function (record) {
      var p = el("p", null);
      var net = record.realized && record.realized.net_usdt;
      p.appendChild(el("span", "badge b-gray", record.verdict || "record"));
      p.appendChild(document.createTextNode(
        " " + (record.symbol || "") + " · net " +
        (fmtUsdt(net) || "not recorded") + " · " +
        (shortTime(record.utc || "") || "time unrecorded")));
      root.appendChild(p);
      root.appendChild(techDrawer(record));
    });
    root.appendChild(el("p", "meta",
      "Partial: Basis records only; fees and funding completeness " +
      "depend on each receipt."));
  }

  root = clearTo("ov-momentum-realized");
  var mom = state.data.momentum;
  var realized = mom && mom.realized && mom.realized.records || [];
  if (realized.length === 0) {
    root.appendChild(el("p", "empty",
      "No realized Volume results in the delivered data."));
  } else {
    realized.slice(-6).reverse().forEach(function (record) {
      var p = el("p", null);
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      p.appendChild(el("span", "badge b-gray", record.verdict || "result"));
      p.appendChild(document.createTextNode(
        " " + (record.symbol || "") + " · net " +
        (fmtUsdt(net) || "not recorded")));
      root.appendChild(p);
      root.appendChild(techDrawer(record));
    });
    root.appendChild(el("p", "meta",
      "Partial: runner results only, not account-wide profit."));
  }

  root = clearTo("ov-risk");
  var strip = state.data.strip || {};
  var gov = strip.governor || {};
  var kv = el("dl", "kv");
  function addKv(label, value) {
    kv.appendChild(el("dt", null, label));
    kv.appendChild(el("dd", null, value));
  }
  addKv("Entries today", (gov.present ? gov.entries_used : 0) + " of " +
    (gov.entries_cap || 5));
  addKv("Loss+fees today", (gov.present ? gov.loss_fees_used_usdt : "0") +
    " of " + (gov.loss_fees_cap_usdt || "1.00") + " USDT");
  addKv("Hard ceiling", "8 USDT first trade (Basis per leg)");
  addKv("Leverage", "1× — locked");
  addKv("Entry lock", ownerLockLine(gov.entry_lock_present));
  root.appendChild(kv);
  root.appendChild(el("p", "meta",
    "Recorded governor usage — incomplete for Basis. Caps are code law; " +
    "this page cannot change them."));

  root = clearTo("ov-activity");
  var events = buildEventList().slice(0, 5);
  if (events.length === 0) {
    root.appendChild(el("p", "empty",
      "No completed trades yet. The machine is watching markets."));
  } else {
    events.forEach(function (event) { root.appendChild(eventLine(event)); });
    root.appendChild(el("p", "meta", "Recent — partial sources."));
  }
}

/* ---------- shared event builder (bounded, provable) ---------- */

function buildEventList() {
  var out = [];
  var mom = state.data.momentum;
  if (mom && mom.receipts && Array.isArray(mom.receipts.records)) {
    mom.receipts.records.forEach(function (record) {
      out.push({
        ts: record.utc || record.decision_utc || "",
        sev: "info",
        strat: "Volume",
        sym: record.symbol || "",
        text: (record.action || "action") + " recorded",
        money: null, raw: record
      });
    });
  }
  if (mom && mom.realized && Array.isArray(mom.realized.records)) {
    mom.realized.records.forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      out.push({
        ts: record.utc || "",
        sev: "trade",
        strat: "Volume",
        sym: record.symbol || "",
        text: "closed: realized net " + (fmtUsdt(net) || "not recorded"),
        money: fmtUsdt(net), raw: record
      });
    });
  }
  var pnl = state.data.pnl;
  if (pnl && Array.isArray(pnl.records)) {
    pnl.records.forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      out.push({
        ts: record.utc || "",
        sev: "trade",
        strat: "Basis",
        sym: record.symbol || "",
        text: (record.verdict || "record") + ": net " +
          (fmtUsdt(net) || "not recorded"),
        money: fmtUsdt(net), raw: record
      });
    });
  }
  var failures = state.data.failures;
  if (failures && Array.isArray(failures.failures)) {
    failures.failures.forEach(function (record) {
      out.push({
        ts: record.utc || "",
        sev: String(record.verdict || "").indexOf("UNKNOWN") >= 0 ?
          "attention" : "refused",
        strat: record.op && String(record.op).indexOf("BASIS") >= 0 ?
          "Basis" : "Volume",
        sym: record.symbol || "",
        text: (record.verdict || "refused") +
          (record.reason ? ": " + record.reason : ""),
        money: null, raw: record
      });
    });
  }
  var dec = state.data.decisions;
  if (dec && Array.isArray(dec.decisions)) {
    dec.decisions.forEach(function (record) {
      var action = String(record.action || "");
      if (!action || action === "WATCH") return;
      out.push({
        ts: record.decision_utc || "",
        sev: "info",
        strat: "Volume",
        sym: record.symbol ||
          (record.lifecycle_state && record.lifecycle_state.symbol) || "",
        text: "decision: " + action +
          (record.job_reason ? " — " + record.job_reason : ""),
        money: null, raw: record
      });
    });
  }
  out.sort(function (a, b) { return a.ts < b.ts ? 1 : -1; });
  return out;
}

function eventLine(event) {
  var p = el("p", "ev");
  p.appendChild(el("span", "ts", shortTime(event.ts) || "time unrecorded"));
  var cls = event.sev === "attention" ? "b-red" :
    (event.sev === "refused" ? "b-amber" :
      (event.sev === "trade" ? "b-green" : "b-gray"));
  p.appendChild(el("span", "badge " + cls, event.strat));
  p.appendChild(document.createTextNode(
    " " + (event.sym ? event.sym + " " : "") + event.text));
  return p;
}

/* ---------- basis tab ---------- */

var basisFilter = "all";
var basisTable = null;

function proposalBySymbol() {
  var map = {};
  var payload = state.data.candidates;
  if (payload && Array.isArray(payload.candidates)) {
    payload.candidates.forEach(function (candidate) {
      var record = candidate.proposal || candidate;
      if (record && record.symbol) map[record.symbol] = record;
    });
  }
  return map;
}

function basisRows() {
  var stream = state.data.stream;
  if (!stream || !Array.isArray(stream.tokens)) return [];
  var proposals = proposalBySymbol();
  var pos = state.data.position;
  var openSymbol = pos && pos.position && pos.position.symbol;
  var rows = [];
  stream.tokens.forEach(function (token) {
    var sel = token.selection || [];
    if (sel.indexOf("basis_gap") < 0) return;
    var data = token.data || {};
    if (data.venues !== "both") return;
    if (data.basis_gap_pct === null || data.basis_gap_pct === undefined) {
      return;
    }
    var proposal = proposals[token.symbol] || null;
    rows.push({
      key: token.symbol,
      symbol: token.symbol,
      spot: data.spot_ask,
      perp: data.perp_bid,
      gap: data.basis_gap_pct,
      funding: data.funding_rate,
      edge: proposal ? proposal.net_edge_pct : null,
      planned: proposal ? proposal.quantity : null,
      phase: openSymbol === token.symbol ? "Open" :
        (proposal ? "Proposal" : "Observed"),
      proposal: proposal,
      data: data
    });
  });
  if (basisFilter === "edge") {
    rows = rows.filter(function (row) {
      return row.edge !== null && Number(row.edge) > 0;
    });
  } else if (basisFilter === "proposal") {
    rows = rows.filter(function (row) { return row.proposal; });
  } else if (basisFilter === "open") {
    rows = rows.filter(function (row) { return row.phase === "Open"; });
  }
  var query = document.getElementById("basis-search").value.trim()
    .toUpperCase();
  if (query) {
    rows = rows.filter(function (row) {
      return row.symbol.indexOf(query) >= 0;
    });
  }
  return rows;
}

function basisExpand(row) {
  var box = el("div");
  if (row.proposal) {
    var proposal = row.proposal;
    box.appendChild(el("p", "meta", "Cost waterfall — values exactly as " +
      "the Basis backend supplied them; this page recalculates nothing."));
    var table = el("table", "waterfall");
    function wf(label, value, total) {
      var tr = el("tr", total ? "total" : "");
      tr.appendChild(el("td", null, label));
      tr.appendChild(el("td", null, value === null ? "not supplied" : value));
      table.appendChild(tr);
    }
    wf("Raw entry gap", fmtPct(proposal.entry_gap_pct));
    wf("Spot taker fee", fmtPct(proposal.spot_taker_commission_pct));
    wf("Perp taker fee", fmtPct(proposal.perp_taker_commission_pct));
    wf("Entry slippage", fmtPct(proposal.entry_slippage_pct));
    wf("Exit slippage", fmtPct(proposal.exit_slippage_pct));
    wf("Funding carry", fmtPct(proposal.carry_pct));
    wf("Exit gap assumption", fmtPct(proposal.exit_gap_pct));
    wf("Safety buffer", fmtPct(proposal.safety_buffer_pct));
    wf("Executable net edge", fmtPct(proposal.net_edge_pct), true);
    box.appendChild(table);
    box.appendChild(el("p", "meta",
      "Intended action: buy Spot, short Perp · quantity " +
      (fmtNum(proposal.quantity) || "?") + " evaluated per leg."));
    var ttl = secondsUntil(proposal.expires_utc);
    box.appendChild(el("p", "meta", ttl === null ? "Expiry unrecorded" :
      (ttl > 0 ? "Opportunity expires in " + ttl + "s" :
        "Proposal expired " + (-ttl) + "s ago")));
  } else {
    box.appendChild(el("p", "unavail",
      "Not evaluated — the watcher’s decision rows are not exposed to " +
      "this page yet. Only symbols with a current proposal show a real " +
      "net edge."));
  }
  var data = row.data || {};
  box.appendChild(el("p", "meta",
    "Exchange facts: Spot step " + (fmtNum(data.spot_step) || "?") +
    ", min " + (fmtNum(data.spot_min_notional) || "?") + " USDT · Perp step " +
    (fmtNum(data.perp_step) || "?") + ", min " +
    (fmtNum(data.perp_min_notional) || "?") + " USDT · funding " +
    (fmtPct(data.funding_rate) || "?")));
  box.appendChild(el("p", "unavail", "History collecting — no measured " +
    "price series exists yet, so there is no chart to show."));
  box.appendChild(techDrawer(row.proposal || row.data));
  return box;
}

function renderBasis() {
  if (!basisTable) return;
  var rows = basisRows();
  basisTable.update(rows);
  var stream = state.data.stream;
  var meta = document.getElementById("basis-meta");
  if (!stream || !stream.present) {
    meta.textContent = "Market data unavailable — nothing has been " +
      "delivered to this page yet.";
  } else {
    meta.textContent = rows.length + " both-venue symbols in the current " +
      "delivery · data " + (shortTime(stream.as_of || "") || "age unknown") +
      " · Edge exists only where a real proposal was evaluated.";
  }
}

/* ---------- volume tab ---------- */

var volumeFilter = "interesting";
var volumeTable = null;

function decisionBySymbol() {
  var map = {};
  var payload = state.data.decisions;
  if (payload && Array.isArray(payload.decisions)) {
    payload.decisions.forEach(function (record) {
      var sym = record.symbol ||
        (record.lifecycle_state && record.lifecycle_state.symbol);
      if (sym) map[sym] = record;
    });
  }
  return map;
}

function volumeRows() {
  var stream = state.data.stream;
  if (!stream || !Array.isArray(stream.tokens)) return [];
  var decisions = decisionBySymbol();
  var rows = [];
  stream.tokens.forEach(function (token) {
    var sel = token.selection || [];
    if (sel.indexOf("top_surge") < 0) return;
    var data = token.data || {};
    var decision = decisions[token.symbol] || null;
    var market = data.venues === "both" ? "Both" :
      (data.venues === "spot" ? "Spot" : "Perp");
    rows.push({
      key: token.symbol,
      symbol: token.symbol,
      market: market,
      route: (data.venues === "both" || data.venues === "perp") ?
        "Futures long" : "Observe only",
      price: data.mark || data.spot_ask,
      surge: data.surge_ratio,
      phase: decision ?
        (String(decision.action || "WATCH") === "WATCH" ? "Quiet" :
          String(decision.action)) : null,
      decision: decision,
      data: data
    });
  });
  if (volumeFilter === "both") {
    rows = rows.filter(function (row) { return row.market === "Both"; });
  } else if (volumeFilter === "spot") {
    rows = rows.filter(function (row) { return row.market === "Spot"; });
  } else if (volumeFilter === "perp") {
    rows = rows.filter(function (row) { return row.market === "Perp"; });
  }
  var query = document.getElementById("volume-search").value.trim()
    .toUpperCase();
  if (query) {
    rows = rows.filter(function (row) {
      return row.symbol.indexOf(query) >= 0;
    });
  }
  return rows;
}

function volumeExpand(row) {
  var box = el("div");
  if (row.route === "Observe only") {
    box.appendChild(el("p", "meta",
      "This symbol has no perpetual future, so the machine can only " +
      "observe it. It will never trade it on the current route."));
  }
  var decision = row.decision;
  if (decision) {
    box.appendChild(el("p", "meta",
      "Latest decision: " + (decision.action || "?") +
      (decision.job_reason ? " — " + decision.job_reason : "") +
      " · " + (shortTime(decision.decision_utc || "") || "")));
  } else {
    box.appendChild(el("p", "unavail",
      "Decision detail unavailable — this symbol is outside the bounded " +
      "recent-decision window the machine exposes."));
  }
  var data = row.data || {};
  box.appendChild(el("p", "meta",
    "1m quote volume " + (fmtNum(data.vol_1m_quote, 0) || "?") +
    " vs median " + (fmtNum(data.vol_med_1m, 0) || "?") +
    (data.oi ? " · open interest " + fmtNum(data.oi, 0) : "")));
  box.appendChild(el("p", "unavail", "History collecting — no measured " +
    "series exists yet."));
  box.appendChild(techDrawer(decision || row.data));
  return box;
}

function renderVolume() {
  if (!volumeTable) return;
  var rows = volumeRows();
  volumeTable.update(rows);
  var stream = state.data.stream;
  var meta = document.getElementById("volume-meta");
  if (!stream || !stream.present) {
    meta.textContent = "Market data unavailable — nothing has been " +
      "delivered to this page yet.";
  } else if (rows.length === 0) {
    meta.textContent = "No interesting movers in the current data window. " +
      "The full universe view arrives with a later data contract.";
  } else {
    meta.textContent = rows.length + " movers ranked by surge · " +
      "phases come from the machine’s own recent decisions where exposed.";
  }
}

/* ---------- quant tab ---------- */

function renderQuant() {
  var strip = state.data.strip || {};
  var states = collectPositionStates();
  var phase = executionPhase({
    positionStates: states,
    reconciling: false,
    runtimeFresh: runtimeFresh()
  });
  document.getElementById("quant-state").textContent =
    "Current state: " + phase + (ownerStopLine(strip.stop_present,
      strip.stop_jobs_present) ? " · " + ownerStopLine(strip.stop_present,
      strip.stop_jobs_present) : "");

  var root = clearTo("quant-trail");
  var events = buildEventList();
  if (events.length === 0) {
    root.appendChild(el("p", "unavail",
      "Activity detail unavailable — the machine has not recorded " +
      "trail-worthy events in the delivered data, and the structured " +
      "activity contract does not exist yet."));
  } else {
    root.appendChild(el("p", "meta", "Recent — partial. Built only from " +
      "the bounded records the machine exposes today."));
    events.slice(0, 30).forEach(function (event) {
      root.appendChild(eventLine(event));
    });
  }

  root = clearTo("quant-authority");
  root.appendChild(el("p", "unavail",
    "Unavailable — the operator contract does not exist yet. Entry " +
    "authority cannot be read, granted, or revoked from this page, and " +
    "nothing here can pause or arm the machine."));

  root = clearTo("quant-policy");
  var kv = el("dl", "kv");
  function addKv(term, value) {
    kv.appendChild(el("dt", null, term));
    kv.appendChild(el("dd", null, value));
  }
  addKv("Leverage", "1× — locked");
  addKv("First-trade ceiling", "8 USDT (Basis: per leg)");
  addKv("Later ceiling", "32 USDT — unlocks only after one clean close");
  addKv("Entries per day", "5 — locked");
  addKv("Daily loss+fees", "1.00 USDT — locked");
  addKv("Adverse stop", "2% realized incl. fees (Volume runner)");
  var fees = state.data.fees;
  addKv("Measured commissions", fees && fees.present && fees.rates ?
    (fmtPct(fees.rates.round_trip_taker_fees_pct) || "recorded") +
    " round trip" : "Unavailable");
  var gov = (state.data.strip || {}).governor || {};
  addKv("Entry lock", ownerLockLine(gov.entry_lock_present));
  root.appendChild(kv);
  root.appendChild(el("p", "meta",
    "These are code law, shown read-only. Changing any of them is a " +
    "reviewed code decision, never a dashboard action."));
}

/* ---------- journal tab ---------- */

function renderJournal() {
  var root = clearTo("journal-rows");
  var events = buildEventList();
  if (events.length === 0) {
    root.appendChild(el("p", "empty",
      "No durable trade, control, or failure records in the delivered " +
      "data yet."));
  } else {
    events.slice(0, 50).forEach(function (event) {
      var row = el("div", "jrow");
      row.appendChild(el("span", "ts",
        shortTime(event.ts) || "time unrecorded"));
      var cls = event.sev === "attention" ? "b-red" :
        (event.sev === "refused" ? "b-amber" :
          (event.sev === "trade" ? "b-green" : "b-gray"));
      row.appendChild(el("span", "badge " + cls, event.strat));
      row.appendChild(el("span", null,
        (event.sym ? event.sym + " — " : "") + event.text));
      if (event.money) row.appendChild(el("span", "money", event.money));
      root.appendChild(row);
    });
  }

  if (state.eventsLoaded) renderJournalHistory();
}

function renderJournalHistory() {
  var payload = state.data.events;
  var root = clearTo("journal-history");
  if (!payload || !payload.present || !Array.isArray(payload.events) ||
      payload.events.length === 0) {
    root.appendChild(el("p", "empty", "No archived runs."));
    return;
  }
  var runs = {};
  var order = [];
  payload.events.forEach(function (event) {
    var id = event.run_id || "(no run)";
    if (!runs[id]) { runs[id] = []; order.push(id); }
    runs[id].push(event);
  });
  order.forEach(function (id) {
    var events = runs[id];
    var verdict = null;
    events.forEach(function (event) {
      if (event.kind === "run_verdict" && event.verdict) {
        verdict = event.verdict;
      }
    });
    var row = el("div", "jrow");
    row.appendChild(el("span", "ts", shortTime(events[0].ts || "")));
    row.appendChild(el("span", "badge " +
      (verdict === "CLOSED" ? "b-green" : "b-amber"), verdict || "run"));
    row.appendChild(el("span", null,
      (events[0].symbol || "") + " — archived spot run " + id));
    root.appendChild(row);
    var drawer = techDrawer(events);
    root.appendChild(drawer);
  });
}

/* ---------- tabs + polling ---------- */

var TABS = ["overview", "basis", "volume", "quant", "journal"];

function selectTab(name) {
  if (TABS.indexOf(name) < 0) name = "overview";
  state.tab = name;
  TABS.forEach(function (tab) {
    document.getElementById("tab-" + tab)
      .setAttribute("aria-selected", tab === name ? "true" : "false");
    document.getElementById("panel-" + tab)
      .classList.toggle("on", tab === name);
  });
  if (window.location.hash !== "#" + name) {
    window.history.replaceState(null, "", "#" + name);
  }
  renderCurrent();
}

function renderCurrent() {
  renderStrip();
  if (state.tab === "overview") renderOverview();
  else if (state.tab === "basis") renderBasis();
  else if (state.tab === "volume") renderVolume();
  else if (state.tab === "quant") renderQuant();
  else if (state.tab === "journal") renderJournal();
}

function coreUrls() {
  var urls = ["strip", "position", "momentum", "failures"];
  if (state.tab === "overview") urls.push("pnl", "decisions");
  if (state.tab === "basis") urls.push("stream", "candidates");
  if (state.tab === "volume") urls.push("stream", "decisions");
  if (state.tab === "quant") urls.push("decisions", "pnl", "fees");
  if (state.tab === "journal") urls.push("decisions", "pnl");
  return urls;
}

function pollOnce() {
  var names = coreUrls();
  Promise.all(names.map(function (name) {
    return getJson(API[name]).then(function (payload) {
      return { name: name, payload: payload };
    });
  })).then(function (results) {
    results.forEach(function (result) {
      state.data[result.name] = result.payload;
    });
    state.lastGoodAt = new Date().toISOString();
    setConnection(true);
    renderCurrent();
  }).catch(function () {
    setConnection(false);
  });
  if (!state.eventsLoaded) {
    getJson(API.events).then(function (payload) {
      state.data.events = payload;
      state.eventsLoaded = true;
      if (state.tab === "journal") renderJournal();
    }).catch(function () { /* archived history stays pending */ });
  }
}

/* ---------- boot ---------- */

if (typeof document !== "undefined" && document.getElementById("gstrip")) {
  basisTable = new KeyedTable({
    tableId: "basis-table",
    rankchipId: "basis-rankchip",
    sortKey: "edge",
    expandRender: basisExpand,
    columns: [
      { key: "symbol", cls: "sym",
        render: function (r) { return { text: r.symbol }; } },
      { key: "spot", cls: "num",
        render: function (r) { return { text: fmtNum(r.spot) || "—" }; } },
      { key: "perp", cls: "num",
        render: function (r) { return { text: fmtNum(r.perp) || "—" }; } },
      { key: "gap", cls: "num", render: function (r) {
        var text = fmtPct(r.gap);
        return { text: text || "—",
          cls: Number(r.gap) > 0 ? "pos" : "neg" };
      } },
      { key: "edge", cls: "num", render: function (r) {
        if (r.edge === null) return { text: "Not evaluated", cls: "dim" };
        return { text: fmtPct(r.edge),
          cls: Number(r.edge) > 0 ? "pos" : "neg" };
      } },
      { key: "funding", cls: "num",
        render: function (r) { return { text: fmtPct(r.funding) || "—" }; } },
      { key: "planned", cls: "num", render: function (r) {
        return r.planned === null ? { text: "—", cls: "dim" } :
          { text: fmtNum(r.planned) + " /leg" };
      } },
      { key: "phase", render: function (r) {
        return { text: r.phase,
          cls: r.phase === "Open" ? "pos" :
            (r.phase === "Proposal" ? "" : "dim") };
      } }
    ]
  });

  volumeTable = new KeyedTable({
    tableId: "volume-table",
    rankchipId: "volume-rankchip",
    sortKey: "surge",
    expandRender: volumeExpand,
    columns: [
      { key: "symbol", cls: "sym",
        render: function (r) { return { text: r.symbol }; } },
      { key: "market",
        render: function (r) { return { text: r.market }; } },
      { key: "route", render: function (r) {
        return { text: r.route,
          cls: r.route === "Observe only" ? "dim" : "" };
      } },
      { key: "price", cls: "num",
        render: function (r) { return { text: fmtNum(r.price) || "—" }; } },
      { key: "surge", cls: "num", render: function (r) {
        var text = fmtNum(r.surge, 1);
        return { text: text === null ? "—" : text + "×" };
      } },
      { key: "phase", render: function (r) {
        return r.phase === null ?
          { text: "No decision", cls: "dim" } : { text: r.phase };
      } }
    ]
  });

  TABS.forEach(function (tab) {
    document.getElementById("tab-" + tab).addEventListener("click",
      function () { selectTab(tab); });
  });
  ["basis", "volume"].forEach(function (which) {
    document.getElementById(which + "-toolbar").addEventListener("click",
      function (ev) {
        var btn = ev.target.closest(".fbtn");
        if (!btn) return;
        var toolbar = ev.currentTarget;
        toolbar.querySelectorAll(".fbtn").forEach(function (node) {
          node.setAttribute("aria-pressed",
            node === btn ? "true" : "false");
        });
        if (which === "basis") basisFilter = btn.getAttribute("data-filter");
        else volumeFilter = btn.getAttribute("data-filter");
        renderCurrent();
      });
    document.getElementById(which + "-search").addEventListener("input",
      renderCurrent);
  });

  var initial = window.location.hash.replace("#", "");
  selectTab(TABS.indexOf(initial) >= 0 ? initial : "overview");
  pollOnce();
  window.setInterval(pollOnce, REFRESH_MS);
}

/* Pure helpers exported for offline tests. */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    fmtNum: fmtNum,
    fmtPct: fmtPct,
    fmtUsdt: fmtUsdt,
    ageText: ageText,
    ownerStopLine: ownerStopLine,
    ownerLockLine: ownerLockLine,
    executionPhase: executionPhase
  };
}
