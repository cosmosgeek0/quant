/* Quant shell — UTC clock + live truth strip.
   Reads /api/strip only; renders ages as measured, absence as absence.
   No authority claims here: the mirror does not carry STOP flags yet,
   so machine state is deliberately not shown. */
"use strict";

(function () {
  var clockEl = document.getElementById("clock");

  function two(n) { return n < 10 ? "0" + n : "" + n; }

  function tickClock() {
    var d = new Date();
    clockEl.textContent =
      two(d.getUTCHours()) + ":" + two(d.getUTCMinutes()) + ":" +
      two(d.getUTCSeconds()) + " UTC";
  }
  tickClock();
  setInterval(tickClock, 1000);

  var els = {
    market: document.getElementById("c-market"),
    superv: document.getElementById("c-super"),
    pull: document.getElementById("c-pull")
  };

  function ageText(seconds) {
    if (seconds == null || !isFinite(seconds)) { return "—"; }
    var s = Math.round(seconds);
    if (s < 120) { return s + "s"; }
    return Math.floor(s / 60) + "m " + (s % 60) + "s";
  }

  function cls(level) {
    if (level === "green") { return "v ok"; }
    if (level === "amber") { return "v warn"; }
    if (level === "red") { return "v bad"; }
    return "v off";
  }

  function ageLevel(seconds, warnAt, badAt) {
    if (seconds == null || !isFinite(seconds)) { return "off"; }
    if (seconds > badAt) { return "red"; }
    if (seconds > warnAt) { return "amber"; }
    return "green";
  }

  function setChip(el, seconds, level) {
    el.textContent = ageText(seconds);
    el.className = cls(level);
  }

  function setAbsent() {
    setChip(els.market, null, "off");
    setChip(els.superv, null, "off");
    setChip(els.pull, null, "off");
  }

  var inFlight = false;

  function poll() {
    if (inFlight) { return; }
    inFlight = true;
    fetch("/api/strip", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        inFlight = false;
        if (!d || d.ok !== true) { setAbsent(); return; }
        var mp = d.market_pass || {};
        var sv = d.supervisor || {};
        var pl = d.pull || {};
        setChip(els.market, mp.age_seconds,
          mp.state === "live" ? ageLevel(mp.age_seconds, 45, 120) : "off");
        setChip(els.superv, sv.age_seconds,
          sv.level || ageLevel(sv.age_seconds, 240, 600));
        setChip(els.pull, pl.age_seconds,
          pl.level || ageLevel(pl.age_seconds, 45, 120));
      })
      .catch(function () { inFlight = false; setAbsent(); });
  }

  poll();
  setInterval(poll, 2000);
})();
