"use strict";

/* Basis — both-venue opportunities (contract §5.2, iteration-1 truth).
   Rows come from /api/stream rows whose selection includes basis_gap;
   Edge exists only where the backend evaluated a proposal (via
   /api/candidates); everything else says Not evaluated. The browser
   never recalculates the equation. Charts say History collecting. */

(function () {
  var U = window.QuantUI;
  var view = { filter: "all" };
  var tape = null, host = null;

  function streamBasisRows() {
    var stream = U.state.data.stream;
    var records = (stream && Array.isArray(stream.records)) ?
      stream.records : [];
    return records.filter(function (record) {
      var sel = Array.isArray(record.selection) ? record.selection : [];
      return sel.indexOf("basis_gap") >= 0;
    });
  }
  function candidateFor(symbol) {
    var c = U.state.data.candidates;
    var list = (c && Array.isArray(c.candidates)) ? c.candidates :
      ((c && Array.isArray(c.records)) ? c.records : []);
    for (var i = 0; i < list.length; i++) {
      if ((list[i].symbol || "") === symbol) return list[i];
    }
    return null;
  }
  function openSymbol() {
    var pos = U.state.data.position;
    return (pos && pos.position && pos.position.symbol) || null;
  }

  function buildRows() {
    var open = openSymbol();
    return streamBasisRows().map(function (record) {
      var cand = candidateFor(record.symbol);
      var gap = record.basis_gap_pct !== undefined ?
        record.basis_gap_pct : record.gap_pct;
      var edge = cand && (cand.net_edge_pct !== undefined ?
        cand.net_edge_pct : (cand.equation &&
          cand.equation.net_edge_pct));
      var phase = open === record.symbol ? "Open" :
        (cand ? "Proposed" : "Watching");
      if (view.filter === "proposals" && !cand) return null;
      if (view.filter === "open" && phase !== "Open") return null;
      return {
        key: record.symbol, symbol: record.symbol,
        spot: record.spot_ask !== undefined ? record.spot_ask :
          record.spot_price,
        perp: record.perp_bid !== undefined ? record.perp_bid :
          record.futures_price,
        gap: gap, edge: edge === undefined ? null : edge,
        funding: record.funding_rate, phase: phase, cand: cand,
        raw: record
      };
    }).filter(Boolean);
  }

  function expand(row) {
    var wrap = U.el("div");
    if (row.cand) {
      var eq = row.cand.equation || row.cand;
      var pairs = [];
      [["Raw entry gap", eq.gross_gap_pct !== undefined ?
          eq.gross_gap_pct : row.gap, "%"],
        ["Trading fees (4 legs)", eq.fees_pct, "%"],
        ["Slippage", eq.slippage_pct, "%"],
        ["Funding carry", eq.carry_pct !== undefined ? eq.carry_pct :
          eq.funding_carry_pct, "%"],
        ["Exit-gap assumption", eq.exit_gap_pct, "%"],
        ["Safety buffer", eq.safety_buffer_pct, "%"],
        ["Executable net edge", eq.net_edge_pct, "%"],
        ["Planned notional (per leg)", eq.notional_usdt ||
          row.cand.notional_usdt, " USDT"],
        ["Proposal", row.cand.proposal_id || row.cand.id, ""],
        ["Expires", row.cand.expires_utc || row.cand.expiry_utc, ""]
      ].forEach(function (item) {
        if (item[1] !== undefined && item[1] !== null) {
          pairs.push([item[0],
            (U.fmtNum(item[1], 4) || String(item[1])) + item[2]]);
        }
      });
      wrap.appendChild(U.facts(pairs));
    } else {
      wrap.appendChild(U.facts([
        ["Decision", "Not evaluated — no live proposal for this " +
          "symbol in the delivered data"],
        ["Intended action", "Buy Spot / Short Perp (when evaluated)"]
      ]));
    }
    wrap.appendChild(U.stateBlock("unavailable", "History collecting.",
      "Gap and price charts arrive with the measured history " +
      "endpoint — nothing is fabricated from snapshots."));
    wrap.appendChild(U.techDrawer(row.raw));
    return wrap;
  }

  function columns() {
    return [
      { key: "sym", label: "Asset", sort: "symbol",
        render: function (r) {
          return { node: U.c2(r.symbol, "both venues") };
        } },
      { key: "spot", label: "Spot", cls: "num", sort: "spot",
        render: function (r) {
          return { node: U.c2(U.fmtNum(r.spot, 6) || "—", "Spot ask",
            { num: true }) };
        } },
      { key: "perp", label: "Perp", cls: "num", sort: "perp",
        render: function (r) {
          return { node: U.c2(U.fmtNum(r.perp, 6) || "—", "Perp bid",
            { num: true }) };
        } },
      { key: "gap", label: "Gap", cls: "num", sort: "gap",
        render: function (r) {
          var text = U.fmtPct(r.gap);
          return { node: U.c2(text || "—", "entry executable",
            { num: true, tone: r.gap > 0 ? "good" : null }) };
        } },
      { key: "edge", label: "Edge", cls: "num", sort: "edge",
        render: function (r) {
          if (r.edge === null) {
            return { node: U.c2("—", "Not evaluated", { num: true }) };
          }
          return { node: U.c2(U.fmtPct(r.edge) || "—",
            "backend equation",
            { num: true, tone: Number(r.edge) > 0 ? "good" : "bad" }) };
        } },
      { key: "funding", label: "Funding", cls: "num", sort: "funding",
        render: function (r) {
          return { node: U.c2(U.fmtPct(r.funding) || "—",
            "current rate", { num: true }) };
        } },
      { key: "phase", label: "Phase",
        render: function (r) {
          var tone = r.phase === "Open" ? "warn" :
            (r.phase === "Proposed" ? "info" : "");
          return { node: U.chip(r.phase, tone) };
        } }
    ];
  }

  function render(fresh) {
    var rows = buildRows();
    if (fresh || !host) {
      var root = U.clearTo("pageroot");
      host = U.el("div", "frame");
      root.appendChild(host);
      var count = streamBasisRows().length;
      var candCount = rows.filter(function (r) { return r.cand; }).length;
      host.appendChild(U.tiles([
        U.tile({ label: "Eligible pairs (delivered)",
          value: String(count),
          sub: "stream rows with a measurable two-venue gap" }),
        U.tile({ label: "Valid proposals", value: String(candCount),
          sub: candCount ? "full equation available" :
            "none unexpired in the delivered data" }),
        U.tile({ label: "Open position",
          value: openSymbol() || "None",
          tone: openSymbol() ? "warn" : null,
          sub: openSymbol() ? "supervised" : "flat" })
      ]));
      host.appendChild(U.pillBar([
        { key: "all", label: "All" },
        { key: "proposals", label: "Proposals" },
        { key: "open", label: "Open" }
      ], view.filter, function (key) {
        view.filter = key; render(true);
      }));
      if (rows.length === 0) {
        host.appendChild(U.stateBlock("empty",
          view.filter === "all" ?
            "No two-venue gaps in the delivered data right now." :
            "Nothing matches this filter right now.",
          "Rows appear when the stream delivers measurable Spot/Perp " +
          "gaps."));
        tape = null;
        return;
      }
      var t = U.tapeTable(760, columns());
      host.appendChild(t.wrap);
      tape = new U.Tape({ table: t.table, columns: columns(),
        expandRender: expand, sortKey: "edge" });
    }
    if (tape) tape.update(rows);
  }

  window.QuantTabs.basis = {
    needs: ["stream", "candidates", "pnl"],
    init: function () {}, render: render
  };
})();
