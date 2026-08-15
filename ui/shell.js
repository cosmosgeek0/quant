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

  /* ---- News page v4: two-column — list left; Sources box +
     Activity box right. Real favicons (mirrored same-origin),
     97-source board with live/delayed/error/pending statuses. ---- */
  var newsBtn = document.getElementById("rb-news");
  var newsData = null;
  var newsFilter = "all";
  var newsCat = "all";
  var newsDay = "all";
  var newsTimer = null;
  var newsHost = null;
  var newsSeen = null;
  var newsSrcOff = {};
  var newsMaster = true;
  var srcSearch = "";
  var NEWS_SRC_KEY = "quant.news.srcoff";
  var NEWS_MASTER_KEY = "quant.news.master";
  try {
    var rawOff = localStorage.getItem(NEWS_SRC_KEY);
    if (rawOff) { newsSrcOff = JSON.parse(rawOff) || {}; }
    newsMaster = localStorage.getItem(NEWS_MASTER_KEY) !== "off";
  } catch (e) { newsSrcOff = {}; newsMaster = true; }

  newsBtn.addEventListener("click", function () {
    location.hash = "#news";
  });

  var NEWS_CATS = ["All News", "Crypto", "Macro", "Markets", "Regulation",
    "Exchange", "On-Chain", "Security"];

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
    if (!newsMaster) { return; }
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
    if (newsMaster) { fetchNews(true); } else { renderNews(); }
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

  function newsLevel(item) {
    return item.impact === 3 ? 3 : (item.impact === 2 ? 2 : 1);
  }

  function svgEl(tag, attrs) {
    var el = document.createElementNS("http://www.w3.org/2000/svg", tag);
    for (var k in attrs) {
      if (Object.prototype.hasOwnProperty.call(attrs, k)) {
        el.setAttribute(k, attrs[k]);
      }
    }
    return el;
  }

  function barsSvg(level) {
    var svg = svgEl("svg", { viewBox: "0 0 13 12", "class": "bars b" + level });
    var dims = [[0, 8, 3, 4, "r1"], [5, 5, 3, 7, "r2"], [10, 2, 3, 10, "r3"]];
    for (var i = 0; i < dims.length; i++) {
      svg.appendChild(svgEl("rect", {
        x: dims[i][0], y: dims[i][1], width: dims[i][2],
        height: dims[i][3], rx: 1, "class": dims[i][4]
      }));
    }
    return svg;
  }

  function linkSvg() {
    var svg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    svg.appendChild(svgEl("path", { d: "M15 3h6v6" }));
    svg.appendChild(svgEl("path", { d: "M10 14 21 3" }));
    svg.appendChild(svgEl("path", {
      d: "M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6" }));
    return svg;
  }

  function letterBadge(name) {
    var b = document.createElement("span");
    b.className = "badge";
    var m = String(name || "?").match(/[A-Za-z0-9]/);
    b.textContent = m ? m[0].toUpperCase() : "?";
    return b;
  }

  function iconFor(name, domain) {
    if (!domain) { return letterBadge(name); }
    var img = document.createElement("img");
    img.className = "fav";
    img.alt = "";
    img.src = "live/favicons/" + domain + ".png";
    img.addEventListener("error", function () {
      var badge = letterBadge(name);
      if (img.parentNode) { img.parentNode.replaceChild(badge, img); }
    });
    return img;
  }

  /* activity chart (right box): 24 hourly buckets, stacked L/M/H;
     dataviz-validated palette H #dc2626 / M #f59e0b / L #9aa3af. */
  function activityBox(items) {
    var box = document.createElement("div");
    box.className = "rbox";
    var head = document.createElement("div");
    head.className = "rbox-h";
    var t = document.createElement("span");
    t.className = "rbox-title";
    t.textContent = "Activity · 24h";
    var lgwrap = document.createElement("span");
    lgwrap.className = "rbox-sub act-h";
    var defs = [["H", "#e8850c"], ["M", "#3b82f6"], ["L", "#9aa3af"]];
    for (var li2 = 0; li2 < defs.length; li2++) {
      var lg = document.createElement("span");
      lg.className = "lg";
      var dot = document.createElement("span");
      dot.className = "dotc";
      dot.style.background = defs[li2][1];
      lg.appendChild(dot);
      lg.appendChild(document.createTextNode(defs[li2][0]));
      lgwrap.appendChild(lg);
    }
    head.appendChild(t);
    head.appendChild(lgwrap);
    box.appendChild(head);

    var now = Date.now();
    var buckets = [];
    for (var b2 = 0; b2 < 24; b2++) { buckets.push({ h: 0, m: 0, l: 0 }); }
    for (var ii = 0; ii < items.length; ii++) {
      var t2 = Date.parse(items[ii].ts);
      if (!isFinite(t2)) { continue; }
      var ageH = (now - t2) / 3600000;
      if (ageH < 0 || ageH >= 24) { continue; }
      var idx = 23 - Math.floor(ageH);
      var lvl = newsLevel(items[ii]);
      if (lvl === 3) { buckets[idx].h += 1; }
      else if (lvl === 2) { buckets[idx].m += 1; }
      else { buckets[idx].l += 1; }
    }
    var maxN = 1;
    for (var mb = 0; mb < 24; mb++) {
      var tot = buckets[mb].h + buckets[mb].m + buckets[mb].l;
      if (tot > maxN) { maxN = tot; }
    }
    var W = 312, H = 88, plotH = 64, baseY = 72;
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H,
      "class": "actchart" });
    var slot = W / 24;
    var bw = Math.max(5, slot - 3);
    var nowHour = new Date().getUTCHours();
    for (var x = 0; x < 24; x++) {
      var bkt = buckets[x];
      var x0 = x * slot + (slot - bw) / 2;
      var hourLabel = ((nowHour - (23 - x)) % 24 + 24) % 24;
      var col = svgEl("g", {});
      var y = baseY;
      var segs = [["l", "#9aa3af"], ["m", "#3b82f6"], ["h", "#e8850c"]];
      for (var s2 = 0; s2 < segs.length; s2++) {
        var n = bkt[segs[s2][0]];
        if (!n) { continue; }
        var hpx = Math.max(2, (n / maxN) * plotH);
        y -= hpx;
        col.appendChild(svgEl("rect", {
          x: x0, y: y, width: bw, height: Math.max(1, hpx - 1),
          rx: 1.5, fill: segs[s2][1]
        }));
      }
      var hit = svgEl("rect", {
        x: x * slot, y: 0, width: slot, height: H, fill: "transparent"
      });
      hit.setAttribute("data-tip",
        ("0" + hourLabel).slice(-2) + ":00 · " + bkt.h + "H " +
        bkt.m + "M " + bkt.l + "L");
      col.appendChild(hit);
      svg.appendChild(col);
      if (hourLabel % 6 === 0) {
        var tx = svgEl("text", { x: x * slot + slot / 2, y: H - 2,
          "text-anchor": "middle", "class": "acthour" });
        tx.textContent = ("0" + hourLabel).slice(-2);
        svg.appendChild(tx);
      }
    }
    svg.appendChild(svgEl("line", {
      x1: 0, y1: baseY + 0.5, x2: W, y2: baseY + 0.5,
      "class": "bl", "stroke-width": 1
    }));
    box.appendChild(svg);
    return box;
  }

  function sourcesBox() {
    var board = (newsData && newsData.sources_board) || {};
    var rows = board.sources || [];
    var box = document.createElement("div");
    box.className = "rbox";
    var head = document.createElement("div");
    head.className = "rbox-h";
    var t = document.createElement("span");
    t.className = "rbox-title";
    t.textContent = "Sources";
    var sub = document.createElement("span");
    sub.className = "rbox-sub";
    sub.textContent = board.present
      ? board.available + " · " + board.live + " live"
      : "not mirrored yet";
    head.appendChild(t);
    head.appendChild(sub);
    box.appendChild(head);

    var search = document.createElement("div");
    search.className = "srcsearch";
    var sicon = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    sicon.appendChild(svgEl("circle", { cx: 11, cy: 11, r: 8 }));
    sicon.appendChild(svgEl("path", { d: "m21 21-4.3-4.3" }));
    search.appendChild(sicon);
    var input = document.createElement("input");
    input.type = "text";
    input.placeholder = "Search sources";
    input.value = srcSearch;
    search.appendChild(input);
    box.appendChild(search);

    var list = document.createElement("div");
    list.className = "srclist";
    box.appendChild(list);

    function paint() {
      list.textContent = "";
      var needle = srcSearch.trim().toLowerCase();
      var shown = 0;
      for (var i = 0; i < rows.length; i++) {
        var srcRow = rows[i];
        var label = srcRow.label || srcRow.key;
        if (needle && label.toLowerCase().indexOf(needle) === -1) { continue; }
        shown += 1;
        (function (name, srcRow2) {
          var row = document.createElement("div");
          row.className = "src-row" + (newsSrcOff[name] ? " off" : "");
          row.appendChild(iconFor(name, srcRow2.domain));
          var nm = document.createElement("span");
          nm.className = "src-name";
          nm.textContent = name;
          nm.title = name + (srcRow2.domain ? " · " + srcRow2.domain : "") +
            " · " + (srcRow2.status || "unknown");
          row.appendChild(nm);
          var dot = document.createElement("span");
          dot.className = "src-dot " + (srcRow2.status || "");
          row.appendChild(dot);
          var cnt = document.createElement("span");
          cnt.className = "src-cnt";
          cnt.textContent = srcRow2.status === "pending"
            ? "–" : String(srcRow2.count || 0);
          row.appendChild(cnt);
          var sw = document.createElement("button");
          sw.className = "switch sm";
          sw.setAttribute("role", "switch");
          sw.setAttribute("aria-checked",
            newsSrcOff[name] ? "false" : "true");
          sw.setAttribute("aria-label", name + " feed");
          var knob = document.createElement("span");
          knob.className = "knob";
          sw.appendChild(knob);
          sw.addEventListener("click", function () {
            if (newsSrcOff[name]) { delete newsSrcOff[name]; }
            else { newsSrcOff[name] = true; }
            try {
              localStorage.setItem(NEWS_SRC_KEY,
                JSON.stringify(newsSrcOff));
            } catch (e) { /* private mode */ }
            renderNews();
          });
          row.appendChild(sw);
          list.appendChild(row);
        })(label, srcRow);
      }
      if (!shown) {
        var none = document.createElement("div");
        none.className = "sp-note";
        none.textContent = "No source matches.";
        list.appendChild(none);
      }
    }
    input.addEventListener("input", function () {
      srcSearch = input.value;
      paint();
    });
    paint();

    var note = document.createElement("div");
    note.className = "sp-note";
    note.textContent = "Gray = parser pending port. Toggles filter this " +
      "feed; upstream shutdown lands with the wiring build.";
    box.appendChild(note);
    return box;
  }

  function setMaster(on) {
    newsMaster = on;
    try {
      localStorage.setItem(NEWS_MASTER_KEY, on ? "on" : "off");
    } catch (e) { /* private mode */ }
    if (on) { fetchNews(true); } else { renderNews(); }
  }

  function renderNews() {
    if (!newsHost) { return; }
    var prevSeen = newsSeen;
    var keepSearch = srcSearch;
    newsHost.textContent = "";
    newsHost.classList.toggle("dead", !newsMaster);
    srcSearch = keepSearch;

    var board = (newsData && newsData.sources_board) || {};

    var head = document.createElement("div");
    head.className = "news-head";
    var title = document.createElement("span");
    title.className = "news-title";
    title.textContent = "News";
    head.appendChild(title);
    var liveLbl = document.createElement("span");
    liveLbl.className = "news-live";
    liveLbl.textContent = board.present
      ? board.available + " sources · " + board.live + " live" +
        (board.pending ? " · " + board.pending + " pending" : "")
      : "";
    head.appendChild(liveLbl);
    var right = document.createElement("div");
    right.className = "news-right";
    var refresh = document.createElement("button");
    refresh.className = "refresh-btn";
    refresh.setAttribute("aria-label", "Refresh");
    refresh.setAttribute("data-tip", "Refresh");
    var rsvg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    rsvg.appendChild(svgEl("path", {
      d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }));
    rsvg.appendChild(svgEl("path", { d: "M3 3v5h5" }));
    refresh.appendChild(rsvg);
    refresh.addEventListener("click", function () { fetchNews(true); });
    var upd = document.createElement("span");
    upd.className = "news-upd";
    var masterWrap = document.createElement("span");
    masterWrap.className = "master-wrap";
    var mlabel = document.createElement("span");
    mlabel.className = "mlabel";
    mlabel.textContent = newsMaster ? "News on" : "News off";
    var msw = document.createElement("button");
    msw.className = "switch";
    msw.setAttribute("role", "switch");
    msw.setAttribute("aria-checked", newsMaster ? "true" : "false");
    msw.setAttribute("aria-label", "News master");
    var mknob = document.createElement("span");
    mknob.className = "knob";
    msw.appendChild(mknob);
    msw.addEventListener("click", function () { setMaster(!newsMaster); });
    masterWrap.appendChild(mlabel);
    masterWrap.appendChild(msw);
    right.appendChild(refresh);
    right.appendChild(upd);
    right.appendChild(masterWrap);
    head.appendChild(right);
    newsHost.appendChild(head);

    if (!newsMaster) {
      var offnote = document.createElement("div");
      offnote.className = "news-offnote";
      offnote.textContent = "News is turned off — display and polling " +
        "halted here. Upstream fetch-loop shutdown lands with the wiring " +
        "build (needs the control endpoint).";
      newsHost.appendChild(offnote);
    }

    var items = (newsData && newsData.present && newsData.items) || [];

    if (newsMaster && (!newsData || !newsData.present)) {
      upd.textContent = "—";
      var empty0 = document.createElement("div");
      empty0.className = "news-empty";
      empty0.textContent = newsData
        ? "No news in the mirror yet — the fetch loop fills ui/live/news.jsonl."
        : "Server unreachable.";
      newsHost.appendChild(empty0);
      newsSeen = null;
      return;
    }
    if (newsData) {
      upd.textContent = newsAgeText(newsData.as_of);
      var ageSec = (Date.now() - Date.parse(newsData.as_of)) / 1000;
      upd.classList.toggle("stale", isFinite(ageSec) && ageSec > 400);
    }

    /* day strip */
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
    var now2 = new Date();
    var dayDefs = [{ key: "all", label: "All", sub: items.length + " items" }];
    for (var dd = 6; dd >= 0; dd--) {
      var dt = new Date(now2.getTime() - dd * 86400000);
      var key = dt.toISOString().slice(0, 10);
      dayDefs.push({
        key: key,
        label: (dd === 0 ? "Today" :
          dayNames[dt.getUTCDay()] + " " + dt.getUTCDate()),
        stats: byDay[key] || { n: 0, h: 0, m: 0 }
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
          var mm = document.createElement("span");
          mm.className = "mm"; mm.textContent = "M" + def.stats.m;
          l2.appendChild(hh);
          l2.appendChild(document.createTextNode(" "));
          l2.appendChild(mm);
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
    newsHost.appendChild(strip);

    /* pools */
    var dayPool = [];
    for (var i0 = 0; i0 < items.length; i0++) {
      if (newsDay === "all" || utcDate(items[i0].ts) === newsDay) {
        dayPool.push(items[i0]);
      }
    }
    var catPool = [];
    for (var c0 = 0; c0 < dayPool.length; c0++) {
      if (newsCat === "all" ||
          (dayPool[c0].category || "Crypto") === newsCat) {
        catPool.push(dayPool[c0]);
      }
    }
    var pool = [];
    for (var i2 = 0; i2 < catPool.length; i2++) {
      if (!newsSrcOff[catPool[i2].source || "—"]) { pool.push(catPool[i2]); }
    }

    /* category + impact pill rows */
    var catbar = document.createElement("div");
    catbar.className = "pillbar";
    for (var ci2 = 0; ci2 < NEWS_CATS.length; ci2++) {
      (function (name) {
        var val = name === "All News" ? "all" : name;
        var b = document.createElement("button");
        b.className = "fpill" + (newsCat === val ? " on" : "");
        b.textContent = name;
        b.addEventListener("click", function () {
          newsCat = val;
          renderNews();
        });
        catbar.appendChild(b);
      })(NEWS_CATS[ci2]);
    }
    newsHost.appendChild(catbar);

    var counts = { 3: 0, 2: 0, 1: 0 };
    for (var cc = 0; cc < pool.length; cc++) { counts[newsLevel(pool[cc])] += 1; }
    var impbar = document.createElement("div");
    impbar.className = "pillbar";
    var defs2 = [["all", "All Impact", null],
      ["3", "High", 3], ["2", "Medium", 2], ["1", "Low", 1]];
    for (var fi = 0; fi < defs2.length; fi++) {
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
        impbar.appendChild(b);
      })(defs2[fi][0], defs2[fi][1], defs2[fi][2]);
    }
    newsHost.appendChild(impbar);

    /* two columns */
    var cols = document.createElement("div");
    cols.className = "news-cols";
    var maincol = document.createElement("div");
    maincol.className = "news-maincol";
    var rightcol = document.createElement("div");
    rightcol.className = "news-rightcol";

    var list = document.createElement("div");
    list.className = "news-list";
    var hrow = document.createElement("div");
    hrow.className = "news-hrow";
    var labels = ["Time", "", "Source", "Headline", "Category", ""];
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
      var srcWrap = document.createElement("span");
      srcWrap.className = "n-srcwrap";
      srcWrap.appendChild(iconFor(it.source, it.domain));
      var srcCol = document.createElement("span");
      srcCol.className = "n-srccol";
      var srcName = document.createElement("span");
      srcName.className = "n-src";
      srcName.textContent = it.source || "—";
      srcCol.appendChild(srcName);
      if (it.domain) {
        var dom = document.createElement("span");
        dom.className = "n-dom";
        dom.textContent = it.domain;
        srcCol.appendChild(dom);
      }
      srcWrap.appendChild(srcCol);
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
      var lnk = document.createElement("span");
      lnk.className = "n-link";
      if (it.url) {
        var la = document.createElement("a");
        la.href = it.url;
        la.target = "_blank";
        la.rel = "noopener noreferrer";
        la.setAttribute("aria-label", "Open source article");
        la.appendChild(linkSvg());
        lnk.appendChild(la);
      }
      row.appendChild(tm);
      row.appendChild(impCell);
      row.appendChild(srcWrap);
      row.appendChild(hd);
      row.appendChild(cat);
      row.appendChild(lnk);
      list.appendChild(row);
    }
    maincol.appendChild(list);
    if (!shown) {
      var none = document.createElement("div");
      none.className = "news-empty";
      none.textContent = "Nothing matches these filters in the mirror.";
      maincol.appendChild(none);
    }

    rightcol.appendChild(sourcesBox());
    rightcol.appendChild(activityBox(pool));

    cols.appendChild(maincol);
    cols.appendChild(rightcol);
    newsHost.appendChild(cols);
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
