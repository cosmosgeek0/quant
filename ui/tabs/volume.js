"use strict";

/* Volume tab — interesting movers. Owned by the Volume lane.
   Sources: stream (selection contains top_surge), decisions (bounded
   recent window), momentum. Observation is distinguished from execution
   eligibility: Spot-only symbols are Observe only, never tradeable. */

(function () {
  var UI = window.QuantUI;
  var table = null;
  var filter = "interesting";

  function init() {
    var root = document.getElementById("root-volume");
    root.textContent = "";
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, "Volume — interesting movers"));

    var toolbar = UI.el("div", "toolbar");
    toolbar.id = "volume-toolbar";
    var search = document.createElement("input");
    search.type = "search";
    search.id = "volume-search";
    search.placeholder = "symbol";
    search.setAttribute("aria-label", "Search symbol");
    toolbar.appendChild(search);
    [["interesting", "Interesting"], ["both", "Both venues"],
     ["spot", "Spot"], ["perp", "Perp"]].forEach(function (pair, index) {
      var btn = UI.el("button", "fbtn", pair[1]);
      btn.setAttribute("data-filter", pair[0]);
      btn.setAttribute("aria-pressed", index === 0 ? "true" : "false");
      toolbar.appendChild(btn);
    });
    block.appendChild(toolbar);

    var wrap = UI.el("div", "tablewrap");
    var tableNode = document.createElement("table");
    tableNode.className = "data";
    tableNode.id = "volume-table";
    tableNode.innerHTML =
      "<thead><tr>" +
      '<th data-sort="symbol">Asset</th>' +
      '<th data-sort="market">Market</th>' +
      '<th data-sort="route">Route</th>' +
      '<th data-sort="price">Price</th>' +
      '<th data-sort="surge" class="sorted">Surge</th>' +
      '<th data-sort="phase">Phase</th>' +
      "</tr></thead><tbody></tbody>";
    wrap.appendChild(tableNode);
    block.appendChild(wrap);

    var chipRow = UI.el("p", "rankchip");
    chipRow.id = "volume-rankchip";
    chipRow.appendChild(document.createTextNode(
      "Ranks changed while inspecting. "));
    chipRow.appendChild(UI.el("button", null, "Apply new order"));
    block.appendChild(chipRow);
    var meta = UI.el("p", "meta");
    meta.id = "volume-meta";
    block.appendChild(meta);
    root.appendChild(block);

    toolbar.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".fbtn");
      if (!btn) return;
      toolbar.querySelectorAll(".fbtn").forEach(function (node) {
        node.setAttribute("aria-pressed", node === btn ? "true" : "false");
      });
      filter = btn.getAttribute("data-filter");
      render();
    });
    search.addEventListener("input", render);

    table = new UI.KeyedTable({
      tableId: "volume-table",
      rankchipId: "volume-rankchip",
      sortKey: "surge",
      expandRender: expand,
      columns: [
        { key: "symbol", cls: "sym",
          render: function (r) { return { text: r.symbol }; } },
        { key: "market",
          render: function (r) { return { text: r.market }; } },
        { key: "route", render: function (r) {
          return { text: r.route,
            cls: r.route === "Observe only" ? "dim" : "" };
        } },
        { key: "price", cls: "num", render: function (r) {
          return { text: UI.fmtNum(r.price) || "—" };
        } },
        { key: "surge", cls: "num", render: function (r) {
          var text = UI.fmtNum(r.surge, 1);
          return { text: text === null ? "—" : text + "×" };
        } },
        { key: "phase", render: function (r) {
          return r.phase === null ?
            { text: "No decision", cls: "dim" } : { text: r.phase };
        } }
      ]
    });
  }

  function decisionsBySymbol() {
    var map = {};
    var payload = UI.state.data.decisions;
    if (payload && Array.isArray(payload.decisions)) {
      payload.decisions.forEach(function (record) {
        var sym = record.symbol ||
          (record.lifecycle_state && record.lifecycle_state.symbol);
        if (sym) map[sym] = record;
      });
    }
    return map;
  }

  function rows() {
    var stream = UI.state.data.stream;
    if (!stream || !Array.isArray(stream.tokens)) return [];
    var decisions = decisionsBySymbol();
    var out = [];
    stream.tokens.forEach(function (token) {
      var sel = token.selection || [];
      if (sel.indexOf("top_surge") < 0) return;
      var data = token.data || {};
      var decision = decisions[token.symbol] || null;
      var market = data.venues === "both" ? "Both" :
        (data.venues === "spot" ? "Spot" : "Perp");
      out.push({
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
    if (filter === "both") {
      out = out.filter(function (row) { return row.market === "Both"; });
    } else if (filter === "spot") {
      out = out.filter(function (row) { return row.market === "Spot"; });
    } else if (filter === "perp") {
      out = out.filter(function (row) { return row.market === "Perp"; });
    }
    var query = document.getElementById("volume-search").value.trim()
      .toUpperCase();
    if (query) {
      out = out.filter(function (row) {
        return row.symbol.indexOf(query) >= 0;
      });
    }
    return out;
  }

  function expand(row) {
    var box = UI.el("div");
    if (row.route === "Observe only") {
      box.appendChild(UI.el("p", "meta",
        "This symbol has no perpetual future, so the machine can only " +
        "observe it. It will never trade it on the current route."));
    }
    var decision = row.decision;
    if (decision) {
      box.appendChild(UI.el("p", "meta",
        "Latest decision: " + (decision.action || "?") +
        (decision.job_reason ? " — " + decision.job_reason : "") +
        " · " + (UI.shortTime(decision.decision_utc || "") || "")));
    } else {
      box.appendChild(UI.el("p", "unavail",
        "Decision detail unavailable — this symbol is outside the " +
        "bounded recent-decision window the machine exposes."));
    }
    var data = row.data || {};
    box.appendChild(UI.el("p", "meta",
      "1m quote volume " + (UI.fmtNum(data.vol_1m_quote, 0) || "?") +
      " vs median " + (UI.fmtNum(data.vol_med_1m, 0) || "?") +
      (data.oi ? " · open interest " + UI.fmtNum(data.oi, 0) : "")));
    box.appendChild(UI.el("p", "unavail", "History collecting — no " +
      "measured series exists yet."));
    box.appendChild(UI.techDrawer(decision || row.data));
    return box;
  }

  function render() {
    if (!table) return;
    var list = rows();
    table.update(list);
    var stream = UI.state.data.stream;
    var meta = document.getElementById("volume-meta");
    if (!stream) {
      meta.textContent = "Loading market data…";
    } else if (!stream.present) {
      meta.textContent = "Market data unavailable — nothing has been " +
        "delivered to this page yet.";
    } else if (list.length === 0) {
      meta.textContent = "No interesting movers in the current data " +
        "window. The full universe view arrives with a later data " +
        "contract.";
    } else {
      meta.textContent = list.length + " movers ranked by surge · phases " +
        "come from the machine’s own recent decisions where exposed.";
    }
  }

  window.QuantTabs.volume = {
    needs: ["stream", "decisions"],
    init: init,
    render: render
  };
})();
