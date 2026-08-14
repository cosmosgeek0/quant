"use strict";

/* Volume — interesting movers only (contract §5.3, iteration-1).
   Rows: /api/stream selection top_surge. Route states execution truth:
   Futures long only where a perpetual exists; Spot-only is Observe
   only. Decision detail beyond the last-50 window says partial. */

(function () {
  var U = window.QuantUI;
  var view = { filter: "interesting" };
  var tape = null, host = null;

  function surgeRows() {
    var stream = U.state.data.stream;
    var records = (stream && Array.isArray(stream.records)) ?
      stream.records : [];
    return records.filter(function (record) {
      var sel = Array.isArray(record.selection) ? record.selection : [];
      return sel.indexOf("top_surge") >= 0;
    });
  }
  function decisionFor(symbol) {
    var d = U.state.data.decisions;
    var list = (d && Array.isArray(d.decisions)) ? d.decisions : [];
    for (var i = 0; i < list.length; i++) {
      if ((list[i].symbol || "") === symbol) return list[i];
    }
    return null;
  }
  function positionFor(symbol) {
    var mom = U.state.data.momentum;
    var positions = mom && mom.state && mom.state.positions;
    if (!positions) return null;
    var record = positions[symbol];
    if (!record) return null;
    var st = String(record.state || "").toUpperCase();
    if (!st || st === "CLOSED" || st === "NONE") return null;
    return record;
  }

  function buildRows() {
    return surgeRows().map(function (record) {
      var venue = record.venue || (record.has_perp ? "both" : "spot");
      var hasPerp = /both|perp/i.test(String(venue)) ||
        record.has_perp === true;
      var pos = positionFor(record.symbol);
      var dec = decisionFor(record.symbol);
      var phase = pos ? "Open" :
        (dec && /ENTER/.test(String(dec.action || "")) ? "Ready" :
          (dec ? String(dec.action || "Quiet") : "Quiet"));
      if (view.filter === "open" && !pos) return null;
      if (view.filter === "eligible" && !hasPerp) return null;
      return {
        key: record.symbol, symbol: record.symbol,
        market: hasPerp ? "Both" : "Spot",
        route: hasPerp ? "Futures long" : "Observe only",
        price: record.price !== undefined ? record.price :
          record.last_price,
        surge: record.surge_ratio !== undefined ? record.surge_ratio :
          record.surge,
        move: record.move_pct !== undefined ? record.move_pct :
          record.price_change_pct,
        phase: phase, pos: pos, dec: dec, raw: record
      };
    }).filter(Boolean);
  }

  function expand(row) {
    var wrap = U.el("div");
    var pairs = [
      ["Execution eligibility", row.route +
        (row.market === "Spot" ?
          " — no perpetual exists for this symbol" :
          " — perpetual available, 1× only")]
    ];
    if (row.dec) {
      pairs.push(["Latest decision", String(row.dec.action || "—") +
        (row.dec.job_reason ? " — " + row.dec.job_reason : "")]);
      if (row.dec.decision_utc) {
        pairs.push(["Decided", row.dec.decision_utc]);
      }
    } else {
      pairs.push(["Decision detail",
        "Unavailable — outside the last-50 decision window " +
        "(partial source)"]);
    }
    if (row.pos) {
      pairs.push(["Position state", String(row.pos.state || "—")]);
      if (row.pos.entry_price !== undefined) {
        pairs.push(["Entry", U.fmtNum(row.pos.entry_price, 6) || "—"]);
      }
    }
    wrap.appendChild(U.facts(pairs));
    wrap.appendChild(U.techDrawer(row.raw));
    return wrap;
  }

  function columns() {
    return [
      { key: "sym", label: "Asset", sort: "symbol",
        render: function (r) {
          return { node: U.c2(r.symbol, r.market === "Both" ?
            "spot + perp" : "spot only") };
        } },
      { key: "route", label: "Route", sort: "route",
        render: function (r) {
          return { node: U.c2(r.route, r.market === "Both" ?
            "eligible venue" : "not tradeable by the runner") };
        } },
      { key: "price", label: "Price", cls: "num", sort: "price",
        render: function (r) {
          return { node: U.c2(U.fmtNum(r.price, 6) || "—",
            "reference", { num: true }) };
        } },
      { key: "surge", label: "Surge", cls: "num", sort: "surge",
        render: function (r) {
          var text = U.fmtNum(r.surge, 1);
          return { node: U.c2(text ? text + "×" : "—",
            "vs rolling median", { num: true,
              tone: Number(r.surge) >= 10 ? "good" : null }) };
        } },
      { key: "move", label: "Move", cls: "num", sort: "move",
        render: function (r) {
          var text = U.fmtPct(r.move);
          return { node: U.c2(text || "—", "since ignition",
            { num: true, tone: r.move > 0 ? "good" :
              (r.move < 0 ? "bad" : null) }) };
        } },
      { key: "phase", label: "Phase", sort: "phase",
        render: function (r) {
          var tone = r.phase === "Open" ? "warn" :
            (r.phase === "Ready" ? "ok" : "");
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
      var openCount = rows.filter(function (r) { return r.pos; }).length;
      host.appendChild(U.tiles([
        U.tile({ label: "Interesting movers (delivered)",
          value: String(surgeRows().length),
          sub: "top surges — not the whole universe" }),
        U.tile({ label: "Open positions", value: String(openCount),
          tone: openCount ? "warn" : null,
          sub: openCount ? "runner-managed" : "flat" }),
        U.tile({ label: "Full universe view", absent: true,
          absentWhy: "Arrives with the complete volume read model" })
      ]));
      host.appendChild(U.pillBar([
        { key: "interesting", label: "Interesting" },
        { key: "eligible", label: "Eligible" },
        { key: "open", label: "Open" }
      ], view.filter, function (key) {
        view.filter = key; render(true);
      }));
      if (rows.length === 0) {
        host.appendChild(U.stateBlock("empty",
          "No interesting movers in the delivered data right now.",
          "Rows appear when the stream delivers volume surges."));
        tape = null;
        return;
      }
      var t = U.tapeTable(720, columns());
      host.appendChild(t.wrap);
      tape = new U.Tape({ table: t.table, columns: columns(),
        expandRender: expand, sortKey: "surge" });
    }
    if (tape) tape.update(rows);
  }

  window.QuantTabs.volume = {
    needs: ["stream", "decisions"],
    init: function () {}, render: render
  };
})();
