"use strict";

/* quant read-only screen.
   Pure functions first (also loadable in Node for headless verification),
   DOM wiring at the bottom, guarded so this file needs no build step. */

var PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT";
var FIXTURE_PATH = "fixtures/events.jsonl";

/* ---------- pure: contract-v1 rendering ---------- */

function fmtPairs(obj) {
  if (!obj) return "";
  return Object.keys(obj).map(function (k) { return k + " " + obj[k]; }).join(", ");
}

/* One contract-v1 event -> one factual display row. No derived opinions:
   every value shown is a field of the event, verbatim. */
function eventRow(ev) {
  var d = [];
  switch (ev.kind) {
    case "run_start":
      if (ev.balances) d.push("balances: " + fmtPairs(ev.balances));
      break;
    case "order_submitted":
      if (ev.side) d.push(ev.side);
      if (ev.client_id_last4) d.push("client id …" + ev.client_id_last4);
      break;
    case "order_terminal":
      if (ev.side) d.push(ev.side);
      if (ev.executed) d.push("executed " + ev.executed);
      if (ev.quote) d.push("quote " + ev.quote);
      if (ev.fees) d.push("fees: " + fmtPairs(ev.fees));
      if (ev.client_id_last4) d.push("client id …" + ev.client_id_last4);
      if (ev.order_id_last4) d.push("order id …" + ev.order_id_last4);
      break;
    case "balance_snapshot":
      if (ev.balances) d.push("balances: " + fmtPairs(ev.balances));
      break;
    case "run_verdict":
      if (ev.verdict) d.push(ev.verdict);
      if (ev.residual) d.push("residual " + ev.residual);
      break;
    case "gap_read":
      if (ev.symbol) d.push(ev.symbol);
      break;
    default:
      /* unknown kind: still factual — show the raw record */
      d.push(JSON.stringify(ev));
  }
  if (ev.note) d.push(ev.note);
  return { ts: ev.ts, kind: ev.kind, detail: d.join(" · "), run_id: ev.run_id };
}

/* JSONL text -> array of event objects, line order preserved. */
function parseJsonl(text) {
  return text.split("\n")
    .map(function (l) { return l.trim(); })
    .filter(function (l) { return l.length > 0; })
    .map(function (l) { return JSON.parse(l); });
}

/* Group events by run_id, preserving first-appearance order. */
function groupRuns(events) {
  var order = [];
  var byId = {};
  events.forEach(function (ev) {
    var id = ev.run_id || "(no run_id)";
    if (!byId[id]) { byId[id] = { run_id: id, sha8: ev.sha8, symbol: ev.symbol, events: [] }; order.push(id); }
    byId[id].events.push(ev);
  });
  return order.map(function (id) { return byId[id]; });
}

/* ---------- DOM wiring (browser only) ---------- */

if (typeof document !== "undefined") {

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function renderRuns(events) {
    var root = document.getElementById("runs");
    root.textContent = "";
    if (events.length === 0) {
      root.appendChild(el("p", "empty", "nothing yet — no events recorded"));
      return;
    }
    groupRuns(events).forEach(function (run) {
      var box = el("div", "run");
      var head = el("p", "run-head");
      head.appendChild(el("span", null, "run " + run.run_id));
      head.appendChild(el("span", "muted",
        "  · " + (run.symbol || "") + " · sha8 " + (run.sha8 || "")));
      box.appendChild(head);

      var table = el("table");
      var thead = el("thead");
      var hr = el("tr");
      ["ts (UTC)", "kind", "detail"].forEach(function (h) { hr.appendChild(el("th", null, h)); });
      thead.appendChild(hr);
      table.appendChild(thead);

      var tbody = el("tbody");
      run.events.forEach(function (ev) {
        var row = eventRow(ev);
        var tr = el("tr");
        tr.setAttribute("data-kind", row.kind);
        var tds = [["ts", row.ts], ["kind", row.kind], ["detail", row.detail]];
        tds.forEach(function (p) { tr.appendChild(el("td", p[0], p[1])); });
        tbody.appendChild(tr);
      });
      table.appendChild(tbody);
      box.appendChild(table);
      root.appendChild(box);
    });
  }

  function loadFixture() {
    fetch(FIXTURE_PATH)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.text();
      })
      .then(function (text) { renderRuns(parseJsonl(text)); })
      .catch(function (err) {
        var root = document.getElementById("runs");
        root.textContent = "";
        root.appendChild(el("p", "empty",
          "could not load " + FIXTURE_PATH + " (" + err.message + ")"));
      });
  }

  function fetchPrice() {
    var btn = document.getElementById("refresh");
    var priceEl = document.getElementById("price");
    var unitEl = document.getElementById("price-unit");
    var metaEl = document.getElementById("price-meta");
    btn.disabled = true;
    fetch(PRICE_URL)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(function (data) {
        priceEl.textContent = data.price;
        unitEl.textContent = "USDT · " + data.symbol;
        metaEl.textContent = "fetched at " + new Date().toISOString();
      })
      .catch(function (err) {
        priceEl.textContent = "—";
        unitEl.textContent = "";
        metaEl.textContent = "fetch failed at " + new Date().toISOString() +
          " (" + err.message + ")";
      })
      .then(function () { btn.disabled = false; });
  }

  document.getElementById("refresh").addEventListener("click", fetchPrice);
  loadFixture();
  fetchPrice();
}

/* ---------- Node export for headless verification ---------- */
if (typeof module !== "undefined" && module.exports) {
  module.exports = { eventRow: eventRow, parseJsonl: parseJsonl, groupRuns: groupRuns };
}
