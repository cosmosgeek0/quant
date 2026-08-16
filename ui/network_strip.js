(function (root) {
  "use strict";

  var DASH = "\u2014",
    SITE_EDGES = ["browser_site", "cf_connector", "connector_origin"],
    BOX_EDGES = ["spot_rest", "spot_wss", "usdm_rest", "usdm_wss",
      "news_fetch", "feed_machine"],
    ALL_EDGES = SITE_EDGES.concat(BOX_EDGES),
    LABELS = {
      browser_site: "Browser \u2192 site", cf_connector: "Cloudflare connector",
      connector_origin: "Connector \u2192 origin", spot_rest: "Spot REST",
      spot_wss: "Spot WSS", usdm_rest: "USD-M REST", usdm_wss: "USD-M WSS",
      news_fetch: "News fetch", feed_machine: "Feed \u2192 Machine"
    },
    STATUS_CLASS = {
      Operational: "", Degraded: "warn", Stale: "warn",
      Outage: "bad", Unknown: "off", Off: "off"
    };

  function element(tag, className, text) {
    var node = root.document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function isNumber(value) { return typeof value === "number" && isFinite(value); }

  function sharedFormat(names, value) {
    var fmt = root.NetV1 && root.NetV1.fmt, result, index, fn;
    if (!fmt) { return null; }
    for (index = 0; index < names.length; index += 1) {
      fn = fmt[names[index]];
      if (typeof fn !== "function") { continue; }
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
    var shared, seconds, minutes, remainder;
    if (!isNumber(value) || value < 0) { return DASH; }
    shared = sharedFormat(["ageMs", "age"], value);
    if (shared !== null) { return shared; }
    seconds = value / 1000;
    if (seconds < 10) {
      return (Math.round(seconds * 10) / 10).toFixed(1) + " s";
    }
    if (seconds < 60) { return Math.round(seconds) + " s"; }
    minutes = Math.floor(seconds / 60);
    remainder = Math.round(seconds - (minutes * 60));
    if (remainder === 60) { minutes += 1; remainder = 0; }
    return minutes + " m " + remainder + " s";
  }

  function metric(edge, key) {
    if (!edge || !edge.metrics ||
        !Object.prototype.hasOwnProperty.call(edge.metrics, key)) { return null; }
    return edge.metrics[key];
  }

  function withUnit(value, unit) {
    var formatted = formatNumber(value);
    return formatted === DASH ? DASH : formatted + " " + unit;
  }

  function primaryReading(id, edge, current) {
    var first, second, age;
    if (!edge) { return DASH; }
    if (id === "browser_site") {
      first = current && current.sessionFetchMs;
      if (!isNumber(first)) { first = metric(edge, "fetch_ms"); }
      return withUnit(first, "ms");
    }
    if (id === "cf_connector") {
      first = metric(edge, "ha_connections"); second = metric(edge, "latest_rtt_ms_max");
      if (!isNumber(first) && !isNumber(second)) { return DASH; }
      return formatNumber(first) + "/4 conns \u00b7 " + withUnit(second, "ms");
    }
    if (id === "connector_origin" || id === "spot_rest" || id === "usdm_rest") {
      return withUnit(metric(edge, "rtt_ms"), "ms"); }
    if (id === "spot_wss" || id === "usdm_wss") {
      age = formatAgeMs(metric(edge, "last_message_age_ms"));
      return age === DASH ? DASH : age + " ago"; }
    if (id === "news_fetch") {
      first = metric(edge, "sources_live"); second = metric(edge, "sources_available");
      if (!isNumber(first) && !isNumber(second)) { return DASH; }
      return formatNumber(first) + " / " + formatNumber(second) + " live";
    }
    if (id === "feed_machine") {
      age = formatAgeMs(metric(edge, "book_age_p50_ms"));
      return age === DASH ? DASH : "books " + age + " old"; }
    return DASH;
  }

  function edgeMap(current) {
    var map = Object.create(null),
      edges = current && Array.isArray(current.edges) ? current.edges : [];
    edges.forEach(function (edge) {
      if (edge && typeof edge.id === "string" && !map[edge.id]) { map[edge.id] = edge; }
    });
    return map;
  }

  function edgeHealth(edge) {
    if (!edge) { return DASH; }
    return Object.prototype.hasOwnProperty.call(STATUS_CLASS, edge.health)
      ? edge.health : "Unknown";
  }

  function edgeReason(edge) {
    return edge && typeof edge.health_reason === "string" && edge.health_reason
      ? edge.health_reason : DASH;
  }

  function patchRecord(record, edge, current, selected) {
    var health = edgeHealth(edge);
    var stateClass = edge ? STATUS_CLASS[health] : "off";
    record.dot.className = "n1-dot" + (stateClass ? " " + stateClass : "");
    record.value.textContent = primaryReading(record.id, edge, current);
    record.word.textContent = health;
    record.word.className = "v5-word" + (stateClass ? " " + stateClass : "");
    record.button.classList.toggle("on", selected);
    record.button.setAttribute("aria-pressed", selected ? "true" : "false");
    record.button.title = LABELS[record.id] + " \u00b7 " + health
      + " \u00b7 " + edgeReason(edge);
  }

  function render(host, current, selectedId, onSelect) {
    var strip = element("div", "st-card n1-strip"), siteLine = element("div"),
      boxLine = element("div"), records = Object.create(null),
      siteNodes = ["Browser", "Cloudflare edge", "Tunnel connector", "Quant UI \u00b7 box"];

    function appendLink(line) {
      var link = element("span", "n1-link");
      link.setAttribute("aria-hidden", "true");
      line.appendChild(link);
    }

    function appendNode(line, label, box) {
      line.appendChild(element("span", "n1-node" + (box ? " n1-node-box" : ""), label));
    }

    function appendEdge(line, id) {
      var button = element("button", "n1-edge"), dot = element("span", "n1-dot off"),
        name = element("span", "n1-edge-name", LABELS[id]),
        value = element("span", "n1-edge-val", DASH),
        word = element("span", "v5-word off", DASH);
      button.type = "button"; button.setAttribute("aria-pressed", "false");
      dot.setAttribute("aria-hidden", "true");
      button.appendChild(dot); button.appendChild(name);
      button.appendChild(value); button.appendChild(word);
      button.addEventListener("click", function () {
        if (typeof onSelect === "function") { onSelect(id); }
      });
      records[id] = { id: id, button: button, dot: dot, value: value, word: word };
      line.appendChild(button);
    }

    function update(nextCurrent, nextSelectedId) {
      var byId = edgeMap(nextCurrent);
      ALL_EDGES.forEach(function (id) {
        patchRecord(records[id], byId[id] || null, nextCurrent, nextSelectedId === id);
      });
    }

    siteLine.setAttribute("aria-label", "Site path");
    siteNodes.forEach(function (label, index) {
      appendNode(siteLine, label, false);
      if (index < SITE_EDGES.length) {
        appendLink(siteLine); appendEdge(siteLine, SITE_EDGES[index]); appendLink(siteLine);
      }
    });
    boxLine.setAttribute("aria-label", "Box paths");
    appendNode(boxLine, "Execution box", true);
    BOX_EDGES.forEach(function (id) {
      appendLink(boxLine); appendEdge(boxLine, id);
    });
    strip.appendChild(siteLine);
    strip.appendChild(boxLine);
    host.appendChild(strip);
    update(current, selectedId);
    return { update: update };
  }

  root.NetV1Strip = { render: render };
}(window));
