"use strict";

/* Journal tab — curated durable history. Owned by the Journal lane.
   Sources: the shared bounded event list (receipts, realized, pnl,
   failures, non-WATCH decisions) plus the archived events fixture,
   clearly labeled historical. The complete journal contract is a later
   backend milestone; everything here is explicitly partial. */

(function () {
  var UI = window.QuantUI;

  function init() {
    var root = document.getElementById("root-journal");
    root.textContent = "";

    var records = UI.el("section", "block");
    records.appendChild(UI.el("h2", null,
      "Journal — durable records (partial)"));
    records.appendChild(UI.el("p", "meta",
      "The complete journal contract does not exist yet. These are the " +
      "durable records the current machine already exposes."));
    var rowsBody = UI.el("div");
    rowsBody.id = "journal-rows";
    rowsBody.appendChild(UI.el("p", "empty skel", "loading"));
    records.appendChild(rowsBody);
    root.appendChild(records);

    var history = UI.el("section", "block");
    history.appendChild(UI.el("h2", null,
      "Historical evidence — archived spot runs"));
    history.appendChild(UI.el("p", "meta",
      "Archived fixture of the first real spot runs. Historical " +
      "evidence, not live data."));
    var historyBody = UI.el("div");
    historyBody.id = "journal-history";
    historyBody.appendChild(UI.el("p", "empty skel", "loading"));
    history.appendChild(historyBody);
    root.appendChild(history);
  }

  function render() {
    var root = UI.clearTo("journal-rows");
    var events = UI.buildEventList();
    if (events.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No durable trade, control, or failure records in the delivered " +
        "data yet."));
    } else {
      events.slice(0, 50).forEach(function (event) {
        var row = UI.el("div", "jrow");
        row.appendChild(UI.el("span", "ts",
          UI.shortTime(event.ts) || "time unrecorded"));
        var cls = event.sev === "attention" ? "b-red" :
          (event.sev === "refused" ? "b-amber" :
            (event.sev === "trade" ? "b-green" : "b-gray"));
        row.appendChild(UI.el("span", "badge " + cls, event.strat));
        row.appendChild(UI.el("span", null,
          (event.sym ? event.sym + " — " : "") + event.text));
        if (event.money) row.appendChild(UI.el("span", "money", event.money));
        root.appendChild(row);
      });
    }
    renderHistory();
  }

  function renderHistory() {
    var payload = UI.state.data.events;
    var root = UI.clearTo("journal-history");
    if (!payload || !payload.present || !Array.isArray(payload.events) ||
        payload.events.length === 0) {
      root.appendChild(UI.el("p", "empty", "No archived runs."));
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
      var row = UI.el("div", "jrow");
      row.appendChild(UI.el("span", "ts",
        UI.shortTime(events[0].ts || "")));
      row.appendChild(UI.el("span", "badge " +
        (verdict === "CLOSED" ? "b-green" : "b-amber"), verdict || "run"));
      row.appendChild(UI.el("span", null,
        (events[0].symbol || "") + " — archived spot run " + id));
      root.appendChild(row);
      root.appendChild(UI.techDrawer(events));
    });
  }

  window.QuantTabs.journal = {
    needs: ["decisions", "pnl"],
    init: init,
    render: render
  };
})();
