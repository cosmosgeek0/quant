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
    if (netChart) { netChart.destroy(); netChart = null; }
    netTable = null;
    netTableHasCadence = false;
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

  function connCadence(def) {
    if (def.id === "server") {
      return { kind: "select", value: pollCfg.strip,
        options: [5, 10, 30, 60] };
    }
    if (def.id === "newsloop") {
      return { kind: "fixed", text: "120s · fixed" };
    }
    if (def.id === "mirror") {
      var iv = lastStrip && lastStrip.pull && lastStrip.pull.interval_ms;
      return (typeof iv === "number" && iv > 0)
        ? { kind: "fixed", text: Math.round(iv / 1000) + "s pull" }
        : null;
    }
    return null;
  }

  function buildTableModel() {
    var groups = [];
    for (var gi = 0; gi < CONN_GROUPS.length; gi++) {
      var rows3 = [];
      for (var mi = 0; mi < CONNECTIONS.length; mi++) {
        var def = CONNECTIONS[mi];
        if (def.group !== CONN_GROUPS[gi][0]) { continue; }
        var st = rowStatus(def);
        rows3.push({
          id: def.id,
          name: def.name,
          detail: def.detail,
          statusCls: st.statusCls,
          statusText: st.statusText,
          value: st.value,
          bars: statusSamples(def.id, 60),
          uptime: uptimePct(def.id),
          cadence: connCadence(def),
          canRefresh: def.id === "server" || def.id === "newsloop" ||
            def.live === "health",
          active: !!connState[def.id],
          hasControl: def.control === "pause" || def.control === "news",
          gated: def.group === "boundary"
        });
      }
      if (rows3.length) {
        groups.push({ id: CONN_GROUPS[gi][0], label: CONN_GROUPS[gi][1],
          rows: rows3 });
      }
    }
    return {
      groups: groups,
      callbacks: {
        onToggle: function (id, on) {
          if (connState[id] === on) { return; }
          connState[id] = on;
          var tdef = null;
          for (var i = 0; i < CONNECTIONS.length; i++) {
            if (CONNECTIONS[i].id === id) { tdef = CONNECTIONS[i]; }
          }
          if (tdef && tdef.control === "pause") {
            pollCfg.paused = !on;
            savePollCfg();
          }
          if (tdef && tdef.control === "news") { setMaster(on); }
          renderSigAll();
        },
        onRefresh: function (id) {
          if (id === "server") { pollStrip(); }
          else if (id === "newsloop") { fetchNews(true); }
          else { fetchNetHealth(); }
        },
        onCadence: function (id, sec) {
          if (id !== "server") { return; }
          pollCfg.strip = sec;
          savePollCfg();
          restartStripPoll();
          if ((location.hash || "").slice(1) === "network") {
            startNetHealthPoll();
          }
        }
      }
    };
  }

  /* ---- the compact status band, restored (the proven old-room
     port the audit ordered back): label / mono value / subtitle,
     value colored by the SAME thresholds the whole page uses. ---- */
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
      machineVal === "ARMED" || machineVal === "—" ? "" : "warn",
      "STOP flags mirrored · authoritative"));
    tiles.appendChild(tile("Server RTT",
      lastStrip ? (lastRtt == null ? "answering" : lastRtt + " ms")
        : "unreachable",
      lastStrip ? thrCls("server", telNum(lastRtt)) : "bad",
      "direct serve.py probe"));
    tiles.appendChild(tile("Mirror continuity",
      ageText2(pull0.age_seconds),
      thrCls("mirror", telNum(pull0.age_seconds)),
      "pull every " + (pull0.interval_ms
        ? Math.round(pull0.interval_ms / 1000) + "s" : "—")));
    tiles.appendChild(tile("Feed freshness",
      ageText2(mp0.age_seconds),
      thrCls("market", telNum(mp0.age_seconds)),
      "market pass on the host"));
    tiles.appendChild(tile("Supervisor",
      (sv0.reported_status || "—"),
      thrCls("supervisor", telNum(sv0.age_seconds)),
      "heartbeat " + ageText2(sv0.age_seconds) + " ago"));
    var newsAgeTile = (newsData && newsData.as_of)
      ? (Date.now() - Date.parse(newsData.as_of)) / 1000 : null;
    tiles.appendChild(tile("News loop",
      newsAgeTile == null ? "no read yet" : ageText2(newsAgeTile),
      newsAgeTile == null ? "" : thrCls("newsloop", newsAgeTile),
      "104 public sources"));
    return tiles;
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
  var netChartSel = "server";

  function netMetric(key) {
    for (var i = 0; i < NET_METRICS.length; i++) {
      if (NET_METRICS[i].key === key) { return NET_METRICS[i]; }
    }
    return null;
  }

  function fetchTelemetry() {
    fetch("/api/telemetry?range=" + netTelem.range, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        netTelem.data = d;
        if ((location.hash || "").slice(1) === "network") {
          renderTelemetry();
        }
      })
      .catch(function () { netTelem.data = null; });
  }

  function startNetTelemetry() {
    fetchTelemetry();
    if (netTelemTimer) { clearInterval(netTelemTimer); }
    netTelemTimer = setInterval(function () {
      if (!pollCfg.paused) { fetchTelemetry(); }
    }, 30000);
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

  /* cadence estimate: an age series saw a tick land wherever the value
     drops; the value just before each drop ≈ that loop's interval.
     Median of those, sane-clamped. Measured, never configured. */
  function cadenceEst(field) {
    var sv = telemSeries(field)[1];
    var pre = [];
    for (var i = 1; i < sv.length; i++) {
      if (sv[i] != null && sv[i - 1] != null && sv[i] < sv[i - 1] * 0.5) {
        pre.push(sv[i - 1]);
      }
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
    var m = netMetric(compKey);
    if (!m) { return null; }
    var t = thrFor(compKey);
    if (!t) { return null; }
    var vs = telemSeries(m.field)[1];
    var meas = 0;
    var okc = 0;
    for (var i = 0; i < vs.length; i++) {
      if (vs[i] == null) { continue; }
      meas += 1;
      if (vs[i] < t.warn) { okc += 1; }
    }
    if (meas < 5) { return null; }
    return 100 * okc / meas;
  }

  function statusSamples(compKey, maxN) {
    var m = netMetric(compKey);
    if (!m) { return null; }
    var vs = telemSeries(m.field)[1];
    if (!vs.length) { return null; }
    var stride = Math.max(1, Math.ceil(vs.length / maxN));
    var out = [];
    for (var i = 0; i < vs.length; i += stride) {
      out.push(vs[i] == null ? null : thrCls(compKey, vs[i]) || null);
    }
    return out;
  }

  function renderTelemetry() {
    var d = netTelem.data;
    var host = document.getElementById("net-chart");
    var capEl = document.getElementById("net-chart-cap");
    if (host) {
      var m = netMetric(netChartSel) || NET_METRICS[0];
      var series = telemSeries(m.field);
      var thr = thrFor(netChartSel);
      if (!d || !d.present || series[0].length < 2) {
        if (netChart) { netChart.destroy(); netChart = null; }
        host.textContent = "";
        var emptyMsg = document.createElement("div");
        emptyMsg.className = "qnet-note";
        emptyMsg.textContent = !d
          ? "Telemetry endpoint unreachable."
          : "No telemetry history yet — the recorder fills " +
            "ui/live/telemetry.jsonl (python3 ui/telemetry_recorder.py " +
            "--loop 15).";
        host.appendChild(emptyMsg);
      } else if (window.QNet && QNet.chart) {
        if (!netChart) {
          host.textContent = "";
          netChart = QNet.chart.mount(host, {
            points: series,
            unit: m.unit,
            height: 220,
            thresholds: thr || { warn: null, bad: null }
          });
        } else {
          netChart.setData(series, thr || { warn: null, bad: null });
        }
      }
    }
    if (capEl) {
      var n = d && d.present ? (d.count || 0) : 0;
      capEl.textContent = n
        ? "ui/live/telemetry.jsonl · " + n + " samples · " +
          netTelem.range + " window · UTC"
        : "History arrives from the telemetry recorder.";
    }
    if (netTable) { netTable.update(buildTableModel()); }
    /* lamp and rows must repaint from the same threshold state —
       telemetry landing changes measured cadences, so the lamp
       re-evaluates here too, never one poll behind the table */
    updateOverallLamp();
  }

  function updateOverallLamp() {
    var overallEl = document.getElementById("conn-overall");
    if (!overallEl) { return; }
    var worst = connOverallWorst();
    var od = overallEl.querySelector(".livedot");
    var ol = overallEl.querySelector("span:last-child");
    od.className = "livedot" +
      (worst === 2 ? " dead" : (worst === 1 ? " stale" : ""));
    ol.textContent = worst === 2 ? "OUTAGE"
      : (worst === 1 ? "DEGRADED" : "OPERATIONAL");
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
    /* re-sync controllable rows from their single sources of truth */
    connState.server = !pollCfg.paused;
    connState.newsloop = newsMaster;
    var page = document.createElement("div");
    page.className = "conn-page";
    var headRow = document.createElement("div");
    headRow.className = "conn-head-row";
    var t = document.createElement("div");
    t.className = "news-title";
    t.textContent = "Network";
    var mchip = document.createElement("span");
    mchip.className = "machine-chip";
    mchip.id = "net-machine";
    mchip.appendChild(document.createTextNode("MACHINE"));
    var mval = document.createElement("b");
    mval.textContent = "—";
    mchip.appendChild(mval);
    var overall = document.createElement("span");
    overall.className = "conn-overall";
    overall.id = "conn-overall";
    var odot = document.createElement("span");
    odot.className = "livedot";
    var olbl = document.createElement("span");
    olbl.textContent = "CONNECTING";
    overall.appendChild(odot);
    overall.appendChild(olbl);
    headRow.appendChild(t);
    headRow.appendChild(mchip);
    headRow.appendChild(overall);
    var sub = document.createElement("div");
    sub.className = "page-sub";
    sub.textContent =
      "Website · machine mirror · public sources · owner-gated boundaries.";
    page.appendChild(headRow);
    page.appendChild(sub);
    page.appendChild(buildConnTiles());

    /* the ONE chart — real file history, UTC axis, threshold bands */
    var chartCard = document.createElement("div");
    chartCard.className = "qnet-chartcard";
    var tools = document.createElement("div");
    tools.className = "qnet-chart-tools";
    var mwrap = document.createElement("div");
    mwrap.className = "pillbar qnet-chart-pills";
    for (var mi2 = 0; mi2 < NET_METRICS.length; mi2++) {
      (function (m) {
        var pb = document.createElement("button");
        pb.className = "fpill" + (netChartSel === m.key ? " on" : "");
        pb.textContent = m.label;
        pb.addEventListener("click", function () {
          netChartSel = m.key;
          if (netChart) { netChart.destroy(); netChart = null; }
          var pills = mwrap.querySelectorAll(".fpill");
          for (var q = 0; q < pills.length; q++) {
            pills[q].classList.toggle("on",
              pills[q].textContent === m.label);
          }
          renderTelemetry();
        });
        mwrap.appendChild(pb);
      })(NET_METRICS[mi2]);
    }
    var rwrap = document.createElement("div");
    rwrap.className = "pillbar qnet-chart-ranges";
    var rdefs = ["1h", "6h", "24h"];
    for (var ri2 = 0; ri2 < rdefs.length; ri2++) {
      (function (rg) {
        var rb = document.createElement("button");
        rb.className = "fpill" + (netTelem.range === rg ? " on" : "");
        rb.textContent = rg.toUpperCase();
        rb.addEventListener("click", function () {
          netTelem.range = rg;
          var pills = rwrap.querySelectorAll(".fpill");
          for (var q = 0; q < pills.length; q++) {
            pills[q].classList.toggle("on",
              pills[q].textContent === rg.toUpperCase());
          }
          fetchTelemetry();
        });
        rwrap.appendChild(rb);
      })(rdefs[ri2]);
    }
    tools.appendChild(mwrap);
    tools.appendChild(rwrap);
    chartCard.appendChild(tools);
    var chartHost = document.createElement("div");
    chartHost.id = "net-chart";
    chartHost.className = "qnet-chart";
    chartCard.appendChild(chartHost);
    var cap = document.createElement("div");
    cap.id = "net-chart-cap";
    cap.className = "qnet-note";
    cap.textContent = "History arrives from the telemetry recorder.";
    chartCard.appendChild(cap);
    page.appendChild(chartCard);

    var pathHead = document.createElement("div");
    pathHead.className = "conn-head-row";
    pathHead.style.marginTop = "26px";
    var ph1 = document.createElement("div");
    ph1.className = "conn-title";
    ph1.textContent = "Connection path";
    var ph2 = document.createElement("span");
    ph2.className = "conn-overall conn-assessed";
    ph2.textContent = lastStripAt
      ? "Assessed " + Math.max(0, Math.round(
          (Date.now() - lastStripAt) / 1000)) + "s ago"
      : "Connecting";
    var dfl = document.createElement("button");
    dfl.className = "conn-defaults";
    dfl.textContent = "Defaults";
    dfl.addEventListener("click", function () {
      pollCfg.strip = POLL_DEFAULTS.strip;
      pollCfg.news = POLL_DEFAULTS.news;
      savePollCfg();
      restartStripPoll();
      if (newsTimer) { startNewsPoll(); }
      if ((location.hash || "").slice(1) === "network") {
        startNetHealthPoll();
        pageEl.textContent = "";
        buildNetworkPage(pageEl);
      }
    });
    pathHead.appendChild(ph1);
    pathHead.appendChild(ph2);
    pathHead.appendChild(dfl);
    page.appendChild(pathHead);

    var tableEl = document.createElement("div");
    tableEl.className = "qnet-table-host";
    page.appendChild(tableEl);
    var tblNote = document.createElement("div");
    tblNote.className = "qnet-note";
    tblNote.textContent = "Host-side rows are read-only until the signed " +
      "control endpoint lands · the Web server and News loop switches " +
      "act client-side today.";
    page.appendChild(tblNote);
    if (window.QNet && QNet.table) {
      netTable = QNet.table.build(tableEl, buildTableModel());
      netTableHasCadence = !!(lastStrip && lastStrip.pull &&
        lastStrip.pull.interval_ms);
    } else {
      netTable = null;
      var missing = document.createElement("div");
      missing.className = "qnet-note";
      missing.textContent = "Table module not loaded.";
      tableEl.appendChild(missing);
    }
    container.appendChild(page);
    renderSigAll();
    renderTelemetry();
  }

  function renderSigAll() {
    if ((location.hash || "").slice(1) === "network" && sigPage) {
      var oldTiles = document.getElementById("conn-tiles");
      if (oldTiles && oldTiles.parentNode) {
        oldTiles.parentNode.replaceChild(buildConnTiles(), oldTiles);
      }
      if (netTable) {
        /* cadence cells render once at build; if the table was built
           before the first strip landed, rebuild one time so the
           mirror's measured pull interval appears. */
        var ivNow = lastStrip && lastStrip.pull && lastStrip.pull.interval_ms;
        if (!netTableHasCadence && typeof ivNow === "number" && ivNow > 0) {
          netTableHasCadence = true;
          var host = document.querySelector(".qnet-table-host");
          if (host && window.QNet && QNet.table) {
            netTable = QNet.table.build(host, buildTableModel());
          }
        } else {
          netTable.update(buildTableModel());
        }
      }
      var mchipEl = document.getElementById("net-machine");
      if (mchipEl) {
        var mb = mchipEl.querySelector("b");
        var mv = (lastStrip && lastStrip.machine) || "—";
        mb.textContent = mv;
        mb.className = mv === "ARMED" || mv === "—" ? "" : "warn";
      }
      var stamp = document.querySelector(".conn-assessed");
      if (stamp && lastStripAt) {
        stamp.textContent = "Assessed " + Math.max(0, Math.round(
          (Date.now() - lastStripAt) / 1000)) + "s ago";
      }
      updateOverallLamp();
    }
  }

  /* the Network page keeps its live reference for poll refreshes */
  var sigPage = null;
  var netTable = null;
  var netTableHasCadence = false;

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
