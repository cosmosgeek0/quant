(function (root) {
  "use strict";

  var DASH = "\u2014";
  var EMPTY_BUCKETS = [];
  var RANGE_MS = { "1h": 3600000, "6h": 21600000, "24h": 86400000 };
  var BUCKET_WORD = { "1h": "1 min", "6h": "5 min", "24h": "15 min" };
  var NODE_LABELS = {
    browser: "Browser", cloudflare: "Cloudflare edge", cloudflared: "Tunnel connector",
    quant_ui: "Quant UI origin", box: "Execution box", binance_spot: "Binance Spot",
    binance_usdm: "Binance USD-M", feed: "Market feed", machine: "Machine", news: "News sources",
    binance_account: "Binance signed reads", binance_exec: "Binance execution",
    aws_secrets: "AWS Secrets Manager", aws_ssm: "AWS SSM"
  };
  var FLOWS = {
    browser_site: "UI requests through Cloudflare Access", cf_connector: "tunnel connections and proxied requests",
    connector_origin: "proxied requests to the loopback UI", spot_rest: "public time probe",
    spot_wss: "book ticker + kline streams", usdm_rest: "public time probe",
    usdm_wss: "book ticker + kline streams", feed_machine: "published books into the stream stage",
    news_fetch: "public sources \u2192 news fetcher", binance_account: "signed account reads (on demand)",
    binance_exec: "orders and job receipts (on demand)", aws_secrets: "credential reads (on demand)",
    aws_ssm: "operator sessions (on demand)"
  };
  var PRIMARY_METRIC = {
    browser_site: "fetch_ms", cf_connector: "latest_rtt_ms_max",
    connector_origin: "rtt_ms", spot_rest: "rtt_ms",
    spot_wss: "last_message_age_ms", usdm_rest: "rtt_ms",
    usdm_wss: "last_message_age_ms", feed_machine: "book_age_p50_ms",
    news_fetch: "sources_live"
  };
  var METRICS = {
    rtt_ms: ["Round trip", "ms"], http_status: ["HTTP", ""], clock_offset_ms: ["Clock offset", "ms"],
    used_weight_1m: ["Weight used", ""], weight_limit_1m: ["Weight limit", ""], retry_after_s: ["Retry after", "s"],
    consecutive_failures: ["Consecutive failures", ""], expected_connections: ["Sockets expected", ""],
    connected_count: ["Sockets connected", ""], successful_handshakes: ["Handshakes", ""], handshake_failures: ["Handshake failures", ""],
    reconnects: ["Reconnects", ""], stream_count: ["Streams", ""], last_message_age_ms: ["Last message", "age"],
    snapshot_age_ms: ["Snapshot age", "age"], dropped_frames_total: ["Frames dropped", ""], dropped_frames_delta: ["Dropped this sample", ""],
    feed_status: ["Feed status", "text"], feed_snapshot_age_ms: ["Feed snapshot", "age"], stream_row_age_ms: ["Stream row", "age"],
    symbols_total: ["Symbols", ""], book_age_p50_ms: ["Book age p50", "age"], book_age_p95_ms: ["Book age p95", "age"],
    book_age_max_ms: ["Book age max", "age"], symbols_book_le_5s: ["Symbols under 5 s", ""], freshness_p50_ms: ["Freshness p50", "age"],
    source: ["Source", "text"], ha_connections: ["HA connections", ""], ready_connections: ["Ready", ""], ready_status: ["Ready HTTP", ""],
    total_requests_delta: ["Requests this sample", ""], concurrent_requests: ["Concurrent", ""], latest_rtt_ms_max: ["QUIC RTT", "ms"],
    smoothed_rtt_ms_max: ["QUIC RTT smoothed", "ms"], min_rtt_ms_min: ["QUIC RTT min", "ms"], lost_packets_delta: ["Lost packets", ""],
    connect_streams_errors_delta: ["Stream errors", ""], register_success_total: ["Registrations", ""], metrics_url_port: ["Metrics port", ""],
    size_bytes: ["Bytes", ""], request_errors_delta: ["Proxy errors", ""], response_5xx_delta: ["5xx", ""],
    sources_available: ["Sources", ""], sources_live: ["Live", ""], sources_pending: ["Pending", ""], sources_error: ["Errors", ""],
    as_of_age_s: ["As of", "s"], last_fetch_age_s: ["Last fetch", "s"], last_used_at: ["Last used", "text"],
    last_op: ["Last op", "text"], last_outcome: ["Last outcome", "text"], fetch_ms: ["Fetch", "ms"]
  };

  function element(tag, className, value) {
    var node = document.createElement(tag);
    if (className) { node.className = className; } if (value !== undefined) { node.textContent = value; }
    return node;
  }

  function own(value, key) { return Object.prototype.hasOwnProperty.call(value || {}, key); }

  function finiteNumber(value) { return typeof value === "number" && Number.isFinite(value); }

  function shown(value) { return value === null || value === undefined || value === "" ? DASH : String(value); }

  function numberWord(value) {
    if (!finiteNumber(value)) { return DASH; }
    return Number.isInteger(value) ? String(value) : value.toFixed(1).replace(/\.0$/, "");
  }

  function ageMetric(value) {
    if (!finiteNumber(value) || value < 0) { return DASH; }
    var seconds = value / 1000;
    if (seconds < 10) { return seconds.toFixed(1) + " s"; }
    if (seconds < 60) { return Math.round(seconds) + " s"; }
    var whole = Math.round(seconds), minutes = Math.floor(whole / 60);
    return minutes + "m " + (whole % 60) + "s";
  }

  function metricWord(key, value, values) {
    if (value === null || value === undefined || value === "") { return DASH; }
    var definition = METRICS[key] || [key, finiteNumber(value) ? "" : "text"];
    if (definition[1] === "text") { return shown(value); }
    if (definition[1] === "age") { return ageMetric(value); }
    var word = numberWord(value);
    if (word === DASH) { return DASH; }
    if (key === "used_weight_1m") {
      return word + " / " + numberWord(values && values.weight_limit_1m);
    }
    return word + (definition[1] ? " " + definition[1] : "");
  }

  function metricAxisWord(key, value) {
    var definition = METRICS[key] || [key, ""];
    if (definition[1] === "age") { return ageMetric(value); }
    var word = numberWord(value); return word === DASH ? word : word + (definition[1] ? " " + definition[1] : "");
  }

  function timestampMs(value) {
    if (finiteNumber(value)) { return value; }
    if (typeof value !== "string" || !value) { return null; }
    var parsed = Date.parse(value); return Number.isFinite(parsed) ? parsed : null;
  }

  function clockWord(value) {
    var stamp = timestampMs(value);
    if (stamp === null) { return DASH; }
    var date = new Date(stamp);
    function two(part) { return String(part).padStart(2, "0"); }
    return two(date.getUTCHours()) + ":" + two(date.getUTCMinutes()) + ":" + two(date.getUTCSeconds());
  }

  function elapsedWord(stamp, now) {
    if (stamp === null || stamp > now + 1000) { return DASH; }
    var seconds = Math.max(0, Math.floor((now - stamp) / 1000));
    if (seconds < 60) { return seconds + " s"; }
    if (seconds < 3600) { return Math.floor(seconds / 60) + " min"; }
    if (seconds < 86400) { return Math.floor(seconds / 3600) + " h"; }
    return Math.floor(seconds / 86400) + " d";
  }

  function healthWord(edge) {
    var known = ["Operational", "Degraded", "Stale", "Outage", "Unknown", "Off"];
    return edge && known.indexOf(edge.health) !== -1 ? edge.health : "Unknown";
  }

  function healthClass(health) {
    if (health === "Outage") { return "bad"; }
    if (health === "Degraded" || health === "Stale") { return "warn"; }
    return health === "Unknown" || health === "Off" ? "off" : "";
  }

  function cadenceWord(value) {
    if (!finiteNumber(value) || value < 0) { return DASH; }
    var seconds = value / 1000; return seconds >= 60 && seconds % 60 === 0
      ? numberWord(seconds / 60) + " min" : numberWord(seconds) + " s";
  }

  function latestEvent(edge, opts) {
    var rows = [], direct = edge && (edge.last_event || edge.lastEvent || edge.latest_event || edge.latestEvent);
    if (direct) { rows.push(direct); }
    [edge && edge.events, opts && opts.events].forEach(function (items) { if (Array.isArray(items)) { rows = rows.concat(items); } });
    if (opts && (opts.lastEvent || opts.event)) { rows.push(opts.lastEvent || opts.event); }
    rows = rows.filter(function (event) { return event && (!event.edge_id || !edge || event.edge_id === edge.id); });
    rows.sort(function (a, b) {
      var right = timestampMs(b.ts), left = timestampMs(a.ts);
      return (right === null ? -1 : right) - (left === null ? -1 : left);
    });
    return rows.length ? rows[0] : null;
  }

  function render(host, edge, buckets, opts) {
    if (!host || typeof host.appendChild !== "function") {
      throw new TypeError("NetV1Drawer needs a host element");
    }
    var state = { edge: null, buckets: EMPTY_BUCKETS, opts: {}, metric: null, destroyed: false };
    var drawnBuckets = null, drawnWidth = -1, metricRows = Object.create(null);
    var card = element("section", "st-card n1-drawer");
    var head = element("div", "st-card-head"), dot = element("span", "n1-dot");
    var name = element("span", "n1-dr-name", DASH), status = element("span", "v5-word", "Unknown");
    var reason = element("span", "n1-dr-reason", DASH), spacer = element("span", "st-spacer");
    var close = element("button", "st-iconbtn", "\u00d7");
    close.type = "button";
    close.title = "close selected connection";
    close.setAttribute("aria-label", "Close selected connection");
    [dot, name, status, reason, spacer, close].forEach(function (item) { head.appendChild(item); });
    card.appendChild(head);

    var body = element("div", "n1-dr-grid"), facts = element("div", "n1-kv");
    var chart = element("div", "n1-chart");
    body.appendChild(facts); body.appendChild(chart); card.appendChild(body); host.appendChild(card);

    function factRow(label, valueClass) {
      var row = element("div"), key = element("span", "v5-card-lbl", label);
      var value = element("span", valueClass || "st-t-meta", DASH);
      row.appendChild(key); row.appendChild(value); facts.appendChild(row);
      return { row: row, value: value };
    }

    var truth = factRow("Truth", "st-t-meta");
    truth.value.textContent = "";
    var truthParts = {};
    ["configured", "transport", "application"].forEach(function (key, index) {
      if (index) { truth.value.appendChild(document.createTextNode(" \u00b7 ")); }
      truth.value.appendChild(document.createTextNode(key + " "));
      truthParts[key] = element("span", "", DASH); truth.value.appendChild(truthParts[key]);
    });
    var direction = factRow("Direction"), flows = factRow("Flows"), vantage = factRow("Vantage");
    var mode = factRow("Mode / authority"), sample = factRow("Last sample", "st-t-datum");
    var change = factRow("Last change", "st-t-datum"), samples = factRow("Samples", "st-t-datum");
    var monitor = factRow("Monitor"), controls = factRow("Controls implemented");

    var chartHead = element("div", "n1-chart-head"), select = element("select", "st-select");
    select.setAttribute("aria-label", "Chart metric");
    var caption = element("span", "st-t-meta", DASH);
    chartHead.appendChild(select); chartHead.appendChild(caption); chart.appendChild(chartHead);
    var canvas = element("canvas");
    canvas.setAttribute("role", "img"); canvas.textContent = "Metric history chart";
    var collecting = element("div", "st-t-meta", "Collecting history \u2014 0 cells so far");
    chart.appendChild(canvas); chart.appendChild(collecting);

    function patchOpts(next) {
      if (!next) { return; }
      ["range", "metric", "onMetric", "onClose", "events", "event", "lastEvent"].forEach(function (key) {
        if (own(next, key)) { state.opts[key] = next[key]; }
      });
    }

    function numericMetricKeys(currentEdge, wanted) {
      var values = currentEdge && currentEdge.metrics && typeof currentEdge.metrics === "object"
        ? currentEdge.metrics : {};
      var keys = Object.keys(values).filter(function (key) {
        return finiteNumber(values[key]) && (!METRICS[key] || METRICS[key][1] !== "text"); });
      if (wanted && METRICS[wanted] && METRICS[wanted][1] !== "text" && keys.indexOf(wanted) === -1) {
        keys.unshift(wanted);
      }
      return keys;
    }

    function syncSelect(currentEdge) {
      var wanted = typeof state.opts.metric === "string" && state.opts.metric
        ? state.opts.metric : (state.metric || PRIMARY_METRIC[currentEdge && currentEdge.id] || null);
      var keys = numericMetricKeys(currentEdge, wanted), existing = Object.create(null);
      Array.prototype.forEach.call(select.options, function (option) { existing[option.value] = option; });
      if (!keys.length) { keys = [""]; }
      keys.forEach(function (key) {
        var option = existing[key] || element("option");
        option.value = key; option.textContent = key ? (METRICS[key] ? METRICS[key][0] : key) : DASH;
        option.disabled = !key; select.appendChild(option); delete existing[key];
      });
      Object.keys(existing).forEach(function (key) { existing[key].remove(); });
      state.metric = keys.indexOf(wanted) !== -1 ? wanted : (keys[0] || null);
      select.value = state.metric || "";
      select.disabled = !state.metric;
    }

    function syncMetricFacts(currentEdge) {
      var values = currentEdge && currentEdge.metrics && typeof currentEdge.metrics === "object"
        ? currentEdge.metrics : {};
      var keys = Object.keys(values).filter(function (key) { return values[key] !== null && values[key] !== undefined; });
      if (currentEdge && currentEdge.id === "binance_exec") {
        ["last_used_at", "last_op", "last_outcome"].forEach(function (key) {
          if (keys.indexOf(key) === -1) { keys.push(key); }
        });
      }
      keys.forEach(function (key) {
        var item = metricRows[key];
        if (!item) {
          item = factRow(METRICS[key] ? METRICS[key][0] : key, "st-t-datum"); metricRows[key] = item;
        }
        item.value.textContent = metricWord(key, values[key], values); facts.insertBefore(item.row, controls.row);
      });
      Object.keys(metricRows).forEach(function (key) {
        if (keys.indexOf(key) === -1) { metricRows[key].row.remove(); delete metricRows[key]; }
      });
    }

    function cssColor(names, fallback) {
      var style = root.getComputedStyle ? root.getComputedStyle(canvas) : null;
      for (var i = 0; style && i < names.length; i += 1) {
        var value = style.getPropertyValue(names[i]).trim(); if (value) { return value; }
      }
      return fallback;
    }

    function chartSize() {
      var rect = canvas.getBoundingClientRect(); return { width: Math.round(rect.width || canvas.clientWidth || 0),
        height: Math.round(rect.height || canvas.clientHeight || 0) };
    }

    function drawIfNeeded() {
      if (state.destroyed || state.buckets.length < 2 || card.hidden) { return; }
      var size = chartSize();
      if (!size.width || !size.height) { return; }
      if (state.buckets === drawnBuckets && size.width === drawnWidth) { return; }
      drawnBuckets = state.buckets; drawnWidth = size.width;
      var dpr = Math.max(1, root.devicePixelRatio || 1), context = canvas.getContext("2d");
      if (!context) { return; }
      canvas.width = Math.round(size.width * dpr); canvas.height = Math.round(size.height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0); context.clearRect(0, 0, size.width, size.height);
      var ink = cssColor(["--ink", "--st-ink"], "#161618");
      var muted = cssColor(["--muted", "--st-mid"], "#787882");
      var faint = cssColor(["--faint", "--st-faint"], "#95959d");
      var points = state.buckets.filter(function (bucket) {
        return bucket && finiteNumber(bucket.t) && finiteNumber(bucket.avg);
      });
      var values = [];
      points.forEach(function (point) { values.push(point.avg); if (finiteNumber(point.max)) { values.push(point.max); } });
      if (!values.length) { return; }
      var minimum = Math.min.apply(Math, values), maximum = Math.max.apply(Math, values);
      var low = minimum, high = maximum;
      if (low === high) { var pad = Math.abs(low) * 0.05 || 1; low -= pad; high += pad; }
      var left = 54, right = Math.max(left + 1, size.width - 8), top = 10;
      var bottom = Math.max(top + 1, size.height - 22), plotWidth = right - left;
      var duration = RANGE_MS[state.opts.range] || RANGE_MS["1h"], end = Date.now(), start = end - duration;
      function x(point) { return left + Math.max(0, Math.min(1, (point.t - start) / duration)) * plotWidth; }
      function y(value) { return bottom - ((value - low) / (high - low)) * (bottom - top); }
      context.fillStyle = faint; context.font = "11px InterVariable, Inter, sans-serif";
      context.textAlign = "left"; context.textBaseline = "middle";
      context.fillText(metricAxisWord(state.metric, maximum), 0, top);
      context.fillText(metricAxisWord(state.metric, minimum), 0, bottom);
      context.textAlign = "right"; context.textBaseline = "top"; context.fillText("now", right, bottom + 4);
      context.strokeStyle = ink; context.lineWidth = 1.25; context.beginPath();
      var prior = null, gap = (duration / ({ "1h": 60, "6h": 72, "24h": 96 }[state.opts.range] || 60)) * 1.5;
      points.forEach(function (point) {
        if (!prior || point.t - prior.t > gap) { context.moveTo(x(point), y(point.avg)); }
        else { context.lineTo(x(point), y(point.avg)); } prior = point;
      });
      context.stroke(); context.fillStyle = muted;
      points.forEach(function (point) {
        if (!finiteNumber(point.max)) { return; }
        context.beginPath(); context.arc(x(point), y(point.max), 1.75, 0, Math.PI * 2); context.fill();
      });
    }

    function syncFacts(currentEdge) {
      var health = healthWord(currentEdge), stateClass = healthClass(health);
      name.textContent = shown(currentEdge && currentEdge.label);
      status.textContent = health; status.className = "v5-word" + (stateClass ? " " + stateClass : "");
      dot.className = "n1-dot" + (stateClass ? " " + stateClass : "");
      reason.textContent = shown(currentEdge && currentEdge.health_reason);
      var truthValues = currentEdge && currentEdge.truth && typeof currentEdge.truth === "object" ? currentEdge.truth : {};
      Object.keys(truthParts).forEach(function (key) {
        var value = shown(truthValues[key]); truthParts[key].textContent = value;
        truthParts[key].className = String(value).toLowerCase() === "fail" ? "bad" : "";
      });
      var arrow = currentEdge && currentEdge.direction === "<->" ? " \u2194 " : " \u2192 ";
      direction.value.textContent = shown(currentEdge && NODE_LABELS[currentEdge.source]) + arrow
        + shown(currentEdge && NODE_LABELS[currentEdge.target]);
      flows.value.textContent = shown(currentEdge && FLOWS[currentEdge.id]);
      vantage.value.textContent = shown(currentEdge && currentEdge.evidence && currentEdge.evidence.source);
      mode.value.textContent = shown(currentEdge && currentEdge.mode) + " \u00b7 " + shown(currentEdge && currentEdge.authority);
      var observed = currentEdge ? timestampMs(currentEdge.observed_ms) : null;
      if (observed === null && currentEdge) { observed = timestampMs(currentEdge.observed_at); }
      sample.value.textContent = observed === null ? DASH : clockWord(observed) + " (" + elapsedWord(observed, Date.now()) + " ago)";
      var event = latestEvent(currentEdge, state.opts);
      change.value.textContent = event ? clockWord(event.ts) + " " + shown(event.transition) : DASH;
      samples.value.textContent = currentEdge && finiteNumber(currentEdge.sample_count) ? numberWord(currentEdge.sample_count) : DASH;
      monitor.value.textContent = shown(currentEdge && currentEdge.monitoring) + " \u00b7 every "
        + cadenceWord(currentEdge && currentEdge.cadence_ms);
      var implemented = currentEdge && Array.isArray(currentEdge.controls)
        ? currentEdge.controls.filter(function (item) { return typeof item === "string" && item; }) : [];
      controls.value.textContent = implemented.length
        ? implemented.map(function (item) { return item.replace(/_/g, " "); }).join(" \u00b7 ") : "none";
      syncMetricFacts(currentEdge); syncSelect(currentEdge);
    }

    function syncChart(nextBuckets) {
      var next = Array.isArray(nextBuckets) ? nextBuckets : EMPTY_BUCKETS;
      state.buckets = next; collecting.textContent = "Collecting history \u2014 " + next.length + " cells so far";
      collecting.hidden = next.length >= 2; canvas.hidden = next.length < 2;
      caption.textContent = shown(state.opts.range && String(state.opts.range).toUpperCase())
        + " \u00b7 avg per " + shown(BUCKET_WORD[state.opts.range]) + " cell \u00b7 max as dots \u00b7 UTC";
      canvas.setAttribute("aria-label", (state.metric && METRICS[state.metric] ? METRICS[state.metric][0] : "Metric")
        + " history, " + next.length + " cells");
      drawIfNeeded();
    }

    function update(nextEdge, nextBuckets, nextOpts) {
      if (state.destroyed) { return; }
      var priorId = state.edge && state.edge.id, nextId = nextEdge && nextEdge.id;
      if (priorId !== nextId) {
        state.metric = null;
        if (!nextOpts || !own(nextOpts, "metric")) { delete state.opts.metric; }
      }
      patchOpts(nextOpts); state.edge = nextEdge || null; card.hidden = !state.edge;
      if (state.edge) { syncFacts(state.edge); }
      syncChart(nextBuckets);
    }

    function onSelectChange() {
      if (!select.value) { return; }
      state.metric = select.value; state.opts.metric = state.metric;
      if (typeof state.opts.onMetric === "function") { state.opts.onMetric(state.metric); }
    }

    function onClose() {
      if (typeof state.opts.onClose === "function") { state.opts.onClose(); }
    }

    select.addEventListener("change", onSelectChange); close.addEventListener("click", onClose);
    var observer = typeof root.ResizeObserver === "function"
      ? new root.ResizeObserver(function () { drawIfNeeded(); }) : null;
    if (observer) { observer.observe(canvas); }

    function destroy() {
      if (state.destroyed) { return; }
      state.destroyed = true;
      if (observer) { observer.disconnect(); }
      select.removeEventListener("change", onSelectChange);
      close.removeEventListener("click", onClose);
    }

    update(edge, buckets, opts || {});
    return { update: update, destroy: destroy };
  }

  root.NetV1Drawer = { render: render };
}(window));
