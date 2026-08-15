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
    connections: ["Connections", ""],
    news: ["News", ""]
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
    stopNewsPoll();
    if (h === "connections") {
      pageEl.style.padding = "0";
      sigPage = pageEl;
      buildConnectionsPage(pageEl);
    } else if (h === "news") {
      pageEl.style.padding = "0";
      sigPage = null;
      buildNewsPage(pageEl);
      startNewsPoll();
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

  /* ---- News page: wide live layout — day strip, impact bars,
     sources rail with display toggles, refresh, fresh-row pulse ---- */
  var newsBtn = document.getElementById("rb-news");
  var newsData = null;
  var newsFilter = "all";
  var newsDay = "all";
  var newsTimer = null;
  var newsHost = null;
  var newsSeen = null;
  var newsSrcOff = {};
  var NEWS_SRC_KEY = "quant.news.srcoff";
  try {
    var rawOff = localStorage.getItem(NEWS_SRC_KEY);
    if (rawOff) { newsSrcOff = JSON.parse(rawOff) || {}; }
  } catch (e) { newsSrcOff = {}; }

  newsBtn.addEventListener("click", function () {
    location.hash = "#news";
  });

  function newsAgeText(asOf) {
    if (!asOf) { return "—"; }
    var then = Date.parse(asOf);
    if (!isFinite(then)) { return "—"; }
    var sec = Math.max(0, Math.round((Date.now() - then) / 1000));
    if (sec < 90) { return "updated " + sec + "s ago"; }
    var min = Math.round(sec / 60);
    if (min < 90) { return "updated " + min + "m ago"; }
    return "updated " + Math.round(min / 60) + "h ago";
  }

  function fetchNews(force) {
    var btn = newsHost ? newsHost.querySelector(".refresh-btn") : null;
    if (btn) { btn.classList.add("busy"); }
    fetch("/api/news", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        var prev = newsData && newsData.as_of;
        newsData = d;
        if (newsHost && (force || !prev || !d || prev !== d.as_of)) {
          renderNews();
        } else if (newsHost) {
          var upd = newsHost.querySelector(".news-upd");
          if (upd && d) { upd.textContent = newsAgeText(d.as_of); }
        }
        if (btn) { btn.classList.remove("busy"); }
      })
      .catch(function () {
        newsData = null;
        if (btn) { btn.classList.remove("busy"); }
        if (newsHost) { renderNews(); }
      });
  }

  function startNewsPoll() {
    fetchNews(true);
    if (!newsTimer) {
      newsTimer = setInterval(function () { fetchNews(false); }, 20000);
    }
  }

  function stopNewsPoll() {
    if (newsTimer) { clearInterval(newsTimer); newsTimer = null; }
    newsHost = null;
    newsSeen = null;
  }

  function utcDate(ts) {
    return (typeof ts === "string" && ts.length >= 10) ? ts.slice(0, 10) : "";
  }

  function barsSvg(level) {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("viewBox", "0 0 13 12");
    svg.setAttribute("class", "bars b" + level);
    var dims = [[0, 8, 3, 4, "r1"], [5, 5, 3, 7, "r2"], [10, 2, 3, 10, "r3"]];
    for (var i = 0; i < dims.length; i++) {
      var r = document.createElementNS("http://www.w3.org/2000/svg", "rect");
      r.setAttribute("x", dims[i][0]);
      r.setAttribute("y", dims[i][1]);
      r.setAttribute("width", dims[i][2]);
      r.setAttribute("height", dims[i][3]);
      r.setAttribute("rx", 1);
      r.setAttribute("class", dims[i][4]);
      svg.appendChild(r);
    }
    return svg;
  }

  function newsLevel(item) {
    return item.impact === 3 ? 3 : (item.impact === 2 ? 2 : 1);
  }

  function renderNews() {
    if (!newsHost) { return; }
    var prevSeen = newsSeen;
    newsHost.textContent = "";

    /* top row: eyebrow · day strip · refresh + age */
    var top = document.createElement("div");
    top.className = "news-top";
    var eyebrow = document.createElement("span");
    eyebrow.className = "news-eyebrow";
    eyebrow.textContent = "News";
    top.appendChild(eyebrow);

    var items = (newsData && newsData.present && newsData.items) || [];

    var strip = document.createElement("div");
    strip.className = "daystrip";
    var dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var byDay = {};
    for (var bi = 0; bi < items.length; bi++) {
      var dkey = utcDate(items[bi].ts);
      if (!byDay[dkey]) { byDay[dkey] = { n: 0, h: 0, m: 0 }; }
      byDay[dkey].n += 1;
      var lv = newsLevel(items[bi]);
      if (lv === 3) { byDay[dkey].h += 1; }
      if (lv === 2) { byDay[dkey].m += 1; }
    }
    var now = new Date();
    var dayDefs = [{ key: "all", label: "All", sub: items.length + " items" }];
    for (var dd = 6; dd >= 0; dd--) {
      var dt = new Date(now.getTime() - dd * 86400000);
      var key = dt.toISOString().slice(0, 10);
      var stats = byDay[key] || { n: 0, h: 0, m: 0 };
      dayDefs.push({
        key: key,
        label: (dd === 0 ? "Today" :
          dayNames[dt.getUTCDay()] + " " + dt.getUTCDate()),
        stats: stats
      });
    }
    for (var di = 0; di < dayDefs.length; di++) {
      (function (def) {
        var cell = document.createElement("button");
        cell.className = "day" + (newsDay === def.key ? " on" : "") +
          (def.stats && def.stats.n === 0 ? " dim" : "");
        var l1 = document.createElement("span");
        l1.className = "d1";
        l1.textContent = def.label;
        var l2 = document.createElement("span");
        l2.className = "d2";
        if (def.stats) {
          l2.textContent = def.stats.n + " · ";
          var hh = document.createElement("span");
          hh.className = "hh"; hh.textContent = "H" + def.stats.h;
          var sep = document.createTextNode(" ");
          var mm = document.createElement("span");
          mm.className = "mm"; mm.textContent = "M" + def.stats.m;
          l2.appendChild(hh); l2.appendChild(sep); l2.appendChild(mm);
        } else {
          l2.textContent = def.sub;
        }
        cell.appendChild(l1);
        cell.appendChild(l2);
        cell.addEventListener("click", function () {
          newsDay = def.key;
          renderNews();
        });
        strip.appendChild(cell);
      })(dayDefs[di]);
    }
    top.appendChild(strip);

    var right = document.createElement("div");
    right.className = "news-right";
    var refresh = document.createElement("button");
    refresh.className = "refresh-btn";
    refresh.setAttribute("aria-label", "Refresh");
    refresh.setAttribute("data-tip", "Refresh");
    var rsvg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    rsvg.setAttribute("viewBox", "0 0 24 24");
    rsvg.setAttribute("fill", "none");
    rsvg.setAttribute("stroke", "currentColor");
    rsvg.setAttribute("stroke-linecap", "round");
    rsvg.setAttribute("stroke-linejoin", "round");
    var rp1 = document.createElementNS("http://www.w3.org/2000/svg", "path");
    rp1.setAttribute("d", "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8");
    var rp2 = document.createElementNS("http://www.w3.org/2000/svg", "path");
    rp2.setAttribute("d", "M3 3v5h5");
    rsvg.appendChild(rp1); rsvg.appendChild(rp2);
    refresh.appendChild(rsvg);
    refresh.addEventListener("click", function () { fetchNews(true); });
    var upd = document.createElement("span");
    upd.className = "news-upd";
    right.appendChild(refresh);
    right.appendChild(upd);
    top.appendChild(right);
    newsHost.appendChild(top);

    if (!newsData || !newsData.present) {
      upd.textContent = "—";
      var empty = document.createElement("div");
      empty.className = "news-empty";
      empty.textContent = newsData
        ? "No news in the mirror yet — the fetch loop fills ui/live/news.jsonl."
        : "Server unreachable.";
      newsHost.appendChild(empty);
      newsSeen = null;
      return;
    }

    upd.textContent = newsAgeText(newsData.as_of);
    var ageSec = (Date.now() - Date.parse(newsData.as_of)) / 1000;
    upd.classList.toggle("stale", isFinite(ageSec) && ageSec > 300);

    /* day + source filtered pools */
    var dayPool = [];
    for (var i0 = 0; i0 < items.length; i0++) {
      if (newsDay === "all" || utcDate(items[i0].ts) === newsDay) {
        dayPool.push(items[i0]);
      }
    }
    var srcCounts = {};
    for (var i1 = 0; i1 < dayPool.length; i1++) {
      var nm = dayPool[i1].source || "—";
      srcCounts[nm] = (srcCounts[nm] || 0) + 1;
    }
    var pool = [];
    for (var i2 = 0; i2 < dayPool.length; i2++) {
      if (!newsSrcOff[dayPool[i2].source || "—"]) { pool.push(dayPool[i2]); }
    }

    /* impact pills with counts */
    var counts = { 3: 0, 2: 0, 1: 0 };
    for (var ci = 0; ci < pool.length; ci++) { counts[newsLevel(pool[ci])] += 1; }
    var filters = document.createElement("div");
    filters.className = "news-filters";
    var defs = [["all", "All", null],
      ["3", "High", 3], ["2", "Medium", 2], ["1", "Low", 1]];
    for (var fi = 0; fi < defs.length; fi++) {
      (function (val, label, lvl) {
        var b = document.createElement("button");
        b.className = "fpill" + (newsFilter === val ? " on" : "");
        if (lvl) { b.appendChild(barsSvg(lvl)); }
        b.appendChild(document.createTextNode(label));
        var c = document.createElement("span");
        c.className = "cnt";
        c.textContent = lvl ? String(counts[lvl]) : String(pool.length);
        b.appendChild(c);
        b.addEventListener("click", function () {
          newsFilter = val;
          renderNews();
        });
        filters.appendChild(b);
      })(defs[fi][0], defs[fi][1], defs[fi][2]);
    }
    var total = document.createElement("span");
    total.className = "news-total";
    total.textContent = "UTC";
    filters.appendChild(total);
    newsHost.appendChild(filters);

    /* body: list + sources rail */
    var body = document.createElement("div");
    body.className = "news-body";
    var main = document.createElement("div");
    main.className = "news-main";
    var list = document.createElement("div");
    var hrow = document.createElement("div");
    hrow.className = "news-hrow";
    var labels = ["Time", "", "Source", "Headline", "Category"];
    for (var li = 0; li < labels.length; li++) {
      var hcell = document.createElement("span");
      hcell.textContent = labels[li];
      hrow.appendChild(hcell);
    }
    list.appendChild(hrow);

    var seenNow = {};
    var shown = 0;
    for (var ri = 0; ri < pool.length; ri++) {
      var it = pool[ri];
      var lvl2 = newsLevel(it);
      if (newsFilter !== "all" && String(lvl2) !== newsFilter) { continue; }
      shown += 1;
      seenNow[it.id] = true;
      var row = document.createElement("div");
      row.className = "news-row" +
        (prevSeen && !prevSeen[it.id] ? " fresh" : "");
      var tm = document.createElement("span");
      tm.className = "n-time";
      tm.textContent = (typeof it.ts === "string" && it.ts.length >= 16)
        ? it.ts.slice(11, 16) : "—";
      var impCell = document.createElement("span");
      impCell.appendChild(barsSvg(lvl2));
      var src = document.createElement("span");
      src.className = "n-src";
      src.textContent = it.source || "—";
      var hd = document.createElement("span");
      hd.className = "n-head";
      var link;
      if (it.url) {
        link = document.createElement("a");
        link.href = it.url;
        link.target = "_blank";
        link.rel = "noopener noreferrer";
      } else {
        link = document.createElement("span");
      }
      link.textContent = it.title || "—";
      link.title = it.title || "";
      hd.appendChild(link);
      var cat = document.createElement("span");
      cat.className = "n-cat";
      cat.textContent = it.category || "—";
      row.appendChild(tm);
      row.appendChild(impCell);
      row.appendChild(src);
      row.appendChild(hd);
      row.appendChild(cat);
      list.appendChild(row);
    }
    main.appendChild(list);
    if (!shown) {
      var none = document.createElement("div");
      none.className = "news-empty";
      none.textContent = "Nothing matches these filters in the mirror.";
      main.appendChild(none);
    }
    body.appendChild(main);

    /* sources rail */
    var side = document.createElement("div");
    side.className = "news-side";
    var sh = document.createElement("div");
    sh.className = "side-h";
    sh.textContent = "Sources";
    side.appendChild(sh);
    var srcNames = Object.keys(srcCounts).sort(function (a, b) {
      return srcCounts[b] - srcCounts[a];
    }).slice(0, 14);
    for (var si = 0; si < srcNames.length; si++) {
      (function (name) {
        var rowS = document.createElement("div");
        rowS.className = "src-row" + (newsSrcOff[name] ? " off" : "");
        var nm2 = document.createElement("span");
        nm2.className = "src-name";
        nm2.textContent = name;
        nm2.title = name;
        var cn = document.createElement("span");
        cn.className = "src-cnt";
        cn.textContent = String(srcCounts[name]);
        var sw = document.createElement("button");
        sw.className = "switch sm";
        sw.setAttribute("role", "switch");
        sw.setAttribute("aria-checked", newsSrcOff[name] ? "false" : "true");
        sw.setAttribute("aria-label", name + " feed");
        var knob = document.createElement("span");
        knob.className = "knob";
        sw.appendChild(knob);
        sw.addEventListener("click", function () {
          if (newsSrcOff[name]) { delete newsSrcOff[name]; }
          else { newsSrcOff[name] = true; }
          try {
            localStorage.setItem(NEWS_SRC_KEY, JSON.stringify(newsSrcOff));
          } catch (e) { /* private mode */ }
          renderNews();
        });
        rowS.appendChild(nm2);
        rowS.appendChild(cn);
        rowS.appendChild(sw);
        side.appendChild(rowS);
      })(srcNames[si]);
    }
    var note = document.createElement("div");
    note.className = "side-note";
    note.textContent =
      "Toggles filter this feed. Upstream shutdown per source lands with the wiring build.";
    side.appendChild(note);
    body.appendChild(side);
    newsHost.appendChild(body);

    newsSeen = seenNow;
  }

  function buildNewsPage(container) {
    var page = document.createElement("div");
    page.className = "news-page";
    container.appendChild(page);
    newsHost = page;
    renderNews();
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
