/* Quant shell — clock, collapsible sidebar, tooltips, router, and the
   account popup (hover the bottom avatar; click it for #settings).
   Theme: system/light/dark, persisted, system tracked live. Log out is
   real on the hosted site (Cloudflare Access endpoint) and honestly
   disabled on this Mac where no session exists. */
"use strict";

(function () {
  var clockEl = document.getElementById("clock");

  function two(n) { return n < 10 ? "0" + n : "" + n; }

  /* Old room's premium clock, ported: HH:MM, en-GB h23, UTC zone,
     250ms tick (Header.tsx clockParts + its interval). Falls back to
     manual UTC parts exactly as their try/catch does. */
  var clockFmt = null;
  try {
    clockFmt = new Intl.DateTimeFormat("en-GB", {
      hour: "2-digit", minute: "2-digit",
      hourCycle: "h23", timeZone: "UTC"
    });
  } catch (e) { clockFmt = null; }

  function tickClock() {
    var d = new Date();
    clockEl.textContent = clockFmt ? clockFmt.format(d)
      : two(d.getUTCHours()) + ":" + two(d.getUTCMinutes());
  }
  tickClock();
  setInterval(tickClock, 250);

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
      fetchNews(false);
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
  var lastStripAt = null;

  function mirrorWarnBad() {
    var iv = lastStrip && lastStrip.pull && lastStrip.pull.interval_ms;
    var sec = (typeof iv === "number" && iv > 0) ? iv / 1000 : 45;
    return [Math.max(45, sec * 2), Math.max(120, sec * 4)];
  }

  var POLL_DEFAULTS = { strip: 10, news: 20 };
  var pollCfg = { strip: 10, news: 20, paused: false };
  try {
    var rawPoll = localStorage.getItem("quant.poll");
    if (rawPoll) {
      var parsedPoll = JSON.parse(rawPoll);
      if (parsedPoll && typeof parsedPoll === "object") {
        if (parsedPoll.strip > 0) { pollCfg.strip = parsedPoll.strip; }
        if (parsedPoll.news > 0) { pollCfg.news = parsedPoll.news; }
        pollCfg.paused = !!parsedPoll.paused;
      }
    }
  } catch (e) { /* private mode */ }

  function savePollCfg() {
    try {
      localStorage.setItem("quant.poll", JSON.stringify(pollCfg));
    } catch (e) { /* private mode */ }
  }

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
        lastStripAt = Date.now();
        setOnline(!!d);
        if (!d) { sigClass("sig-bad"); renderSigAll(); return; }
        var age = d.pull && d.pull.age_seconds;
        var wb = mirrorWarnBad();
        if (age == null || !isFinite(age)) { sigClass("sig-bad"); }
        else if (age > wb[1]) { sigClass("sig-bad"); }
        else if (age > wb[0]) { sigClass("sig-stale"); }
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
  var stripTimer = null;

  function restartStripPoll() {
    if (stripTimer) { clearInterval(stripTimer); }
    stripTimer = setInterval(function () {
      if (!pollCfg.paused) { pollStrip(); }
    }, pollCfg.strip * 1000);
  }
  pollStrip();
  restartStripPoll();

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
    var d = lastStrip || {};
    var wb = mirrorWarnBad();
    var pl = d.pull || {};
    var mp = d.market_pass || {};
    var sv = d.supervisor || {};
    var newsAge = (newsData && newsData.as_of)
      ? (Date.now() - Date.parse(newsData.as_of)) / 1000 : null;

    function cellCls(chunk) {
      return chunk.replace("v ", "").replace("v", "").trim();
    }
    var cells = [
      ["Machine", d.machine || "—",
        d.machine === "ARMED" ? "ok" : (d.machine ? "warn" : "")],
      ["Server RTT",
        lastStrip ? (lastRtt == null ? "OK" : lastRtt + " ms") : "DOWN",
        lastStrip ? "ok" : "bad"],
      ["Mirror", ageText2(pl.age_seconds),
        cellCls(ageCls(pl.age_seconds, wb[0], wb[1]))],
      ["Market", ageText2(mp.age_seconds),
        cellCls(ageCls(mp.age_seconds, 45, 120))],
      ["Supervisor",
        (sv.reported_status || "—") + " · " + ageText2(sv.age_seconds),
        cellCls(ageCls(sv.age_seconds, 240, 600))],
      ["News loop", newsAge == null ? "—" : ageText2(newsAge),
        newsAge == null ? "" : (newsAge < 300 ? "ok" : "warn")]
    ];
    var worst = lastStrip ? 0 : 2;
    for (var ci3 = 0; ci3 < cells.length; ci3++) {
      if (cells[ci3][2] === "warn" && worst < 1) { worst = 1; }
      if (cells[ci3][2] === "bad") { worst = 2; }
      var cell = document.createElement("div");
      cell.className = "sig-cell";
      var em = document.createElement("em");
      em.textContent = cells[ci3][0];
      var b = document.createElement("b");
      b.textContent = cells[ci3][1];
      if (cells[ci3][2]) { b.className = cells[ci3][2]; }
      cell.appendChild(em);
      cell.appendChild(b);
      target.appendChild(cell);
    }
    var pulse = sigPop.querySelector(".sp-pulse");
    if (pulse) {
      var pd = pulse.querySelector(".livedot");
      var plb = pulse.querySelector(".plabel");
      pd.className = "livedot" +
        (worst === 2 ? " dead" : (worst === 1 ? " stale" : ""));
      plb.textContent = worst === 2 ? "Down"
        : (worst === 1 ? "Degraded" : "Live monitor");
    }
    if (sigAsOf) {
      sigAsOf.textContent = lastStripAt
        ? "assessed " + Math.max(0, Math.round(
            (Date.now() - lastStripAt) / 1000)) + "s ago"
        : "connecting";
    }
  }

  /* the Connections page: real rows live-tinted; rows the browser
     cannot measure are honestly "not wired". Toggles are visual until
     the shutdown wiring lands (separate build, owner-sanctioned). */
  var CONNECTIONS = [
    { id: "server", name: "Web server",
      detail: "this dashboard ↔ serve.py · toggle pauses all polling here",
      live: true, control: "pause" },
    { id: "mirror", name: "Data mirror",
      detail: "EC2 host → this Mac · SSM pull loop (Lead-run)",
      live: true, control: "wiring" },
    { id: "market", name: "Market feed",
      detail: "exchange stream pass on the host", live: true,
      control: "wiring" },
    { id: "supervisor", name: "Supervisor",
      detail: "machine heartbeat on the host", live: true,
      control: "wiring" },
    { id: "binance", name: "Binance API",
      detail: "host ↔ exchange · not measurable from this browser",
      live: false, control: "wiring" },
    { id: "newsloop", name: "News fetch loop",
      detail: "this Mac → 104 public sources · bound to the News master",
      live: "news", control: "news" },
    { id: "host", name: "EC2 host",
      detail: "AWS runtime · reached via SSM only", live: false,
      control: "wiring" }
  ];
  var connState = {};
  for (var ci = 0; ci < CONNECTIONS.length; ci++) {
    connState[CONNECTIONS[ci].id] = true;
  }
  connState.server = !pollCfg.paused;
  connState.newsloop = newsMaster;

  function connLive(id) {
    if (id === "newsloop") {
      if (newsData && newsData.as_of) {
        var na = (Date.now() - Date.parse(newsData.as_of)) / 1000;
        return { cls: na < 300 ? "ok" : "warn", val: ageText2(na) };
      }
      return { cls: "", val: "no mirror read yet" };
    }
    if (!lastStrip) {
      return { cls: "bad", val: id === "server" ? "unreachable" : "—" };
    }
    var d = lastStrip;
    if (id === "server") {
      return { cls: "ok", val: lastRtt == null ? "answering" : lastRtt + " ms" };
    }
    if (id === "mirror") {
      var a = d.pull && d.pull.age_seconds;
      var wb2 = mirrorWarnBad();
      return { cls: ageCls(a, wb2[0], wb2[1]).replace("v ", ""),
        val: ageText2(a) };
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
    return { cls: "", val: "via host" };
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
        val.textContent = def && def.control === "pause"
          ? "polling paused" : "off";
        continue;
      }
      if (def && def.live) {
        var lv = connLive(id);
        dot.className = "conn-dot" + (lv.cls ? " " + lv.cls : "");
        val.textContent = lv.val;
      } else {
        dot.className = "conn-dot";
        val.textContent = def && def.id === "binance" ? "via host" : "not wired";
      }
    }
  }

  function buildConnTiles() {
    var tiles = document.createElement("div");
    tiles.className = "conn-tiles";
    tiles.id = "conn-tiles";
    function tile(label, valueText, cls, smallText) {
      var cell = document.createElement("div");
      cell.className = "conn-tile";
      var em = document.createElement("em");
      em.textContent = label;
      var b = document.createElement("b");
      b.textContent = valueText;
      if (cls) { b.className = cls; }
      var sm = document.createElement("small");
      sm.textContent = smallText;
      cell.appendChild(em);
      cell.appendChild(b);
      cell.appendChild(sm);
      return cell;
    }
    var d0 = lastStrip || {};
    var pull0 = d0.pull || {};
    var mp0 = d0.market_pass || {};
    var sv0 = d0.supervisor || {};
    var machineVal = d0.machine || "—";
    tiles.appendChild(tile("Machine", machineVal,
      machineVal === "ARMED" ? "ok" : (machineVal === "—" ? "" : "warn"),
      "STOP flags mirrored · authoritative"));
    tiles.appendChild(tile("Server RTT",
      lastStrip ? (lastRtt == null ? "answering" : lastRtt + " ms")
        : "unreachable",
      lastStrip ? "ok" : "bad", "direct serve.py probe"));
    var wbT = mirrorWarnBad();
    tiles.appendChild(tile("Mirror continuity",
      ageText2(pull0.age_seconds),
      ageCls(pull0.age_seconds, wbT[0], wbT[1]).replace("v ", ""),
      "pull every " + (pull0.interval_ms
        ? Math.round(pull0.interval_ms / 1000) + "s" : "—")));
    tiles.appendChild(tile("Feed freshness",
      ageText2(mp0.age_seconds),
      ageCls(mp0.age_seconds, 45, 120).replace("v ", ""),
      "market pass on the host"));
    tiles.appendChild(tile("Supervisor",
      (sv0.reported_status || "—"),
      ageCls(sv0.age_seconds, 240, 600).replace("v ", ""),
      "heartbeat " + ageText2(sv0.age_seconds) + " ago"));
    var newsAgeTile = (newsData && newsData.as_of)
      ? (Date.now() - Date.parse(newsData.as_of)) / 1000 : null;
    tiles.appendChild(tile("News loop",
      newsAgeTile == null ? "no read yet" : ageText2(newsAgeTile),
      newsAgeTile == null ? "" : (newsAgeTile < 300 ? "ok" : "warn"),
      "104 public sources"));
    return tiles;
  }

  function buildConnectionsPage(container) {
    container.textContent = "";
    /* re-sync controllable rows from their single sources of truth */
    connState.server = !pollCfg.paused;
    connState.newsloop = newsMaster;
    var page = document.createElement("div");
    page.className = "conn-page";
    var t = document.createElement("div");
    t.className = "conn-title";
    t.textContent = "Connections";
    var sub = document.createElement("div");
    sub.className = "conn-sub";
    sub.textContent =
      "Website · machine mirror · public sources · owner-gated boundaries.";
    var note = document.createElement("div");
    note.className = "conn-note";
    note.textContent =
      "Web server and News loop toggles act now (client side). " +
      "Host-side toggles stay visual until the control endpoint lands.";
    page.appendChild(t);
    page.appendChild(sub);
    page.appendChild(note);
    var tiles = buildConnTiles();
    page.appendChild(tiles);

    var cols = document.createElement("div");
    cols.className = "conn-cols";
    var mainCol = document.createElement("div");
    mainCol.className = "conn-main";
    var sideCol = document.createElement("div");
    sideCol.className = "conn-side";
    var pathHead = document.createElement("div");
    pathHead.className = "rbox-h";
    pathHead.style.padding = "0 0 4px";
    var ph1 = document.createElement("span");
    ph1.className = "rbox-title";
    ph1.textContent = "Connection path";
    var ph2 = document.createElement("span");
    ph2.className = "rbox-sub conn-assessed";
    ph2.textContent = lastStripAt
      ? "Assessed " + Math.max(0, Math.round((Date.now() - lastStripAt) / 1000)) + "s ago"
      : "Connecting";
    pathHead.appendChild(ph1);
    pathHead.appendChild(ph2);
    mainCol.appendChild(pathHead);
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
          if (def.control === "pause") {
            pollCfg.paused = !connState[def.id];
            savePollCfg();
          }
          if (def.control === "news") {
            setMaster(connState[def.id]);
          }
          sw.setAttribute("aria-checked",
            connState[def.id] ? "true" : "false");
          refreshConnRows();
        });
        var rec = document.createElement("button");
        rec.className = "conn-recover";
        var recAllowed = (def.id === "server" || def.id === "newsloop");
        var rsvg2 = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
          stroke: "currentColor", "stroke-linecap": "round",
          "stroke-linejoin": "round" });
        rsvg2.appendChild(svgEl("path", {
          d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }));
        rsvg2.appendChild(svgEl("path", { d: "M3 3v5h5" }));
        rec.appendChild(rsvg2);
        if (recAllowed) {
          rec.setAttribute("data-tip", "Refresh now");
          rec.addEventListener("click", function () {
            rec.classList.add("kick");
            setTimeout(function () { rec.classList.remove("kick"); }, 650);
            if (def.id === "server") { pollStrip(); }
            if (def.id === "newsloop") { fetchNews(true); }
          });
        } else {
          rec.setAttribute("disabled", "");
          rec.title = "Control preview required — recovery lands with the wiring build";
        }
        row.appendChild(dot);
        row.appendChild(main);
        row.appendChild(val);
        row.appendChild(rec);
        row.appendChild(sw);
        list.appendChild(row);
      })(CONNECTIONS[i]);
    }
    mainCol.appendChild(list);
    cols.appendChild(mainCol);
    sideCol.appendChild(pollingBox());
    cols.appendChild(sideCol);
    page.appendChild(cols);
    container.appendChild(page);
    refreshConnRows();
  }

  function pollingBox() {
    var box = document.createElement("div");
    box.className = "rbox";
    var head = document.createElement("div");
    head.className = "rbox-h";
    var t = document.createElement("span");
    t.className = "rbox-title";
    t.textContent = "Polling cadence";
    var def = document.createElement("button");
    def.className = "conn-defaults";
    def.textContent = "Defaults";
    def.addEventListener("click", function () {
      pollCfg.strip = POLL_DEFAULTS.strip;
      pollCfg.news = POLL_DEFAULTS.news;
      savePollCfg();
      restartStripPoll();
      if (newsTimer) { startNewsPoll(); }
      buildConnectionsPage(pageEl.parentNode ? pageEl : pageEl);
      if ((location.hash || "").slice(1) === "connections") {
        pageEl.textContent = "";
        buildConnectionsPage(pageEl);
      }
    });
    var spacer = document.createElement("span");
    spacer.className = "rbox-sub";
    spacer.appendChild(def);
    head.appendChild(t);
    head.appendChild(spacer);
    box.appendChild(head);

    function selRow(name, subText, key, options, onchange, fixedText) {
      var row = document.createElement("div");
      row.className = "poll-row";
      var main = document.createElement("div");
      main.className = "poll-main";
      var nm = document.createElement("div");
      nm.className = "poll-name";
      nm.textContent = name;
      var sb = document.createElement("div");
      sb.className = "poll-sub";
      sb.textContent = subText;
      main.appendChild(nm);
      main.appendChild(sb);
      row.appendChild(main);
      if (fixedText) {
        var fx = document.createElement("span");
        fx.className = "poll-fixed";
        fx.textContent = fixedText;
        row.appendChild(fx);
        return row;
      }
      var sel = document.createElement("select");
      sel.className = "poll-sel";
      for (var i = 0; i < options.length; i++) {
        var opt = document.createElement("option");
        opt.value = String(options[i]);
        opt.textContent = options[i] + "s";
        if (pollCfg[key] === options[i]) { opt.selected = true; }
        sel.appendChild(opt);
      }
      sel.addEventListener("change", function () {
        pollCfg[key] = parseInt(sel.value, 10);
        savePollCfg();
        onchange();
      });
      row.appendChild(sel);
      var go = document.createElement("button");
      go.className = "poll-go";
      go.setAttribute("aria-label", "Refresh now");
      go.setAttribute("data-tip", "Refresh now");
      var gs = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
        stroke: "currentColor", "stroke-linecap": "round",
        "stroke-linejoin": "round" });
      gs.appendChild(svgEl("path", {
        d: "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8" }));
      gs.appendChild(svgEl("path", { d: "M3 3v5h5" }));
      go.appendChild(gs);
      go.addEventListener("click", function () {
        go.classList.add("kick");
        setTimeout(function () { go.classList.remove("kick"); }, 650);
        if (key === "strip") { pollStrip(); }
        if (key === "news") { fetchNews(true); }
      });
      row.appendChild(go);
      return row;
    }

    box.appendChild(selRow("Machine status",
      "strip · avatar dot · Signal tint", "strip", [5, 10, 30, 60],
      restartStripPoll));
    box.appendChild(selRow("News mirror read",
      "list refresh while on the News page", "news", [10, 20, 60, 120],
      function () { if (newsTimer) { startNewsPoll(); } }));
    box.appendChild(selRow("News fetch loop",
      "process-set · becomes a control with the wiring build", null, null,
      null, "120s"));
    box.appendChild(selRow("Mirror pull (host → Mac)",
      "Lead's SSM loop · read-only here", null, null, null, "~30–60s"));
    return box;
  }

  function renderSigAll() {
    if (!sigPop.hidden) { buildSigRows(sigRowsEl); }
    if ((location.hash || "").slice(1) === "connections" && sigPage) {
      refreshConnRows();
      var oldTiles = document.getElementById("conn-tiles");
      if (oldTiles && oldTiles.parentNode) {
        oldTiles.parentNode.replaceChild(buildConnTiles(), oldTiles);
      }
      var stamp = document.querySelector(".conn-assessed");
      if (stamp && lastStripAt) {
        stamp.textContent = "Assessed " + Math.max(0, Math.round(
          (Date.now() - lastStripAt) / 1000)) + "s ago";
      }
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
  var newsPage = 1;

  function newsPageSize() {
    /* sized so the pager lands level with the right column's last box */
    return 28;
  }
  var newsResizeTimer = null;
  window.addEventListener("resize", function () {
    if (newsResizeTimer) { clearTimeout(newsResizeTimer); }
    newsResizeTimer = setTimeout(function () {
      if (newsHost) { renderNews(); }
    }, 200);
  });
  var newsQuery = "";
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
    if (sec < 90) { return sec + "s"; }
    var min = Math.round(sec / 60);
    if (min < 90) { return min + "m"; }
    return Math.round(min / 60) + "h";
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
        } else if (newsHost && d && d.as_of) {
          var lbl = newsHost.querySelector(".livelabel");
          var dot = newsHost.querySelector(".livedot");
          if (lbl && dot) {
            var a2 = (Date.now() - Date.parse(d.as_of)) / 1000;
            if (a2 < 300) {
              dot.className = "livedot";
              lbl.textContent = "Live";
            } else {
              dot.className = "livedot stale";
              lbl.textContent = Math.round(a2 / 60) + "m";
            }
          }
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
    if (newsTimer) { clearInterval(newsTimer); }
    newsTimer = setInterval(function () {
      if (!pollCfg.paused) { fetchNews(false); }
    }, pollCfg.news * 1000);
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
    var defs = [["H", "#16a34a"], ["M", "#3b82f6"], ["L", "#eab308"]];
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
    var W = 356, H = 118, plotH = 88, baseY = 98;
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
      var segs = [["l", "#eab308"], ["m", "#3b82f6"], ["h", "#16a34a"]];
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

  function hbarBox(title, pairs) {
    var box = document.createElement("div");
    box.className = "rbox";
    var head = document.createElement("div");
    head.className = "rbox-h";
    var t = document.createElement("span");
    t.className = "rbox-title";
    t.textContent = title;
    head.appendChild(t);
    box.appendChild(head);
    var maxV = 1;
    for (var i = 0; i < pairs.length; i++) {
      if (pairs[i][1] > maxV) { maxV = pairs[i][1]; }
    }
    for (var r = 0; r < pairs.length; r++) {
      var row = document.createElement("div");
      row.className = "hbar-row";
      var lab = document.createElement("span");
      lab.className = "hbar-label";
      lab.textContent = pairs[r][0];
      lab.title = pairs[r][0];
      var track = document.createElement("span");
      track.className = "hbar-track";
      var fill = document.createElement("span");
      fill.className = "hbar-fill";
      fill.style.width = Math.max(2, Math.round(
        (pairs[r][1] / maxV) * 100)) + "%";
      track.appendChild(fill);
      var val = document.createElement("span");
      val.className = "hbar-val";
      val.textContent = String(pairs[r][1]);
      row.appendChild(lab);
      row.appendChild(track);
      row.appendChild(val);
      box.appendChild(row);
    }
    return box;
  }

  function topSourcesBox(pool) {
    var counts = {};
    for (var i = 0; i < pool.length; i++) {
      var nm = pool[i].source || "—";
      counts[nm] = (counts[nm] || 0) + 1;
    }
    var pairs = Object.keys(counts).map(function (k) {
      return [k, counts[k]];
    }).sort(function (a, b) { return b[1] - a[1]; }).slice(0, 8);
    return hbarBox("Top sources", pairs);
  }

  function categoriesBox(pool) {
    var counts = {};
    for (var i = 0; i < pool.length; i++) {
      var c = pool[i].category || "Crypto";
      counts[c] = (counts[c] || 0) + 1;
    }
    var pairs = Object.keys(counts).map(function (k) {
      return [k, counts[k]];
    }).sort(function (a, b) { return b[1] - a[1]; });
    return hbarBox("Categories", pairs);
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

    var STATUS_RANK = { live: 3, delayed: 2, error: 1, pending: 0 };
    var ordered = rows.slice().sort(function (a, b) {
      var d = (b.count || 0) - (a.count || 0);
      if (d) { return d; }
      d = (STATUS_RANK[b.status] || 0) - (STATUS_RANK[a.status] || 0);
      if (d) { return d; }
      return String(a.label).localeCompare(String(b.label));
    });

    function paint() {
      list.textContent = "";
      var needle = srcSearch.trim().toLowerCase();
      var shown = 0;
      for (var i = 0; i < ordered.length; i++) {
        var srcRow = ordered[i];
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
    refresh.addEventListener("click", function () {
      refresh.classList.add("kick");
      setTimeout(function () { refresh.classList.remove("kick"); }, 650);
      fetchNews(true);
    });
    var livewrap = document.createElement("span");
    livewrap.className = "livewrap";
    var livedot = document.createElement("span");
    livedot.className = "livedot";
    var livelabel = document.createElement("span");
    livelabel.className = "livelabel";
    livewrap.appendChild(livedot);
    livewrap.appendChild(livelabel);
    right.appendChild(refresh);
    right.appendChild(livewrap);
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
    right.appendChild(masterWrap);
    head.appendChild(right);
    newsHost.appendChild(head);

    if (newsData && newsData.as_of) {
      var liveAge = (Date.now() - Date.parse(newsData.as_of)) / 1000;
      if (!isFinite(liveAge)) {
        livedot.className = "livedot dead";
        livelabel.textContent = "offline";
      } else if (liveAge < 300) {
        livedot.className = "livedot";
        livelabel.textContent = "Live";
      } else {
        livedot.className = "livedot stale";
        livelabel.textContent = Math.round(liveAge / 60) + "m";
      }
    } else {
      livedot.className = "livedot dead";
      livelabel.textContent = newsData ? "no data" : "offline";
    }
    if (!newsMaster) {
      livedot.className = "livedot dead";
      livelabel.textContent = "off";
      var offnote = document.createElement("div");
      offnote.className = "news-offnote";
      offnote.textContent = "News is turned off — display and polling " +
        "halted here. Upstream fetch-loop shutdown lands with the wiring " +
        "build (needs the control endpoint).";
      newsHost.appendChild(offnote);
    }

    var items = (newsData && newsData.present && newsData.items) || [];

    if (newsMaster && (!newsData || !newsData.present)) {
      var empty0 = document.createElement("div");
      empty0.className = "news-empty";
      empty0.textContent = newsData
        ? "No news in the mirror yet — the fetch loop fills ui/live/news.jsonl."
        : "Server unreachable.";
      newsHost.appendChild(empty0);
      newsSeen = null;
      return;
    }


    /* day strip */
    var strip = document.createElement("div");
    strip.className = "daystrip";
    var dayNames = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var byDay = {};
    for (var bi = 0; bi < items.length; bi++) {
      var dkey = utcDate(items[bi].ts);
      if (!byDay[dkey]) { byDay[dkey] = { n: 0, h: 0, m: 0, l: 0 }; }
      byDay[dkey].n += 1;
      var lv = newsLevel(items[bi]);
      if (lv === 3) { byDay[dkey].h += 1; }
      else if (lv === 2) { byDay[dkey].m += 1; }
      else { byDay[dkey].l += 1; }
    }
    var now2 = new Date();
    var dayDefs = [{ key: "all", label: "All",
      sub: items.length + " · 7d" }];
    for (var dd = 6; dd >= 0; dd--) {
      var dt = new Date(now2.getTime() - dd * 86400000);
      var key = dt.toISOString().slice(0, 10);
      dayDefs.push({
        key: key,
        label: (dd === 0 ? "Today" :
          dayNames[dt.getUTCDay()] + " " + dt.getUTCDate()),
        stats: byDay[key] || { n: 0, h: 0, m: 0, l: 0 }
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
          var trio = [["h", def.stats.h], ["m", def.stats.m],
            ["l", def.stats.l]];
          for (var ti = 0; ti < trio.length; ti++) {
            var grp = document.createElement("span");
            var lt = document.createElement("span");
            lt.className = "lt " + trio[ti][0];
            lt.textContent = trio[ti][0].toUpperCase();
            var nm2 = document.createElement("span");
            nm2.className = "nm2";
            nm2.textContent = String(trio[ti][1]);
            grp.appendChild(lt);
            grp.appendChild(nm2);
            l2.appendChild(grp);
          }
        } else {
          l2.textContent = def.sub;
        }
        cell.appendChild(l1);
        cell.appendChild(l2);
        cell.addEventListener("click", function () {
          newsDay = def.key;
          newsPage = 1;
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
    var needleQ = newsQuery.trim().toLowerCase();
    for (var i2 = 0; i2 < catPool.length; i2++) {
      var itQ = catPool[i2];
      if (newsSrcOff[itQ.source || "—"]) { continue; }
      if (needleQ &&
          String(itQ.title || "").toLowerCase().indexOf(needleQ) === -1 &&
          String(itQ.source || "").toLowerCase().indexOf(needleQ) === -1) {
        continue;
      }
      pool.push(itQ);
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
          newsPage = 1;
          renderNews();
        });
        catbar.appendChild(b);
      })(NEWS_CATS[ci2]);
    }
    var searchWrap = document.createElement("span");
    searchWrap.className = "bar-right";
    var nsearch = document.createElement("span");
    nsearch.className = "newsearch";
    var nsIcon = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    nsIcon.appendChild(svgEl("circle", { cx: 11, cy: 11, r: 8 }));
    nsIcon.appendChild(svgEl("path", { d: "m21 21-4.3-4.3" }));
    nsearch.appendChild(nsIcon);
    var nsInput = document.createElement("input");
    nsInput.type = "text";
    nsInput.placeholder = "Search news…";
    nsInput.value = newsQuery;
    if (newsQuery) { nsearch.classList.add("hasq"); }
    function commitQuery(value) {
      newsQuery = value;
      newsPage = 1;
      renderNews();
      var again = newsHost.querySelector(".newsearch input");
      if (again && value) {
        again.focus();
        again.setSelectionRange(again.value.length, again.value.length);
      }
    }
    nsInput.addEventListener("input", function () {
      commitQuery(nsInput.value);
    });
    nsInput.addEventListener("keydown", function (ev) {
      if (ev.key === "Escape" && nsInput.value) {
        ev.stopPropagation();
        commitQuery("");
      }
    });
    nsearch.appendChild(nsInput);
    var nclear = document.createElement("button");
    nclear.className = "nclear";
    nclear.setAttribute("aria-label", "Clear search");
    var xs = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    xs.appendChild(svgEl("path", { d: "M18 6 6 18" }));
    xs.appendChild(svgEl("path", { d: "m6 6 12 12" }));
    nclear.appendChild(xs);
    nclear.addEventListener("click", function () { commitQuery(""); });
    nsearch.appendChild(nclear);
    searchWrap.appendChild(nsearch);
    catbar.appendChild(searchWrap);
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
          newsPage = 1;
          renderNews();
        });
        impbar.appendChild(b);
      })(defs2[fi][0], defs2[fi][1], defs2[fi][2]);
    }
    var infoTip = document.createElement("span");
    infoTip.className = "rbtn";
    infoTip.style.width = "26px";
    infoTip.style.height = "26px";
    infoTip.style.color = "var(--faint)";
    infoTip.setAttribute("data-tip",
      "Impact engine (ported from the old room): critical events — " +
      "hacks, halts, insolvency — are High; decisive macro (FOMC, CPI), " +
      "regulatory actions, listings/delistings, big-money on-chain moves " +
      "stack points; official and venue sources weigh extra.");
    var iSvg = svgEl("svg", { viewBox: "0 0 24 24", fill: "none",
      stroke: "currentColor", "stroke-linecap": "round",
      "stroke-linejoin": "round" });
    iSvg.setAttribute("style", "width:14px;height:14px;stroke-width:1.8");
    iSvg.appendChild(svgEl("circle", { cx: 12, cy: 12, r: 10 }));
    iSvg.appendChild(svgEl("path", { d: "M12 16v-4" }));
    iSvg.appendChild(svgEl("path", { d: "M12 8h.01" }));
    infoTip.appendChild(iSvg);
    impbar.appendChild(infoTip);
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

    var filtered = [];
    for (var fi2 = 0; fi2 < pool.length; fi2++) {
      var lvlF = newsLevel(pool[fi2]);
      if (newsFilter !== "all" && String(lvlF) !== newsFilter) { continue; }
      filtered.push(pool[fi2]);
    }
    var pageSize = newsPageSize();
    var totalPages = Math.max(1, Math.ceil(filtered.length / pageSize));
    if (newsPage > totalPages) { newsPage = totalPages; }
    var pageStart = (newsPage - 1) * pageSize;
    var pageItems = filtered.slice(pageStart, pageStart + pageSize);

    var seenNow = {};
    var shown = 0;
    for (var ri = 0; ri < pageItems.length; ri++) {
      var it = pageItems[ri];
      var lvl2 = newsLevel(it);
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
    } else {
      var pager = document.createElement("div");
      pager.className = "pager";
      function pageBtn(label, page, disabled, on) {
        var b = document.createElement("button");
        b.className = "pbtn" + (on ? " on" : "");
        b.textContent = label;
        if (disabled) { b.setAttribute("disabled", ""); }
        else {
          b.addEventListener("click", function () {
            newsPage = page;
            renderNews();
          });
        }
        return b;
      }
      pager.appendChild(pageBtn("‹", newsPage - 1, newsPage === 1, false));
      var windowStart = Math.max(1, Math.min(newsPage - 2, totalPages - 4));
      var windowEnd = Math.min(totalPages, windowStart + 4);
      if (windowStart > 1) {
        pager.appendChild(pageBtn("1", 1, false, newsPage === 1));
        if (windowStart > 2) {
          var dots1 = document.createElement("span");
          dots1.className = "pbtn";
          dots1.textContent = "…";
          pager.appendChild(dots1);
        }
      }
      for (var pn = windowStart; pn <= windowEnd; pn++) {
        pager.appendChild(pageBtn(String(pn), pn, false, pn === newsPage));
      }
      if (windowEnd < totalPages) {
        if (windowEnd < totalPages - 1) {
          var dots2 = document.createElement("span");
          dots2.className = "pbtn";
          dots2.textContent = "…";
          pager.appendChild(dots2);
        }
        pager.appendChild(pageBtn(String(totalPages), totalPages, false,
          newsPage === totalPages));
      }
      pager.appendChild(pageBtn("›", newsPage + 1,
        newsPage === totalPages, false));
      var pinfo = document.createElement("span");
      pinfo.className = "pinfo";
      pinfo.textContent = (pageStart + 1) + "–" +
        Math.min(pageStart + pageSize, filtered.length) +
        " of " + filtered.length;
      pager.appendChild(pinfo);
      maincol.appendChild(pager);
    }

    rightcol.appendChild(sourcesBox());
    rightcol.appendChild(activityBox(pool));
    rightcol.appendChild(topSourcesBox(pool));
    rightcol.appendChild(categoriesBox(pool));

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
