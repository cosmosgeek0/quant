"use strict";

/* Overview — the money page (contract §5.1, spec §overview).
   Assembled from QuantParts components. Honest-calm absence: a missing
   number is an em dash with its reason, never a shouted word, never a
   zero. The global topstrip is the only strip; this page adds richer
   context, not duplicates. */

(function () {
  var U = window.QuantUI;
  var P = window.QuantParts;

  function money(raw) {
    var text = U.fmtNum(raw, 4);
    if (text === null) return null;
    return text.replace("-", "−");
  }

  function feedTime(iso) {
    var text = U.shortTime(iso);
    return text ? text.replace(" UTC", "Z") : null;
  }

  function hasNet(net) {
    return net !== undefined && net !== null && money(net) !== null;
  }

  function card(title, meta) {
    var box = U.el("section", "card");
    var head = U.el("div", "card-head");
    head.appendChild(U.el("span", "card-title", title));
    if (meta) head.appendChild(U.el("span", "card-meta", meta));
    box.appendChild(head);
    var body = U.el("div", "card-body");
    box.appendChild(body);
    return { box: box, body: body };
  }

  function statRowTop() {
    var d = U.state.data;
    var strip = d.strip || {};
    var cards = [];

    cards.push(P.statCard({
      label: "Net profit and loss — today",
      absent: true,
      absentWhy: "Ledger incomplete — account-wide accounting is not built yet",
      title: "Partial Basis and Volume records render below; they are " +
        "never summed into a total."
    }));

    var expo = U.exposureLines();
    if (expo.length === 0) {
      cards.push(P.statCard({
        label: "Current exposure", value: "None", sub: "The account is flat"
      }));
    } else {
      var first = expo[0];
      cards.push(P.statCard({
        label: "Current exposure",
        value: first.symbol,
        tone: "warn",
        sub: first.strategy + " · " + U.ownerPhaseForState(first.st) +
          (expo.length > 1 ? " · +" + (expo.length - 1) + " more" : "")
      }));
    }

    cards.push(P.statCard({
      label: "Capital",
      absent: true,
      absentWhy: "No account contract yet — equity arrives with the " +
        "account snapshot endpoint"
    }));

    var gov = strip.governor || {};
    if (gov.present) {
      cards.push(P.statCard({
        label: "Risk used today",
        value: gov.entries_used + " of " + gov.entries_cap + " entries",
        tone: gov.entries_used >= gov.entries_cap ? "bad" :
          (gov.entries_used > 0 ? "warn" : null),
        sub: "Loss+fees " + gov.loss_fees_used_usdt + " of " +
          gov.loss_fees_cap_usdt + " USDT"
      }));
    } else {
      cards.push(P.statCard({
        label: "Risk used today",
        value: "0 of 5 entries",
        sub: "No entries recorded today · caps are code law"
      }));
    }

    var pull = strip.pull || {};
    var dataTone = null, dataValue = "Live";
    if (!pull.present) { dataValue = null; }
    else if (pull.ok === false || pull.level === "red") {
      dataValue = "Stale"; dataTone = "bad";
    } else if (pull.level === "amber") {
      dataValue = "Delayed"; dataTone = "warn";
    } else { dataValue = "Live"; dataTone = "good"; }
    cards.push(P.statCard({
      label: "Data",
      value: dataValue,
      tone: dataTone,
      absent: dataValue === null,
      absentWhy: "No data delivery yet",
      sub: pull.present ? "Updated " + U.ageText(pull.age_seconds) : null
    }));

    return P.statRow(cards);
  }

  function basisRecordsCard() {
    var c = card("Realized records — Basis", "partial");
    var pnl = U.state.data.pnl;
    var records = (pnl && Array.isArray(pnl.records)) ? pnl.records : [];
    if (records.length === 0) {
      c.body.appendChild(P.stateBlock("empty",
        "No completed Basis accounting records in the delivered data.",
        "Basis accounting records only — never account-wide P&L."));
      return c.box;
    }
    records.slice(0, 6).forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      var verdict = String(record.verdict || "record");
      c.body.appendChild(P.feedRow({
        timeText: feedTime(record.utc),
        title: verdict,
        caption: hasNet(net) ? "net " + money(net) + " USDT" :
          "net not recorded",
        tone: verdict === "CLOSED" ? "ok" :
          (verdict.indexOf("REJECT") >= 0 ? "warn" : "bad"),
        symbol: record.symbol || null,
        onExpand: function () {
          return P.tapeWaterfall([
            { label: "Verdict", value: verdict },
            { label: "Net", value: hasNet(net) ?
                money(net) + " USDT" : "—" },
            { label: "Recorded", value: record.utc || "—" }
          ]);
        }
      }));
    });
    c.body.appendChild(U.el("p", "card-note",
      "Fees and funding completeness depend on each receipt; rows are " +
      "never summed."));
    return c.box;
  }

  function volumeRecordsCard() {
    var c = card("Realized records — Volume", "partial");
    var mom = U.state.data.momentum;
    var records = (mom && mom.realized && Array.isArray(mom.realized.records))
      ? mom.realized.records : [];
    if (records.length === 0) {
      c.body.appendChild(P.stateBlock("empty",
        "No realized Volume results in the delivered data.",
        "Runner realized results only — not account-wide profit."));
      return c.box;
    }
    records.slice(0, 6).forEach(function (record) {
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      var verdict = String(record.verdict || record.action || "closed");
      c.body.appendChild(P.feedRow({
        timeText: feedTime(record.utc),
        title: verdict,
        caption: hasNet(net) ? "net " + money(net) + " USDT" :
          "realized net not recorded",
        tone: hasNet(net) && Number(net) > 0 ? "ok" :
          (hasNet(net) && Number(net) < 0 ? "bad" : "info"),
        symbol: record.symbol || null,
        onExpand: function () {
          return P.tapeWaterfall([
            { label: "Result", value: verdict },
            { label: "Net", value: hasNet(net) ?
                money(net) + " USDT" : "—" },
            { label: "Recorded", value: record.utc || "—" }
          ]);
        }
      }));
    });
    c.body.appendChild(U.el("p", "card-note",
      "Runner realized results only — not account-wide profit."));
    return c.box;
  }

  function activityCard() {
    var c = card("Recent activity", "last 5 · partial sources");
    var events = U.buildEventList().slice(0, 5);
    if (events.length === 0) {
      c.body.appendChild(P.stateBlock("empty",
        "No completed trades yet.", "The machine is watching markets."));
      return c.box;
    }
    events.forEach(function (event) {
      c.body.appendChild(P.feedRow({
        timeText: feedTime(event.ts),
        title: event.strat + (event.sym ? " · " + event.sym : ""),
        caption: event.text,
        tone: event.sev === "attention" ? "bad" :
          (event.sev === "refused" ? "warn" :
            (event.sev === "trade" ? "ok" : "info")),
        symbol: null,
        onExpand: null
      }));
    });
    return c.box;
  }

  function render() {
    var root = U.clearTo("root-overview");
    root.appendChild(statRowTop());

    var grid = U.el("div", "grid2 ov-grid");
    grid.appendChild(basisRecordsCard());
    grid.appendChild(volumeRecordsCard());
    root.appendChild(grid);

    root.appendChild(activityCard());
  }

  window.QuantTabs.overview = {
    needs: ["pnl", "decisions", "fees"],
    init: function () {},
    render: render
  };
})();
