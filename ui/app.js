"use strict";

/* quant read-only screen, served runtime.
   Every section is fetched over HTTP from ui/serve.py's read-only GET
   endpoints — no file:// path, no fixture-only assumption. Absent sources
   are shown as honest "nothing yet" states, exactly as the server reports
   them. Pure functions first (also loadable in Node for headless
   verification), DOM wiring at the bottom. */

var PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=BTCUSDT";
var EVENTS_URL = "/api/events";
var BASIS_URL = "/api/basis";
var FUTURES_URL = "/api/futures";
var VOLUME_URL = "/api/volume";

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

/* One volume paper receipt record -> one factual display row, verbatim fields. */
function paperReceiptRow(rec) {
  var d = [];
  if (rec.side) d.push(rec.side);
  if (rec.qty) d.push("qty " + rec.qty);
  if (rec.price) d.push("price " + rec.price);
  if (rec.paper_cost) d.push("paper cost " + rec.paper_cost);
  if (rec.paper_proceeds) d.push("paper proceeds " + rec.paper_proceeds);
  if (rec.paper_pnl_quote) d.push("paper pnl " + rec.paper_pnl_quote);
  if (rec.exit_reason) d.push("exit " + rec.exit_reason);
  if (rec.trail_pct) d.push("trail " + rec.trail_pct);
  if (rec.trail_stop_price) d.push("trail stop " + rec.trail_stop_price);
  if (rec.peak_price) d.push("peak " + rec.peak_price);
  if (rec.paper === true) d.push("paper: true");
  if (rec.note) d.push(rec.note);
  return { ts: rec.ts, kind: rec.kind, detail: d.join(" · "), run_id: rec.run_id };
}

/* Any receipt/state object -> [key, value-as-string] pairs, key-sorted,
   values verbatim (nested objects shown as their JSON). */
function receiptPairs(obj) {
  if (!obj || typeof obj !== "object") return [];
  return Object.keys(obj).sort().map(function (k) {
    var v = obj[k];
    var s = (v !== null && typeof v === "object") ? JSON.stringify(v) : String(v);
    return [k, s];
  });
}

/* ---------- DOM wiring (browser only) ---------- */

if (typeof document !== "undefined") {

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) n.className = cls;
    if (text !== undefined) n.textContent = text;
    return n;
  }

  function table(headers) {
    var t = el("table");
    var thead = el("thead");
    var hr = el("tr");
    headers.forEach(function (h) { hr.appendChild(el("th", null, h)); });
    thead.appendChild(hr);
    t.appendChild(thead);
    var tbody = el("tbody");
    t.appendChild(tbody);
    return { table: t, tbody: tbody };
  }

  function rowTr(row) {
    var tr = el("tr");
    tr.setAttribute("data-kind", row.kind);
    [["ts", row.ts], ["kind", row.kind], ["detail", row.detail]].forEach(function (p) {
      tr.appendChild(el("td", p[0], p[1]));
    });
    return tr;
  }

  function sourceMeta(source, present) {
    return el("p", "meta", "source: " + source + (present ? "" : " (absent)"));
  }

  function clearTo(id) {
    var root = document.getElementById(id);
    root.textContent = "";
    return root;
  }

  function couldNotLoad(id, url, err) {
    var root = clearTo(id);
    root.appendChild(el("p", "empty",
      "could not load " + url + " (" + err.message + ")"));
  }

  function getJson(url, render, targetId) {
    fetch(url)
      .then(function (r) {
        if (!r.ok) throw new Error("HTTP " + r.status);
        return r.json();
      })
      .then(render)
      .catch(function (err) { couldNotLoad(targetId, url, err); });
  }

  /* -- spot: contract-v1 events ------------------------------------- */
  function renderEvents(payload) {
    var root = clearTo("runs");
    root.appendChild(sourceMeta(payload.source, payload.present));
    if (!payload.present || payload.count === 0) {
      root.appendChild(el("p", "empty", "nothing yet — no events recorded"));
      return;
    }
    groupRuns(payload.events).forEach(function (run) {
      var box = el("div", "run");
      var head = el("p", "run-head");
      head.appendChild(el("span", null, "run " + run.run_id));
      head.appendChild(el("span", "muted",
        "  · " + (run.symbol || "") + " · sha8 " + (run.sha8 || "")));
      box.appendChild(head);
      var t = table(["ts (UTC)", "kind", "detail"]);
      run.events.forEach(function (ev) { t.tbody.appendChild(rowTr(eventRow(ev))); });
      box.appendChild(t.table);
      root.appendChild(box);
    });
  }

  /* -- basis: recorded basis_read output ------------------------------ */
  function renderBasis(payload) {
    var root = clearTo("basis");
    root.appendChild(sourceMeta(payload.source, payload.present));
    if (!payload.present || payload.rows.length === 0) {
      root.appendChild(el("p", "empty", "nothing yet — no basis read recorded"));
      return;
    }
    payload.formulas.forEach(function (f) { root.appendChild(el("p", "meta", f)); });
    var t = table(payload.columns);
    payload.rows.forEach(function (row) {
      var tr = el("tr");
      payload.columns.forEach(function (c) {
        tr.appendChild(el("td", "detail", row[c]));
      });
      t.tbody.appendChild(tr);
    });
    root.appendChild(t.table);
  }

  /* -- futures: receipts from the receipt dir ------------------------- */
  function renderFutures(payload) {
    var root = clearTo("futures");
    root.appendChild(sourceMeta(payload.source, payload.present));
    if (!payload.present || payload.count === 0) {
      root.appendChild(el("p", "empty", "nothing yet — no futures receipts recorded"));
      return;
    }
    payload.receipts.forEach(function (entry) {
      var box = el("div", "run");
      box.appendChild(el("p", "run-head", entry.file));
      if (!entry.receipt) {
        box.appendChild(el("p", "empty",
          "unreadable receipt (" + (entry.error || "unknown") + ")"));
      } else {
        var t = table(["field", "value"]);
        receiptPairs(entry.receipt).forEach(function (pair) {
          var tr = el("tr");
          tr.appendChild(el("td", "kind", pair[0]));
          tr.appendChild(el("td", "detail", pair[1]));
          t.tbody.appendChild(tr);
        });
        box.appendChild(t.table);
      }
      root.appendChild(box);
    });
  }

  /* -- volume: paper receipts + state --------------------------------- */
  function renderVolume(payload) {
    var root = clearTo("volume");
    var receipts = payload.receipts;
    root.appendChild(sourceMeta(receipts.source, receipts.present));
    if (!receipts.present || receipts.count === 0) {
      root.appendChild(el("p", "empty", "nothing yet — no volume paper receipts"));
    } else {
      var t = table(["ts (UTC)", "kind", "detail"]);
      receipts.records.forEach(function (rec) {
        t.tbody.appendChild(rowTr(paperReceiptRow(rec)));
      });
      root.appendChild(t.table);
    }
    var state = payload.state;
    if (state.present && state.state) {
      root.appendChild(el("p", "run-head", "volume state"));
      var st = table(["field", "value"]);
      receiptPairs(state.state).forEach(function (pair) {
        var tr = el("tr");
        tr.appendChild(el("td", "kind", pair[0]));
        tr.appendChild(el("td", "detail", pair[1]));
        st.tbody.appendChild(tr);
      });
      root.appendChild(st.table);
    } else {
      root.appendChild(el("p", "meta",
        "no volume state file (" + state.source + " absent)"));
    }
  }

  /* -- live price: the one control, unchanged public endpoint --------- */
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
  getJson(EVENTS_URL, renderEvents, "runs");
  getJson(BASIS_URL, renderBasis, "basis");
  getJson(FUTURES_URL, renderFutures, "futures");
  getJson(VOLUME_URL, renderVolume, "volume");
  fetchPrice();
}

/* ---------- Node export for headless verification ---------- */
if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    eventRow: eventRow,
    parseJsonl: parseJsonl,
    groupRuns: groupRuns,
    paperReceiptRow: paperReceiptRow,
    receiptPairs: receiptPairs
  };
}
