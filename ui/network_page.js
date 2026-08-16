(function (root) {
  "use strict";

  var state = null, SVG_NS = "http://www.w3.org/2000/svg";
  var REFRESH_D = "M8 2.5a5.5 5.5 0 1 0 5.35 4.19.75.75 0 0 1 1.46-.35" +
    "A7 7 0 1 1 8 1c1.73 0 3.32.63 4.55 1.67V1.75a.75.75 0 0 1 1.5 0v3.5" +
    "a.75.75 0 0 1-.75.75h-3.5a.75.75 0 0 1 0-1.5h1.72A5.48 5.48 0 0 0 8 2.5Z";
  var HEALTH_RANK = { Operational: 1, Off: 2, Unknown: 3,
    Degraded: 4, Stale: 5, Outage: 6 };
  var PRIMARY_METRIC = {
    browser_site: "fetch_ms", cf_connector: "latest_rtt_ms_max",
    connector_origin: "rtt_ms", spot_rest: "rtt_ms",
    spot_wss: "last_message_age_ms", usdm_rest: "rtt_ms",
    usdm_wss: "last_message_age_ms", feed_machine: "book_age_p50_ms",
    news_fetch: "sources_live"
  };
  var CONTROL_CONFIG = {
    rest: { key: "rest_probe_s", values: [15, 30, 60] },
    wss: { key: "wss_read_s", values: [5, 10, 15] },
    cf: { key: "cf_monitor_s", values: [15, 30, 60] },
    news: { key: "news_monitor_s", values: [120, 300, 600] }
  };

  function finiteNumber(value) { return typeof value === "number" && isFinite(value); }

  function age(ms) {
    if (!finiteNumber(ms)) { return "—"; }
    var seconds = Math.max(0, ms) / 1000, minutes, remaining, hours;
    if (seconds < 10) { return seconds.toFixed(1) + " s"; }
    if (seconds < 60) { return Math.round(seconds) + " s"; }
    minutes = Math.floor(seconds / 60); remaining = Math.round(seconds - minutes * 60);
    if (remaining === 60) { minutes += 1; remaining = 0; }
    if (minutes < 60) { return minutes + " m " + remaining + " s"; }
    hours = Math.floor(minutes / 60);
    return hours + " h " + (minutes - hours * 60) + " m";
  }

  function clock(iso) {
    if (iso === null || iso === undefined || iso === "") { return "—"; }
    var date = new Date(iso); if (!isFinite(date.getTime())) { return "—"; }
    function pad(value) { return value < 10 ? "0" + value : String(value); }
    return pad(date.getUTCHours()) + ":" + pad(date.getUTCMinutes()) +
      ":" + pad(date.getUTCSeconds());
  }

  function rel(ms) {
    if (!finiteNumber(ms)) { return "—"; }
    var shown = age(ms > 100000000000 ? Date.now() - ms : ms);
    return shown === "—" ? shown : shown + " ago";
  }

  function num(value, unit) {
    if (!finiteNumber(value)) { return "—"; }
    var shown = String(Math.round(value * 10) / 10); return unit ? shown + " " + unit : shown;
  }

  var fmt = { age: age, clock: clock, rel: rel, num: num };

  function rank(health) { return Object.prototype.hasOwnProperty.call(HEALTH_RANK, health)
    ? HEALTH_RANK[health] : HEALTH_RANK.Unknown; }

  function el(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined && text !== null) { node.textContent = text; }
    return node;
  }

  function appendText(host, value) {
    var node = document.createTextNode(value); host.appendChild(node); return node;
  }

  function active(view) { return !!view && view.active && state === view; }
  function edges(view) { return view.current && Array.isArray(view.current.edges)
    ? view.current.edges : []; }

  function findEdge(view, edgeId) {
    var list = edges(view), i;
    for (i = 0; i < list.length; i += 1) {
      if (list[i] && list[i].id === edgeId) { return list[i]; }
    } return null;
  }

  function edgeLabel(view, edgeId) {
    if (edgeId === null || edgeId === undefined) { return "Recorder"; }
    var edge = findEdge(view, edgeId); return edge && edge.label ? edge.label : "—";
  }

  function healthClass(health) {
    if (health === "Outage") { return "bad"; }
    if (health === "Degraded" || health === "Stale") { return "warn"; }
    return health === "Unknown" || health === "Off" ? "off" : "";
  }

  function shown(value) { return value === null || value === undefined || value === ""
    ? "—" : String(value); }
  function shortRelease(value) { return value === null || value === undefined || value === ""
    ? "—" : String(value).slice(0, 7); }

  function configValue(current, key, group) {
    var collector = current && current.collector;
    var config = collector && collector.config;
    if (!config || typeof config !== "object") { return null; }
    if (key === "paused") {
      return config.paused && typeof config.paused === "object"
        ? config.paused[group] : null;
    }
    return Object.prototype.hasOwnProperty.call(config, key) ? config[key] : null;
  }

  function isCooldownLine(message) {
    return /^cooldown(?:\s|$)/.test(message) || message === "probe already requested";
  }

  function visibleEdges(view) {
    return edges(view).filter(function (edge) {
      if (!edge) { return false; }
      if (view.filter === "all") { return true; }
      if (view.filter === "unhealthy") { return edge.health !== "Operational"; }
      return edge.required === true; });
  }

  function worstRequiredId(view) {
    var edgeId = null, top = -1;
    edges(view).forEach(function (edge) {
      if (!edge || edge.required !== true || rank(edge.health) <= top) { return; }
      edgeId = edge.id; top = rank(edge.health); });
    return edgeId;
  }

  function refreshButton(onClick) {
    var button = el("button", "st-iconbtn");
    button.type = "button"; button.title = "refresh now";
    button.setAttribute("aria-label", "refresh now");
    var svg = document.createElementNS(SVG_NS, "svg"), path;
    svg.setAttribute("width", "13"); svg.setAttribute("height", "13");
    svg.setAttribute("viewBox", "0 0 16 16"); svg.setAttribute("aria-hidden", "true");
    path = document.createElementNS(SVG_NS, "path");
    path.setAttribute("d", REFRESH_D); path.setAttribute("fill", "currentColor");
    svg.appendChild(path); button.appendChild(svg);
    button.addEventListener("click", onClick); return button;
  }

  function liveSwitch(view) {
    var button = el("button", "switch n1-switch on");
    button.type = "button"; button.setAttribute("role", "switch");
    button.setAttribute("aria-label", "Live view"); button.setAttribute("aria-checked", "true");
    button.appendChild(el("span", "knob"));
    button.addEventListener("click", function () { setLive(view, !view.live); }); return button;
  }

  function segment(items, selected, label, onSelect) {
    var host = el("div", "st-seg"); host.setAttribute("aria-label", label);
    items.forEach(function (item) {
      var button = el("button", item.value === selected ? "on" : "", item.label);
      button.type = "button"; button.setAttribute("data-value", item.value);
      button.setAttribute("aria-pressed", item.value === selected ? "true" : "false");
      button.addEventListener("click", function () { onSelect(item.value); });
      host.appendChild(button); }); return host;
  }

  function patchSegment(host, selected) {
    var buttons = host.querySelectorAll("button");
    for (var i = 0; i < buttons.length; i += 1) {
      var on = buttons[i].getAttribute("data-value") === selected;
      buttons[i].classList.toggle("on", on);
      buttons[i].setAttribute("aria-pressed", on ? "true" : "false"); }
  }

  function summaryParts() {
    var host = el("div", "n1-summary"), refs = { host: host };
    refs.recorder = el("span", "v5-word warn"); refs.recorder.hidden = true; host.appendChild(refs.recorder); refs.recorderSep = appendText(host, "");
    refs.operational = el("span", "st-t-datum", "—"); host.appendChild(refs.operational); appendText(host, " / "); refs.required = el("span", "st-t-datum", "—");
    host.appendChild(refs.required); appendText(host, " required paths operational · "); refs.unknown = el("span", "st-t-datum", "—"); host.appendChild(refs.unknown);
    appendText(host, " unknown · worst "); refs.worstLabel = el("span", "", "—"); host.appendChild(refs.worstLabel); appendText(host, " — ");
    refs.worstHealth = el("span", "v5-word off", "—"); host.appendChild(refs.worstHealth); appendText(host, " · oldest evidence ");
    refs.oldest = el("span", "st-t-datum", "—"); host.appendChild(refs.oldest); appendText(host, " · last change "); refs.changeClock = el("span", "st-t-datum", "—");
    host.appendChild(refs.changeClock); appendText(host, " "); refs.changeLabel = el("span", "", "—"); host.appendChild(refs.changeLabel); appendText(host, " ");
    refs.transition = el("span", "", "—"); host.appendChild(refs.transition); return refs;
  }

  function patchSummary(view) {
    var current = view.current, summary = current && current.summary, collector = current && current.collector;
    var status = collector && typeof collector.status === "string"
      ? collector.status : null;
    var recorderWarn = !!current && (status === "stopped" || status === "unknown");
    view.summary.recorder.hidden = !recorderWarn; view.summary.recorder.textContent = recorderWarn ? "Recorder " + status : "";
    view.summary.recorderSep.nodeValue = recorderWarn ? " · " : "";
    view.summary.operational.textContent = summary ? shown(summary.operational_required) : "—"; view.summary.required.textContent = summary ? shown(summary.required_total) : "—";
    view.summary.unknown.textContent = summary ? shown(summary.unknown_count) : "—";
    var worst = summary && summary.worst, health = worst && worst.health || "—";
    view.summary.worstLabel.textContent = worst ? edgeLabel(view, worst.edge_id) : "—"; var className = healthClass(health);
    view.summary.worstHealth.className = "v5-word" + (className ? " " + className : ""); view.summary.worstHealth.textContent = health;
    var oldest = summary && summary.oldest_required_evidence_at
      ? new Date(summary.oldest_required_evidence_at).getTime() : NaN;
    view.summary.oldest.textContent = isFinite(oldest) ? fmt.age(Date.now() - oldest) : "—";
    var change = summary && summary.last_transition;
    view.summary.changeClock.textContent = change ? fmt.clock(change.ts) : "—"; view.summary.changeLabel.textContent = change ? edgeLabel(view, change.edge_id) : "—";
    view.summary.transition.textContent = change ? shown(change.transition) : "—";
  }

  function patchFoot(view) {
    var current = view.current, collector = current && current.collector;
    var generated = current && current.generated_at
      ? String(current.generated_at).replace(/Z$/, "") : "—";
    view.foot.textContent = "recorder " +
      shortRelease(collector && collector.recorder_release) + " · ui " +
      shortRelease(current && current.deployed_ui_release) + " · collector " +
      shown(collector && collector.status) + " · " + generated + " UTC";
  }

  function patchError(view) {
    if (!view.hasError) { view.errorLine.hidden = true; view.errorLine.textContent = ""; return; }
    var elapsed = finiteNumber(view.lastAnswerAt) ? Date.now() - view.lastAnswerAt : null;
    view.errorLine.textContent =
      "Server unreachable — showing the last answer (" + fmt.age(elapsed) + ")";
    view.errorLine.hidden = false;
  }

  /* The browser → site wire is session evidence: the page measured it
     itself, so a fresh measurement is Operational here even though the
     recorder (rightly) stores nothing for it. */
  function sessionEdge(edge, view) {
    if (!edge || edge.id !== "browser_site") { return edge; }
    var copy = Object.assign({}, edge);
    copy.metrics = Object.assign({}, edge.metrics || {});
    copy.metrics.fetch_ms = view.sessionFetchMs;
    var fresh = typeof view.sessionFetchMs === "number" && isFinite(view.sessionFetchMs) &&
      typeof view.sessionMeasuredAt === "number" && (Date.now() - view.sessionMeasuredAt) < 90000;
    if (fresh) {
      copy.health = "Operational";
      copy.health_reason = "browser fetched /api/network in " + Math.round(view.sessionFetchMs) + " ms (this session)";
      copy.truth = { configured: "Pass", transport: "Pass", application: "Pass" };
      copy.observed_at = new Date(view.sessionMeasuredAt).toISOString();
      copy.observed_ms = view.sessionMeasuredAt;
      copy.evidence = { source: "browser session (this page)", source_age_s: (Date.now() - view.sessionMeasuredAt) / 1000 };
    }
    return copy;
  }

  function decorateCurrent(view) {
    if (!view.current) { return null; }
    var current = Object.assign({}, view.current);
    current.sessionFetchMs = view.sessionFetchMs;
    if (Array.isArray(current.edges)) {
      current.edges = current.edges.map(function (e) { return sessionEdge(e, view); });
    }
    return current;
  }

  function tableModel(view) { return { current: decorateCurrent(view),
    historyByEdge: view.historyByEdge, range: view.range, filter: view.filter,
    selectedId: view.selectedId, nowMs: Date.now(), sessionFetchMs: view.sessionFetchMs }; }

  function stripCurrent(view) { return decorateCurrent(view); }

  function drawerEdge(view) {
    return sessionEdge(findEdge(view, view.selectedId), view);
  }

  function drawerOpts(view) {
    return { range: view.range, metric: view.metric,
      events: view.current && Array.isArray(view.current.events) ? view.current.events : [],
      onMetric: function (name) {
        if (!active(view) || typeof name !== "string") { return; }
        view.metric = name; view.drawerBuckets = [];
        updateDrawer(view); fetchDrawerHistory(view);
      },
      onClose: function () { selectFor(view, null); } };
  }

  function safeUpdate(instance, args) {
    if (!instance || typeof instance.update !== "function") { return; }
    try { instance.update.apply(instance, args); } catch (ignored) {}
  }

  function updateStrip(view) {
    safeUpdate(view.components.strip, [stripCurrent(view), view.selectedId]); }
  function updateTable(view) { safeUpdate(view.components.table, [tableModel(view)]); }
  function updateDrawer(view) {
    var edge = drawerEdge(view); view.hosts.drawer.hidden = !edge;
    safeUpdate(view.components.drawer, [edge, view.drawerBuckets, drawerOpts(view)]);
  }
  function updateLedger(view) { var events = view.current && Array.isArray(view.current.events)
    ? view.current.events : []; safeUpdate(view.components.ledger, [events]); }
  function updateComponents(view) {
    updateStrip(view); updateTable(view); updateDrawer(view); updateLedger(view); }

  function setReceipt(view, token, text, warn) {
    if (!active(view) || token !== view.controlToken) { return; }
    view.receipt.className = "n1-receipt" + (warn ? " warn" : "");
    view.receipt.textContent = text; view.receipt.hidden = false;
  }

  function controlReceipt(view, control, key, answer) {
    var message, saved, savedKey;
    if (!answer || answer.ok !== true) {
      message = answer && typeof answer.error === "string" && answer.error
        ? answer.error : "Control request did not return an answer";
      return { text: message, warn: !isCooldownLine(message), config: false,
        success: false, key: key, group: control.group };
    }
    saved = answer.saved;
    if (!saved || typeof saved !== "object") {
      return { text: "Receipt unavailable", warn: true, config: false,
        success: false, key: key, group: control.group };
    }
    if (control.type === "probe") {
      return { text: "probe requested · " + edgeLabel(view, saved.edge_id || control.edgeId) +
        " · request " + shown(saved.request_id), warn: false, config: false,
        success: true, key: null, group: control.group };
    }
    savedKey = saved.key || key;
    if (saved.unchanged === true) {
      return { text: "unchanged · " + shown(savedKey) + " " + shown(saved.new),
        warn: false, config: true, success: true, key: savedKey,
        group: saved.group || control.group };
    }
    return { text: "saved · " + shown(savedKey) + " " + shown(saved.old) +
      " → " + shown(saved.new) + " · receipt #" + shown(saved.receipt_id) +
      " · reread " + shown(saved.reread), warn: false, config: true,
      success: true, key: savedKey, group: saved.group || control.group };
  }

  function onControl(view, control) {
    if (!active(view)) { return Promise.resolve(null); }
    var data = root.NetworkData, group = control && CONTROL_CONFIG[control.group];
    var key = null, request = null, token = view.controlToken + 1;
    view.controlToken = token;
    if (!control || typeof control.type !== "string") {
      request = Promise.resolve({ ok: false, error: "Control request is incomplete" });
    } else if (control.type === "every") {
      key = group && group.key;
      if (!group || group.values.indexOf(control.value) === -1) {
        request = Promise.resolve({ ok: false, error: "Monitor interval is unavailable" });
      } else if (!data || typeof data.postConfig !== "function") {
        request = Promise.resolve({ ok: false, error: "Control endpoint unavailable" });
      } else { request = data.postConfig(key, control.value, control.group); }
    } else if (control.type === "pause") {
      key = "paused";
      if (!group || typeof control.value !== "boolean") {
        request = Promise.resolve({ ok: false, error: "Monitor state is unavailable" });
      } else if (!data || typeof data.postConfig !== "function") {
        request = Promise.resolve({ ok: false, error: "Control endpoint unavailable" });
      } else { request = data.postConfig("paused", !control.value, control.group); }
    } else if (control.type === "probe") {
      if (!data || typeof data.postProbe !== "function") {
        request = Promise.resolve({ ok: false, error: "Probe endpoint unavailable" });
      } else { request = data.postProbe(control.edgeId); }
    } else {
      request = Promise.resolve({ ok: false, error: "Control type is unavailable" });
    }
    return Promise.resolve(request).catch(function (error) {
      return { ok: false, error: error && error.message
        ? error.message : "Control request did not return an answer" };
    }).then(function (answer) {
      if (!active(view)) { return null; }
      var receipt = controlReceipt(view, control || {}, key, answer);
      setReceipt(view, token, receipt.text, receipt.warn);
      if (!data || typeof data.fetchCurrent !== "function") {
        updateTable(view);
        if (receipt.config && receipt.success) {
          setReceipt(view, token, receipt.text + " · server now —", receipt.warn);
        }
        return answer;
      }
      return data.fetchCurrent().then(function (current) {
        if (!active(view)) { return current; }
        if (current && typeof current === "object") {
          view.current = current; view.lastAnswerAt = Date.now(); view.hasError = false;
          patchError(view);
        }
        updateTable(view);
        if (receipt.config && receipt.success) {
          /* The file is saved and re-read at once; the recorder picks the
             new value up on its next tick (within 5 s) — say which is which. */
          setReceipt(view, token, receipt.text + " · recorder still on " +
            shown(configValue(current, receipt.key, receipt.group)) + " until its next tick", receipt.warn);
          view.timers.push(setTimeout(function () {
            if (!active(view) || view.controlToken !== token) { return; }
            data.fetchCurrent().then(function (later) {
              if (!active(view) || view.controlToken !== token) { return; }
              if (later && typeof later === "object") { view.current = later; updateTable(view); }
              setReceipt(view, token, receipt.text + " · recorder now " +
                shown(configValue(later, receipt.key, receipt.group)), receipt.warn);
            });
          }, 7000));
        }
        return current;
      });
    });
  }

  function missing(host, className) {
    host.className = className; host.textContent = "";
    host.appendChild(el("div", "v5-note", "component not loaded")); return null;
  }

  function mount(host, component, args, className) {
    if (!component || typeof component.render !== "function") { return missing(host, className); }
    try { return component.render.apply(component, args) || null; }
    catch (ignored) { return missing(host, className); }
  }

  function mountComponents(view) {
    view.components.strip = mount(view.hosts.strip, root.NetV1Strip,
      [view.hosts.strip, stripCurrent(view), view.selectedId,
        function (id) { selectFor(view, id); }], "st-card n1-strip");
    view.components.table = mount(view.hosts.table, root.NetV1Table,
      [view.hosts.table, tableModel(view),
        { onSelect: function (id) { selectFor(view, id); },
          onControl: function (control) { return onControl(view, control); } }],
      "st-card n1-table");
    view.components.drawer = mount(view.hosts.drawer, root.NetV1Drawer,
      [view.hosts.drawer, null, [], drawerOpts(view)], "st-card n1-drawer");
    view.components.ledger = mount(view.hosts.ledger, root.NetV1Ledger,
      [view.hosts.ledger, [],
        { onSelect: function (id) { selectFor(view, id); } }], "st-card n1-ledger");
  }

  function fetchVisibleHistory(view) {
    var data = root.NetworkData;
    if (!active(view) || !data || typeof data.fetchHistory !== "function") { return; }
    var range = view.range;
    visibleEdges(view).forEach(function (edge) {
      data.fetchHistory(edge.id, "status", range).then(function (result) {
        if (!active(view) || range !== view.range || !result ||
            (result.range && result.range !== range)) { return; }
        view.historyByEdge[edge.id] = Array.isArray(result.buckets) ? result.buckets : [];
        updateTable(view); }); });
  }

  function fetchDrawerHistory(view) {
    var data = root.NetworkData, edge = findEdge(view, view.selectedId);
    if (!active(view) || !data || typeof data.fetchHistory !== "function" ||
        !edge || !view.metric) { return; }
    var edgeId = edge.id, metric = view.metric, range = view.range;
    data.fetchHistory(edgeId, metric, range).then(function (result) {
      if (!active(view) || view.selectedId !== edgeId || view.metric !== metric ||
          view.range !== range || !result || (result.range && result.range !== range))
        { return; }
      view.drawerBuckets = Array.isArray(result.buckets) ? result.buckets : [];
      updateDrawer(view); });
  }

  function selectFor(view, edgeId) {
    if (!active(view)) { return; }
    var next = edgeId === null || edgeId === undefined ? null : String(edgeId);
    if (next && !findEdge(view, next)) { return; }
    if (view.selectedId !== next) {
      view.selectedId = next; view.metric = next ? PRIMARY_METRIC[next] || null : null;
      view.drawerBuckets = [];
    }
    updateStrip(view); updateTable(view); updateDrawer(view);
    if (next) { fetchDrawerHistory(view); }
  }

  function select(edgeId) { if (state) { selectFor(state, edgeId); } }

  function applyCurrent(view, current) {
    if (!active(view) || !current || typeof current !== "object") { return; }
    view.current = current; view.lastAnswerAt = Date.now(); view.hasError = false;
    if (!view.didDefaultSelect) {
      view.didDefaultSelect = true; view.selectedId = worstRequiredId(view);
      view.metric = view.selectedId ? PRIMARY_METRIC[view.selectedId] || null : null;
      view.drawerBuckets = [];
    } else if (view.selectedId && !findEdge(view, view.selectedId)) {
      view.selectedId = null; view.metric = null; view.drawerBuckets = [];
    }
    patchSummary(view); patchFoot(view); patchError(view);
    updateComponents(view); fetchVisibleHistory(view); fetchDrawerHistory(view);
  }

  function setFilter(view, value) {
    if (!active(view) || view.filter === value) { return; }
    view.filter = value; patchSegment(view.filterSegment, value);
    updateTable(view); fetchVisibleHistory(view);
  }

  function setRange(view, value) {
    if (!active(view) || view.range === value) { return; }
    view.range = value; view.historyByEdge = Object.create(null);
    view.drawerBuckets = []; patchSegment(view.rangeSegment, value);
    updateTable(view); updateDrawer(view); fetchVisibleHistory(view); fetchDrawerHistory(view);
  }

  function setLive(view, on) {
    if (!active(view)) { return; }
    view.live = !!on; view.liveSwitch.classList.toggle("on", view.live);
    view.liveSwitch.setAttribute("aria-checked", view.live ? "true" : "false");
    view.page.classList.toggle("paused", !view.live); var data = root.NetworkData;
    if (!data) { return; }
    if (view.live && typeof data.resume === "function") { data.resume(); }
    if (!view.live && typeof data.pause === "function") { data.pause(); }
  }

  function refreshNow(view) {
    var data = root.NetworkData; if (!active(view) || !data) { return; }
    fetchVisibleHistory(view); fetchDrawerHistory(view);
    if (typeof data.fetchCurrent === "function") {
      data.fetchCurrent().then(function (current) { applyCurrent(view, current); });
    }
  }

  function measureSession(view) {
    var data = root.NetworkData; if (!active(view) || !view.live || !data ||
        typeof data.measureFetchMs !== "function") { return; }
    data.measureFetchMs().then(function (value) {
      if (!active(view) || !finiteNumber(value)) { return; }
      view.sessionFetchMs = value; view.sessionMeasuredAt = Date.now(); view.hasError = false;
      patchError(view); updateStrip(view); updateTable(view); updateDrawer(view);
    });
  }

  function build(container) {
    destroy(); if (!container || typeof container.appendChild !== "function") { return null; }
    container.textContent = "";
    var view = {
      active: true, container: container, current: null,
      filter: "required", range: "1h", refreshMs: 5000,
      selectedId: null, metric: null, live: true, didDefaultSelect: false,
      historyByEdge: Object.create(null), drawerBuckets: [],
      sessionFetchMs: null, sessionMeasuredAt: null, lastAnswerAt: null, hasError: false,
      controlToken: 0, components: {}, hosts: {}, unsubscribers: [], timers: []
    };
    state = view; view.page = el("div", "st-page");
    var head = el("div", "st-head");
    head.appendChild(el("div", "st-t-title", "Network")); head.appendChild(el("span", "st-spacer"));
    head.appendChild(refreshButton(function () { refreshNow(view); }));
    var master = el("span", "st-master"); master.appendChild(el("span", "st-t-mid", "Live view"));
    view.liveSwitch = liveSwitch(view); master.appendChild(view.liveSwitch);
    head.appendChild(master); view.page.appendChild(head);
    view.page.appendChild(el("div", "n1-sub",
      "9 paths measured on the box · every 15 s · UTC"));

    view.summary = summaryParts(); view.page.appendChild(view.summary.host);
    view.errorLine = el("div", "v5-note warn"); view.errorLine.hidden = true;
    view.page.appendChild(view.errorLine);
    view.hosts.strip = el("div"); view.page.appendChild(view.hosts.strip);

    var filters = el("div", "n1-filters");
    view.filterSegment = segment([
      { value: "required", label: "Required" },
      { value: "unhealthy", label: "Unhealthy" },
      { value: "all", label: "All" }
    ], view.filter, "Connection filter", function (value) { setFilter(view, value); });
    filters.appendChild(view.filterSegment); filters.appendChild(el("span", "st-spacer"));
    view.rangeSegment = segment([
      { value: "1h", label: "1H" }, { value: "6h", label: "6H" },
      { value: "24h", label: "24H" }
    ], view.range, "History range", function (value) { setRange(view, value); });
    filters.appendChild(view.rangeSegment); filters.appendChild(el("span", "n1-refresh", "Refresh"));
    var refreshSelect = el("select", "st-select"); refreshSelect.setAttribute("aria-label", "Refresh interval");
    [2, 5, 10, 30].forEach(function (seconds) {
      var option = el("option", "", seconds + " s");
      option.value = String(seconds * 1000); option.selected = seconds === 5;
      refreshSelect.appendChild(option);
    });
    refreshSelect.addEventListener("change", function () {
      var next = Number(refreshSelect.value), data = root.NetworkData;
      if (data && typeof data.setInterval === "function" && data.setInterval(next))
        { view.refreshMs = next; }
      else { refreshSelect.value = String(view.refreshMs); }
    });
    filters.appendChild(refreshSelect); view.page.appendChild(filters);

    view.hosts.table = el("div", "st-card n1-table");
    view.hosts.drawer = el("div"); view.hosts.drawer.hidden = true;
    view.hosts.ledger = el("div", "st-card n1-ledger");
    view.receipt = el("div", "n1-receipt"); view.receipt.hidden = true;
    view.receipt.setAttribute("role", "status"); view.receipt.setAttribute("aria-live", "polite");
    view.page.appendChild(view.hosts.table); view.page.appendChild(view.receipt);
    view.page.appendChild(view.hosts.drawer);
    view.page.appendChild(view.hosts.ledger);
    view.foot = el("div", "n1-foot", "recorder — · ui — · collector — · — UTC");
    view.page.appendChild(view.foot); container.appendChild(view.page);
    mountComponents(view); patchSummary(view);

    var data = root.NetworkData;
    if (data && typeof data.subscribe === "function") {
      view.unsubscribers.push(data.subscribe("error", function () {
        if (!active(view)) { return; } view.hasError = true; patchError(view);
      }));
    }
    if (data && typeof data.fetchCurrent === "function")
      { data.fetchCurrent().then(function (current) { applyCurrent(view, current); }); }
    if (data && typeof data.startPolling === "function") {
      data.startPolling(view.refreshMs, function (current) { applyCurrent(view, current); });
    } else { view.hasError = true; patchError(view); }

    measureSession(view); view.timers.push(root.setInterval(function () {
      measureSession(view); }, 60000));
    view.timers.push(root.setInterval(function () {
      if (!active(view)) { return; }
      if (!document.body.contains(view.container)) { destroy(); return; }
      patchSummary(view); patchError(view);
    }, 1000));
    return view.page;
  }

  function destroy() {
    var view = state; if (!view) { return; }
    view.active = false; var data = root.NetworkData;
    if (data && typeof data.stopPolling === "function") { data.stopPolling(); }
    view.timers.forEach(function (timer) { root.clearInterval(timer); });
    view.unsubscribers.forEach(function (unsubscribe) {
      if (typeof unsubscribe === "function") { unsubscribe(); } });
    Object.keys(view.components).forEach(function (key) {
      var component = view.components[key];
      if (component && typeof component.destroy === "function")
        { try { component.destroy(); } catch (ignored) {} }
    });
    state = null;
  }

  root.NetV1 = { build: build, destroy: destroy, select: select,
    rank: rank, fmt: fmt };
}(window));
