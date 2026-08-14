(function registerClockChip() {
  "use strict";

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function pad2(value) {
    return String(value).padStart(2, "0");
  }

  function clockZone() {
    if (typeof window.QuantParts.getPrefs !== "function") return "UTC";

    const prefs = window.QuantParts.getPrefs();
    return prefs && prefs.clockZone === "IST" ? "IST" : "UTC";
  }

  window.QuantParts = window.QuantParts || {};
  window.QuantParts.clockChip = function clockChip() {
    const node = el("span", "s4-clock");
    const icon = el("span", "ic s4-clock__icon");
    const time = el("span", "s4-clock__time");
    const separator = el("span", "s4-clock__separator");
    const zone = el("span", "s4-clock__zone");

    node.setAttribute("role", "timer");
    icon.setAttribute("aria-hidden", "true");
    icon.style.setProperty("--ic", "url(vendor/icons/clock.svg)");
    separator.setAttribute("aria-hidden", "true");
    node.append(icon, time, separator, zone);

    function tick() {
      const now = new Date();
      const activeZone = clockZone();
      const offsetMinutes = activeZone === "IST" ? 330 : 0;
      const zonedTime = new Date(now.getTime() + offsetMinutes * 60 * 1000);
      const display = [
        pad2(zonedTime.getUTCHours()),
        pad2(zonedTime.getUTCMinutes()),
        pad2(zonedTime.getUTCSeconds()),
      ].join(":");

      time.textContent = display;
      zone.textContent = activeZone;
      node.setAttribute("aria-label", "Current time " + display + " " + activeZone);
    }

    tick();
    return { node: node, tick: tick };
  };
})();
