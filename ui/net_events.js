"use strict";

(function () {
  var WORDS = {
    ok: "Operational",
    warn: "Degraded",
    bad: "Outage"
  };

  function state(value, thresholds) {
    if (value === null || typeof value === "undefined" || !thresholds) {
      return null;
    }
    if (value >= thresholds.bad) { return "bad"; }
    if (value >= thresholds.warn) { return "warn"; }
    return "ok";
  }

  function compute(points, defs) {
    var samples = Array.isArray(points) ? points : [];
    var metrics = Array.isArray(defs) ? defs : [];
    var rows = [];
    var i;
    var j;

    for (i = 0; i < metrics.length; i += 1) {
      var def = metrics[i];
      var confirmed = null;
      var pending = null;
      var pendingTs = null;

      for (j = 0; j < samples.length; j += 1) {
        var point = samples[j];
        var current = state(point[def.field], def.thr);

        if (current === null) { continue; }
        if (confirmed === null) { confirmed = current; continue; }
        if (current === confirmed) { pending = null; continue; }
        /* audit fix: one-sample flickers are not incidents — a new
           state must hold for two consecutive samples to count */
        if (pending === current) {
          rows.push({
            ts: pendingTs,
            key: def.key,
            label: def.label,
            from: confirmed,
            to: current
          });
          confirmed = current;
          pending = null;
        } else {
          pending = current;
          pendingTs = point.ts;
        }
      }
    }

    rows.sort(function (a, b) {
      return new Date(b.ts).getTime() - new Date(a.ts).getTime();
    });
    /* the CALLER counts the full list; display capping is render's job */
    return rows;
  }

  function utcTime(timestamp) {
    var date = new Date(timestamp);
    var iso;

    if (isNaN(date.getTime())) { return ""; }
    iso = date.toISOString();
    return iso.slice(11, 19);
  }

  function makeState(value) {
    var status = document.createElement("span");

    status.className = "qevents-state qe-" + value;
    status.textContent = WORDS[value];
    return status;
  }

  function render(el, rows) {
    rows = (rows || []).slice(0, 30);
    var events = Array.isArray(rows) ? rows : [];
    var list = document.createElement("div");
    var i;

    list.className = "qevents";
    el.textContent = "";

    if (!events.length) {
      var empty = document.createElement("div");
      empty.className = "qevents-empty";
      empty.textContent = "No state changes in this window.";
      list.appendChild(empty);
      el.appendChild(list);
      return;
    }

    for (i = 0; i < events.length; i += 1) {
      var event = events[i];
      var row = document.createElement("div");
      var time = document.createElement("span");
      var name = document.createElement("span");
      var arrow = document.createElement("span");

      row.className = "qevents-row";
      time.className = "qevents-time";
      time.textContent = utcTime(event.ts);
      name.className = "qevents-name";
      name.textContent = event.label;
      arrow.className = "qevents-arrow";
      arrow.textContent = "\u2192";

      row.appendChild(time);
      row.appendChild(name);
      row.appendChild(makeState(event.from));
      row.appendChild(arrow);
      row.appendChild(makeState(event.to));
      list.appendChild(row);
    }

    el.appendChild(list);
  }

  window.QNet = window.QNet || {};
  window.QNet.events = {
    compute: compute,
    render: render
  };
}());
