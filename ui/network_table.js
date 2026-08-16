(function (root) {
  "use strict";

  var DASH = "\u2014";
  var HISTORY_HEIGHT = 22;
  var HISTORY_GAP = 2;
  var RANGE = {
    "1h": { cells: 60, bucketMs: 60000 },
    "6h": { cells: 72, bucketMs: 300000 },
    "24h": { cells: 96, bucketMs: 900000 }
  };
  var RANK = { Operational: 1, Off: 2, Unknown: 3,
    Degraded: 4, Stale: 5, Outage: 6 };
  var STATUS_CLASS = { Operational: "", Degraded: "warn", Stale: "warn",
    Outage: "bad", Unknown: "off", Off: "off" };
  var EMPTY_BUCKETS = [];
  var EDGES = [
    ["browser_site", "Browser \u2192 site", "UI requests through Cloudflare Access"],
    ["cf_connector", "Cloudflare connector", "tunnel connections and proxied requests"],
    ["connector_origin", "Connector \u2192 origin", "proxied requests to the loopback UI"],
    ["spot_rest", "Spot REST", "public time probe"], ["spot_wss", "Spot WSS", "book ticker + kline streams"],
    ["usdm_rest", "USD-M REST", "public time probe"], ["usdm_wss", "USD-M WSS", "book ticker + kline streams"],
    ["feed_machine", "Feed \u2192 Machine", "published books into the stream stage"],
    ["news_fetch", "News fetch", "public sources \u2192 news fetcher"],
    ["binance_account", "Binance account reads", "signed account reads (on demand)"],
    ["binance_exec", "Binance execution", "orders and job receipts (on demand)"],
    ["aws_secrets", "AWS Secrets Manager", "credential reads (on demand)"], ["aws_ssm", "AWS SSM", "operator sessions (on demand)"]
  ];
  var EDGE_INFO = Object.create(null);
  EDGES.forEach(function (item, index) {
    EDGE_INFO[item[0]] = { label: item[1], detail: item[2], order: index };
  });
  var CONTROL_GROUPS = {
    rest: { label: "REST", key: "rest_probe_s", values: [15, 30, 60],
      edges: ["spot_rest", "usdm_rest"] },
    wss: { label: "WSS", key: "wss_read_s", values: [5, 10, 15],
      edges: ["spot_wss", "usdm_wss", "feed_machine"] },
    cf: { label: "Cloudflare", key: "cf_monitor_s", values: [15, 30, 60],
      edges: ["cf_connector", "connector_origin"] },
    news: { label: "News", key: "news_monitor_s", values: [120, 300, 600],
      edges: ["news_fetch"] }
  };

  function element(tag, className, text) {
    var node = root.document.createElement(tag);
    if (className) { node.className = className; } if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function isNumber(value) { return typeof value === "number" && isFinite(value); }
  function isMissing(value) { return value === null || value === undefined || value === ""; }

  function sharedFormat(names, value) {
    var fmt = root.NetV1 && root.NetV1.fmt, index, fn, result;
    if (!fmt) { return null; }
    for (index = 0; index < names.length; index += 1) {
      fn = fmt[names[index]]; if (typeof fn !== "function") { continue; }
      try { result = fn(value); } catch (ignored) { result = null; }
      if (result !== null && result !== undefined && result !== "") { return String(result); }
    }
    return null;
  }

  function formatNumber(value) {
    if (!isNumber(value)) { return DASH; }
    var shared = sharedFormat(["number", "num"], value);
    return shared !== null ? shared : String(Math.round(value * 10) / 10);
  }

  function formatAgeMs(value) {
    if (!isNumber(value) || value < 0) { return DASH; }
    var shared = sharedFormat(["ageMs", "age"], value); if (shared !== null) { return shared; }
    var seconds = value / 1000, minutes, remainder;
    if (seconds < 10) { return (Math.round(seconds * 10) / 10).toFixed(1) + " s"; }
    if (seconds < 60) { return Math.round(seconds) + " s"; }
    minutes = Math.floor(seconds / 60); remainder = Math.round(seconds - minutes * 60);
    if (remainder === 60) { minutes += 1; remainder = 0; }
    return minutes + " m " + remainder + " s";
  }

  function formatRelativeMs(value) {
    if (!isNumber(value) || value < 0) { return DASH; }
    var shared = sharedFormat(["rel", "relative"], value);
    return shared !== null ? shared : formatAgeMs(value) + " ago";
  }

  function metric(edge, key) {
    return edge && edge.metrics && Object.prototype.hasOwnProperty.call(edge.metrics, key) ? edge.metrics[key] : null;
  }

  function withUnit(value, unit) {
    var shown = formatNumber(value); return shown === DASH ? DASH : shown + " " + unit;
  }

  function primaryReading(edge, model) {
    var id = edge.id, first, second, age;
    if (id === "browser_site") { return withUnit(model.sessionFetchMs, "ms"); }
    if (id === "cf_connector") {
      first = metric(edge, "ha_connections"); second = metric(edge, "latest_rtt_ms_max");
      if (!isNumber(first) && !isNumber(second)) { return DASH; }
      return formatNumber(first) + "/4 conns \u00b7 " + withUnit(second, "ms");
    }
    if (id === "connector_origin" || id === "spot_rest" || id === "usdm_rest")
      { return withUnit(metric(edge, "rtt_ms"), "ms"); }
    if (id === "spot_wss" || id === "usdm_wss") {
      age = formatAgeMs(metric(edge, "last_message_age_ms")); return age === DASH ? DASH : age + " ago";
    }
    if (id === "feed_machine") {
      age = formatAgeMs(metric(edge, "book_age_p50_ms")); return age === DASH ? DASH : "books " + age + " old";
    }
    if (id === "news_fetch") {
      first = metric(edge, "sources_live"); second = metric(edge, "sources_available");
      if (!isNumber(first) && !isNumber(second)) { return DASH; }
      return formatNumber(first) + " / " + formatNumber(second) + " live";
    }
    if (id === "binance_exec") {
      first = metric(edge, "last_op"); second = metric(edge, "last_outcome");
      if (isMissing(first) && isMissing(second)) { return DASH; }
      return (isMissing(first) ? DASH : String(first)) + " " +
        (isMissing(second) ? DASH : String(second));
    }
    return DASH;
  }

  function headroom(edge) {
    if (edge.id !== "spot_rest" && edge.id !== "usdm_rest") { return DASH; }
    var used = metric(edge, "used_weight_1m"), limit = metric(edge, "weight_limit_1m");
    if (!isNumber(used) && !isNumber(limit)) { return DASH; }
    return formatNumber(used) + " / " + formatNumber(limit);
  }

  function timeMs(value) {
    if (isNumber(value)) { return value; }
    if (typeof value !== "string" || !value) { return null; }
    var parsed = Date.parse(value); return isFinite(parsed) ? parsed : null;
  }

  function nowMs(model) {
    var current = model.current || {};
    if (isNumber(model.nowMs)) { return model.nowMs; }
    return isNumber(current.generated_ms) ? current.generated_ms : Date.now();
  }

  function healthWord(edge) { var value = edge && (edge.health || edge.status);
    return Object.prototype.hasOwnProperty.call(RANK, value) ? value : "Unknown"; }

  function newestEvents(current) {
    var result = new Map(), events = current && Array.isArray(current.events) ? current.events : [];
    events.forEach(function (event, index) {
      var id, stamp, prior, eventId;
      if (!event || event.edge_id === null || event.edge_id === undefined) { return; }
      id = String(event.edge_id); stamp = timeMs(event.ts);
      eventId = isNumber(event.event_id) ? event.event_id : -index; prior = result.get(id);
      if (!prior || (stamp !== null && (prior.stamp === null || stamp > prior.stamp)) ||
          (stamp === prior.stamp && eventId > prior.eventId))
        { result.set(id, { stamp: stamp, eventId: eventId }); }
    });
    return result;
  }

  function historyBuckets(value) { return Array.isArray(value) ? value : EMPTY_BUCKETS; }

  function historyGrid(value, rangeName, fallbackNow, carryMs) {
    var spec = RANGE[rangeName] || RANGE["1h"];
    var buckets = historyBuckets(value), step = spec.bucketMs, end = fallbackNow;
    var start = Math.floor(end / step) * step - (spec.cells - 1) * step;
    var slots = new Array(spec.cells), index, lastFilled = -1;
    buckets.forEach(function (bucket) {
      var slot, prior;
      if (!bucket || !isNumber(bucket.t)) { return; }
      slot = Math.round((bucket.t - start) / step); if (slot < 0 || slot >= spec.cells) { return; }
      prior = slots[slot];
      if (!prior || RANK[healthWord(bucket)] > RANK[healthWord(prior)])
        { slots[slot] = bucket; }
    });
    /* A wire sampled less often than the cell width (news every 120 s
       against 60 s cells) keeps its last known state for up to two of its
       own cadences; such cells are marked carried so the tooltip says so.
       Nothing is carried past the newest real sample's window. */
    if (isNumber(carryMs) && carryMs > step) {
      for (index = 0; index < spec.cells; index += 1) {
        if (slots[index]) { lastFilled = index; continue; }
        if (lastFilled >= 0 && (index - lastFilled) * step <= carryMs) {
          slots[index] = Object.assign({}, slots[lastFilled], { carried: true, t: start + index * step });
        }
      }
    }
    return { slots: slots, count: spec.cells, step: step, start: start, end: end };
  }

  function cssColor(canvas, name, fallback) {
    if (typeof root.getComputedStyle !== "function") { return fallback; }
    var styles = root.getComputedStyle(canvas), value = styles && styles.getPropertyValue(name).trim();
    return value || fallback;
  }

  function cellPaint(canvas, status) {
    if (status === "Operational") { return { color: "#57b47a", alpha: 0.32 }; }
    if (status === "Degraded") { return { color: "#d4a406", alpha: 1 }; }
    if (status === "Stale") { return { color: "#d4a406", alpha: 0.55 }; }
    if (status === "Outage") { return { color: "#cc2f3c", alpha: 1 }; }
    if (status === "Unknown" || status === "Off") { return { color: "#9aa0a6", alpha: 0.35 }; }
    return { color: cssColor(canvas, "--hair",
      cssColor(canvas, "--st-line", "#e5e7eb")), alpha: 0.5 };
  }

  function canvasWidth(canvas) { var rect = canvas.getBoundingClientRect();
    return Math.max(0, Math.round(rect.width || canvas.clientWidth || 0)); }

  function formatHHMM(value) {
    var date = new Date(value);
    if (!isFinite(date.getTime())) { return DASH; }
    return String(date.getUTCHours()).padStart(2, "0") + ":" + String(date.getUTCMinutes()).padStart(2, "0");
  }

  function controlTitle(groupName) {
    var group = CONTROL_GROUPS[groupName];
    if (!group) { return ""; }
    return "applies to " + group.label + ": " + group.edges.map(function (edgeId) {
      return EDGE_INFO[edgeId] ? EDGE_INFO[edgeId].label : edgeId;
    }).join(", ");
  }

  function collectorConfig(model) {
    var current = model && model.current, collector = current && current.collector;
    return collector && collector.config && typeof collector.config === "object"
      ? collector.config : null;
  }

  function render(host, model, opts) {
    if (!host || typeof host.appendChild !== "function")
      { throw new TypeError("NetV1Table.render requires a host element"); }
    model = model || {}; opts = opts || {};
    var ownsCard = !(host.classList && host.classList.contains("n1-table"));
    var card = ownsCard ? element("section", "st-card n1-table") : host;
    var table = element("table", "v5-table n1-grid");
    var head = element("thead"), headRow = element("tr"), body = element("tbody");
    var axis = element("div", "n1-axis"), axisLabels = [], rows = new Map();
    var tip = element("div", "n1-tip"), activeModel = model, destroyed = false;
    var onSelect = typeof opts.onSelect === "function" ? opts.onSelect : null;
    var onControl = typeof opts.onControl === "function" ? opts.onControl : null;

    ["Connection", "Status", "Reading", "Fresh", "Headroom", "Last change", "History", "CONTROL"]
      .forEach(function (label, index) {
        var th = element("th", index === 1 ? "c-status" : "", label);
        th.scope = "col"; headRow.appendChild(th);
      });
    head.appendChild(headRow); table.appendChild(head); table.appendChild(body);
    [0, 1, 2, 3].forEach(function () {
      var label = element("span", "", DASH); axisLabels.push(label); axis.appendChild(label);
    });
    tip.hidden = true; tip.setAttribute("role", "tooltip");
    axis.setAttribute("aria-label", "History timeline UTC");
    card.appendChild(table); card.appendChild(axis); card.appendChild(tip);
    if (ownsCard) { host.appendChild(card); }

    function hideTip() { tip.hidden = true; }
    function positionTip(event) { tip.style.left = event.clientX + 12 + "px";
      tip.style.top = event.clientY + 12 + "px"; }

    function showTip(record, event) {
      var rect = record.canvas.getBoundingClientRect(), drawing = record.drawing;
      var x, slot, bucket, start;
      if (!drawing || !rect.width) { hideTip(); return; }
      x = event.clientX - rect.left; slot = Math.floor((x - drawing.offset) / drawing.slotWidth);
      if (x < drawing.offset || slot < 0 || slot >= drawing.grid.count) { hideTip(); return; }
      bucket = drawing.grid.slots[slot]; if (!bucket) { tip.textContent = "no data"; }
      else {
        start = bucket.t; tip.textContent = formatHHMM(start) + "\u2013" +
          formatHHMM(start + drawing.grid.step) + " UTC \u00b7 " +
          healthWord(bucket) + " \u00b7 " + (bucket.carried ? "carried from the last sample" :
          (isNumber(bucket.n) ? String(bucket.n) : DASH) + " samples");
      }
      tip.hidden = false; positionTip(event);
    }

    function drawHistory(record, force) {
      var width = canvasWidth(record.canvas), ratio = Math.max(1, root.devicePixelRatio || 1);
      var source = record.historyValue, buckets = historyBuckets(source), grid;
      var slotWidth, markWidth, offset, context, index, paint;
      if (!force && buckets === record.bucketRef && width === record.drawWidth &&
          record.drawRange === activeModel.range && ratio === record.drawRatio) { return; }
      grid = historyGrid(source, activeModel.range, nowMs(activeModel),
        record.edge && isNumber(record.edge.cadence_ms) ? record.edge.cadence_ms * 2 : 0);
      record.bucketRef = buckets; record.drawWidth = width;
      record.drawRange = activeModel.range; record.drawRatio = ratio;
      if (!width) { record.drawing = null; return; }
      slotWidth = Math.max(1, Math.floor(width / grid.count));
      markWidth = Math.max(1, slotWidth - HISTORY_GAP);
      offset = Math.max(0, width - slotWidth * grid.count);
      record.canvas.width = Math.round(width * ratio);
      record.canvas.height = Math.round(HISTORY_HEIGHT * ratio);
      context = record.canvas.getContext("2d"); record.drawing = {
        grid: grid, slotWidth: slotWidth, offset: offset };
      if (!context) { return; }
      if (typeof context.setTransform === "function") { context.setTransform(ratio, 0, 0, ratio, 0, 0); }
      context.clearRect(0, 0, width, HISTORY_HEIGHT);
      for (index = 0; index < grid.count; index += 1) {
        paint = cellPaint(record.canvas, grid.slots[index] ? healthWord(grid.slots[index]) : null);
        context.fillStyle = paint.color; context.globalAlpha = paint.alpha;
        context.fillRect(offset + index * slotWidth, 0, markWidth, HISTORY_HEIGHT);
      }
      context.globalAlpha = 1;
    }

    function makeRow(edge) {
      var row = element("tr"), nameCell = element("td", "v5-td-name");
      var name = element("div", "nm"), detail = element("div", "n1-detail");
      var status = element("td", "v5-td-status"), dot = element("span", "n1-dot off");
      var word = element("span", "v5-word off"), reading = element("td");
      var wordText = root.document.createTextNode(DASH);
      var fresh = element("td"), room = element("td"), change = element("td", "n1-detail");
      var history = element("td"), canvas = element("canvas", "n1-cells");
      var control = element("td"), controlDash = element("span", "", DASH), record;
      row.tabIndex = 0; row.setAttribute("data-edge", edge.id); dot.setAttribute("aria-hidden", "true");
      nameCell.appendChild(name); nameCell.appendChild(detail);
      word.appendChild(dot); word.appendChild(wordText); status.appendChild(word); history.appendChild(canvas);
      control.appendChild(controlDash);
      [nameCell, status, reading, fresh, room, change, history, control]
        .forEach(function (cell) { row.appendChild(cell); });
      record = { id: edge.id, row: row, name: name, detail: detail, dot: dot,
        word: word, wordText: wordText, reading: reading, fresh: fresh, room: room, change: change,
        canvas: canvas, historyValue: null, bucketRef: null, drawWidth: -1,
        drawRange: null, drawRatio: null, drawing: null, observer: null, visible: false,
        control: control, controlDash: controlDash, every: null,
        pauseLabel: null, pause: null, monitorOn: null, probe: null };
      canvas.setAttribute("aria-label", (EDGE_INFO[edge.id] || {}).label + " history");
      canvas.addEventListener("mousemove", function (event) { showTip(record, event); });
      canvas.addEventListener("mouseleave", hideTip);
      row.addEventListener("click", function () { if (!destroyed && onSelect) { onSelect(record.id); } });
      row.addEventListener("keydown", function (event) {
        if (event.target !== row) { return; }
        if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") { return; }
        event.preventDefault(); if (!destroyed && onSelect) { onSelect(record.id); }
      });
      if (typeof root.ResizeObserver === "function") {
        record.observer = new root.ResizeObserver(function () {
          if (record.visible && !destroyed) { drawHistory(record, false); } });
      }
      return record;
    }

    function setControlReady(control, enabled) {
      if (!control) { return; }
      control.disabled = !enabled;
      control.setAttribute("aria-busy", "false");
    }

    function sendControl(record, control, detail) {
      if (destroyed || !onControl || !control || control.disabled) { return; }
      control.disabled = true; control.setAttribute("aria-busy", "true");
      try { onControl(detail); } catch (ignored) {}
    }

    function ensureEvery(record, edge) {
      var group = CONTROL_GROUPS[edge.group], select, option;
      if (record.every || !group) { return; }
      select = element("select", "st-select n1-ctl-every");
      select.setAttribute("aria-label", "Monitor interval");
      option = element("option", "", DASH); option.value = ""; select.appendChild(option);
      group.values.forEach(function (value) {
        option = element("option", "", value + " s");
        option.value = String(value); select.appendChild(option);
      });
      record.every = select;
      select.addEventListener("click", function (event) { event.stopPropagation(); });
      select.addEventListener("change", function () {
        var currentGroup = CONTROL_GROUPS[record.edge && record.edge.group];
        var value = Number(select.value);
        if (!currentGroup || currentGroup.values.indexOf(value) === -1) { return; }
        sendControl(record, select, { type: "every", edgeId: record.id,
          group: record.edge.group, value: value });
      });
      record.control.appendChild(select);
    }

    function ensurePause(record) {
      var button;
      if (record.pause) { return; }
      record.pauseLabel = element("span", "st-t-meta", "Monitor");
      button = element("button", "switch n1-switch");
      button.type = "button"; button.setAttribute("role", "switch");
      button.setAttribute("aria-label", "Monitor"); button.setAttribute("aria-checked", "false");
      button.appendChild(element("span", "knob")); record.pause = button;
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        if (record.monitorOn === null) { return; }
        sendControl(record, button, { type: "pause", edgeId: record.id,
          group: record.edge.group, value: !record.monitorOn });
      });
      record.control.appendChild(record.pauseLabel); record.control.appendChild(button);
    }

    function ensureProbe(record) {
      var button;
      if (record.probe) { return; }
      button = element("button", "st-ctl-btn n1-ctl-probe", "Probe now");
      button.type = "button"; record.probe = button;
      button.addEventListener("click", function (event) {
        event.stopPropagation();
        sendControl(record, button, { type: "probe", edgeId: record.id,
          group: record.edge.group, value: null });
      });
      record.control.appendChild(button);
    }

    function patchControls(record, edge) {
      var declared = Array.isArray(edge.controls) ? edge.controls : [];
      var group = CONTROL_GROUPS[edge.group], config = collectorConfig(activeModel);
      var showEvery = !!onControl && !!group && declared.indexOf("monitor_every") !== -1;
      var showPause = !!onControl && !!group && declared.indexOf("pause_monitor") !== -1;
      var showProbe = !!onControl && declared.indexOf("probe_now") !== -1;
      var title = controlTitle(edge.group), current, paused, monitorOn;
      if (showEvery) { ensureEvery(record, edge); }
      if (showPause) { ensurePause(record); }
      if (showProbe) { ensureProbe(record); }
      record.controlDash.hidden = showEvery || showPause || showProbe;
      if (record.every) {
        record.every.hidden = !showEvery;
        current = showEvery && config ? config[group.key] : null;
        current = showEvery && group.values.indexOf(current) !== -1 ? String(current) : "";
        if (record.every.value !== current) { record.every.value = current; }
        record.every.title = title;
        record.every.setAttribute("aria-label", "Monitor interval, " + title);
        setControlReady(record.every, showEvery);
      }
      if (record.pause) {
        paused = showPause && config && config.paused ? config.paused[edge.group] : null;
        monitorOn = paused === false ? true : (paused === true ? false : null);
        record.monitorOn = monitorOn; record.pause.hidden = !showPause;
        record.pauseLabel.hidden = !showPause; record.pause.title = title;
        record.pause.setAttribute("aria-label", "Monitor, " + title);
        record.pause.setAttribute("aria-checked", monitorOn === true ? "true" : "false");
        record.pause.classList.toggle("on", monitorOn === true);
        setControlReady(record.pause, showPause && monitorOn !== null);
      }
      if (record.probe) {
        record.probe.hidden = !showProbe;
        record.probe.title = "Probe now: " + ((EDGE_INFO[edge.id] || {}).label || edge.id);
        record.probe.setAttribute("aria-label", record.probe.title);
        setControlReady(record.probe, showProbe);
      }
    }

    function patchRow(record, edge, event, selected) {
      var info = EDGE_INFO[edge.id] || { label: edge.label || edge.id, detail: "" };
      var health = healthWord(edge), stateClass = STATUS_CLASS[health];
      var observed = isNumber(edge.observed_ms) ? edge.observed_ms : timeMs(edge.observed_at);
      record.name.textContent = info.label; record.detail.textContent = info.detail || DASH;
      record.dot.className = "n1-dot" + (stateClass ? " " + stateClass : "");
      record.dot.setAttribute("data-health", health);
      record.wordText.nodeValue = health;
      record.word.className = "v5-word" + (stateClass ? " " + stateClass : "");
      record.reading.textContent = primaryReading(edge, activeModel);
      record.fresh.textContent = observed === null ? DASH : formatAgeMs(nowMs(activeModel) - observed);
      record.room.textContent = headroom(edge);
      record.change.textContent = event && event.stamp !== null
        ? formatRelativeMs(nowMs(activeModel) - event.stamp) : DASH;
      record.row.classList.toggle("on", selected);
      record.row.setAttribute("aria-selected", selected ? "true" : "false");
      record.canvas.setAttribute("aria-label", info.label + " history");
      patchControls(record, edge);
    }

    function wantedEdge(edge, filter) {
      if (filter === "all") { return true; }
      if (filter === "unhealthy") { return healthWord(edge) !== "Operational"; }
      return edge.required === true;
    }

    function updateAxis(visible) {
      var spec = RANGE[activeModel.range] || RANGE["1h"], to = nowMs(activeModel);
      var hasBuckets = visible.some(function (record) {
        return historyBuckets(record.historyValue).length > 0;
      });
      var from = hasBuckets ? Math.floor(to / spec.bucketMs) * spec.bucketMs -
        (spec.cells - 1) * spec.bucketMs : to - spec.cells * spec.bucketMs;
      axisLabels.forEach(function (label, index) {
        label.textContent = formatHHMM(from + (to - from) * index / 3); });
    }

    function update(nextModel) {
      var current, byId = Object.create(null), events, visible = [], wanted = new Set();
      var focused = root.document.activeElement, focusedId = null;
      if (destroyed) { return; }
      hideTip();
      activeModel = nextModel || {}; current = activeModel.current || {}; events = newestEvents(current);
      (Array.isArray(current.edges) ? current.edges : []).forEach(function (edge) {
        if (edge && typeof edge.id === "string" && EDGE_INFO[edge.id]) { byId[edge.id] = edge; }
      });
      rows.forEach(function (record) {
        if (focused === record.row || record.row.contains(focused)) { focusedId = record.id; }
      });
      EDGES.forEach(function (definition) {
        var edge = byId[definition[0]], record;
        if (!edge || !wantedEdge(edge, activeModel.filter)) { return; }
        record = rows.get(edge.id);
        if (!record) { record = makeRow(edge); rows.set(edge.id, record); }
        record.historyValue = activeModel.historyByEdge && activeModel.historyByEdge[edge.id];
        record.edge = edge;
        patchRow(record, edge, events.get(edge.id), activeModel.selectedId === edge.id);
        visible.push(record); wanted.add(edge.id);
      });
      visible.sort(function (left, right) {
        var leftRank = RANK[healthWord(byId[left.id])], rightRank = RANK[healthWord(byId[right.id])];
        return rightRank - leftRank || EDGE_INFO[left.id].order - EDGE_INFO[right.id].order;
      });
      rows.forEach(function (record) {
        if (wanted.has(record.id)) { return; }
        record.visible = false; if (record.observer) { record.observer.disconnect(); }
        if (record.row.parentNode) { record.row.parentNode.removeChild(record.row); }
      });
      var cursor = body.firstChild;
      visible.forEach(function (record) {
        if (record.row !== cursor) { body.insertBefore(record.row, cursor); }
        cursor = record.row.nextSibling; record.visible = true;
        if (record.observer) { record.observer.observe(record.canvas); } drawHistory(record, false);
      });
      updateAxis(visible);
      if (focusedId && wanted.has(focusedId) && root.document.activeElement !== focused) {
        try { focused.focus({ preventScroll: true }); }
        catch (ignored) { focused.focus(); }
      }
    }

    function destroy() {
      if (destroyed) { return; } destroyed = true; hideTip();
      rows.forEach(function (record) { record.visible = false;
        if (record.observer) { record.observer.disconnect(); } });
      rows.clear(); if (ownsCard && card.parentNode) { card.parentNode.removeChild(card); }
      if (!ownsCard) {
        [table, axis, tip].forEach(function (node) {
          if (node.parentNode) { node.parentNode.removeChild(node); }
        });
      }
    }

    update(model);
    return { update: update, destroy: destroy };
  }

  root.NetV1Table = { render: render };
}(window));
