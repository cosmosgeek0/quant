"use strict";

/* Quant — the trader page (contract §5.4, iteration-1 truth only).
   Left: the LIVE TRAIL — the machine's real activity, breathing.
   Right: effective policy and authority, read-only facts. No controls
   exist here until the operator service exists — not even disabled
   ones. Never called AI thinking: it is a deterministic decision
   trail; "Activity detail unavailable" when sources cannot prove it. */

(function () {
  var U = window.QuantUI;
  var P = window.QuantParts;
  var trail = null;

  function feedTime(iso) {
    var text = U.shortTime(iso);
    return text ? text.replace(" UTC", "Z") : null;
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

  function factRows(pairs) {
    return P.tapeWaterfall(pairs.map(function (pair) {
      return { label: pair[0], value: pair[1] };
    }));
  }

  function nowState() {
    var strip = U.state.data.strip || {};
    var phase = U.currentPhase();
    var lamp = "ok", breathing = "calm";
    if (phase === "Attention") { lamp = "bad"; breathing = "urgent"; }
    else if (phase === "Offline") { lamp = "off"; breathing = "none"; }
    else if (phase === "Holding" || phase === "Entering" ||
             phase === "Exiting" || phase === "Reconciling") {
      lamp = "warn";
    }
    var stopLine = U.ownerStopLine(strip.stop_present,
      strip.stop_jobs_present);
    if (stopLine) breathing = "none";
    var pull = strip.pull || {};
    var detail = stopLine ||
      (pull.present ? "Data updated " + U.ageText(pull.age_seconds) :
        "No data delivery yet");
    var sup = strip.supervisor || {};
    if (sup.last_seen_utc) {
      detail += " · supervisor tick " + U.ageText(sup.age_seconds);
    }
    return { phase: phase, lamp: lamp, detail: detail,
      breathing: breathing };
  }

  function trailEvents() {
    return U.buildEventList().slice(0, 40).map(function (event, index) {
      return {
        key: (event.ts || "t" + index) + "|" + (event.sym || "") + "|" +
          String(event.text || "").slice(0, 40),
        timeText: feedTime(event.ts),
        title: event.strat + (event.sym ? " · " + event.sym : ""),
        caption: event.text,
        tone: event.sev === "attention" ? "bad" :
          (event.sev === "refused" ? "warn" :
            (event.sev === "trade" ? "ok" : "info")),
        symbol: null
      };
    });
  }

  function policyCard() {
    var c = card("Policy — effective values", "code law · read-only");
    var strip = U.state.data.strip || {};
    var gov = strip.governor || {};
    var fees = U.state.data.fees;
    var commission = "—";
    if (fees && fees.present !== false) {
      var maker = fees.maker !== undefined ? U.fmtPct(fees.maker) : null;
      var taker = fees.taker !== undefined ? U.fmtPct(fees.taker) : null;
      if (maker || taker) {
        commission = (maker ? "maker " + maker : "") +
          (maker && taker ? " · " : "") + (taker ? "taker " + taker : "");
      }
    }
    c.body.appendChild(factRows([
      ["Leverage", "1× — locked"],
      ["First-trade ceiling", "8 USDT (Basis: per leg)"],
      ["Later ceiling", "32 USDT — after one clean machine-managed close"],
      ["Entries per day", "5 — locked"],
      ["Daily loss+fees", "1.00 USDT — locked"],
      ["Measured commissions", commission],
      ["Entry lock", U.ownerLockLine(gov.entry_lock_present)]
    ]));
    c.body.appendChild(U.el("p", "card-note",
      "These are code law, shown read-only. Changing any of them is a " +
      "reviewed code decision, never a dashboard action."));
    return c.box;
  }

  function authorityCard() {
    var c = card("Authority", null);
    c.body.appendChild(P.stateBlock("unavailable",
      "No operator contract yet.",
      "Entry authority cannot be read, granted, or revoked from this " +
      "page, and nothing here can pause or arm the machine."));
    c.body.appendChild(U.el("p", "card-note",
      "Controls arrive with the operator service."));
    return c.box;
  }

  function render() {
    var root = document.getElementById("root-quant");
    if (!root.firstChild || !root.querySelector(".q-left")) {
      root.textContent = "";
      var wrap = U.el("div", "quantwrap");
      var left = U.el("div", "q-left");
      var right = U.el("div", "q-right");
      wrap.appendChild(left);
      wrap.appendChild(right);
      root.appendChild(wrap);
      var trailCard = card("Decision trail", "Recent — partial");
      left.appendChild(trailCard.box);
      trail = P.liveTrail(trailCard.body);
      right.appendChild(policyCard());
      right.appendChild(authorityCard());
    } else {
      var rightCol = root.querySelector(".q-right");
      rightCol.textContent = "";
      rightCol.appendChild(policyCard());
      rightCol.appendChild(authorityCard());
    }
    var events = trailEvents();
    if (events.length === 0 && trail) {
      trail.update(nowState(), []);
      var leftCol = root.querySelector(".q-left");
      if (!leftCol.querySelector(".q-empty")) {
        var empty = P.stateBlock("empty", "Activity detail unavailable.",
          "Only bounded records the machine exposes today can appear " +
          "here; the full decision stream arrives with the quant read " +
          "contract.");
        empty.classList.add("q-empty");
        leftCol.appendChild(empty);
      }
    } else if (trail) {
      var leftEmpty = root.querySelector(".q-empty");
      if (leftEmpty) leftEmpty.remove();
      trail.update(nowState(), events);
    }
  }

  window.QuantTabs.quant = {
    needs: ["decisions", "pnl", "fees"],
    init: function () {},
    render: render
  };
})();
