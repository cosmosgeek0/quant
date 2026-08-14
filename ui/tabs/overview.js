"use strict";

/* Overview tab — the money page. Owned by the Overview lane.
   Sources: strip, position, momentum, pnl, decisions, failures, fees.
   Iteration 1: Net today is Unavailable — ledger incomplete; equity and
   capital have no account contract; partial records stay separate. */

(function () {
  var UI = window.QuantUI;

  function init() {
    var root = document.getElementById("root-overview");
    root.textContent = "";
    var net = UI.el("section", "block");
    net.appendChild(UI.el("h2", null, "Net profit and loss — today"));
    var big = UI.el("p", "bignum unavailable",
      "Unavailable — ledger incomplete");
    big.id = "ov-net";
    net.appendChild(big);
    net.appendChild(UI.el("p", "meta",
      "Account-wide accounting does not exist yet. Partial records below " +
      "are real but incomplete."));
    root.appendChild(net);

    var grid1 = UI.el("div", "grid2");
    grid1.appendChild(section("ov-exposure", "Current exposure"));
    grid1.appendChild(section("ov-capital", "Capital"));
    root.appendChild(grid1);

    var grid2 = UI.el("div", "grid2");
    grid2.appendChild(section("ov-basis-realized",
      "Realized records — Basis (partial)"));
    grid2.appendChild(section("ov-momentum-realized",
      "Realized records — Volume (partial)"));
    root.appendChild(grid2);

    var grid3 = UI.el("div", "grid2");
    grid3.appendChild(section("ov-risk", "Risk usage"));
    grid3.appendChild(section("ov-activity", "Recent activity"));
    root.appendChild(grid3);
  }

  function section(id, title) {
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, title));
    var body = UI.el("div");
    body.id = id;
    body.appendChild(UI.el("p", "empty skel", "loading"));
    block.appendChild(body);
    return block;
  }

  function render() {
    var state = UI.state;
    var root = UI.clearTo("ov-exposure");
    var expo = UI.exposureLines();
    if (expo.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No standing position. The account is flat."));
    } else {
      expo.forEach(function (line) {
        var p = UI.el("p", null);
        var phase = UI.ownerPhaseForState(line.st);
        p.appendChild(UI.el("span", "badge " +
          (phase === "Attention" ? "b-red" : "b-amber"), phase));
        p.appendChild(document.createTextNode(
          " " + line.strategy + " · " + line.symbol));
        root.appendChild(p);
        root.appendChild(UI.techDrawer(line.record));
      });
    }

    root = UI.clearTo("ov-capital");
    [["Total equity", "Unavailable — no account contract yet"],
     ["Free Spot USDT", "Unavailable — no account contract yet"],
     ["Free Futures USDT", "Unavailable — no account contract yet"],
     ["Capital exposed", "Unavailable — no account contract yet"]
    ].forEach(function (pair) {
      var p = UI.el("p", "unavail");
      p.appendChild(UI.el("strong", null, pair[0] + ": "));
      p.appendChild(document.createTextNode(pair[1]));
      root.appendChild(p);
    });

    root = UI.clearTo("ov-basis-realized");
    var pnl = state.data.pnl;
    if (!pnl || !pnl.present || !pnl.records || pnl.records.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No completed Basis accounting records in the delivered data."));
    } else {
      pnl.records.slice(0, 6).forEach(function (record) {
        var p = UI.el("p", null);
        var net = record.realized && record.realized.net_usdt;
        p.appendChild(UI.el("span", "badge b-gray",
          record.verdict || "record"));
        p.appendChild(document.createTextNode(
          " " + (record.symbol || "") + " · net " +
          (UI.fmtUsdt(net) || "not recorded") + " · " +
          (UI.shortTime(record.utc || "") || "time unrecorded")));
        root.appendChild(p);
        root.appendChild(UI.techDrawer(record));
      });
      root.appendChild(UI.el("p", "meta",
        "Partial: Basis records only; fees and funding completeness " +
        "depend on each receipt."));
    }

    root = UI.clearTo("ov-momentum-realized");
    var mom = state.data.momentum;
    var realized = mom && mom.realized && mom.realized.records || [];
    if (realized.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No realized Volume results in the delivered data."));
    } else {
      realized.slice(-6).reverse().forEach(function (record) {
        var p = UI.el("p", null);
        var net = record.realized && record.realized.net_usdt;
        if (net === undefined) net = record.net_usdt;
        p.appendChild(UI.el("span", "badge b-gray",
          record.verdict || "result"));
        p.appendChild(document.createTextNode(
          " " + (record.symbol || "") + " · net " +
          (UI.fmtUsdt(net) || "not recorded")));
        root.appendChild(p);
        root.appendChild(UI.techDrawer(record));
      });
      root.appendChild(UI.el("p", "meta",
        "Partial: runner results only, not account-wide profit."));
    }

    root = UI.clearTo("ov-risk");
    var strip = state.data.strip || {};
    var gov = strip.governor || {};
    var kv = UI.el("dl", "kv");
    function addKv(label, value) {
      kv.appendChild(UI.el("dt", null, label));
      kv.appendChild(UI.el("dd", null, value));
    }
    addKv("Entries today", (gov.present ? gov.entries_used : 0) + " of " +
      (gov.entries_cap || 5));
    addKv("Loss+fees today", (gov.present ? gov.loss_fees_used_usdt : "0") +
      " of " + (gov.loss_fees_cap_usdt || "1.00") + " USDT");
    addKv("Hard ceiling", "8 USDT first trade (Basis per leg)");
    addKv("Leverage", "1× — locked");
    addKv("Entry lock", UI.ownerLockLine(gov.entry_lock_present));
    root.appendChild(kv);
    root.appendChild(UI.el("p", "meta",
      "Recorded governor usage — incomplete for Basis. Caps are code " +
      "law; this page cannot change them."));

    root = UI.clearTo("ov-activity");
    var events = UI.buildEventList().slice(0, 5);
    if (events.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No completed trades yet. The machine is watching markets."));
    } else {
      events.forEach(function (event) {
        root.appendChild(UI.eventLine(event));
      });
      root.appendChild(UI.el("p", "meta", "Recent — partial sources."));
    }
  }

  window.QuantTabs.overview = {
    needs: ["pnl", "decisions", "fees"],
    init: init,
    render: render
  };
})();
