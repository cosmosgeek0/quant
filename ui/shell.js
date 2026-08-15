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
    if ((location.hash || "").slice(1) === "network") { route(); }
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
    settings: ["Settings", "The space is prepared — its sections land on your word. The clock's timezone material arrives here."],
    network: ["Network", ""],
    news: ["News", ""]
  };

  /* every page shares one grid: title exactly where News's title sits. */
  function buildSkeletonPage(container, title, subtitle) {
    var page = document.createElement("div");
    page.className = "page-shell";
    var t = document.createElement("div");
    t.className = "news-title";
    t.textContent = title;
    var sub = document.createElement("div");
    sub.className = "page-sub";
    sub.textContent = subtitle;
    page.appendChild(t);
    page.appendChild(sub);
    container.appendChild(page);
  }

  function route() {
    var h = (location.hash || "#overview").slice(1);
    if (h === "connections") { location.hash = "#network"; return; }
    if (!Object.prototype.hasOwnProperty.call(PAGES, h)) { h = "overview"; }
    for (var i = 0; i < rows.length; i++) {
      var on = rows[i].getAttribute("data-page") === h;
      rows[i].classList.toggle("active", on);
      if (on) { rows[i].setAttribute("aria-current", "page"); }
      else { rows[i].removeAttribute("aria-current"); }
    }
    pageEl.textContent = "";
    stopNewsPoll();
    stopNetHealthPoll();
    stopNetTelemetry();
    stopActivity();
    if (netChart) { netChart.destroy(); netChart = null; }
    if (h === "network") {
      sigPage = pageEl;
      buildNetworkPage(pageEl);
      startNetHealthPoll();
      startNetTelemetry();
      fetchNews(false);
    } else if (h === "news") {
      sigPage = null;
      buildNewsPage(pageEl);
      startNewsPoll();
    } else {
      sigPage = null;
      buildSkeletonPage(pageEl, PAGES[h][0], PAGES[h][1]);
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
  var navNet = document.getElementById("nav-network");
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
    navNet.classList.remove("sig-ok", "sig-stale", "sig-bad");
    if (cls) { navNet.classList.add(cls); }
  }

  var lastRtt = null;

  function telNum(v) {
    /* audit F: Number(null) and Number("") are 0 — a FAILED reading
       must never render as perfect freshness */
    if (v == null || v === "" || typeof v === "boolean") { return null; }
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

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

  /* right rail removed by owner order 2026-08-15: watchlist and
     calendar are gone; News and Network live as left tabs. */
  try { localStorage.removeItem("quant.rpanel"); } catch (e) { /* ok */ }

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

  /* the Network page — the old room's Connection Watchman, from its
     source: grouped path (control path / sources / boundaries), status
     vocabulary Operational/Degraded/Outage/Owner gated, RTT thresholds
     (<350 operational, <800 degraded, else outage). Rows the browser
     cannot measure stay honest boundaries. Toggles: Web server and
     News loop act client-side today; host-side stays visual until the
     control endpoint lands (owner-sanctioned). */
  var CONNECTIONS = [
    { id: "server", name: "Web server", group: "path",
      detail: "serve.py · loopback probe",
      live: true, control: "pause" },
    { id: "mirror", name: "Data mirror", group: "path",
      detail: "host → Mac · SSM pull",
      live: true, control: "wiring" },
    { id: "market", name: "Market feed", group: "path",
      detail: "exchange stream pass · host", live: true,
      control: "wiring" },
    { id: "supervisor", name: "Supervisor", group: "path",
      detail: "machine heartbeat · host", live: true,
      control: "wiring" },
    { id: "basiswatch", name: "Basis watch", group: "machine",
      detail: "basis pass loop · host",
      live: "health", healthKey: "basis_watch", control: "wiring" },
    { id: "basissup", name: "Basis supervisor", group: "machine",
      detail: "position supervision · host",
      live: "health", healthKey: "basis_supervisor", control: "wiring" },
    { id: "jobrunner", name: "Job runner", group: "machine",
      detail: "restricted jobs · 1m cron",
      live: "health", healthKey: "jobs", control: "wiring" },
    { id: "newsloop", name: "News fetch loop", group: "sources",
      detail: "104 public sources · News master",
      live: "news", control: "news" },
    { id: "binance", name: "Binance API", group: "boundary",
      detail: "signed reads · host only",
      live: false, control: "wiring" },
    { id: "host", name: "EC2 host", group: "boundary",
      detail: "AWS runtime · SSM only", live: false,
      control: "wiring" }
  ];
  var CONN_GROUPS = [
    ["path", "Control path"],
    ["machine", "Machine loops"],
    ["sources", "Sources"],
    ["boundary", "Owner boundaries"]
  ];
  function connStatusText(cls) {
    return cls === "ok" ? "Operational"
      : cls === "warn" ? "Degraded"
      : cls === "bad" ? "Outage" : "";
  }
  var connState = {};
  for (var ci = 0; ci < CONNECTIONS.length; ci++) {
    connState[CONNECTIONS[ci].id] = true;
  }
  connState.server = !pollCfg.paused;
  connState.newsloop = newsMaster;

  var netHealth = null;
  var netHealthTimer = null;

  function fetchNetHealth() {
    fetch("/api/health", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        netHealth = d;
        if ((location.hash || "").slice(1) === "network") { renderSigAll(); }
      })
      .catch(function () { netHealth = null; });
  }

  function startNetHealthPoll() {
    fetchNetHealth();
    if (netHealthTimer) { clearInterval(netHealthTimer); }
    netHealthTimer = setInterval(function () {
      if (!pollCfg.paused) { fetchNetHealth(); }
    }, pollCfg.strip * 1000);
  }

  function stopNetHealthPoll() {
    if (netHealthTimer) {
      clearInterval(netHealthTimer);
      netHealthTimer = null;
    }
  }

  function healthService(key) {
    var list = (netHealth && netHealth.services) || [];
    for (var i = 0; i < list.length; i++) {
      if (list[i].service === key) { return list[i]; }
    }
    return null;
  }

  function connLive(id) {
    var hdef = null;
    for (var hi = 0; hi < CONNECTIONS.length; hi++) {
      if (CONNECTIONS[hi].id === id) { hdef = CONNECTIONS[hi]; }
    }
    if (hdef && hdef.live === "health") {
      if (!netHealth || !netHealth.present) {
        return { cls: "", val: "no health read yet" };
      }
      var lvl = null;
      var hage = null;
      var hstatus = "";
      if (hdef.healthKey === "jobs") {
        var jl = netHealth.jobs_log || {};
        lvl = jl.level || null;
        hage = telNum(jl.last_tick_age_seconds);
      } else {
        var svc = healthService(hdef.healthKey);
        if (!svc) { return { cls: "", val: "not in health file" }; }
        lvl = svc.level || null;
        hage = telNum(svc.last_seen_age_seconds);
        hstatus = svc.reported_status ? " · " + svc.reported_status : "";
      }
      var hcls = lvl === "green" ? "ok"
        : lvl === "amber" ? "warn"
        : lvl === "red" ? "bad" : "";
      return { cls: hcls, val: ageText2(hage) + hstatus };
    }
    if (id === "newsloop") {
      var nAge = currentVal("newsloop");
      if (nAge != null) {
        return { cls: thrCls("newsloop", nAge), val: ageText2(nAge) };
      }
      return { cls: "", val: "no mirror read yet" };
    }
    if (!lastStrip) {
      return { cls: "bad", val: id === "server" ? "unreachable" : "—" };
    }
    var d = lastStrip;
    if (id === "server") {
      var rttCls = lastRtt == null ? "ok"
        : (lastRtt >= 800 ? "bad" : (lastRtt >= 350 ? "warn" : "ok"));
      return { cls: rttCls,
        val: lastRtt == null ? "answering" : lastRtt + " ms" };
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

  function rowStatus(def) {
    /* one place computes a row's value + status pair */
    if (!connState[def.id]) {
      return { value: def.control === "pause" ? "polling paused" : "off",
        statusCls: "", statusText: "Off" };
    }
    if (def.live) {
      var lv = connLive(def.id);
      var cur = currentVal(def.id);
      var cls = cur == null ? (lv.cls || "") : thrCls(def.id, cur);
      return { value: lv.val,
        statusCls: cls,
        statusText: connStatusText(cls) || "Measuring" };
    }
    return { value: def.id === "binance" ? "via host" : "SSM only",
      statusCls: "", statusText: "Owner gated" };
  }

  /* ---- status-page anatomy: banner + rows. Every measurement in
     net.css and here is read off status.openai.com's live DOM
     (2026-08-15). The banner is the ONE place overall state is
     announced; rows carry dot/name left, datum/word right, real
     uptime bars from the telemetry file underneath. ---- */
  function stEl(tag, cls, text) {
    var el = document.createElement(tag);
    if (cls) { el.className = cls; }
    if (text != null) { el.textContent = text; }
    return el;
  }

  var SVG_NS = "http://www.w3.org/2000/svg";
  function stIcon(state) {
    var svg = document.createElementNS(SVG_NS, "svg");
    svg.setAttribute("width", "16");
    svg.setAttribute("height", "16");
    svg.setAttribute("viewBox", "0 0 16 16");
    svg.setAttribute("aria-hidden", "true");
    var c = document.createElementNS(SVG_NS, "circle");
    c.setAttribute("cx", "8");
    c.setAttribute("cy", "8");
    c.setAttribute("r", "8");
    c.setAttribute("fill", state === 3 ? "#c9cbd1"
      : state === 2 ? "#f87171"
      : state === 1 ? "#fbbf24" : "#24c19a");
    svg.appendChild(c);
    var p = document.createElementNS(SVG_NS, "path");
    p.setAttribute("stroke", "#ffffff");
    p.setAttribute("stroke-width", "1.7");
    p.setAttribute("fill", "none");
    p.setAttribute("stroke-linecap", "round");
    if (state === 0) {
      p.setAttribute("d", "M4.6 8.4l2.2 2.2 4.6-4.8");
      p.setAttribute("stroke-linejoin", "round");
    } else if (state === 3) {
      p.setAttribute("d", "M5 8h6");
    } else {
      p.setAttribute("d", "M8 4.4v4.4m0 2.6v.2");
    }
    svg.appendChild(p);
    return svg;
  }

  function mkSwitch(on, onChange) {
    var b = stEl("button", "switch" + (on ? " on" : ""));
    b.setAttribute("type", "button");
    b.setAttribute("role", "switch");
    b.setAttribute("aria-checked", on ? "true" : "false");
    b.appendChild(stEl("span", "knob"));
    b.addEventListener("click", function () {
      var now = !b.classList.contains("on");
      b.classList.toggle("on", now);
      b.setAttribute("aria-checked", now ? "true" : "false");
      onChange(now);
    });
    return b;
  }

  function bannerState() {
    if (!lastStrip) {
      return { worst: 1, title: "Connecting to the local server",
        sub: "No status read has landed yet." };
    }
    var worst = connOverallWorst();
    if (worst === 0) {
      var n = 0;
      for (var i = 0; i < CONNECTIONS.length; i++) {
        if (CONNECTIONS[i].live && connState[CONNECTIONS[i].id]) { n += 1; }
      }
      return { worst: 0, title: "All connections operational",
        sub: "All " + n + " loops are reporting normally." };
    }
    var names = [];
    for (var j = 0; j < CONNECTIONS.length; j += 1) {
      var def = CONNECTIONS[j];
      if (!def.live || !connState[def.id]) { continue; }
      var cls = rowStatus(def).statusCls;
      if ((worst === 2 && cls === "bad") ||
          (worst === 1 && cls !== "" && cls !== "ok")) {
        names.push(def.name);
      }
    }
    return { worst: worst,
      title: (worst === 2 ? "Outage: " : "Degraded: ") +
        (names.join(", ") || "one loop"),
      sub: worst === 2
        ? "The named loop has stopped reporting \u2014 rows below."
        : "Past its measured cadence \u2014 rows below." };
  }

  function updateBanner() {
    var card = document.getElementById("net-banner");
    if (!card) { return; }
    var st = bannerState();
    var sig = st.worst + "|" + st.title + "|" + st.sub;
    if (card.getAttribute("data-sig") === sig) { return; }
    card.setAttribute("data-sig", sig);
    var plate = document.getElementById("net-plate");
    if (plate) {
      plate.className = "st-plate" +
        (st.worst === 2 ? " bad" : st.worst === 1 ? " warn" : "");
    }
    card.textContent = "";
    var band = stEl("div", "st-banner-band" +
      (st.worst === 2 ? " bad" : st.worst === 1 ? " warn" : ""));
    band.appendChild(stIcon(st.worst));
    band.appendChild(stEl("div", "st-t-h", st.title));
    card.appendChild(band);
    var sub = stEl("div", "st-banner-sub");
    sub.appendChild(stEl("div", "st-t-body", st.sub));
    card.appendChild(sub);
  }

  function uptimeText(compKey) {
    var pct = uptimePct(compKey);
    if (pct == null) { return "\u2014"; }
    if (pct >= 99.995) { return "100% uptime"; }
    return (Math.round(pct * 100) / 100).toFixed(2) + "% uptime";
  }

  /* ---- per-loop poll config: the page's ONE write, backed by the
     recorder (ui/live/poll_config.json). Selects are real. ---- */
  var pollConf = { default_s: 15, metrics: {}, allowed: [5, 10, 15, 30,
    60, 120] };

  function fetchPollConfig(done) {
    fetch("/api/poll_config", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.ok) {
          pollConf.default_s = d.default_s;
          pollConf.metrics = d.metrics || {};
          if (d.allowed && d.allowed.length) { pollConf.allowed = d.allowed; }
        }
        if (done) { done(); }
      })
      .catch(function () { if (done) { done(); } });
  }

  function postPollConfig(patch, done) {
    fetch("/api/poll_config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(patch)
    })
      .then(function (r) { return r.json().catch(function () { return null; }); })
      .then(function (d) {
        if (d && d.ok && d.saved) {
          pollConf.default_s = d.saved.default_s;
          pollConf.metrics = d.saved.metrics || pollConf.metrics;
        }
        if (done) { done(d && d.ok); }
      })
      .catch(function () { if (done) { done(false); } });
  }

  function pollIntervalFor(key) {
    var v = pollConf.metrics[key];
    return typeof v === "number" ? v : pollConf.default_s;
  }

  /* centered moving average; nulls stay null so real gaps stay gaps */
  function movAvg(values, w) {
    if (w <= 1) { return values; }
    var half = Math.floor(w / 2);
    var out = [];
    for (var i = 0; i < values.length; i += 1) {
      if (values[i] == null) { out.push(null); continue; }
      var sum = 0;
      var n = 0;
      for (var j = Math.max(0, i - half);
           j <= Math.min(values.length - 1, i + half); j += 1) {
        if (values[j] != null) { sum += values[j]; n += 1; }
      }
      out.push(n ? sum / n : null);
    }
    return out;
  }

  /* time-bucket averaging: the anti-aliasing the sawtooths need.
     A bucket wide enough to hold a loop's whole tooth period turns
     the see-saw into its honest average staleness level; stride
     sampling (which turned 6H/24H into noise) never touches the
     display path again. */
  function rangeSeconds() {
    return netTelem.range === "24h" ? 86400
      : netTelem.range === "6h" ? 21600 : 3600;
  }

  var panelCharts = {};
  var actTimer = null;
  var actData = null;

  function fetchActivity() {
    fetch("/api/telemetry?range=24h", { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        actData = d && d.present ? (d.points || []) : [];
        renderActivity();
      })
      .catch(function () { actData = null; });
  }

  function startActivity() {
    fetchActivity();
    if (actTimer) { clearInterval(actTimer); }
    actTimer = setInterval(function () {
      if (!pollCfg.paused) { fetchActivity(); }
    }, 60000);
  }

  function stopActivity() {
    if (actTimer) { clearInterval(actTimer); actTimer = null; }
  }

  function activityBuckets() {
    /* one stacked column per hour, 24 back from now: transitions
       into bad (H), into warn (M), recoveries to ok (L) */
    var buckets = [];
    for (var b = 0; b < 24; b += 1) {
      buckets.push({ h: 0, m: 0, l: 0 });
    }
    if (!actData || actData.length < 2) { return buckets; }
    var t0 = Date.now() - 24 * 3600 * 1000;
    for (var mi = 0; mi < NET_METRICS.length; mi += 1) {
      var m = NET_METRICS[mi];
      var thr = thrFor(m.key);
      if (!thr) { continue; }
      var confirmed = null;
      var pending = null;
      var pendingTs = null;
      for (var i = 0; i < actData.length; i += 1) {
        var v = telNum(actData[i][m.field]);
        if (v == null) { continue; }
        var cur = v >= thr.bad ? "bad"
          : v >= thr.warn ? "warn" : "ok";
        if (confirmed === null) { confirmed = cur; continue; }
        if (cur === confirmed) { pending = null; continue; }
        if (pending === cur) {
          var ts = Date.parse(pendingTs);
          if (isFinite(ts) && ts >= t0) {
            var at = Math.min(23, Math.max(0,
              Math.floor((ts - t0) / 3600000)));
            if (cur === "bad") { buckets[at].h += 1; }
            else if (cur === "warn") { buckets[at].m += 1; }
            else { buckets[at].l += 1; }
          }
          confirmed = cur;
          pending = null;
        } else {
          pending = cur;
          pendingTs = actData[i].ts;
        }
      }
    }
    return buckets;
  }

  function renderActivity() {
    var host = document.getElementById("net-activity");
    if (!host) { return; }
    var buckets = activityBuckets();
    var canvas = host.querySelector("canvas");
    if (!canvas) {
      host.textContent = "";
      canvas = document.createElement("canvas");
      host.appendChild(canvas);
    }
    var w = Math.max(120, host.clientWidth);
    var h = 96;
    var ratio = Math.max(1, window.devicePixelRatio || 1);
    canvas.style.width = w + "px";
    canvas.style.height = h + "px";
    canvas.width = Math.round(w * ratio);
    canvas.height = Math.round(h * ratio);
    var ctx = canvas.getContext("2d");
    if (!ctx) { return; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, w, h);
    var plotH = h - 18;
    var maxN = 3;
    for (var i = 0; i < 24; i += 1) {
      var tot = buckets[i].h + buckets[i].m + buckets[i].l;
      if (tot > maxN) { maxN = tot; }
    }
    var slot = w / 24;
    var bw = slot * 0.72;
    var colors = { l: "#24c19a", m: "#fbbf24", h: "#f87171" };
    for (var b2 = 0; b2 < 24; b2 += 1) {
      var x = b2 * slot + (slot - bw) / 2;
      var y = plotH;
      var parts = ["l", "m", "h"];
      for (var p = 0; p < parts.length; p += 1) {
        var n = buckets[b2][parts[p]];
        if (!n) { continue; }
        var bh = Math.max(2, (n / maxN) * (plotH - 6));
        y -= bh;
        ctx.fillStyle = colors[parts[p]];
        ctx.fillRect(x, y, bw, bh);
        y -= 1;
      }
    }
    /* baseline + sparse hour labels, UTC */
    var styles = getComputedStyle(host);
    ctx.fillStyle = styles.getPropertyValue("--st-faint").trim() ||
      "#95959d";
    ctx.strokeStyle = styles.getPropertyValue("--st-divider").trim() ||
      "#f0f0f2";
    ctx.beginPath();
    ctx.moveTo(0, plotH + 0.5);
    ctx.lineTo(w, plotH + 0.5);
    ctx.stroke();
    ctx.font = "11px " + (styles.getPropertyValue("--sans").trim() ||
      "InterVariable, sans-serif");
    var nowH = new Date().getUTCHours();
    for (var lb = 0; lb < 24; lb += 1) {
      var hourAt = (nowH - 23 + lb + 48) % 24;
      if (hourAt % 6 !== 0) { continue; }
      var tx = lb * slot + slot / 2;
      var txt = (hourAt < 10 ? "0" : "") + hourAt;
      ctx.fillText(txt, tx - 6, h - 4);
    }
  }

  /* theme flips repaint tokens instantly but canvases keep their
     mount-time colors — remount the page's charts on the flip */
  (function watchTheme() {
    if (!window.MutationObserver) { return; }
    new MutationObserver(function () {
      if ((location.hash || "").slice(1) !== "network") { return; }
      if (netOverview) { netOverview.destroy(); netOverview = null; }
      var keys = Object.keys(panelCharts);
      for (var i = 0; i < keys.length; i += 1) {
        panelCharts[keys[i]].destroy();
      }
      panelCharts = {};
      renderTelemetry();
    }).observe(document.documentElement,
      { attributes: true, attributeFilter: ["data-theme"] });
  })();

  function buildPanel(def, idx) {
    var panel = stEl("div", "st-card st-panel");
    panel.id = "strow-" + def.id;
    var head = stEl("div", "st-panel-head");
    var icon = stEl("span", "st-check");
    icon.appendChild(stIcon(0));
    head.appendChild(icon);
    head.appendChild(stEl("span", "st-t-name", def.name));
    head.appendChild(stEl("span", "st-spacer"));
    var cur = stEl("span", "st-t-datum st-val", "\u2014");
    cur.setAttribute("data-current", def.id);
    head.appendChild(cur);
    var up = stEl("span", "st-uptime", "\u2014");
    up.setAttribute("data-uptime", def.id);
    head.appendChild(up);
    panel.appendChild(head);
    var bars = stEl("div", "st-bars");
    bars.setAttribute("data-bars", def.id);
    panel.appendChild(bars);
    var graph = stEl("div", "st-panel-graph");
    graph.setAttribute("data-graph", def.id);
    panel.appendChild(graph);

    var foot = stEl("div", "st-panel-foot");
    foot.appendChild(stEl("span", "st-t-meta", "Poll"));
    var sel = stEl("select", "st-select");
    for (var ai = 0; ai < pollConf.allowed.length; ai += 1) {
      var o = stEl("option", "", pollConf.allowed[ai] + "s");
      o.value = String(pollConf.allowed[ai]);
      if (pollIntervalFor(def.id) === pollConf.allowed[ai]) {
        o.selected = true;
      }
      sel.appendChild(o);
    }
    sel.setAttribute("data-poll", def.id);
    sel.addEventListener("change", function () {
      var patch = { metrics: {} };
      patch.metrics[def.id] = parseInt(sel.value, 10);
      postPollConfig(patch, function (ok) {
        if (!ok) { sel.value = String(pollIntervalFor(def.id)); }
      });
    });
    foot.appendChild(sel);
    var stamp = stEl("span", "st-t-meta", "");
    stamp.setAttribute("data-sampled", def.id);
    foot.appendChild(stamp);
    foot.appendChild(stEl("span", "st-spacer"));
    var rfr = stEl("button", "st-ctl-btn", "Refresh");
    rfr.setAttribute("type", "button");
    rfr.setAttribute("title", "fetch this loop's reading now");
    rfr.addEventListener("click", function () {
      if (def.id === "server" || def.id === "mirror" ||
          def.id === "market" || def.id === "supervisor") { pollStrip(); }
      else if (def.id === "newsloop") { fetchNews(true); }
      else { fetchNetHealth(); }
      fetchTelemetry();
    });
    foot.appendChild(rfr);
    var raw = stEl("button", "st-ctl-btn", "Raw");
    raw.setAttribute("type", "button");
    raw.setAttribute("title", "open the backing telemetry feed");
    raw.addEventListener("click", function () {
      window.open("/api/telemetry?range=" + netTelem.range, "_blank");
    });
    foot.appendChild(raw);
    if (def.id !== "server") {
      var view = stEl("button", "st-ctl-btn", "View");
      view.setAttribute("type", "button");
      view.setAttribute("title", "solo this loop in the master graph");
      view.addEventListener("click", function () {
        for (var vi = 0; vi < NET_METRICS.length; vi += 1) {
          var mk = NET_METRICS[vi].key;
          chipOn[mk] = mk === def.id;
        }
        var chipsAll = document.querySelectorAll(".st-chip");
        for (var cq = 0; cq < chipsAll.length; cq += 1) {
          var lbl = chipsAll[cq].textContent;
          var on = false;
          for (var vj = 0; vj < NET_METRICS.length; vj += 1) {
            if (NET_METRICS[vj].label === lbl) {
              on = chipOn[NET_METRICS[vj].key] !== false;
            }
          }
          chipsAll[cq].classList.toggle("off", !on);
        }
        syncOverviewShow();
        var g = document.getElementById("net-overview");
        if (g) { g.scrollIntoView({ behavior: "smooth", block: "center" }); }
      });
      foot.appendChild(view);
    }
    foot.appendChild(mkSwitch(connState[def.id] !== false, function (on) {
      connState[def.id] = on;
      if (def.id === "server") {
        pollCfg.paused = !on;
        savePollCfg();
      }
      if (def.id === "newsloop") { setMaster(on); }
      renderSigAll();
      syncOverviewShow();
    }));
    panel.appendChild(foot);
    return panel;
  }

  function statTile(label, value) {
    var t = stEl("div", "st-stat");
    t.appendChild(stEl("div", "st-t-meta", label));
    t.appendChild(stEl("div", "st-t-datum st-stat-val", value));
    return t;
  }

  function updateStats() {
    var host = document.getElementById("net-stats");
    if (!host) { return; }
    var pts = telemPoints();
    var rtts = [];
    var worstKey = null;
    var worstRatio = 0;
    for (var i = 0; i < pts.length; i += 1) {
      var r = telNum(pts[i].rtt_ms);
      if (r != null) { rtts.push(r); }
    }
    rtts.sort(function (a, b) { return a - b; });
    var p50 = rtts.length
      ? rtts[Math.floor(rtts.length / 2)] + " ms" : "\u2014";
    for (var mi2 = 0; mi2 < NET_METRICS.length; mi2 += 1) {
      var mm = NET_METRICS[mi2];
      if (mm.unit !== "s" || connState[mm.key] === false) { continue; }
      var cur2 = currentVal(mm.key);
      var cad2 = cadenceEst(mm.field);
      if (cur2 != null && cad2) {
        var ratio = cur2 / cad2;
        if (ratio > worstRatio) {
          worstRatio = ratio;
          worstKey = mm.label;
        }
      }
    }
    var changes = String(netEvCount);
    host.textContent = "";
    host.appendChild(statTile("Server RTT p50", p50));
    host.appendChild(statTile("Slowest vs rhythm",
      worstKey ? worstKey + " \u00d7" +
        (Math.round(worstRatio * 10) / 10) : "\u2014"));
    host.appendChild(statTile("Samples in window",
      String(pts.length)));
    host.appendChild(statTile("State changes", changes));
  }

  function updatePanels() {
    for (var i = 0; i < CONNECTIONS.length; i += 1) {
      var def = CONNECTIONS[i];
      if (def.group === "boundary") { continue; }
      var panel = document.getElementById("strow-" + def.id);
      if (!panel) { continue; }
      var st = rowStatus(def);
      var off = !connState[def.id];
      var state = off ? 3
        : st.statusCls === "bad" ? 2
        : st.statusCls === "warn" ? 1 : 0;
      panel.classList.toggle("off", off);
      var icon = panel.querySelector(".st-check");
      if (icon && icon.getAttribute("data-state") !== String(state)) {
        icon.setAttribute("data-state", String(state));
        icon.textContent = "";
        icon.appendChild(stIcon(state));
      }
      var up = panel.querySelector("[data-uptime]");
      if (up) { up.textContent = off ? "Off" : uptimeText(def.id); }
      var curEl = panel.querySelector("[data-current]");
      if (curEl) {
        curEl.textContent = st.value == null ? "\u2014" : String(st.value);
      }
      var stampEl = panel.querySelector("[data-sampled]");
      if (stampEl) {
        var m2 = netMetric(def.id);
        var age2 = null;
        if (m2) {
          var pts2 = telemPoints();
          for (var pj = pts2.length - 1; pj >= 0; pj -= 1) {
            if (telNum(pts2[pj][m2.field]) != null) {
              var pt2 = Date.parse(pts2[pj].ts);
              if (isFinite(pt2)) {
                age2 = Math.max(0, Math.round(
                  (Date.now() - pt2) / 1000));
              }
              break;
            }
          }
        }
        stampEl.textContent = age2 == null ? ""
          : "sampled " + age2 + "s ago";
      }
      var bars = panel.querySelector("[data-bars]");
      if (bars && window.QNet && QNet.bars) {
        var samples = statusSamples(def.id, 90);
        if (samples && samples.length) {
          QNet.bars.render(bars, samples);
        } else { bars.textContent = ""; }
      }
    }
  }

  /* ---- telemetry file client: real history via /api/telemetry
     (recorder writes ui/live/telemetry.jsonl every 15s). All chart
     data, uptime %, trend bars, and thresholds come from HERE — the
     page never invents a series again. ---- */
  var NET_METRICS = [
    { key: "server", field: "rtt_ms", label: "Server RTT", unit: "ms" },
    { key: "mirror", field: "mirror_s", label: "Mirror age", unit: "s" },
    { key: "market", field: "market_s", label: "Feed age", unit: "s" },
    { key: "supervisor", field: "supervisor_s", label: "Supervisor age",
      unit: "s" },
    { key: "newsloop", field: "news_s", label: "News loop age", unit: "s" },
    { key: "basiswatch", field: "basis_watch_s", label: "Basis watch age",
      unit: "s" },
    { key: "basissup", field: "basis_sup_s", label: "Basis sup age",
      unit: "s" },
    { key: "jobrunner", field: "jobs_s", label: "Job runner age",
      unit: "s" }
  ];
  var netTelem = { range: "1h", data: null };
  var netTelemTimer = null;
  var netChart = null;
  var netOverview = null;
  var chipOn = {};
  var netScaleMode = "expand";
  var netSmooth = "soft";

  function destroyAllCharts() {
    if (netOverview) { netOverview.destroy(); netOverview = null; }
    var keys = Object.keys(panelCharts);
    for (var ki = 0; ki < keys.length; ki += 1) {
      panelCharts[keys[ki]].destroy();
    }
    panelCharts = {};
  }

  function remountCharts() {
    destroyAllCharts();
    renderTelemetry();
  }

  function syncOverviewShow() {
    if (!netOverview) { return; }
    for (var i = 0; i < NET_METRICS.length; i += 1) {
      var k = NET_METRICS[i].key;
      netOverview.setShow(i,
        chipOn[k] !== false && connState[k] !== false);
    }
  }

  function netMetric(key) {
    for (var i = 0; i < NET_METRICS.length; i++) {
      if (NET_METRICS[i].key === key) { return NET_METRICS[i]; }
    }
    return null;
  }

  var netFetchSeq = 0;
  var netEvLedger = {};
  var netEvCount = 0;

  function fetchTelemetry() {
    var seq = ++netFetchSeq;
    fetch("/api/telemetry?range=" + netTelem.range, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (seq !== netFetchSeq) { return; }
        netTelem.data = d;
        if ((location.hash || "").slice(1) === "network") {
          renderTelemetry();
        }
      })
      .catch(function () {
        if (seq === netFetchSeq) { netTelem.data = null; }
      });
  }

  function startNetTelemetry() {
    fetchTelemetry();
    if (netTelemTimer) { clearInterval(netTelemTimer); }
    netTelemTimer = setInterval(function () {
      if (!pollCfg.paused) { fetchTelemetry(); }
    }, netTelem.range === "1h" ? 10000 : 30000);
  }

  function stopNetTelemetry() {
    if (netTelemTimer) { clearInterval(netTelemTimer); netTelemTimer = null; }
  }

  function telemPoints() {
    var d = netTelem.data;
    return (d && d.present && d.points) || [];
  }

  function telemSeries(field) {
    var pts = telemPoints();
    var ts = [];
    var vs = [];
    for (var i = 0; i < pts.length; i++) {
      var t = Date.parse(pts[i].ts);
      if (!isFinite(t)) { continue; }
      ts.push(Math.round(t / 1000));
      vs.push(telNum(pts[i][field]));
    }
    return [ts, vs];
  }

  /* per-metric series on the shared grid: carry the last measured
     value within its own poll tolerance; AGE fields extrapolate by
     elapsed time (physics: age grows while unsampled). Beyond
     tolerance the line honestly breaks. */
  function seriesFilled(field, key) {
    var pts = telemPoints();
    var tol = pollIntervalFor(key) * 1.6 + 10;
    var isAge = field !== "rtt_ms";
    var ts = [];
    var vs = [];
    var lastT = null;
    var lastV = null;
    for (var i = 0; i < pts.length; i += 1) {
      var t = Date.parse(pts[i].ts);
      if (!isFinite(t)) { continue; }
      t = Math.round(t / 1000);
      var raw = telNum(pts[i][field]);
      if (raw != null) { lastT = t; lastV = raw; }
      ts.push(t);
      if (lastT == null || t - lastT > tol) {
        vs.push(null);
      } else {
        vs.push(isAge && t > lastT ? lastV + (t - lastT) : lastV);
      }
    }
    return [ts, vs];
  }

  /* ONE shared display grid for every graph on the page: identical
     x range, identical step, identical point count — a loop sampled
     at 1s and a loop sampled at 30s land on the same skeleton, so
     every chart draws the same shape of box (owner symmetry law). */
  function displayGrid() {
    /* second-level granularity: 720 grid points per window, floor 4s
       (1H draws 5s steps; a 1s-polled loop shows near-raw detail) */
    var span = Math.max(4, Math.round(rangeSeconds() / 720));
    var t1 = Math.floor(Date.now() / 1000 / span) * span;
    var t0 = t1 - rangeSeconds();
    var times = [];
    for (var t = t0; t <= t1; t += span) { times.push(t); }
    return { span: span, times: times };
  }

  function gridSeries(m, grid) {
    var pts = telemPoints();
    var tol = Math.max(pollIntervalFor(m.key) * 1.6 + 10, grid.span * 1.5);
    var isAge = m.field !== "rtt_ms";
    var samples = [];
    for (var i = 0; i < pts.length; i += 1) {
      var t = Date.parse(pts[i].ts);
      if (!isFinite(t)) { continue; }
      var raw = telNum(pts[i][m.field]);
      if (raw != null) { samples.push([Math.round(t / 1000), raw]); }
    }
    if (netSmooth === "raw") {
      /* fall through: no averaging at all in raw mode */
    }
    if (samples.length >= 3) {
      /* eras sampled slower than today's setting must not tear into
         holes: learn the metric's median spacing and widen tolerance
         to cover it (a truly dead recorder still breaks the line) */
      var gaps = [];
      for (var gi2 = 1; gi2 < samples.length; gi2 += 1) {
        gaps.push(samples[gi2][0] - samples[gi2 - 1][0]);
      }
      gaps.sort(function (a, b) { return a - b; });
      var medGap = gaps[Math.floor(gaps.length / 2)];
      if (medGap > 0) { tol = Math.max(tol, medGap * 2.2 + 5); }
    }
    var out = [];
    var si = 0;
    var lastT = null;
    var lastV = null;
    for (var g = 0; g < grid.times.length; g += 1) {
      var gt = grid.times[g];
      while (si < samples.length && samples[si][0] <= gt) {
        lastT = samples[si][0];
        lastV = samples[si][1];
        si += 1;
      }
      if (lastT == null || gt - lastT > tol) {
        out.push(null);
      } else {
        out.push(isAge && gt > lastT ? lastV + (gt - lastT) : lastV);
      }
    }
    /* soften each loop against its OWN rhythm (~0.7 of a tooth):
       fast loops keep fine detail, slow loops roll like the owner's
       reference — waves, never razors, never flat lines */
    if (netSmooth === "raw") { return out; }
    var cad = cadenceEst(m.field);
    var w = 3;
    if (cad) {
      var factor = netSmooth === "silk" ? 1.8 : 0.7;
      w = Math.round((cad * factor) / grid.span);
      if (w % 2 === 0) { w += 1; }
      w = Math.max(3, Math.min(61, w));
    }
    return movAvg(out, w);
  }

  /* cadence estimate: an age series saw a tick land wherever the value
     drops; the value just before each drop ≈ that loop's interval.
     Median of those, sane-clamped. Measured, never configured. */
  function cadenceEst(field) {
    /* audit F: sparse rows interleave metrics — compare the metric's
       own consecutive non-null samples, not adjacent rows */
    var sv = telemSeries(field)[1];
    var pre = [];
    var prevV = null;
    for (var i = 0; i < sv.length; i += 1) {
      if (sv[i] == null) { continue; }
      if (prevV != null && sv[i] < prevV * 0.5) {
        pre.push(prevV);
      }
      prevV = sv[i];
    }
    if (pre.length < 2) { return null; }
    pre.sort(function (a, b) { return a - b; });
    var med = pre[Math.floor(pre.length / 2)];
    if (!(med >= 5) || med > 3600) { return null; }
    return med;
  }

  function thrFor(compKey) {
    if (compKey === "server") { return { warn: 350, bad: 800 }; }
    var m = netMetric(compKey);
    if (m) {
      var c = cadenceEst(m.field);
      if (c) { return { warn: c * 2, bad: c * 4 }; }
    }
    /* honest fallbacks while history is thin */
    if (compKey === "mirror") {
      var wb = mirrorWarnBad();
      return { warn: wb[0], bad: wb[1] };
    }
    if (compKey === "market") { return { warn: 45, bad: 120 }; }
    if (compKey === "supervisor") { return { warn: 360, bad: 720 }; }
    if (compKey === "newsloop") { return { warn: 300, bad: 900 }; }
    /* host loop cadences from the deploy record (runner 1m cron,
       runtime chain 3m) - replaced by measured cadence as soon as
       the telemetry window holds two tick resets */
    if (compKey === "basiswatch" || compKey === "basissup") {
      return { warn: 360, bad: 720 };
    }
    if (compKey === "jobrunner") { return { warn: 120, bad: 240 }; }
    return null;
  }

  function thrCls(compKey, v) {
    if (v == null) { return ""; }
    var t = thrFor(compKey);
    if (!t) { return ""; }
    return v >= t.bad ? "bad" : (v >= t.warn ? "warn" : "ok");
  }

  function currentVal(compKey) {
    var d = lastStrip || {};
    if (compKey === "server") { return telNum(lastRtt); }
    if (compKey === "mirror") { return telNum(d.pull && d.pull.age_seconds); }
    if (compKey === "market") {
      return telNum(d.market_pass && d.market_pass.age_seconds);
    }
    if (compKey === "supervisor") {
      return telNum(d.supervisor && d.supervisor.age_seconds);
    }
    if (compKey === "newsloop") {
      /* disk truth first: the recorder measures the mirror's real age
         every 15s. The browser's own as_of goes stale while the user
         camps on this page (no news refetch here) and once invented a
         20m OUTAGE the disk never saw. Telemetry sample + elapsed time
         = an honest upper bound; browser as_of is only the fallback. */
      var tp2 = telemPoints();
      var lastP = tp2.length ? tp2[tp2.length - 1] : null;
      if (lastP && lastP.news_s != null) {
        var pAge = (Date.now() - Date.parse(lastP.ts)) / 1000;
        if (isFinite(pAge) && pAge >= 0 && pAge < 90) {
          return lastP.news_s + pAge;
        }
      }
      return (newsData && newsData.as_of)
        ? (Date.now() - Date.parse(newsData.as_of)) / 1000 : null;
    }
    if (netHealth && netHealth.present) {
      if (compKey === "jobrunner") {
        return telNum(netHealth.jobs_log &&
          netHealth.jobs_log.last_tick_age_seconds);
      }
      var svcKey = compKey === "basiswatch" ? "basis_watch"
        : compKey === "basissup" ? "basis_supervisor" : null;
      if (svcKey) {
        var svc = healthService(svcKey);
        return svc ? telNum(svc.last_seen_age_seconds) : null;
      }
    }
    return null;
  }

  function uptimePct(compKey) {
    /* audit F: uptime uses the SAME grade as the bars — a bucket the
       bars call amber can no longer read 100% */
    var m = netMetric(compKey);
    if (!m) { return null; }
    var t = thrFor(compKey);
    if (!t) { return null; }
    var vs = seriesFilled(m.field, compKey)[1];
    var clean = [];
    var i;
    for (i = 0; i < vs.length; i += 1) {
      if (vs[i] != null) { clean.push(vs[i]); }
    }
    if (clean.length < 5) { return null; }
    var sorted = clean.slice().sort(function (a, b) { return a - b; });
    var med = sorted[Math.floor(sorted.length / 2)];
    var okc = 0;
    for (i = 0; i < clean.length; i += 1) {
      if (gradeValue(clean[i], t, med) === "ok") { okc += 1; }
    }
    return 100 * okc / clean.length;
  }

  function gradeValue(v, t, med) {
    if (v == null) { return null; }
    if (t && t.bad != null && v >= t.bad) { return "bad"; }
    if ((t && t.warn != null && v >= t.warn) ||
        (med > 0 && v > med * 2.5)) { return "warn"; }
    return "ok";
  }

  function statusSamples(compKey, maxN) {
    /* audit F: equal-TIME buckets keeping the WORST state — index
       striding hid spikes between strides and let density lie */
    var m = netMetric(compKey);
    if (!m) { return null; }
    var sf = seriesFilled(m.field, compKey);
    var ts = sf[0];
    var vs = sf[1];
    if (!vs.length) { return null; }
    var t = thrFor(compKey);
    var clean = [];
    for (var c = 0; c < vs.length; c += 1) {
      if (vs[c] != null) { clean.push(vs[c]); }
    }
    if (!clean.length) { return null; }
    clean.sort(function (a, b) { return a - b; });
    var med = clean[Math.floor(clean.length / 2)];
    var rank = { ok: 0, warn: 1, bad: 2 };
    var t1 = ts[ts.length - 1];
    var t0 = t1 - rangeSeconds();
    var span = (t1 - t0) / maxN;
    if (!(span > 0)) { return null; }
    var out = [];
    for (var b = 0; b < maxN; b += 1) { out.push(null); }
    for (var i = 0; i < ts.length; i += 1) {
      if (ts[i] < t0) { continue; }
      var at = Math.min(maxN - 1,
        Math.max(0, Math.floor((ts[i] - t0) / span)));
      var g = gradeValue(vs[i], t, med);
      if (g == null) { continue; }
      if (out[at] == null || rank[g] > rank[out[at]]) { out[at] = g; }
    }
    return out;
  }

  function renderTelemetry() {
    var d = netTelem.data;
    var host = document.getElementById("net-overview");
    var capEl = document.getElementById("net-chart-cap");
    var pts = telemPoints();
    var rootCs = getComputedStyle(document.documentElement);
    var sharedGrid = pts.length >= 2 ? displayGrid() : null;
    var mi;
    if (host) {
      if (!d || !d.present || pts.length < 2) {
        destroyAllCharts();
        host.textContent = "";
        var emptyMsg = stEl("div", "st-t-meta",
          !d ? "Telemetry endpoint unreachable."
             : "History arrives from the telemetry recorder.");
        emptyMsg.style.padding = "8px";
        host.appendChild(emptyMsg);
      } else if (window.QNet && QNet.chart && QNet.chart.mountMulti) {
        /* overview: the seven age loops, one unit (seconds),
           smoothed for reading — RTT lives in its own panel */
        var grid = sharedGrid;
        var times = grid.times;
        var defs = [];
        for (mi = 0; mi < NET_METRICS.length; mi += 1) {
          var m = NET_METRICS[mi];
          if (m.unit !== "s") { continue; }
          defs.push({
            label: m.label,
            unit: "s",
            color: rootCs.getPropertyValue("--st-s" + (mi + 1))
              .replace(/^\s+|\s+$/g, "") || "#3b82f6",
            values: gridSeries(m, grid)
          });
        }
        if (times && times.length >= 2) {
          if (!netOverview) {
            host.textContent = "";
            netOverview = QNet.chart.mountMulti(host,
              { times: times, series: defs, height: 400,
                linearScale: netScaleMode === "linear" });
          } else {
            netOverview.setData(times, defs);
          }
          syncOverviewShow();
        }
      }
    }
    if (capEl) {
      var n = d && d.present ? (d.count || 0) : 0;
      var newestAge = null;
      if (pts.length) {
        var lastTs = Date.parse(pts[pts.length - 1].ts);
        if (isFinite(lastTs)) {
          newestAge = Math.round((Date.now() - lastTs) / 1000);
        }
      }
      var silent = newestAge != null && newestAge > 60;
      var spanTxt = netTelem.range;
      if (pts.length >= 2) {
        var sFirst = Date.parse(pts[0].ts);
        var sLast = Date.parse(pts[pts.length - 1].ts);
        if (isFinite(sFirst) && isFinite(sLast)) {
          var covered = (sLast - sFirst) / 3600000;
          var wanted = rangeSeconds() / 3600;
          if (d && d.truncated && covered < wanted * 0.98) {
            spanTxt = "last " + (Math.round(covered * 10) / 10) +
              "h of " + netTelem.range;
          }
        }
      }
      capEl.textContent = !n
        ? "History arrives from the telemetry recorder."
        : silent
          ? "Recorder silent " + newestAge + "s \u00b7 " +
            spanTxt + " \u00b7 UTC"
          : "Live \u00b7 " + spanTxt + " \u00b7 UTC";
      capEl.style.color = silent ? "var(--st-warn-text)" : "";
      capEl.setAttribute("title", n
        ? "ui/live/telemetry.jsonl \u00b7 " + n + " samples"
        : "");
    }
    /* panel graphs: each loop's own line in its own color */
    if (pts.length >= 2 && window.QNet && QNet.chart &&
        QNet.chart.mountMulti) {
      var pgrid = sharedGrid || displayGrid();
      for (mi = 0; mi < NET_METRICS.length; mi += 1) {
        var pm = NET_METRICS[mi];
        var pHost = document.querySelector(
          "[data-graph=\"" + pm.key + "\"]");
        if (!pHost) { continue; }
        var pdef = [{
          label: pm.label,
          unit: pm.unit,
          color: rootCs.getPropertyValue("--st-s" + (mi + 1))
            .replace(/^\s+|\s+$/g, "") || "#3b82f6",
          values: gridSeries(pm, pgrid)
        }];
        if (pgrid.times.length >= 2) {
          if (panelCharts[pm.key]) {
            panelCharts[pm.key].setData(pgrid.times, pdef);
          } else {
            pHost.textContent = "";
            panelCharts[pm.key] = QNet.chart.mountMulti(pHost, {
              times: pgrid.times, series: pdef, height: 150,
              compact: true, linearScale: netScaleMode === "linear"
            });
          }
        }
      }
    }
    updatePanels();
    var evHost2 = document.getElementById("net-events");
    if (evHost2 && window.QNet && QNet.events) {
      var evDefs = [];
      for (var ei = 0; ei < NET_METRICS.length; ei += 1) {
        evDefs.push({ key: NET_METRICS[ei].key,
          field: NET_METRICS[ei].field,
          label: NET_METRICS[ei].label,
          thr: thrFor(NET_METRICS[ei].key) });
      }
      var evRows = QNet.events.compute(telemPoints(), evDefs);
      /* audit fix: learned thresholds drift as history deepens and
         can re-grade the past, silently erasing real incidents from
         the list. Once observed, a transition enters the session
         ledger and stays — the list shows the ledger, windowed. */
      for (var eli = 0; eli < evRows.length; eli += 1) {
        var er = evRows[eli];
        netEvLedger[er.key + "|" + er.ts + "|" + er.to] = er;
      }
      var cutoff = Date.now() - rangeSeconds() * 1000;
      var ledgerRows = [];
      var evKeys = Object.keys(netEvLedger);
      for (var elk = 0; elk < evKeys.length; elk += 1) {
        var lr = netEvLedger[evKeys[elk]];
        var lts = Date.parse(lr.ts);
        if (isFinite(lts) && lts >= cutoff) { ledgerRows.push(lr); }
      }
      ledgerRows.sort(function (a, b) {
        return new Date(b.ts).getTime() - new Date(a.ts).getTime();
      });
      netEvCount = ledgerRows.length;
      QNet.events.render(evHost2, ledgerRows);
      var evCap2 = document.getElementById("net-events-cap");
      if (evCap2) {
        evCap2.textContent = ledgerRows.length
          ? ledgerRows.length + " in the " + netTelem.range + " window"
          : netTelem.range + " window";
      }
    }
    updateStats();
    updateBanner();
  }

  function connOverallWorst() {
    /* worst-of across measured rows: 0 ok, 1 degraded, 2 outage */
    if (!lastStrip) { return 2; }
    var worst = 0;
    for (var i = 0; i < CONNECTIONS.length; i++) {
      var def = CONNECTIONS[i];
      if (!def.live || !connState[def.id]) { continue; }
      var cls = rowStatus(def).statusCls;
      if (cls === "bad") { worst = 2; }
      else if (cls === "warn" && worst < 1) { worst = 1; }
    }
    return worst;
  }

  function buildNetworkPage(container) {
    container.textContent = "";
    connState.server = !pollCfg.paused;
    connState.newsloop = newsMaster;
    destroyAllCharts();

    var page = stEl("div", "st-page");

    var head = stEl("div", "st-head");
    head.appendChild(stEl("div", "st-t-title", "Network"));
    page.appendChild(head);

    var plate = stEl("div", "st-plate");
    plate.id = "net-plate";
    var banner = stEl("div", "st-card");
    banner.id = "net-banner";
    plate.appendChild(banner);
    page.appendChild(plate);

    /* THE graph — big, one unit, smoothed, every loop a color */
    var graphCard = stEl("div", "st-card");
    var gHead = stEl("div", "st-card-head");
    gHead.appendChild(stEl("div", "st-t-h", "Loop timing"));
    gHead.appendChild(stEl("span", "st-spacer"));
    var seg = stEl("div", "st-seg");
    var rdefs = ["1h", "6h", "24h"];
    for (var si = 0; si < rdefs.length; si += 1) {
      (function (rg) {
        var rb = stEl("button", netTelem.range === rg ? "on" : "",
          rg.toUpperCase());
        rb.setAttribute("type", "button");
        rb.addEventListener("click", function () {
          if (netTelem.range === rg) { return; }
          netTelem.range = rg;
          var bs = seg.querySelectorAll("button");
          for (var q = 0; q < bs.length; q += 1) {
            bs[q].classList.toggle("on",
              bs[q].textContent === rg.toUpperCase());
          }
          startNetTelemetry();
        });
        seg.appendChild(rb);
      })(rdefs[si]);
    }
    gHead.appendChild(seg);
    function miniSeg(defs2, current, onPick) {
      var wrap = stEl("div", "st-seg");
      for (var di2 = 0; di2 < defs2.length; di2 += 1) {
        (function (d2) {
          var b2 = stEl("button", current === d2[0] ? "on" : "",
            d2[1]);
          b2.setAttribute("type", "button");
          b2.addEventListener("click", function () {
            var bs2 = wrap.querySelectorAll("button");
            for (var q2 = 0; q2 < bs2.length; q2 += 1) {
              bs2[q2].classList.toggle("on", bs2[q2] === b2);
            }
            onPick(d2[0]);
          });
          wrap.appendChild(b2);
        })(defs2[di2]);
      }
      return wrap;
    }
    gHead.appendChild(miniSeg(
      [["expand", "Expand"], ["linear", "Linear"]], netScaleMode,
      function (v) { netScaleMode = v; remountCharts(); }));
    gHead.appendChild(miniSeg(
      [["raw", "Raw"], ["soft", "Soft"], ["silk", "Silk"]], netSmooth,
      function (v) { netSmooth = v; remountCharts(); }));
    graphCard.appendChild(gHead);
    var chips = stEl("div", "st-chips");
    for (var ci2 = 0; ci2 < NET_METRICS.length; ci2 += 1) {
      if (NET_METRICS[ci2].unit !== "s") { continue; }
      (function (m, idx) {
        var chip = stEl("button", "st-chip");
        chip.setAttribute("type", "button");
        var key = stEl("span", "st-chip-key");
        key.style.background = "var(--st-s" + (idx + 1) + ")";
        chip.appendChild(key);
        chip.appendChild(document.createTextNode(m.label));
        chip.addEventListener("click", function () {
          var cur = chipOn[m.key] !== false;
          chipOn[m.key] = !cur;
          chip.classList.toggle("off", cur);
          syncOverviewShow();
        });
        chip.addEventListener("mouseenter", function () {
          if (!netOverview || !netOverview.setFocus) { return; }
          var at = 0;
          for (var fx = 0; fx < NET_METRICS.length; fx += 1) {
            if (NET_METRICS[fx].unit !== "s") { continue; }
            if (NET_METRICS[fx].key === m.key) {
              netOverview.setFocus(at);
              return;
            }
            at += 1;
          }
        });
        chip.addEventListener("mouseleave", function () {
          if (netOverview && netOverview.setFocus) {
            netOverview.setFocus(null);
          }
        });
        chips.appendChild(chip);
      })(NET_METRICS[ci2], ci2);
    }
    var allChip = stEl("button", "st-chip st-chip-all", "All");
    allChip.setAttribute("type", "button");
    allChip.setAttribute("title", "show every loop");
    allChip.addEventListener("click", function () {
      chipOn = {};
      var cs2 = chips.querySelectorAll(".st-chip");
      for (var az = 0; az < cs2.length; az += 1) {
        cs2[az].classList.remove("off");
      }
      syncOverviewShow();
    });
    chips.appendChild(allChip);
    graphCard.appendChild(chips);
    var stats = stEl("div", "st-stats");
    stats.id = "net-stats";
    graphCard.appendChild(stats);
    var gHost = stEl("div", "st-graph-host");
    gHost.id = "net-overview";
    graphCard.appendChild(gHost);
    var cap = stEl("div", "st-card-foot st-t-meta",
      "History arrives from the telemetry recorder.");
    cap.id = "net-chart-cap";
    graphCard.appendChild(cap);

    /* section line: one common Defaults that resets everything */
    var secHead = stEl("div", "st-sec-head");
    secHead.appendChild(stEl("div", "st-t-h", "System status"));
    secHead.appendChild(stEl("span", "st-spacer"));
    var dfl = stEl("button", "st-ctl-btn", "Defaults");
    dfl.setAttribute("type", "button");
    dfl.addEventListener("click", function () {
      pollCfg.strip = POLL_DEFAULTS.strip;
      pollCfg.news = POLL_DEFAULTS.news;
      pollCfg.paused = false;
      savePollCfg();
      for (var di = 0; di < CONNECTIONS.length; di += 1) {
        if (CONNECTIONS[di].group !== "boundary") {
          connState[CONNECTIONS[di].id] = true;
        }
      }
      chipOn = {};
      setMaster(true);
      restartStripPoll();
      if (newsTimer) { startNewsPoll(); }
      postPollConfig({ default_s: 15, metrics: {
        server: 15, mirror: 15, market: 15, supervisor: 15,
        newsloop: 15, basiswatch: 15, basissup: 15, jobrunner: 15
      } }, function () {
        if ((location.hash || "").slice(1) === "network") {
          startNetHealthPoll();
          pageEl.textContent = "";
          buildNetworkPage(pageEl);
        }
      });
    });
    secHead.appendChild(dfl);

    /* the master graph left, the network log right — one row
       directly under the banner; panels take the full width below */
    var cols = stEl("div", "st-cols");
    var colL = stEl("div", "");
    var colR = stEl("div", "st-logcol");
    cols.appendChild(colL);
    cols.appendChild(colR);
    colL.appendChild(graphCard);
    page.appendChild(cols);

    /* activity: the log column's own 24h severity histogram */
    var actCard = stEl("div", "st-card");
    var actHead = stEl("div", "st-card-head");
    actHead.appendChild(stEl("div", "st-t-h", "Activity \u00b7 24h"));
    actHead.appendChild(stEl("span", "st-spacer"));
    var lg = stEl("span", "st-t-meta", "");
    var lgDefs = [["#f87171", "H"], ["#fbbf24", "M"], ["#24c19a", "L"]];
    for (var lgi = 0; lgi < lgDefs.length; lgi += 1) {
      var dot = stEl("span", "st-chip-key");
      dot.style.background = lgDefs[lgi][0];
      dot.style.marginLeft = lgi ? "8px" : "0";
      lg.appendChild(dot);
      lg.appendChild(document.createTextNode(" " + lgDefs[lgi][1]));
    }
    actHead.appendChild(lg);
    actCard.appendChild(actHead);
    var actHost = stEl("div", "st-activity");
    actHost.id = "net-activity";
    actCard.appendChild(actHost);
    colR.appendChild(actCard);

    var bottom = colR;
    var evCard = stEl("div", "st-card");
    evCard.id = "net-evcard";
    var evHead = stEl("div", "st-card-head");
    evHead.appendChild(stEl("div", "st-t-h", "State changes"));
    evHead.appendChild(stEl("span", "st-spacer"));
    var evCap = stEl("span", "st-t-meta", "");
    evCap.id = "net-events-cap";
    evHead.appendChild(evCap);
    evCard.appendChild(evHead);
    var evHost = stEl("div", "");
    evHost.id = "net-events";
    evCard.appendChild(evHost);
    bottom.appendChild(evCard);

    var bCard = stEl("div", "st-card");
    var bHead = stEl("div", "st-card-head");
    bHead.appendChild(stEl("div", "st-t-h", "Boundaries"));
    bCard.appendChild(bHead);
    for (var bi = 0; bi < CONNECTIONS.length; bi += 1) {
      var bdef = CONNECTIONS[bi];
      if (bdef.group !== "boundary") { continue; }
      var brow = stEl("div", "st-row");
      var bline = stEl("div", "st-row-line");
      var bicon = stEl("span", "st-check");
      bicon.appendChild(stIcon(3));
      bline.appendChild(bicon);
      bline.appendChild(stEl("span", "st-t-name", bdef.name));
      bline.appendChild(stEl("span", "st-spacer"));
      bline.appendChild(stEl("span", "st-word off", "Owner gated"));
      brow.appendChild(bline);
      bCard.appendChild(brow);
    }
    var bFoot = stEl("div", "st-card-foot st-t-meta",
      "Poll selects set the recorder\u2019s sampling \u00b7 host " +
      "loops run on the host.");
    bCard.appendChild(bFoot);
    bottom.appendChild(bCard);

    /* panels: full width, 4 by 2, under the graph row */
    page.appendChild(secHead);
    var grid = stEl("div", "st-grid st-grid-wide");
    for (var ri = 0; ri < CONNECTIONS.length; ri += 1) {
      if (CONNECTIONS[ri].group === "boundary") { continue; }
      grid.appendChild(buildPanel(CONNECTIONS[ri], ri));
    }
    page.appendChild(grid);

    container.appendChild(page);
    startActivity();
    fetchPollConfig(function () {
      /* the allowed list and current values arrive after build —
         rebuild every select's options from the live config */
      var sels = page.querySelectorAll("[data-poll]");
      for (var pi = 0; pi < sels.length; pi += 1) {
        var key2 = sels[pi].getAttribute("data-poll");
        sels[pi].textContent = "";
        for (var oi = 0; oi < pollConf.allowed.length; oi += 1) {
          var opt2 = document.createElement("option");
          opt2.textContent = pollConf.allowed[oi] + "s";
          opt2.value = String(pollConf.allowed[oi]);
          sels[pi].appendChild(opt2);
        }
        sels[pi].value = String(pollIntervalFor(key2));
      }
    });
    renderSigAll();
    renderTelemetry();
  }

  function renderSigAll() {
    if ((location.hash || "").slice(1) === "network" && sigPage) {
      updateBanner();
      updatePanels();
    }
  }

  /* the Network page keeps its live reference for poll refreshes */
  var sigPage = null;

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
        if ((location.hash || "").slice(1) === "network") { renderSigAll(); }
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
    newsHost.appendChild(head);
    /* owner order: title inline with Overview's start; refresh + Live +
       News-on live BELOW the title row, never at the topbar boundary.
       The controls row always renders so News can be switched back on
       even when the mirror is empty or the page is off. */
    var stripRow = document.createElement("div");
    stripRow.className = "strip-row";
    stripRow.appendChild(right);
    newsHost.appendChild(stripRow);

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
    stripRow.insertBefore(strip, right);

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

  /* ---- global search — the old room's topbar SidebarSearch ported
     from source: opens on focus, grouped results, arrow/enter keys,
     ⌘K focus, outside-pointerdown close, their exact scoring
     (startsWith > word-start > includes > keywords). Index = the real
     product only: pages, live stream symbols, news headlines, news
     sources — all from the mirror; absent mirrors say so honestly. */
  var qsWrap = document.getElementById("qsearch");
  var qsBox = document.getElementById("qsearch-box");
  var qsInput = document.getElementById("qsearch-input");
  var qsKbd = document.getElementById("qsearch-k");
  var qsPop = document.getElementById("qsearch-pop");
  var qsQ = "";
  var qsSel = 0;
  var qsTab = "top";
  var qsFocused = false;
  var qsResults = [];
  var qsStream = null;
  var qsStreamAt = 0;
  var qsNews = null;
  var qsNewsAt = 0;

  var QS_PAGES = [
    { kind: "page", label: "Overview", hint: "Money page — empty by decision",
      page: "overview", keywords: "overview money pnl equity home dashboard" },
    { kind: "page", label: "Basis", hint: "Not built yet", page: "basis",
      keywords: "basis gap spot perp edge funding arbitrage" },
    { kind: "page", label: "Momentum", hint: "Not built yet", page: "momentum",
      keywords: "momentum volume surge ignition runner mover" },
    { kind: "page", label: "Machine", hint: "Not built yet", page: "machine",
      keywords: "machine trader authority policy execution quant" },
    { kind: "page", label: "Journal", hint: "Not built yet", page: "journal",
      keywords: "journal history trades receipts events log" },
    { kind: "page", label: "News", hint: "Live feed · sources · impact",
      page: "news", keywords: "news headlines sources feed impact tree rss" },
    { kind: "page", label: "Network", hint: "Path · toggles · cadence",
      page: "network",
      keywords: "network connections watchman signal polling server mirror" },
    { kind: "page", label: "Settings", hint: "Not built yet", page: "settings",
      keywords: "settings preferences theme language account logout" }
  ];

  function qsOpen() { return qsFocused || qsQ.length > 0; }

  function qsEnsureData() {
    var now = Date.now();
    if (!qsStream && now - qsStreamAt > 60000) {
      qsStreamAt = now;
      fetch("/api/stream", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { qsStream = d; if (qsOpen()) { qsRender(); } })
        .catch(function () { qsStream = null; });
    }
    if (!newsData && !qsNews && now - qsNewsAt > 60000) {
      qsNewsAt = now;
      fetch("/api/news", { cache: "no-store" })
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) { qsNews = d; if (qsOpen()) { qsRender(); } })
        .catch(function () { qsNews = null; });
    }
  }

  function qsNum(v) {
    var n = Number(v);
    return isFinite(n) ? n : null;
  }

  function qsIndex() {
    var out = QS_PAGES.slice();
    var toks = (qsStream && qsStream.present && qsStream.tokens) || [];
    for (var i = 0; i < toks.length; i++) {
      var t = toks[i];
      var d = t.data || {};
      var hasSpot = d.spot_bid != null;
      var hasPerp = d.perp_bid != null;
      var venue = hasSpot && hasPerp ? "Spot + Perp"
        : (hasPerp ? "Perp" : "Spot");
      var isSurge = (t.selection || []).indexOf("top_surge") >= 0;
      out.push({
        kind: "symbol",
        label: String(t.symbol),
        hint: venue + (isSurge ? " · volume surge" : " · basis gap"),
        page: isSurge ? "momentum" : "basis",
        surge: isSurge ? qsNum(d.surge_ratio) : null,
        gap: qsNum(d.basis_gap_pct),
        keywords: (t.symbol + " " + venue +
          " token symbol market coin").toLowerCase()
      });
    }
    var newsSrc = newsData || qsNews;
    var items = (newsSrc && newsSrc.present && newsSrc.items) || [];
    for (var n = 0; n < items.length; n++) {
      var it = items[n];
      out.push({
        kind: "news",
        label: it.title || "—",
        hint: (it.source || "—") +
          (typeof it.ts === "string" && it.ts.length >= 16
            ? " · " + it.ts.slice(11, 16) : ""),
        url: it.url || null,
        domain: it.domain || null,
        keywords: ((it.title || "") + " " +
          (it.source || "")).toLowerCase()
      });
    }
    var board = (newsSrc && newsSrc.sources_board) || {};
    var srcs = board.sources || [];
    for (var s = 0; s < srcs.length; s++) {
      var sr = srcs[s];
      out.push({
        kind: "source",
        label: sr.label || sr.key || "—",
        hint: (sr.domain || "feed") +
          (sr.status ? " · " + sr.status : ""),
        page: "news",
        domain: sr.domain || null,
        count: sr.count || 0,
        keywords: ((sr.label || "") + " " + (sr.domain || "") +
          " source feed").toLowerCase()
      });
    }
    return out;
  }

  function qsInTab(e) {
    if (qsTab === "pages") { return e.kind === "page"; }
    if (qsTab === "symbols") { return e.kind === "symbol"; }
    if (qsTab === "news") { return e.kind === "news"; }
    if (qsTab === "sources") { return e.kind === "source"; }
    return true;
  }

  function qsCompute() {
    var index = qsIndex();
    var ql = qsQ.trim().toLowerCase();
    if (!ql) {
      /* pre-typing board: what's moving now, then the pages — the old
         room also showed its markets before a query existed. */
      var idle = index.filter(function (e) {
        return qsInTab(e) && (e.kind === "symbol" || e.kind === "page");
      });
      idle.sort(function (a, b) {
        if (a.kind !== b.kind) { return a.kind === "symbol" ? -1 : 1; }
        if (a.kind === "symbol") { return (b.surge || 0) - (a.surge || 0); }
        return 0;
      });
      return idle.slice(0, 8);
    }
    var RANK = { page: 0, symbol: 1, news: 2, source: 3 };
    var scored = [];
    for (var i = 0; i < index.length; i++) {
      var e = index[i];
      if (!qsInTab(e)) { continue; }
      var l = e.label.toLowerCase();
      var score = -1;
      if (l.indexOf(ql) === 0) { score = 0; }
      else if (l.split(/\s+/).some(function (w) {
        return w.indexOf(ql) === 0;
      })) { score = 1; }
      else if (l.indexOf(ql) !== -1) { score = 2; }
      else if (e.keywords.indexOf(ql) !== -1) { score = 3; }
      if (score >= 0) { scored.push({ e: e, score: score }); }
    }
    scored.sort(function (a, b) {
      return a.score - b.score ||
        (RANK[a.e.kind] - RANK[b.e.kind]) ||
        ((b.e.surge || b.e.count || 0) - (a.e.surge || a.e.count || 0));
    });
    return scored.slice(0, 12).map(function (x) { return x.e; });
  }

  function qsGo(e) {
    if (!e) { return; }
    if (e.kind === "news") {
      if (e.url) { window.open(e.url, "_blank", "noopener"); }
      else { location.hash = "#news"; }
    } else if (e.page) {
      location.hash = "#" + e.page;
    }
    qsQ = "";
    qsInput.value = "";
    qsSel = 0;
    qsFocused = false;
    qsInput.blur();
    qsRender();
  }

  function qsRowEl(e, i) {
    var row = document.createElement("button");
    row.type = "button";
    row.className = "ssr-row" + (i === qsSel ? " sel" : "");
    var mark = document.createElement("span");
    mark.className = "ssr-mark";
    if ((e.kind === "news" || e.kind === "source") && e.domain) {
      (function (m, label) {
        var img = document.createElement("img");
        img.alt = "";
        img.src = "live/favicons/" + e.domain + ".png";
        img.addEventListener("error", function () {
          m.textContent = (label || "?").charAt(0).toUpperCase();
        });
        m.appendChild(img);
      })(mark, e.label);
    } else if (e.kind === "page") {
      var navIcon = document.querySelector(
        '.nav-row[data-page="' + e.page + '"] svg');
      if (navIcon) { mark.appendChild(navIcon.cloneNode(true)); }
      else { mark.textContent = e.label.charAt(0); }
    } else {
      mark.textContent = e.label.slice(0, 2).toUpperCase();
    }
    var asset = document.createElement("span");
    asset.className = "ssr-asset";
    var lb = document.createElement("span");
    lb.className = "ssr-label";
    lb.textContent = e.label;
    lb.title = e.label;
    var ht = document.createElement("span");
    ht.className = "ssr-hint";
    ht.textContent = e.hint || "";
    asset.appendChild(lb);
    asset.appendChild(ht);
    var right = document.createElement("span");
    if (e.kind === "symbol") {
      right.className = "ssr-price";
      var b = document.createElement("b");
      var sm = document.createElement("small");
      if (e.surge != null) {
        b.textContent = "×" + (e.surge >= 100
          ? Math.round(e.surge) : e.surge.toFixed(1));
        sm.textContent = "surge";
      } else if (e.gap != null) {
        b.textContent = e.gap.toFixed(2) + "%";
        sm.textContent = "gap";
      } else {
        b.textContent = "—";
        sm.textContent = "";
      }
      right.appendChild(b);
      right.appendChild(sm);
    } else if (e.kind === "source") {
      right.className = "ssr-price";
      var b2 = document.createElement("b");
      b2.textContent = String(e.count || 0);
      var sm2 = document.createElement("small");
      sm2.textContent = "items";
      right.appendChild(b2);
      right.appendChild(sm2);
    } else {
      right.className = "ssr-price single";
      right.textContent = e.kind === "news" ? "Open ↗" : "Open";
    }
    row.appendChild(mark);
    row.appendChild(asset);
    row.appendChild(right);
    row.addEventListener("mouseenter", function () {
      if (qsSel !== i) {
        qsSel = i;
        var rows = qsPop.querySelectorAll(".ssr-row");
        for (var r = 0; r < rows.length; r++) {
          rows[r].classList.toggle("sel",
            rows[r].getAttribute("data-i") === String(i));
        }
      }
    });
    row.setAttribute("data-i", String(i));
    row.addEventListener("mousedown", function (ev) {
      ev.preventDefault();
      qsGo(e);
    });
    return row;
  }

  function qsRender() {
    var open = qsOpen();
    qsWrap.classList.toggle("open", open);
    qsBox.classList.toggle("on", qsFocused || qsQ.length > 0);
    qsInput.setAttribute("aria-expanded", open ? "true" : "false");
    if (!open) { qsPop.hidden = true; return; }
    qsPop.hidden = false;
    qsResults = qsCompute();
    if (qsSel >= qsResults.length) {
      qsSel = Math.max(0, qsResults.length - 1);
    }
    qsPop.textContent = "";
    var tabs = document.createElement("div");
    tabs.className = "ssr-tabs";
    tabs.setAttribute("role", "tablist");
    var tdefs = [["top", "Top"], ["pages", "Pages"],
      ["symbols", "Symbols"], ["news", "News"], ["sources", "Sources"]];
    for (var ti = 0; ti < tdefs.length; ti++) {
      (function (id, label) {
        var tb = document.createElement("button");
        tb.type = "button";
        tb.className = qsTab === id ? "on" : "";
        tb.textContent = label;
        tb.addEventListener("mousedown", function (ev) {
          ev.preventDefault();
          qsTab = id;
          qsSel = 0;
          qsRender();
        });
        tabs.appendChild(tb);
      })(tdefs[ti][0], tdefs[ti][1]);
    }
    qsPop.appendChild(tabs);
    if (!qsResults.length) {
      var none = document.createElement("div");
      none.className = "ssr-empty";
      none.textContent = (qsStream || newsData || qsNews)
        ? "No matches. Try a page, symbol, source, or headline."
        : "Mirror not readable yet — pages remain searchable.";
      qsPop.appendChild(none);
      return;
    }
    var GROUPS = [["page", "Pages"], ["symbol", "Symbols"],
      ["news", "News"], ["source", "Sources"]];
    for (var g = 0; g < GROUPS.length; g++) {
      var kind = GROUPS[g][0];
      var rows = [];
      for (var ri2 = 0; ri2 < qsResults.length; ri2++) {
        if (qsResults[ri2].kind === kind) {
          rows.push(qsRowEl(qsResults[ri2], ri2));
        }
      }
      if (!rows.length) { continue; }
      var grp = document.createElement("div");
      grp.className = "ssr-group";
      var gt = document.createElement("div");
      gt.className = "ssr-group-title";
      gt.textContent = GROUPS[g][1];
      grp.appendChild(gt);
      for (var rw = 0; rw < rows.length; rw++) {
        grp.appendChild(rows[rw]);
      }
      qsPop.appendChild(grp);
    }
    var selEl = qsPop.querySelector(".ssr-row.sel");
    if (selEl && selEl.scrollIntoView) {
      selEl.scrollIntoView({ block: "nearest" });
    }
  }

  qsInput.addEventListener("focus", function () {
    qsFocused = true;
    qsEnsureData();
    qsRender();
  });
  qsBox.addEventListener("mousedown", function (ev) {
    if (ev.target !== qsInput) {
      ev.preventDefault();
      qsInput.focus();
    }
  });
  qsInput.addEventListener("input", function () {
    qsQ = qsInput.value;
    qsSel = 0;
    qsRender();
  });
  qsInput.addEventListener("keydown", function (ev) {
    if (ev.key === "ArrowDown") {
      ev.preventDefault();
      qsSel = Math.min(qsSel + 1, qsResults.length - 1);
      qsRender();
    } else if (ev.key === "ArrowUp") {
      ev.preventDefault();
      qsSel = Math.max(qsSel - 1, 0);
      qsRender();
    } else if (ev.key === "Enter") {
      if (qsResults[qsSel]) { qsGo(qsResults[qsSel]); }
    } else if (ev.key === "Escape") {
      ev.stopPropagation();
      qsQ = "";
      qsInput.value = "";
      qsFocused = false;
      qsInput.blur();
      qsRender();
    }
  });
  qsKbd.addEventListener("click", function () { qsInput.focus(); });
  document.addEventListener("pointerdown", function (ev) {
    if (qsOpen() && !qsWrap.contains(ev.target)) {
      qsQ = "";
      qsInput.value = "";
      qsFocused = false;
      qsRender();
    }
  });
  document.addEventListener("keydown", function (ev) {
    if ((ev.key === "k" || ev.key === "K") &&
        (isMac ? ev.metaKey : ev.ctrlKey)) {
      ev.preventDefault();
      qsInput.focus();
    }
  });

  /* boot the router last — routes reference structures defined above */
  window.addEventListener("hashchange", route);
  route();
})();
