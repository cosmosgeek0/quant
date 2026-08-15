/* Quant shell — clock, Dune-grade collapsible sidebar, tooltips.
   Collapsed (default): clicking the mark expands. Expanded: the
   arrow-to-line button collapses. cmd/ctrl+\ toggles from anywhere.
   One floating tooltip element serves every data-tip control. */
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

  /* ---- sidebar state ---- */
  var side = document.getElementById("side");
  var brandBtn = document.getElementById("brand-btn");
  var collapseBtn = document.getElementById("side-collapse");
  var tip = document.getElementById("tip");
  var tipTimer = null;
  var KEY = "quant.side";
  var isMac = /Mac|iP(hone|ad|od)/.test(navigator.platform);
  var KBD = isMac ? "⌘" : "Ctrl";

  function state() { return side.getAttribute("data-state"); }

  function apply(next) {
    side.setAttribute("data-state", next);
    var expanded = next === "expanded";
    brandBtn.setAttribute("aria-label", expanded ? "Quant" : "Expand sidebar");
    if (expanded) { brandBtn.removeAttribute("data-tip"); }
    else { brandBtn.setAttribute("data-tip", "Expand"); }
    hideTip();
  }

  function toggle() {
    var next = state() === "expanded" ? "collapsed" : "expanded";
    apply(next);
    try { localStorage.setItem(KEY, next); } catch (e) { /* private mode */ }
  }

  var saved = null;
  try { saved = localStorage.getItem(KEY); } catch (e) { saved = null; }
  apply(saved === "expanded" ? "expanded" : "collapsed");

  brandBtn.addEventListener("click", function () {
    if (state() === "collapsed") { toggle(); }
  });
  collapseBtn.addEventListener("click", function () {
    if (state() === "expanded") { toggle(); }
  });
  document.addEventListener("keydown", function (ev) {
    if (ev.key === "\\" && (isMac ? ev.metaKey : ev.ctrlKey)) {
      ev.preventDefault();
      toggle();
    }
  });

  /* ---- router: five pages, each honest about its state ---- */
  var pageEl = document.getElementById("page");
  var rows = document.querySelectorAll(".nav-row[data-page]");
  var PAGES = {
    overview: ["Overview", "Empty by decision — sections arrive one at a time on your word."],
    basis: ["Basis", "Not built yet — it arrives when you order it."],
    momentum: ["Momentum", "Not built yet — it arrives when you order it."],
    machine: ["Machine", "Not built yet — it arrives when you order it."],
    journal: ["Journal", "Not built yet — it arrives when you order it."]
  };

  function route() {
    var h = (location.hash || "#overview").slice(1);
    if (!Object.prototype.hasOwnProperty.call(PAGES, h)) { h = "overview"; }
    for (var i = 0; i < rows.length; i++) {
      var on = rows[i].getAttribute("data-page") === h;
      rows[i].classList.toggle("active", on);
      if (on) { rows[i].setAttribute("aria-current", "page"); }
      else { rows[i].removeAttribute("aria-current"); }
    }
    var b = document.createElement("b");
    b.textContent = PAGES[h][0] + ".";
    pageEl.textContent = "";
    pageEl.appendChild(b);
    pageEl.appendChild(document.createTextNode(" " + PAGES[h][1]));
  }

  window.addEventListener("hashchange", route);
  route();

  /* ---- one floating tooltip for every [data-tip] ---- */
  function showTip(el) {
    var text = el.getAttribute("data-tip");
    if (!text) { return; }
    tip.textContent = text;
    if (el.getAttribute("data-kbd")) {
      var k1 = document.createElement("span");
      k1.className = "kbd"; k1.textContent = KBD;
      var k2 = document.createElement("span");
      k2.className = "kbd"; k2.textContent = "\\";
      tip.appendChild(k1); tip.appendChild(k2);
    }
    var r = el.getBoundingClientRect();
    tip.style.left = Math.round(r.right + 10) + "px";
    tip.style.top = "0px";
    tip.setAttribute("aria-hidden", "false");
    /* measure after content set, then center vertically */
    var h = tip.offsetHeight;
    tip.style.top = Math.round(r.top + r.height / 2 - h / 2) + "px";
    tip.classList.add("on");
  }

  function hideTip() {
    if (tipTimer) { clearTimeout(tipTimer); tipTimer = null; }
    tip.classList.remove("on");
    tip.setAttribute("aria-hidden", "true");
  }

  document.addEventListener("mouseover", function (ev) {
    var el = ev.target && ev.target.closest
      ? ev.target.closest("[data-tip]") : null;
    if (!el) { return; }
    if (tipTimer) { clearTimeout(tipTimer); }
    tipTimer = setTimeout(function () { showTip(el); }, 150);
  });
  document.addEventListener("mouseout", function (ev) {
    var el = ev.target && ev.target.closest
      ? ev.target.closest("[data-tip]") : null;
    if (el) { hideTip(); }
  });
  document.addEventListener("scroll", hideTip, true);
})();
