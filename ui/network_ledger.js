(function (root) {
  "use strict";

  var DASH = "\u2014";
  var EDGE_LABELS = {
    browser_site: "Browser \u2192 site", cf_connector: "Cloudflare connector",
    connector_origin: "Connector \u2192 origin", spot_rest: "Spot REST",
    spot_wss: "Spot WSS", usdm_rest: "USD-M REST", usdm_wss: "USD-M WSS",
    feed_machine: "Feed \u2192 Machine", news_fetch: "News fetch",
    binance_account: "Binance account reads", binance_exec: "Binance execution",
    aws_secrets: "AWS Secrets Manager", aws_ssm: "AWS SSM"
  };
  var HEALTH_AT_END = /(Operational|Degraded|Outage|Stale|Unknown|Off)\s*$/;

  function shown(value) { return value === null || value === undefined || value === ""
    ? DASH : String(value); }

  function formatUtcTime(value) {
    if (value === null || value === undefined || value === "") { return DASH; }
    var date = new Date(Number(value));
    if (!isFinite(date.getTime())) { return DASH; }
    return [date.getUTCHours(), date.getUTCMinutes(), date.getUTCSeconds()]
      .map(function (part) { return String(part).padStart(2, "0"); }).join(":");
  }

  function edgeLabel(edgeId) { return edgeId === null || edgeId === undefined
    ? "Recorder" : EDGE_LABELS[String(edgeId)] || DASH; }

  function healthClass(word) {
    if (word === "Outage") { return "bad"; }
    if (word === "Degraded" || word === "Stale") { return "warn"; }
    return word === "Unknown" || word === "Off" ? "off" : "";
  }

  function splitTransition(value) {
    var text = shown(value), match = text.match(HEALTH_AT_END);
    return match ? { prefix: text.slice(0, match.index), health: match[1] }
      : { prefix: text, health: "" };
  }

  function orderedEvents(events) {
    var seen = Object.create(null);
    return (Array.isArray(events) ? events : []).map(function (event, index) {
      var rawTime = event && event.ts, rawId = event && event.event_id;
      var time = rawTime === null || rawTime === undefined || rawTime === ""
        ? -Infinity : Number(rawTime);
      return { event: event || {}, index: index, time: isFinite(time) ? time : -Infinity,
        key: rawId === null || rawId === undefined ? "missing:" + index : String(rawId) };
    }).sort(function (left, right) {
      if (left.time !== right.time) { return right.time - left.time; }
      var leftId = Number(left.event.event_id), rightId = Number(right.event.event_id);
      return isFinite(leftId) && isFinite(rightId) && leftId !== rightId
        ? rightId - leftId : left.index - right.index;
    }).filter(function (item) {
      if (seen[item.key]) { return false; }
      seen[item.key] = true;
      return true;
    }).slice(0, 20);
  }

  function element(tag, className, text) {
    var node = document.createElement(tag);
    if (className) { node.className = className; }
    if (text !== undefined) { node.textContent = text; }
    return node;
  }

  function createRow() {
    var record = { row: element("tr"), time: element("td"),
      connection: element("span", "nm"), prefix: document.createTextNode(""),
      health: element("span", "v5-word"), reason: element("td", "n1-detail") };
    var connectionCell = element("td", "v5-td-name");
    var changeCell = element("td", "n1-detail");
    connectionCell.appendChild(record.connection);
    changeCell.appendChild(record.prefix); changeCell.appendChild(record.health);
    [record.time, connectionCell, changeCell, record.reason].forEach(function (cell) {
      record.row.appendChild(cell);
    });
    return record;
  }

  function render(host, events, opts) {
    if (!host || typeof host.appendChild !== "function") {
      throw new TypeError("NetV1Ledger.render requires a host element"); }
    var onSelect = opts && typeof opts.onSelect === "function" ? opts.onSelect : null;
    if (!host.querySelector || !host.querySelector(".st-card-head")) {
      var cardHead = element("div", "st-card-head");
      cardHead.appendChild(element("div", "n1-led-title", "What changed"));
      cardHead.appendChild(element("div", "n1-led-sub", "transitions only \u00b7 newest first"));
      host.appendChild(cardHead);
    }
    var table = element("table", "v5-table n1-ledger-t");
    var head = element("thead"), headRow = element("tr"), body = element("tbody");
    ["Time", "Connection", "Change", "Reason"].forEach(function (label) {
      headRow.appendChild(element("th", "", label)); });
    head.appendChild(headRow); table.appendChild(head); table.appendChild(body);
    host.appendChild(table);

    var rows = new Map(), destroyed = false;
    var emptyRow = element("tr"), emptyCell = element("td", "n1-detail",
      "No changes recorded yet.");
    emptyCell.colSpan = 4; emptyRow.appendChild(emptyCell);

    function patch(record, item) {
      var event = item.event, edgeId = event.edge_id;
      var transition = splitTransition(event.transition), stateClass = healthClass(transition.health);
      var reason = shown(event.reason);
      record.time.textContent = formatUtcTime(event.ts);
      record.connection.textContent = edgeLabel(edgeId);
      record.prefix.nodeValue = transition.prefix;
      record.health.textContent = transition.health;
      record.health.className = "v5-word" + (stateClass ? " " + stateClass : "");
      record.health.hidden = !transition.health;
      record.reason.textContent = reason;
      record.reason.title = reason;
      record.row.setAttribute("data-event-id", item.key);
      if (edgeId === null || edgeId === undefined) { record.row.removeAttribute("data-edge"); }
      else { record.row.setAttribute("data-edge", String(edgeId)); }
      if (onSelect && edgeId !== null && edgeId !== undefined) {
        record.row.setAttribute("tabindex", "0");
        record.row.setAttribute("role", "button");
      } else {
        record.row.removeAttribute("tabindex"); record.row.removeAttribute("role"); }
    }

    function update(nextEvents) {
      if (destroyed) { return; }
      var items = orderedEvents(nextEvents), wanted = Object.create(null);
      if (emptyRow.parentNode) { emptyRow.parentNode.removeChild(emptyRow); }
      items.forEach(function (item) {
        var record = rows.get(item.key);
        if (!record) { record = createRow(); rows.set(item.key, record); }
        wanted[item.key] = true; patch(record, item);
      });
      var anchor = body.firstChild;
      items.forEach(function (item) {
        var row = rows.get(item.key).row;
        if (row !== anchor) { body.insertBefore(row, anchor); }
        anchor = row.nextSibling;
      });
      rows.forEach(function (record, key) {
        if (wanted[key]) { return; }
        if (record.row.parentNode) { record.row.parentNode.removeChild(record.row); }
        rows.delete(key);
      });
      if (!items.length) { body.appendChild(emptyRow); }
    }

    function targetRow(event) { var row = event.target.closest("tr");
      return row && row.parentNode === body ? row : null; }

    function selectFrom(event) { var row = targetRow(event);
      var edgeId = row && row.getAttribute("data-edge");
      if (onSelect && edgeId !== null) { onSelect(edgeId); } }

    function onKeydown(event) {
      if (event.key !== "Enter" && event.key !== " " && event.key !== "Spacebar") { return; }
      var row = targetRow(event);
      if (!row || row.getAttribute("data-edge") === null) { return; }
      event.preventDefault(); selectFrom(event);
    }

    function destroy() {
      if (destroyed) { return; }
      destroyed = true;
      body.removeEventListener("click", selectFrom);
      body.removeEventListener("keydown", onKeydown);
      rows.clear();
      if (table.parentNode) { table.parentNode.removeChild(table); }
    }

    if (onSelect) { body.addEventListener("click", selectFrom);
      body.addEventListener("keydown", onKeydown); }
    update(events);
    return { update: update, destroy: destroy };
  }

  root.NetV1Ledger = { render: render };
}(window));
