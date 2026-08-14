"use strict";

/* Quant — the trader page (contract §5.4). Left: the live trail, a
   deterministic decision trail with a breathing now-line — never
   called AI thinking. Right: effective policy and authority as
   read-only facts. No controls exist until the operator service does
   — not even disabled ones. */

(function () {
  var U = window.QuantUI;
  var trail = null, host = null;

  function nowState() {
    var strip = U.state.data.strip || {};
    var phase = U.currentPhase();
    var lamp = "ok", breathing = "calm";
    if (phase === "Attention") { lamp = "bad"; breathing = "urgent"; }
    else if (phase === "Offline") { lamp = "off"; breathing = "none"; }
    else if (phase !== "Watching" && phase !== "Evaluating") {
      lamp = "warn";
    }
    var stopLine = U.ownerStopLine(strip.stop_present,
      strip.stop_jobs_present);
    if (stopLine) breathing = "none";
    var pull = strip.pull || {};
    var detail = stopLine || (pull.present ?
      "Data updated " + U.ageText(pull.age_seconds) :
      "No data delivery yet");
    var sup = strip.supervisor || {};
    if (sup.age_seconds !== undefined) {
      detail += " · supervisor tick " + U.ageText(sup.age_seconds);
    }
    return { phase: phase, lamp: lamp, detail: detail,
      breathing: breathing };
  }

  function trailEvents() {
    return U.buildEventList().slice(0, 40).map(function (event, i) {
      return {
        key: (event.ts || "t" + i) + "|" + (event.sym || "") + "|" +
          String(event.text || "").slice(0, 40),
        timeText: U.shortTime(event.ts) || null,
        title: event.strat + (event.sym ? " · " + event.sym : ""),
        caption: event.text,
        tone: U.eventTone(event),
        symbol: null,
        onExpand: function () {
          var wrap = U.el("div");
          wrap.appendChild(U.facts([
            ["Recorded", event.ts || "—"],
            ["Strategy", event.strat || "—"],
            ["Symbol", event.sym || "—"],
            ["Event", event.text || "—"]
          ]));
          wrap.appendChild(U.techDrawer(event.raw || {}));
          return wrap;
        }
      };
    });
  }

  function policyCard() {
    var frame = U.el("div", "frame");
    var b = U.band("Policy — effective values", "code law · read-only");
    var strip = U.state.data.strip || {};
    var gov = strip.governor || {};
    var fees = U.state.data.fees;
    var commission = "—";
    if (fees && fees.present !== false) {
      var maker = fees.maker !== undefined ? U.fmtPct(fees.maker) : null;
      var taker = fees.taker !== undefined ? U.fmtPct(fees.taker) : null;
      if (maker || taker) {
        commission = (maker ? "maker " + maker : "") +
          (maker && taker ? " · " : "") +
          (taker ? "taker " + taker : "");
      }
    }
    b.appendChild(U.facts([
      ["Leverage", "1× — locked"],
      ["First-trade ceiling", "8 USDT (Basis: per leg)"],
      ["Later ceiling",
        "32 USDT — after one clean machine-managed close"],
      ["Entries per day", "5 — locked"],
      ["Daily loss+fees", "1.00 USDT — locked"],
      ["Measured commissions", commission],
      ["Entry lock", U.ownerLockLine(gov.entry_lock_present)]
    ]));
    var note = U.el("p", "band-note",
      "These are code law, shown read-only. Changing any of them is " +
      "a reviewed code decision, never a dashboard action.");
    b.appendChild(note);
    frame.appendChild(b);
    return frame;
  }
  function authorityCard() {
    var frame = U.el("div", "frame");
    var b = U.band("Authority", null);
    b.appendChild(U.stateBlock("unavailable",
      "No operator contract yet.",
      "Entry authority cannot be read, granted, or revoked from this " +
      "page, and nothing here can pause or arm the machine."));
    b.appendChild(U.el("p", "band-note",
      "Controls arrive with the operator service."));
    frame.appendChild(b);
    return frame;
  }

  function render(fresh) {
    if (fresh || !host) {
      var root = U.clearTo("pageroot");
      var wrap = U.el("div", "quantwrap");
      var left = U.el("div", "qcol");
      var leftFrame = U.el("div", "frame");
      var trailBand = U.band("Decision trail", "Recent — partial");
      leftFrame.appendChild(trailBand);
      left.appendChild(leftFrame);
      var right = U.el("div", "qcol");
      wrap.appendChild(left);
      wrap.appendChild(right);
      root.appendChild(wrap);
      host = { right: right, trailBand: trailBand };
      trail = U.liveTrail(trailBand);
    }
    host.right.textContent = "";
    host.right.appendChild(policyCard());
    host.right.appendChild(authorityCard());
    var events = trailEvents();
    trail.update(nowState(), events);
    var existing = host.trailBand.querySelector(".stateblock");
    if (existing) existing.remove();
    if (events.length === 0) {
      host.trailBand.appendChild(U.stateBlock("empty",
        "Activity detail unavailable.",
        "Only bounded records the machine exposes today appear here; " +
        "the full decision stream arrives with the quant read " +
        "contract."));
    }
  }

  window.QuantTabs.quant = {
    needs: ["decisions", "pnl", "fees"],
    init: function () {}, render: render
  };
})();
