"use strict";

/* Quant owner dashboard — core shell and engine.
   Owns: fetch/poll loop, connection state, global strip, tab router,
   keyed no-flicker table engine, owner-language mappings, shared event
   builder, clock. Each tab's content lives in ui/tabs/<tab>.js and
   registers itself on window.QuantTabs — one file per tab, one owner
   per file. The browser calculates no money value; missing data renders
   as Unavailable, never as zero or a sample. */

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

/* ---------- pure helpers ---------- */

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

function shortDay(iso) {
  if (typeof iso !== "string" || iso.length < 10) return "";
  return iso.slice(0, 10);
}

function secondsUntil(iso) {
  if (typeof iso !== "string") return null;
  var t = Date.parse(iso);
  if (!Number.isFinite(t)) return null;
  return Math.round((t - Date.now()) / 1000);
}

/* ---------- owner-language (contract section 4) ---------- */

function ownerStopLine(stopPresent, stopJobsPresent) {
  if (stopPresent) return "Paused — exposure management remains protected";
  if (stopJobsPresent) return "Watching only — new entries paused";
  return "";
}

function ownerLockLine(lockPresent) {
  return lockPresent ? "Another entry currently owns the account"
    : "Entry lock clear";
}

/* Execution precedence: Attention > Reconciling > Exiting > Entering >
   Holding > Evaluating > Watching > Offline. Evaluating is not provable
   from Iteration-1 sources and is never inferred from a healthy feed. */
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

function ownerPhaseForState(raw) {
  var st = String(raw || "").toUpperCase();
  if (st.indexOf("UNKNOWN") >= 0 || st.indexOf("ONE_LEG") >= 0 ||
      st.indexOf("FROZEN") >= 0) return "Attention";
  if (st.indexOf("CLOS") >= 0) return "Exiting";
  if (st.indexOf("OPENING") >= 0) return "Entering";
  if (st === "OPEN" || st.indexOf("HOLD") >= 0) return "Holding";
  return raw ? String(raw) : "Unknown";
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

function chip(label, level, sub) {
  var node = el("span", "chip");
  node.appendChild(el("span", "cdot c-" + (level || "gray")));
  node.appendChild(el("span", null, label));
  if (sub) node.appendChild(el("span", "cmuted", sub));
  return node;
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

/* ---------- derived facts shared across tabs ---------- */

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
  if (!strip || !strip.pull) return false;
  return strip.pull.level === "green" || strip.pull.level === "amber";
}

function currentPhase() {
  return executionPhase({
    positionStates: collectPositionStates(),
    reconciling: false,
    runtimeFresh: runtimeFresh()
  });
}

/* ---------- shared event builder (bounded, provable) ---------- */

function buildEventList() {
  var out = [];
  var mom = state.data.momentum;
  if (mom && mom.receipts && Array.isArray(mom.receipts.records)) {
    mom.receipts.records.forEach(function (record) {
      out.push({
        ts: record.utc || record.decision_utc || "",
        sev: "info", strat: "Volume", sym: record.symbol || "",
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
        ts: record.utc || "", sev: "trade", strat: "Volume",
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
        ts: record.utc || "", sev: "trade", strat: "Basis",
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
        ts: record.decision_utc || "", sev: "info", strat: "Volume",
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

/* ---------- global strip + sidebar chips + clock ---------- */

function setCell(id, value, cls, sub, title) {
  var cell = document.getElementById(id);
  var valueNode = cell.querySelector(".gvalue");
  valueNode.classList.remove("skel", "s-green", "s-amber", "s-red", "s-gray");
  if (cls) valueNode.classList.add(cls);
  if (valueNode.textContent !== value) {
    valueNode.textContent = value;
    valueNode.classList.remove("flash");
    void valueNode.offsetWidth;
    valueNode.classList.add("flash");
  }
  var subNode = cell.querySelector(".gsub");
  if (subNode.textContent !== (sub || "")) subNode.textContent = sub || "";
  cell.title = title || "";
}

function renderStrip() {
  var strip = state.data.strip;
  if (!strip) return;

  var phase = currentPhase();
  var phaseCls = phase === "Attention" ? "s-red" :
    (phase === "Offline" ? "s-gray" :
      (phase === "Holding" || phase === "Entering" || phase === "Exiting" ?
        "s-amber" : "s-green"));
  setCell("g-exec", phase, phase === "Watching" ? "" : phaseCls,
    ownerStopLine(strip.stop_present, strip.stop_jobs_present));

  setCell("g-auth", "Unavailable", "s-gray", "No operator contract yet",
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
    setCell("g-risk", "0 of 5", "", "No entries recorded today",
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

  var side = clearTo("side-status");
  var pullLevel = !pull.present ? "gray" :
    (pull.level === "green" ? "green" :
      (pull.level === "amber" ? "amber" : "red"));
  side.appendChild(chip("Mirror", pullLevel,
    pull.present ? ageText(pull.age_seconds) : "none"));
  side.appendChild(chip("Machine",
    strip.stop_present || strip.stop_jobs_present ? "amber" : "green",
    strip.machine || ""));
}

function tickClock() {
  var node = document.getElementById("clockpill");
  if (!node) return;
  var now = new Date();
  var pad = function (v) { return String(v).padStart(2, "0"); };
  node.textContent = pad(now.getUTCHours()) + ":" +
    pad(now.getUTCMinutes()) + ":" + pad(now.getUTCSeconds()) + " UTC";
}

/* ---------- keyed no-flicker table engine ---------- */

function KeyedTable(options) {
  this.table = document.getElementById(options.tableId);
  this.tbody = this.table.querySelector("tbody");
  this.columns = options.columns;
  this.rankchip = options.rankchipId ?
    document.getElementById(options.rankchipId) : null;
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
      if (value.node) {
        var html = value.node.outerHTML;
        if (entry["_h_" + col.key] !== html) {
          entry["_h_" + col.key] = html;
          td.textContent = "";
          td.appendChild(value.node);
        }
      } else if (td.textContent !== value.text) {
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
  var json = JSON.stringify(entry.row);
  if (entry.exprTr && entry.lastExpandJson === json) return;
  var body = this.expandRender(entry.row);
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

/* ---------- tab registry + router + polling ---------- */

var TABS = ["overview", "basis", "volume", "quant", "journal"];
window.QuantTabs = window.QuantTabs || {};

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
  var module = window.QuantTabs[state.tab];
  if (module && typeof module.render === "function") module.render();
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
      renderCurrent();
    }).catch(function () { /* archived history stays pending */ });
  }
}

/* ---------- shared surface for tab modules ---------- */

window.QuantUI = {
  state: state,
  el: el,
  clearTo: clearTo,
  chip: chip,
  techDrawer: techDrawer,
  KeyedTable: KeyedTable,
  fmtNum: fmtNum,
  fmtPct: fmtPct,
  fmtUsdt: fmtUsdt,
  ageText: ageText,
  shortTime: shortTime,
  shortDay: shortDay,
  secondsUntil: secondsUntil,
  ownerStopLine: ownerStopLine,
  ownerLockLine: ownerLockLine,
  ownerPhaseForState: ownerPhaseForState,
  executionPhase: executionPhase,
  currentPhase: currentPhase,
  exposureLines: exposureLines,
  collectPositionStates: collectPositionStates,
  buildEventList: buildEventList,
  eventLine: eventLine,
  selectTab: selectTab,
  renderCurrent: renderCurrent
};

/* ---------- boot ---------- */

if (typeof document !== "undefined") {
  document.addEventListener("DOMContentLoaded", function () {
    if (!document.getElementById("topbar")) return;
    TABS.forEach(function (tab) {
      document.getElementById("tab-" + tab).addEventListener("click",
        function () { selectTab(tab); });
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
