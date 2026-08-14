"use strict";

/* Quant tab — the machine's trader page. Owned by the Quant lane.
   Left: deterministic decision trail (never called AI thinking; only
   sentences mechanically derivable from structured records; honest
   Activity detail unavailable otherwise). Right: authority (Unavailable
   until the operator contract exists — never inferred, never a control)
   and read-only policy facts that are code law. */

(function () {
  var UI = window.QuantUI;

  function init() {
    var root = document.getElementById("root-quant");
    root.textContent = "";
    var grid = UI.el("div", "quantgrid");

    var trail = UI.el("section", "block trail");
    trail.appendChild(UI.el("h2", null, "Decision trail"));
    var stateLine = UI.el("p", "meta");
    stateLine.id = "quant-state";
    trail.appendChild(stateLine);
    var trailBody = UI.el("div");
    trailBody.id = "quant-trail";
    trailBody.appendChild(UI.el("p", "empty skel", "loading"));
    trail.appendChild(trailBody);
    grid.appendChild(trail);

    var right = UI.el("div");
    var auth = UI.el("section", "block");
    auth.appendChild(UI.el("h2", null, "Authority"));
    var authBody = UI.el("div");
    authBody.id = "quant-authority";
    auth.appendChild(authBody);
    right.appendChild(auth);
    var policy = UI.el("section", "block");
    policy.appendChild(UI.el("h2", null, "Policy — effective values"));
    var policyBody = UI.el("div");
    policyBody.id = "quant-policy";
    policy.appendChild(policyBody);
    right.appendChild(policy);
    grid.appendChild(right);

    root.appendChild(grid);
  }

  function render() {
    var strip = UI.state.data.strip || {};
    var phase = UI.currentPhase();
    var stopLine = UI.ownerStopLine(strip.stop_present,
      strip.stop_jobs_present);
    document.getElementById("quant-state").textContent =
      "Current state: " + phase + (stopLine ? " · " + stopLine : "");

    var root = UI.clearTo("quant-trail");
    var events = UI.buildEventList();
    if (events.length === 0) {
      root.appendChild(UI.el("p", "unavail",
        "Activity detail unavailable — the machine has not recorded " +
        "trail-worthy events in the delivered data, and the structured " +
        "activity contract does not exist yet."));
    } else {
      root.appendChild(UI.el("p", "meta", "Recent — partial. Built only " +
        "from the bounded records the machine exposes today."));
      events.slice(0, 30).forEach(function (event) {
        root.appendChild(UI.eventLine(event));
      });
    }

    root = UI.clearTo("quant-authority");
    root.appendChild(UI.el("p", "unavail",
      "Unavailable — the operator contract does not exist yet. Entry " +
      "authority cannot be read, granted, or revoked from this page, " +
      "and nothing here can pause or arm the machine."));

    root = UI.clearTo("quant-policy");
    var kv = UI.el("dl", "kv");
    function addKv(term, value) {
      kv.appendChild(UI.el("dt", null, term));
      kv.appendChild(UI.el("dd", null, value));
    }
    addKv("Leverage", "1× — locked");
    addKv("First-trade ceiling", "8 USDT (Basis: per leg)");
    addKv("Later ceiling", "32 USDT — unlocks only after one clean close");
    addKv("Entries per day", "5 — locked");
    addKv("Daily loss+fees", "1.00 USDT — locked");
    addKv("Adverse stop", "2% realized incl. fees (Volume runner)");
    var fees = UI.state.data.fees;
    addKv("Measured commissions", fees && fees.present && fees.rates ?
      (UI.fmtPct(fees.rates.round_trip_taker_fees_pct) || "recorded") +
      " round trip" : "Unavailable");
    var gov = (UI.state.data.strip || {}).governor || {};
    addKv("Entry lock", UI.ownerLockLine(gov.entry_lock_present));
    root.appendChild(kv);
    root.appendChild(UI.el("p", "meta",
      "These are code law, shown read-only. Changing any of them is a " +
      "reviewed code decision, never a dashboard action."));
  }

  window.QuantTabs.quant = {
    needs: ["decisions", "pnl", "fees"],
    init: init,
    render: render
  };
})();
