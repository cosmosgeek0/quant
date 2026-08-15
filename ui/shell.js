/* Quant shell — UTC clock only.
   The status strip was REMOVED by owner rejection 2026-08-15: no
   watchdog numbers in the chrome until a real watchdog exists and the
   owner orders its display. Nothing here polls the mirror. */
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
})();
