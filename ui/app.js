"use strict";

/* Read-only runtime screen. Every market, fee, position, and P&L value comes
   from ui/serve.py's local file-backed GET endpoints. The events endpoint is
   deliberately separate and rendered only as historical spot-run evidence. */

var STREAM_URL = "/api/stream";
var CANDIDATES_URL = "/api/candidates";
var POSITION_URL = "/api/position";
var MOMENTUM_URL = "/api/momentum";
var FEES_URL = "/api/fees";
var PNL_URL = "/api/pnl";
var EVENTS_URL = "/api/events";
var REFRESH_MS = 2000;

function valueText(value) {
  if (value === null || value === undefined || value === "") return "—";
  if (typeof value === "object") return JSON.stringify(value);
  return String(value);
}

function receiptPairs(obj) {
  if (!obj || typeof obj !== "object") return [];
  return Object.keys(obj).sort().map(function (key) {
    return [key, valueText(obj[key])];
  });
}

function fmtPairs(obj) {
  if (!obj) return "";
  return Object.keys(obj).map(function (key) {
    return key + " " + valueText(obj[key]);
  }).join(", ");
}

function eventRow(event) {
  var details = [];
  switch (event.kind) {
    case "run_start":
      if (event.balances) details.push("balances: " + fmtPairs(event.balances));
      break;
    case "order_submitted":
      if (event.side) details.push(event.side);
      if (event.client_id_last4) details.push("client id …" + event.client_id_last4);
      break;
    case "order_terminal":
      if (event.side) details.push(event.side);
      if (event.executed) details.push("executed " + event.executed);
      if (event.quote) details.push("quote " + event.quote);
      if (event.fees) details.push("fees: " + fmtPairs(event.fees));
      if (event.client_id_last4) details.push("client id …" + event.client_id_last4);
      if (event.order_id_last4) details.push("order id …" + event.order_id_last4);
      break;
    case "balance_snapshot":
      if (event.balances) details.push("balances: " + fmtPairs(event.balances));
      break;
    case "run_verdict":
      if (event.verdict) details.push(event.verdict);
      if (event.residual) details.push("residual " + event.residual);
      break;
    case "gap_read":
      if (event.symbol) details.push(event.symbol);
      break;
    default:
      details.push(JSON.stringify(event));
  }
  if (event.note) details.push(event.note);
  return {
    ts: event.ts,
    kind: event.kind,
    detail: details.join(" · "),
    run_id: event.run_id
  };
}

function groupRuns(events) {
  var order = [];
  var byId = {};
  events.forEach(function (event) {
    var id = event.run_id || "(no run_id)";
    if (!byId[id]) {
      byId[id] = {
        run_id: id,
        sha8: event.sha8,
        symbol: event.symbol,
        events: []
      };
      order.push(id);
    }
    byId[id].events.push(event);
  });
  return order.map(function (id) { return byId[id]; });
}

if (typeof document !== "undefined") {
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function clearTo(id) {
    var root = document.getElementById(id);
    root.textContent = "";
    return root;
  }

  function table(headers) {
    var result = el("table");
    var head = el("thead");
    var headRow = el("tr");
    headers.forEach(function (header) {
      headRow.appendChild(el("th", null, header));
    });
    head.appendChild(headRow);
    result.appendChild(head);
    var body = el("tbody");
    result.appendChild(body);
    return { table: result, tbody: body };
  }

  function objectTable(obj) {
    var result = table(["field", "value"]);
    receiptPairs(obj).forEach(function (pair) {
      var row = el("tr");
      row.appendChild(el("td", "kind", pair[0]));
      row.appendChild(el("td", "detail", pair[1]));
      result.tbody.appendChild(row);
    });
    return result.table;
  }

  function sourceMeta(source, present, asOf) {
    var status = present ? "present" : "absent";
    return el("p", "meta", "source: " + source + " · " + status +
      " · as of: " + (asOf || "nothing yet"));
  }

  function parseErrorMeta(root, count) {
    if (count) root.appendChild(el("p", "meta", "parse errors: " + count));
  }

  function empty(root, text) {
    root.appendChild(el("p", "empty", "nothing yet — " + text));
  }

  function couldNotLoad(id, url, error) {
    var root = clearTo(id);
    root.appendChild(el("p", "empty",
      "could not load " + url + " (" + error.message + ")"));
  }

  function getJson(url, render, targetId) {
    fetch(url)
      .then(function (response) {
        if (!response.ok) throw new Error("HTTP " + response.status);
        return response.json();
      })
      .then(render)
      .catch(function (error) { couldNotLoad(targetId, url, error); });
  }

  function renderStream(payload) {
    var root = clearTo("stream");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    root.appendChild(el("p", "meta", "append log: " + payload.log_source +
      (payload.log_present ? " · present" : " · absent")));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || payload.tokens.length === 0) {
      empty(root, "no live token state");
      return;
    }
    var columns = [
      ["symbol", function (entry) { return entry.symbol; }],
      ["selected", function (entry) { return entry.selection.join(" + "); }],
      ["venues", function (entry) { return valueText(entry.data.venues); }],
      ["spot bid", function (entry) { return valueText(entry.data.spot_bid); }],
      ["spot ask", function (entry) { return valueText(entry.data.spot_ask); }],
      ["perp bid", function (entry) { return valueText(entry.data.perp_bid); }],
      ["perp ask", function (entry) { return valueText(entry.data.perp_ask); }],
      ["mark", function (entry) { return valueText(entry.data.mark); }],
      ["vol 1m quote", function (entry) { return valueText(entry.data.vol_1m_quote); }],
      ["vol median 1m", function (entry) { return valueText(entry.data.vol_med_1m); }],
      ["surge ratio", function (entry) { return valueText(entry.data.surge_ratio); }],
      ["OI", function (entry) { return valueText(entry.data.oi); }],
      ["OI change %", function (entry) { return valueText(entry.data.oi_change_pct); }],
      ["funding rate", function (entry) { return valueText(entry.data.funding_rate); }],
      ["next funding ms", function (entry) { return valueText(entry.data.next_funding_ms); }],
      ["basis gap %", function (entry) { return valueText(entry.data.basis_gap_pct); }],
      ["freshness ms", function (entry) { return valueText(entry.data.freshness_ms); }],
      ["as of", function (entry) { return valueText(entry.as_of); }]
    ];
    var result = table(columns.map(function (column) { return column[0]; }));
    payload.tokens.forEach(function (entry) {
      var row = el("tr");
      columns.forEach(function (column) {
        row.appendChild(el("td", "detail", column[1](entry)));
      });
      result.tbody.appendChild(row);
    });
    root.appendChild(result.table);
  }

  function renderCandidates(payload) {
    var root = clearTo("candidates");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || payload.candidates.length === 0) {
      empty(root, "no valid unexpired proposals");
      return;
    }
    payload.candidates.forEach(function (entry) {
      var box = el("div", "run");
      box.appendChild(el("p", "run-head",
        entry.proposal.symbol + " · proposal " + entry.proposal.proposal_id));
      box.appendChild(el("p", "meta", entry.file + " · as of: " +
        valueText(entry.as_of)));
      box.appendChild(objectTable(entry.proposal));
      root.appendChild(box);
    });
  }

  function renderPosition(payload) {
    var root = clearTo("position");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || !payload.position) {
      empty(root, "no basis position state");
      return;
    }
    root.appendChild(objectTable(payload.position));
    root.appendChild(el("p", "run-head", "supervision"));
    if (payload.supervision) {
      root.appendChild(objectTable(payload.supervision));
    } else {
      empty(root, "no basis supervision record");
    }
  }

  function collectionEmpty(value) {
    if (Array.isArray(value)) return value.length === 0;
    return !value || Object.keys(value).length === 0;
  }

  function renderMomentum(payload) {
    var root = clearTo("momentum");
    var state = payload.state;
    root.appendChild(sourceMeta(state.source, state.present, state.as_of));
    parseErrorMeta(root, state.parse_errors);
    if (!state.present || collectionEmpty(state.positions)) {
      empty(root, "no momentum paper positions");
    } else if (Array.isArray(state.positions)) {
      state.positions.forEach(function (position, index) {
        var box = el("div", "run");
        box.appendChild(el("p", "run-head",
          valueText(position.symbol || ("position " + (index + 1)))));
        box.appendChild(objectTable(position));
        root.appendChild(box);
      });
    } else {
      Object.keys(state.positions).sort().forEach(function (symbol) {
        var box = el("div", "run");
        box.appendChild(el("p", "run-head", symbol));
        box.appendChild(objectTable(state.positions[symbol]));
        root.appendChild(box);
      });
    }

    var receipts = payload.receipts;
    root.appendChild(el("p", "run-head", "recent momentum paper receipts"));
    root.appendChild(sourceMeta(receipts.source, receipts.present, receipts.as_of));
    parseErrorMeta(root, receipts.parse_errors);
    if (!receipts.present || receipts.records.length === 0) {
      empty(root, "no momentum paper receipts");
    } else {
      receipts.records.forEach(function (record) {
        var box = el("div", "run");
        box.appendChild(el("p", "run-head",
          valueText(record.ts) + " · " + valueText(record.kind) + " · " +
          valueText(record.symbol)));
        box.appendChild(objectTable(record));
        root.appendChild(box);
      });
    }
  }

  function renderFees(payload) {
    var root = clearTo("fees");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || !payload.rates) {
      empty(root, "no commission rates recorded");
      return;
    }
    root.appendChild(objectTable(payload.rates));
  }

  function renderPnl(payload) {
    var root = clearTo("pnl");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || payload.records.length === 0) {
      empty(root, "no realized P&L block in a CLOSED or reconcile receipt");
      return;
    }
    payload.records.forEach(function (record) {
      var box = el("div", "run");
      box.appendChild(el("p", "run-head",
        valueText(record.symbol) + " · " + valueText(record.verdict) +
        " · run " + valueText(record.run_id)));
      box.appendChild(el("p", "meta", record.file + " · as of: " +
        valueText(record.as_of) + " · partial fees: " +
        valueText(record.partial_fees)));
      box.appendChild(objectTable(record.pnl));
      if (Object.keys(record.partial_fee_flags).length) {
        box.appendChild(el("p", "meta", "fee flags: " +
          JSON.stringify(record.partial_fee_flags)));
      }
      root.appendChild(box);
    });
  }

  function renderEvents(payload) {
    var root = clearTo("runs");
    root.appendChild(sourceMeta(payload.source, payload.present, payload.as_of));
    parseErrorMeta(root, payload.parse_errors);
    if (!payload.present || payload.events.length === 0) {
      empty(root, "no historical spot events recorded");
      return;
    }
    groupRuns(payload.events).forEach(function (run) {
      var box = el("div", "run");
      var heading = el("p", "run-head");
      heading.appendChild(el("span", null, "run " + run.run_id));
      heading.appendChild(el("span", "muted",
        " · " + (run.symbol || "") + " · sha8 " + (run.sha8 || "")));
      box.appendChild(heading);
      var result = table(["ts (UTC)", "kind", "detail"]);
      run.events.forEach(function (event) {
        var display = eventRow(event);
        var row = el("tr");
        row.appendChild(el("td", "ts", valueText(display.ts)));
        row.appendChild(el("td", "kind", valueText(display.kind)));
        row.appendChild(el("td", "detail", display.detail));
        result.tbody.appendChild(row);
      });
      box.appendChild(result.table);
      root.appendChild(box);
    });
  }

  function refreshRuntime() {
    getJson(STREAM_URL, renderStream, "stream");
    getJson(CANDIDATES_URL, renderCandidates, "candidates");
    getJson(POSITION_URL, renderPosition, "position");
    getJson(MOMENTUM_URL, renderMomentum, "momentum");
    getJson(FEES_URL, renderFees, "fees");
    getJson(PNL_URL, renderPnl, "pnl");
  }

  document.getElementById("refresh-runtime").addEventListener(
    "click", refreshRuntime);
  refreshRuntime();
  getJson(EVENTS_URL, renderEvents, "runs");
  window.setInterval(refreshRuntime, REFRESH_MS);
}

if (typeof module !== "undefined" && module.exports) {
  module.exports = {
    valueText: valueText,
    receiptPairs: receiptPairs,
    eventRow: eventRow,
    groupRuns: groupRuns
  };
}
