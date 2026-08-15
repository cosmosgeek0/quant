/* Quant shell — clock, collapsible sidebar, tooltips, router, and the
   account popup (hover the bottom avatar; click it for #settings).
   Theme: system/light/dark, persisted, system tracked live. Log out is
   real on the hosted site (Cloudflare Access endpoint) and honestly
   disabled on this Mac where no session exists. */
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

  /* ---- elements ---- */
  var side = document.getElementById("side");
  var brandBtn = document.getElementById("brand-btn");
  var collapseBtn = document.getElementById("side-collapse");
  var tip = document.getElementById("tip");
  var pop = document.getElementById("pop");
  var profile = document.getElementById("profile");
  var logoutRow = document.getElementById("pop-logout");
  var tipTimer = null;
  var KEY = "quant.side";
  var THEME_KEY = "quant.theme";
  var isMac = /Mac|iP(hone|ad|od)/.test(navigator.platform);
  var KBD = isMac ? "⌘" : "Ctrl";

  /* ---- theme: system | light | dark ---- */
  var mql = window.matchMedia("(prefers-color-scheme: dark)");
  var themeChoice = "system";
  try {
    var t = localStorage.getItem(THEME_KEY);
    if (t === "light" || t === "dark" || t === "system") { themeChoice = t; }
  } catch (e) { /* private mode */ }

  function applyTheme() {
    var resolved = themeChoice === "system"
      ? (mql.matches ? "dark" : "light") : themeChoice;
    document.documentElement.setAttribute("data-theme", resolved);
    var btns = pop.querySelectorAll(".tbtn");
    for (var i = 0; i < btns.length; i++) {
      btns[i].classList.toggle(
        "on", btns[i].getAttribute("data-theme-set") === themeChoice);
    }
  }

  function setTheme(choice) {
    themeChoice = choice;
    try { localStorage.setItem(THEME_KEY, choice); } catch (e) { /* ok */ }
    applyTheme();
  }

  if (mql.addEventListener) {
    mql.addEventListener("change", function () {
      if (themeChoice === "system") { applyTheme(); }
    });
  }
  applyTheme();

  pop.addEventListener("click", function (ev) {
    var btn = ev.target && ev.target.closest
      ? ev.target.closest("[data-theme-set]") : null;
    if (btn) { setTheme(btn.getAttribute("data-theme-set")); }
  });

  /* ---- sidebar state ---- */
  function state() { return side.getAttribute("data-state"); }

  function apply(next) {
    side.setAttribute("data-state", next);
    var expanded = next === "expanded";
    brandBtn.setAttribute("aria-label", expanded ? "Quant" : "Expand sidebar");
    if (expanded) { brandBtn.removeAttribute("data-tip"); }
    else { brandBtn.setAttribute("data-tip", "Expand"); }
    hideTip();
    closePop();
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
    if (ev.key === "Escape") { closePop(); }
  });

  /* ---- router: pages honest about their state ---- */
  var pageEl = document.getElementById("page");
  var rows = document.querySelectorAll(".nav-row[data-page]");
  var PAGES = {
    overview: ["Overview", "Empty by decision — sections arrive one at a time on your word."],
    basis: ["Basis", "Not built yet — it arrives when you order it."],
    momentum: ["Momentum", "Not built yet — it arrives when you order it."],
    machine: ["Machine", "Not built yet — it arrives when you order it."],
    journal: ["Journal", "Not built yet — it arrives when you order it."],
    settings: ["Settings", "Not built yet — it arrives when you order it."]
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
    closePop();
  }

  window.addEventListener("hashchange", route);
  route();

  /* ---- account popup: hover opens, click goes to settings ---- */
  var popTimer = null;

  function openPop() {
    if (popTimer) { clearTimeout(popTimer); popTimer = null; }
    if (!pop.hidden) { return; }
    pop.hidden = false;
    var r = profile.getBoundingClientRect();
    var w = pop.offsetWidth;
    var h = pop.offsetHeight;
    var left = Math.min(Math.max(8, Math.round(r.left - 4)),
      window.innerWidth - w - 8);
    var top = Math.max(8, Math.round(r.top - h - 10));
    pop.style.left = left + "px";
    pop.style.top = top + "px";
    profile.setAttribute("aria-expanded", "true");
    requestAnimationFrame(function () { pop.classList.add("on"); });
  }

  function closePop() {
    if (popTimer) { clearTimeout(popTimer); popTimer = null; }
    if (pop.hidden) { return; }
    pop.classList.remove("on");
    profile.setAttribute("aria-expanded", "false");
    pop.hidden = true;
  }

  function scheduleClose() {
    if (popTimer) { clearTimeout(popTimer); }
    popTimer = setTimeout(closePop, 250);
  }

  profile.addEventListener("mouseenter", openPop);
  profile.addEventListener("mouseleave", scheduleClose);
  pop.addEventListener("mouseenter", function () {
    if (popTimer) { clearTimeout(popTimer); popTimer = null; }
  });
  pop.addEventListener("mouseleave", scheduleClose);
  profile.addEventListener("click", function () {
    location.hash = "#settings";
  });
  document.addEventListener("mousedown", function (ev) {
    if (!pop.hidden && !pop.contains(ev.target) &&
        !profile.contains(ev.target)) { closePop(); }
  });

  /* one live poll feeds the avatar dot (online/offline) and the
     right-rail Signal tint (green fresh / amber stale / red dead),
     from /api/strip mirror ages — measured, never decorative. */
  var avatarEl = profile.querySelector(".avatar");
  var sigBtn = document.getElementById("rb-signal");
  var lastStrip = null;

  function setOnline(ok) {
    avatarEl.classList.toggle("on", !!ok);
    profile.title = ok
      ? "Online — data plane answering"
      : "Offline — data plane unreachable";
  }

  function sigClass(cls) {
    sigBtn.classList.remove("sig-ok", "sig-stale", "sig-bad");
    if (cls) { sigBtn.classList.add(cls); }
  }

  function pollStrip() {
    fetch("/api/strip", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        lastStrip = d;
        setOnline(!!d);
        if (!d) { sigClass("sig-bad"); renderSignal(); return; }
        var age = d.pull && d.pull.age_seconds;
        if (age == null || !isFinite(age)) { sigClass("sig-bad"); }
        else if (age > 120) { sigClass("sig-bad"); }
        else if (age > 45) { sigClass("sig-stale"); }
        else { sigClass("sig-ok"); }
        renderSignal();
      })
      .catch(function () {
        lastStrip = null;
        setOnline(false);
        sigClass("sig-bad");
        renderSignal();
      });
  }
  pollStrip();
  setInterval(pollStrip, 10000);

  /* ---- right rail panels (TradingView pattern) ---- */
  var rpanel = document.getElementById("rpanel");
  var rtitle = document.getElementById("rpanel-title");
  var rbody = document.getElementById("rpanel-body");
  var rbtns = document.querySelectorAll(".rbtn[data-panel]");
  var RKEY = "quant.rpanel";
  var rOpen = null;

  var RPANELS = {
    watchlist: ["Watchlist",
      "Empty — tokens arrive when you order them."],
    news: ["News",
      "No source wired yet — arrives with a data contract."],
    calendar: ["Calendar",
      "No source wired yet — arrives with a data contract."]
  };

  function ageText2(sec) {
    if (sec == null || !isFinite(sec)) { return "—"; }
    var v = Math.round(sec);
    if (v < 120) { return v + "s"; }
    return Math.floor(v / 60) + "m " + (v % 60) + "s";
  }

  function ageCls(sec, warnAt, badAt) {
    if (sec == null || !isFinite(sec)) { return "v bad"; }
    if (sec > badAt) { return "v bad"; }
    if (sec > warnAt) { return "v warn"; }
    return "v ok";
  }

  function sigRow(k, vText, vCls) {
    var row = document.createElement("div");
    row.className = "sigrow";
    var kk = document.createElement("span");
    kk.className = "k"; kk.textContent = k;
    var vv = document.createElement("span");
    vv.className = vCls; vv.textContent = vText;
    row.appendChild(kk); row.appendChild(vv);
    return row;
  }

  function renderSignal() {
    if (rOpen !== "signal") { return; }
    rbody.textContent = "";
    if (!lastStrip) {
      rbody.appendChild(sigRow("Server", "unreachable", "v bad"));
      return;
    }
    var d = lastStrip;
    rbody.appendChild(sigRow("Server", "answering", "v ok"));
    var pl = d.pull || {};
    rbody.appendChild(sigRow("Mirror age",
      ageText2(pl.age_seconds), ageCls(pl.age_seconds, 45, 120)));
    var mp = d.market_pass || {};
    rbody.appendChild(sigRow("Market pass",
      ageText2(mp.age_seconds), ageCls(mp.age_seconds, 45, 120)));
    var sv = d.supervisor || {};
    rbody.appendChild(sigRow("Supervisor",
      ageText2(sv.age_seconds), ageCls(sv.age_seconds, 240, 600)));
  }

  function renderPanel(key) {
    if (key === "signal") {
      rtitle.textContent = "Signal";
      renderSignal();
      return;
    }
    var def = RPANELS[key];
    rtitle.textContent = def[0];
    rbody.textContent = "";
    var b = document.createElement("b");
    b.textContent = def[0] + ".";
    rbody.appendChild(b);
    rbody.appendChild(document.createTextNode(" " + def[1]));
  }

  function setRPanel(key) {
    rOpen = key;
    rpanel.classList.toggle("open", !!key);
    for (var i = 0; i < rbtns.length; i++) {
      var on = rbtns[i].getAttribute("data-panel") === key;
      rbtns[i].classList.toggle("active", on);
      rbtns[i].setAttribute("aria-pressed", on ? "true" : "false");
    }
    if (key) { renderPanel(key); }
    try {
      if (key) { localStorage.setItem(RKEY, key); }
      else { localStorage.removeItem(RKEY); }
    } catch (e) { /* private mode */ }
  }

  for (var ri = 0; ri < rbtns.length; ri++) {
    (function (btn) {
      btn.addEventListener("click", function () {
        var key = btn.getAttribute("data-panel");
        setRPanel(rOpen === key ? null : key);
      });
    })(rbtns[ri]);
  }

  var rSaved = null;
  try { rSaved = localStorage.getItem(RKEY); } catch (e) { rSaved = null; }
  if (rSaved && (rSaved === "signal" ||
      Object.prototype.hasOwnProperty.call(RPANELS, rSaved))) {
    setRPanel(rSaved);
  }

  /* log out: real endpoint on the hosted site only.
     OWNER NOTE 2026-08-15 (verify at next deploy): on
     arbitrage.cosmosgeek.org the row must hit /cdn-cgi/access/logout so
     Cloudflare Access (YubiKey login, ~7-day session) drops the session
     and locks the front end for this browser until the next login. */
  var local = location.hostname === "127.0.0.1" ||
    location.hostname === "localhost";
  if (local) {
    logoutRow.classList.add("inert");
    logoutRow.removeAttribute("href");
    logoutRow.setAttribute("title",
      "No session on this Mac — live on the hosted site");
  }

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
    tip.style.left = "0px";
    tip.style.top = "0px";
    tip.setAttribute("aria-hidden", "false");
    var w = tip.offsetWidth;
    var h = tip.offsetHeight;
    var onRight = !!el.closest(".rrail");
    tip.style.left = Math.round(
      onRight ? r.left - w - 10 : r.right + 10) + "px";
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
