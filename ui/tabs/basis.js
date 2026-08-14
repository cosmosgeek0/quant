"use strict";

/* Basis tab — both-venue opportunity product. Owned by the Basis lane.
   Sources: stream (selection contains basis_gap, venues both), candidates
   (real proposals with the full equation), position (Open state).
   Honesty: Edge exists only where a real proposal was evaluated; charts
   say History collecting; the browser never recalculates the kernel. */

(function () {
  var UI = window.QuantUI;
  var table = null;
  var filter = "all";

  function init() {
    var root = document.getElementById("root-basis");
    root.textContent = "";
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, "Basis — both-venue opportunities"));

    var toolbar = UI.el("div", "toolbar");
    toolbar.id = "basis-toolbar";
    var search = document.createElement("input");
    search.type = "search";
    search.id = "basis-search";
    search.placeholder = "symbol";
    search.setAttribute("aria-label", "Search symbol");
    toolbar.appendChild(search);
    [["all", "All"], ["edge", "Positive edge"], ["proposal", "Proposal"],
     ["open", "Open"]].forEach(function (pair, index) {
      var btn = UI.el("button", "fbtn", pair[1]);
      btn.setAttribute("data-filter", pair[0]);
      btn.setAttribute("aria-pressed", index === 0 ? "true" : "false");
      toolbar.appendChild(btn);
    });
    block.appendChild(toolbar);

    var wrap = UI.el("div", "tablewrap");
    var tableNode = document.createElement("table");
    tableNode.className = "data";
    tableNode.id = "basis-table";
    tableNode.innerHTML =
      "<thead><tr>" +
      '<th data-sort="symbol">Asset</th>' +
      '<th data-sort="spot">Spot</th>' +
      '<th data-sort="perp">Perp</th>' +
      '<th data-sort="gap">Gap</th>' +
      '<th data-sort="edge" class="sorted">Edge</th>' +
      '<th data-sort="funding">Funding</th>' +
      '<th data-sort="planned">Planned</th>' +
      '<th data-sort="phase">Phase</th>' +
      "</tr></thead><tbody></tbody>";
    wrap.appendChild(tableNode);
    block.appendChild(wrap);

    var chipRow = UI.el("p", "rankchip");
    chipRow.id = "basis-rankchip";
    chipRow.appendChild(document.createTextNode(
      "Ranks changed while inspecting. "));
    chipRow.appendChild(UI.el("button", null, "Apply new order"));
    block.appendChild(chipRow);
    var meta = UI.el("p", "meta");
    meta.id = "basis-meta";
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
      tableId: "basis-table",
      rankchipId: "basis-rankchip",
      sortKey: "edge",
      expandRender: expand,
      columns: [
        { key: "symbol", cls: "sym",
          render: function (r) { return { text: r.symbol }; } },
        { key: "spot", cls: "num", render: function (r) {
          return { text: UI.fmtNum(r.spot) || "—" };
        } },
        { key: "perp", cls: "num", render: function (r) {
          return { text: UI.fmtNum(r.perp) || "—" };
        } },
        { key: "gap", cls: "num", render: function (r) {
          var text = UI.fmtPct(r.gap);
          return { text: text || "—",
            cls: Number(r.gap) > 0 ? "pos" : "neg" };
        } },
        { key: "edge", cls: "num", render: function (r) {
          if (r.edge === null) return { text: "Not evaluated", cls: "dim" };
          return { text: UI.fmtPct(r.edge),
            cls: Number(r.edge) > 0 ? "pos" : "neg" };
        } },
        { key: "funding", cls: "num", render: function (r) {
          return { text: UI.fmtPct(r.funding) || "—" };
        } },
        { key: "planned", cls: "num", render: function (r) {
          return r.planned === null ? { text: "—", cls: "dim" } :
            { text: UI.fmtNum(r.planned) + " /leg" };
        } },
        { key: "phase", render: function (r) {
          return { text: r.phase,
            cls: r.phase === "Open" ? "pos" :
              (r.phase === "Proposal" ? "" : "dim") };
        } }
      ]
    });
  }

  function proposalsBySymbol() {
    var map = {};
    var payload = UI.state.data.candidates;
    if (payload && Array.isArray(payload.candidates)) {
      payload.candidates.forEach(function (candidate) {
        var record = candidate.proposal || candidate;
        if (record && record.symbol) map[record.symbol] = record;
      });
    }
    return map;
  }

  function rows() {
    var stream = UI.state.data.stream;
    if (!stream || !Array.isArray(stream.tokens)) return [];
    var proposals = proposalsBySymbol();
    var pos = UI.state.data.position;
    var openSymbol = pos && pos.position && pos.position.symbol;
    var out = [];
    stream.tokens.forEach(function (token) {
      var sel = token.selection || [];
      if (sel.indexOf("basis_gap") < 0) return;
      var data = token.data || {};
      if (data.venues !== "both") return;
      if (data.basis_gap_pct === null ||
          data.basis_gap_pct === undefined) return;
      var proposal = proposals[token.symbol] || null;
      out.push({
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
    if (filter === "edge") {
      out = out.filter(function (row) {
        return row.edge !== null && Number(row.edge) > 0;
      });
    } else if (filter === "proposal") {
      out = out.filter(function (row) { return row.proposal; });
    } else if (filter === "open") {
      out = out.filter(function (row) { return row.phase === "Open"; });
    }
    var query = document.getElementById("basis-search").value.trim()
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
    if (row.proposal) {
      var proposal = row.proposal;
      box.appendChild(UI.el("p", "meta", "Cost waterfall — values exactly " +
        "as the Basis backend supplied them; this page recalculates " +
        "nothing."));
      var waterfall = document.createElement("table");
      waterfall.className = "waterfall";
      function wf(label, value, total) {
        var tr = UI.el("tr", total ? "total" : "");
        tr.appendChild(UI.el("td", null, label));
        tr.appendChild(UI.el("td", null,
          value === null ? "not supplied" : value));
        waterfall.appendChild(tr);
      }
      wf("Raw entry gap", UI.fmtPct(proposal.entry_gap_pct));
      wf("Spot taker fee", UI.fmtPct(proposal.spot_taker_commission_pct));
      wf("Perp taker fee", UI.fmtPct(proposal.perp_taker_commission_pct));
      wf("Entry slippage", UI.fmtPct(proposal.entry_slippage_pct));
      wf("Exit slippage", UI.fmtPct(proposal.exit_slippage_pct));
      wf("Funding carry", UI.fmtPct(proposal.carry_pct));
      wf("Exit gap assumption", UI.fmtPct(proposal.exit_gap_pct));
      wf("Safety buffer", UI.fmtPct(proposal.safety_buffer_pct));
      wf("Executable net edge", UI.fmtPct(proposal.net_edge_pct), true);
      box.appendChild(waterfall);
      box.appendChild(UI.el("p", "meta",
        "Intended action: buy Spot, short Perp · quantity " +
        (UI.fmtNum(proposal.quantity) || "?") + " evaluated per leg."));
      var ttl = UI.secondsUntil(proposal.expires_utc);
      box.appendChild(UI.el("p", "meta", ttl === null ?
        "Expiry unrecorded" : (ttl > 0 ?
          "Opportunity expires in " + ttl + "s" :
          "Proposal expired " + (-ttl) + "s ago")));
    } else {
      box.appendChild(UI.el("p", "unavail",
        "Not evaluated — the watcher’s decision rows are not exposed to " +
        "this page yet. Only symbols with a current proposal show a real " +
        "net edge."));
    }
    var data = row.data || {};
    box.appendChild(UI.el("p", "meta",
      "Exchange facts: Spot step " + (UI.fmtNum(data.spot_step) || "?") +
      ", min " + (UI.fmtNum(data.spot_min_notional) || "?") +
      " USDT · Perp step " + (UI.fmtNum(data.perp_step) || "?") +
      ", min " + (UI.fmtNum(data.perp_min_notional) || "?") +
      " USDT · funding " + (UI.fmtPct(data.funding_rate) || "?")));
    box.appendChild(UI.el("p", "unavail", "History collecting — no " +
      "measured price series exists yet, so there is no chart to show."));
    box.appendChild(UI.techDrawer(row.proposal || row.data));
    return box;
  }

  function render() {
    if (!table) return;
    var list = rows();
    table.update(list);
    var stream = UI.state.data.stream;
    var meta = document.getElementById("basis-meta");
    if (!stream) {
      meta.textContent = "Loading market data…";
    } else if (!stream.present) {
      meta.textContent = "Market data unavailable — nothing has been " +
        "delivered to this page yet.";
    } else {
      meta.textContent = list.length + " both-venue symbols in the " +
        "current delivery · data " +
        (UI.shortTime(stream.as_of || "") || "age unknown") +
        " · Edge exists only where a real proposal was evaluated.";
    }
  }

  window.QuantTabs.basis = {
    needs: ["stream", "candidates"],
    init: init,
    render: render
  };
})();
