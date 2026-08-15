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
    settings: ["Settings", "Not built yet — it arrives when you order it."],
    connections: ["Connections", ""]
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
    pageEl.textContent = "";
    if (h === "connections") {
      pageEl.style.padding = "0";
      sigPage = pageEl;
      buildConnectionsPage(pageEl);
    } else {
      pageEl.style.padding = "";
      sigPage = null;
      var b = document.createElement("b");
      b.textContent = PAGES[h][0] + ".";
      pageEl.appendChild(b);
      pageEl.appendChild(document.createTextNode(" " + PAGES[h][1]));
    }
    closePop();
  }

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

  var lastRtt = null;

  function pollStrip() {
    var t0 = performance.now();
    fetch("/api/strip", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        lastRtt = Math.round(performance.now() - t0);
        lastStrip = d;
        setOnline(!!d);
        if (!d) { sigClass("sig-bad"); renderSigAll(); return; }
        var age = d.pull && d.pull.age_seconds;
        if (age == null || !isFinite(age)) { sigClass("sig-bad"); }
        else if (age > 120) { sigClass("sig-bad"); }
        else if (age > 45) { sigClass("sig-stale"); }
        else { sigClass("sig-ok"); }
        renderSigAll();
      })
      .catch(function () {
        lastRtt = null;
        lastStrip = null;
        setOnline(false);
        sigClass("sig-bad");
        renderSigAll();
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

  var sigPop = document.getElementById("sigpop");
  var sigRowsEl = document.getElementById("sigpop-rows");
  var sigAsOf = document.getElementById("sig-asof");

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

  function buildSigRows(target) {
    target.textContent = "";
    if (!lastStrip) {
      target.appendChild(sigRow("Web server", "unreachable", "v bad"));
      target.appendChild(sigRow("Round trip", "—", "v bad"));
      return;
    }
    var d = lastStrip;
    target.appendChild(sigRow("Web server", "answering", "v ok"));
    target.appendChild(sigRow("Round trip",
      lastRtt == null ? "—" : lastRtt + " ms",
      lastRtt != null && lastRtt < 100 ? "v ok" : "v warn"));
    var pl = d.pull || {};
    target.appendChild(sigRow("Data mirror",
      ageText2(pl.age_seconds), ageCls(pl.age_seconds, 45, 120)));
    var mp = d.market_pass || {};
    target.appendChild(sigRow("Market feed",
      ageText2(mp.age_seconds), ageCls(mp.age_seconds, 45, 120)));
    var sv = d.supervisor || {};
    target.appendChild(sigRow("Supervisor",
      ageText2(sv.age_seconds), ageCls(sv.age_seconds, 240, 600)));
    if (sigAsOf) { sigAsOf.textContent = d.as_of || "—"; }
  }

  /* the Connections page: real rows live-tinted; rows the browser
     cannot measure are honestly "not wired". Toggles are visual until
     the shutdown wiring lands (separate build, owner-sanctioned). */
  var CONNECTIONS = [
    { id: "server", name: "Web server", detail: "dashboard ↔ data plane", live: true },
    { id: "mirror", name: "Data mirror", detail: "host → this machine", live: true },
    { id: "market", name: "Market feed", detail: "exchange stream pass", live: true },
    { id: "supervisor", name: "Supervisor", detail: "machine heartbeat", live: true },
    { id: "host", name: "EC2 host", detail: "AWS runtime", live: false },
    { id: "binance", name: "Binance API", detail: "exchange account", live: false },
    { id: "news", name: "News source", detail: "no source wired", live: false },
    { id: "calendar", name: "Calendar source", detail: "no source wired", live: false }
  ];
  var connState = {};
  for (var ci = 0; ci < CONNECTIONS.length; ci++) {
    connState[CONNECTIONS[ci].id] = true;
  }

  function connLive(id) {
    if (!lastStrip) {
      return { cls: "bad", val: id === "server" ? "unreachable" : "—" };
    }
    var d = lastStrip;
    if (id === "server") {
      return { cls: "ok", val: lastRtt == null ? "answering" : lastRtt + " ms" };
    }
    if (id === "mirror") {
      var a = d.pull && d.pull.age_seconds;
      return { cls: ageCls(a, 45, 120).replace("v ", ""), val: ageText2(a) };
    }
    if (id === "market") {
      var m = d.market_pass && d.market_pass.age_seconds;
      return { cls: ageCls(m, 45, 120).replace("v ", ""), val: ageText2(m) };
    }
    if (id === "supervisor") {
      var sv2 = d.supervisor || {};
      var s2 = sv2.age_seconds;
      var st = sv2.reported_status ? " · " + sv2.reported_status : "";
      return { cls: ageCls(s2, 240, 600).replace("v ", ""), val: ageText2(s2) + st };
    }
    return { cls: "", val: "not wired" };
  }

  function refreshConnRows() {
    if (!sigPage) { return; }
    var rows2 = sigPage.querySelectorAll(".conn-row");
    for (var i = 0; i < rows2.length; i++) {
      var id = rows2[i].getAttribute("data-conn");
      var def = null;
      for (var k = 0; k < CONNECTIONS.length; k++) {
        if (CONNECTIONS[k].id === id) { def = CONNECTIONS[k]; }
      }
      var dot = rows2[i].querySelector(".conn-dot");
      var val = rows2[i].querySelector(".conn-val");
      var on = connState[id];
      rows2[i].classList.toggle("off", !on);
      if (!on) {
        dot.className = "conn-dot";
        val.textContent = "off";
        continue;
      }
      if (def && def.live) {
        var lv = connLive(id);
        dot.className = "conn-dot" + (lv.cls ? " " + lv.cls : "");
        val.textContent = lv.val;
      } else {
        dot.className = "conn-dot";
        val.textContent = "not wired";
      }
    }
  }

  function buildConnectionsPage(container) {
    container.textContent = "";
    var page = document.createElement("div");
    page.className = "conn-page";
    var t = document.createElement("div");
    t.className = "conn-title";
    t.textContent = "Connections";
    var sub = document.createElement("div");
    sub.className = "conn-sub";
    sub.textContent =
      "Every line this product depends on, and its live state.";
    var note = document.createElement("div");
    note.className = "conn-note";
    note.textContent =
      "Toggles are visual for now — shutdown wiring lands in a separate build.";
    page.appendChild(t);
    page.appendChild(sub);
    page.appendChild(note);
    var list = document.createElement("div");
    list.className = "conn-list";
    for (var i = 0; i < CONNECTIONS.length; i++) {
      (function (def) {
        var row = document.createElement("div");
        row.className = "conn-row";
        row.setAttribute("data-conn", def.id);
        var dot = document.createElement("span");
        dot.className = "conn-dot";
        var main = document.createElement("div");
        main.className = "conn-main";
        var nm = document.createElement("div");
        nm.className = "conn-name"; nm.textContent = def.name;
        var dt = document.createElement("div");
        dt.className = "conn-detail"; dt.textContent = def.detail;
        main.appendChild(nm); main.appendChild(dt);
        var val = document.createElement("span");
        val.className = "conn-val"; val.textContent = "—";
        var sw = document.createElement("button");
        sw.className = "switch";
        sw.setAttribute("role", "switch");
        sw.setAttribute("aria-checked", connState[def.id] ? "true" : "false");
        sw.setAttribute("aria-label", def.name + " connection");
        var knob = document.createElement("span");
        knob.className = "knob";
        sw.appendChild(knob);
        sw.addEventListener("click", function () {
          connState[def.id] = !connState[def.id];
          sw.setAttribute("aria-checked",
            connState[def.id] ? "true" : "false");
          refreshConnRows();
        });
        row.appendChild(dot);
        row.appendChild(main);
        row.appendChild(val);
        row.appendChild(sw);
        list.appendChild(row);
      })(CONNECTIONS[i]);
    }
    page.appendChild(list);
    container.appendChild(page);
    refreshConnRows();
  }

  function renderSigAll() {
    if (!sigPop.hidden) { buildSigRows(sigRowsEl); }
    if ((location.hash || "").slice(1) === "connections" && sigPage) {
      refreshConnRows();
    }
  }

  function renderPanel(key) {
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
  if (rSaved && Object.prototype.hasOwnProperty.call(RPANELS, rSaved)) {
    setRPanel(rSaved);
  }

  /* ---- signal popup (hover) + dedicated page (click) ---- */
  var sigPage = null;
  var sigTimer = null;

  function openSigPop() {
    if (sigTimer) { clearTimeout(sigTimer); sigTimer = null; }
    if (!sigPop.hidden) { return; }
    sigPop.hidden = false;
    buildSigRows(sigRowsEl);
    var r = sigBtn.getBoundingClientRect();
    var w = sigPop.offsetWidth;
    var h = sigPop.offsetHeight;
    var left = Math.max(12, Math.round(r.left - w - 12));
    var top = Math.min(
      Math.max(12, Math.round(r.top + r.height / 2 - h / 2)),
      window.innerHeight - h - 12);
    sigPop.style.left = left + "px";
    sigPop.style.top = top + "px";
    sigBtn.setAttribute("aria-expanded", "true");
    requestAnimationFrame(function () { sigPop.classList.add("on"); });
  }

  function closeSigPop() {
    if (sigTimer) { clearTimeout(sigTimer); sigTimer = null; }
    if (sigPop.hidden) { return; }
    sigPop.classList.remove("on");
    sigBtn.setAttribute("aria-expanded", "false");
    sigPop.hidden = true;
  }

  function scheduleSigClose() {
    if (sigTimer) { clearTimeout(sigTimer); }
    sigTimer = setTimeout(closeSigPop, 250);
  }

  sigBtn.addEventListener("mouseenter", openSigPop);
  sigBtn.addEventListener("mouseleave", scheduleSigClose);
  sigPop.addEventListener("mouseenter", function () {
    if (sigTimer) { clearTimeout(sigTimer); sigTimer = null; }
  });
  sigPop.addEventListener("mouseleave", scheduleSigClose);
  sigBtn.addEventListener("click", function () {
    closeSigPop();
    location.hash = "#connections";
  });

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

  /* boot the router last — routes reference structures defined above */
  window.addEventListener("hashchange", route);
  route();
})();
