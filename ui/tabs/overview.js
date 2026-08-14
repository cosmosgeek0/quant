"use strict";

/* Overview — the money page (contract §5.1). Net today leads and says
   "—" with its reason until the ledger exists (Ledger incomplete);
   partial records stay separate and are never summed. */

(function () {
  var U = window.QuantUI;

  function hasNet(net) {
    return net !== undefined && net !== null && U.fmtNum(net, 4) !== null;
  }

  function recordRows(host, records, kind) {
    records.slice(0, 6).forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      var verdict = String(record.verdict || record.action || kind);
      host.appendChild(U.feedRow({
        timeText: U.shortTime(record.utc) || null,
        title: verdict,
        caption: hasNet(net) ? "net " + U.fmtUsdt(net) :
          "net not recorded",
        tone: verdict === "CLOSED" ? "ok" :
          (verdict.indexOf("REJECT") >= 0 ? "warn" :
            (hasNet(net) && Number(net) < 0 ? "bad" : "info")),
        symbol: record.symbol || null,
        onExpand: function () {
          return U.facts([
            ["Verdict", verdict],
            ["Net", hasNet(net) ? U.fmtUsdt(net) : "—"],
            ["Recorded", record.utc || "—"]
          ]);
        }
      }));
    });
  }

  function render() {
    var root = U.clearTo("pageroot");
    var frame = U.el("div", "frame");
    var strip = U.state.data.strip || {};
    var gov = strip.governor || {};
    var pull = strip.pull || {};
    var expo = U.exposureLines();

    var tl = [];
    tl.push(U.tile({ label: "Net profit and loss — today", absent: true,
      absentWhy: "Ledger incomplete — account-wide accounting is not " +
        "built yet",
      title: "Partial records below are real but never summed." }));
    tl.push(expo.length === 0 ?
      U.tile({ label: "Current exposure", value: "None",
        sub: "The account is flat" }) :
      U.tile({ label: "Current exposure", value: expo[0].symbol,
        tone: "warn",
        sub: expo[0].strategy + " · " +
          U.ownerPhaseForState(expo[0].st) +
          (expo.length > 1 ? " · +" + (expo.length - 1) + " more" : "") }));
    tl.push(U.tile({ label: "Capital", absent: true,
      absentWhy: "No account contract yet — equity arrives with the " +
        "account snapshot endpoint" }));
    tl.push(gov.present ?
      U.tile({ label: "Risk used today",
        value: gov.entries_used + " of " + gov.entries_cap + " entries",
        tone: gov.entries_used >= gov.entries_cap ? "bad" :
          (gov.entries_used > 0 ? "warn" : null),
        sub: "Loss+fees " + gov.loss_fees_used_usdt + " of " +
          gov.loss_fees_cap_usdt + " USDT" }) :
      U.tile({ label: "Risk used today", value: "0 of 5 entries",
        sub: "No entries recorded today · caps are code law" }));
    tl.push(U.tile({ label: "Data",
      value: !pull.present ? null :
        (pull.ok === false || pull.level === "red" ? "Stale" :
          (pull.level === "amber" ? "Delayed" : "Live")),
      tone: pull.level === "green" ? "good" :
        (pull.level === "amber" ? "warn" : "bad"),
      absentWhy: "No data delivery yet",
      sub: pull.present ? "Updated " + U.ageText(pull.age_seconds) :
        null }));
    frame.appendChild(U.tiles(tl));

    var split = U.el("div", "split band");
    var left = U.band("Realized records — Basis", "partial");
    var pnl = U.state.data.pnl;
    var basisRecords = (pnl && Array.isArray(pnl.records)) ?
      pnl.records : [];
    if (basisRecords.length === 0) {
      left.appendChild(U.stateBlock("empty",
        "No completed Basis accounting records in the delivered data.",
        "Basis accounting records only — never account-wide P&L."));
    } else recordRows(left, basisRecords, "record");
    split.appendChild(left);
    var right = U.band("Realized records — Volume", "partial");
    var mom = U.state.data.momentum;
    var volRecords = (mom && mom.realized &&
      Array.isArray(mom.realized.records)) ? mom.realized.records : [];
    if (volRecords.length === 0) {
      right.appendChild(U.stateBlock("empty",
        "No realized Volume results in the delivered data.",
        "Runner realized results only — not account-wide profit."));
    } else recordRows(right, volRecords, "closed");
    split.appendChild(right);
    frame.appendChild(split);

    var activity = U.band("Recent activity", "last 5 · partial sources");
    var events = U.buildEventList().slice(0, 5);
    if (events.length === 0) {
      activity.appendChild(U.stateBlock("empty",
        "No completed trades yet.", "The machine is watching markets."));
    } else {
      events.forEach(function (event) {
        activity.appendChild(U.feedRow({
          timeText: U.shortTime(event.ts) || null,
          title: event.strat + (event.sym ? " · " + event.sym : ""),
          caption: event.text,
          tone: U.eventTone(event),
          symbol: null, onExpand: null
        }));
      });
    }
    frame.appendChild(activity);
    root.appendChild(frame);
  }

  window.QuantTabs.overview = {
    needs: ["pnl", "decisions", "fees"],
    init: function () {}, render: render
  };
})();
