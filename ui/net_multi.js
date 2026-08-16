"use strict";

(function () {
  var SPARK_HEIGHT = 56; var PAD = 3;
  function isNumber(value) { return typeof value === "number" && isFinite(value); }
  function formatCurrent(value, unit) {
    var minutes;
    if (!isNumber(value)) { return "—"; }
    if (unit === "s" && value >= 120) {
      minutes = Math.floor(value / 60);
      return minutes + "m " + Math.round(value - minutes * 60) + "s";
    }
    return String(Math.round(value));
  }
  function valueClass(metric) {
    var value = metric.current;
    var threshold = metric.thr;
    if (!isNumber(value) || !threshold) { return ""; }
    if (isNumber(threshold.bad) && value >= threshold.bad) { return "bad"; }
    if (isNumber(threshold.warn) && value >= threshold.warn) { return "warn"; }
    return "";
  }
  function pointX(index, count, times, useTime, minTime, maxTime, width) {
    var part = useTime ? (times[index] - minTime) / (maxTime - minTime) :
      (count > 1 ? index / (count - 1) : 0.5);
    return PAD + part * Math.max(0, width - PAD * 2);
  }
  function pointY(value, minValue, maxValue, height) {
    if (maxValue === minValue) { return height / 2; }
    return PAD + (maxValue - value) / (maxValue - minValue) *
      Math.max(0, height - PAD * 2);
  }
  function drawSpark(canvas, times, values) {
    var width;
    var height = Math.max(0, Math.floor(canvas.clientHeight)) || SPARK_HEIGHT;
    var ratio = Math.max(1, window.devicePixelRatio || 1);
    var ctx;
    var ink;
    var minValue = Infinity;
    var maxValue = -Infinity;
    var minTime;
    var maxTime;
    var useTime = times.length >= values.length && values.length > 1;
    var last = -1;
    var start;
    var i;
    var j;
    canvas.style.width = "";
    width = Math.max(0, Math.floor(canvas.clientWidth));
    canvas.style.width = width + "px";
    canvas.style.height = height + "px";
    if (canvas.width !== Math.round(width * ratio)) {
      canvas.width = Math.round(width * ratio);
    }
    if (canvas.height !== Math.round(height * ratio)) {
      canvas.height = Math.round(height * ratio);
    }
    ctx = canvas.getContext("2d");
    if (!ctx) { return; }
    ctx.setTransform(ratio, 0, 0, ratio, 0, 0);
    ctx.clearRect(0, 0, width, height);
    ink = window.getComputedStyle(canvas).getPropertyValue("--ink").trim() ||
      "#0d0d0d";
    for (i = 0; i < values.length; i++) {
      if (isNumber(values[i])) {
        minValue = Math.min(minValue, values[i]);
        maxValue = Math.max(maxValue, values[i]);
        last = i;
      }
      if (useTime && !isNumber(times[i])) { useTime = false; }
    }
    if (useTime) {
      minTime = times[0];
      maxTime = times[values.length - 1];
      if (maxTime <= minTime) { useTime = false; }
    }
    ctx.fillStyle = ink;
    ctx.strokeStyle = ink;
    ctx.lineWidth = 1.25;
    for (i = 0; i < values.length;) {
      while (i < values.length && !isNumber(values[i])) { i++; }
      start = i;
      while (i < values.length && isNumber(values[i])) { i++; }
      if (i - start < 2) { continue; }
      ctx.globalAlpha = 0.04;
      ctx.beginPath();
      ctx.moveTo(pointX(start, values.length, times, useTime, minTime, maxTime,
        width), height);
      for (j = start; j < i; j++) {
        ctx.lineTo(pointX(j, values.length, times, useTime, minTime, maxTime,
          width), pointY(values[j], minValue, maxValue, height));
      }
      ctx.lineTo(pointX(i - 1, values.length, times, useTime, minTime, maxTime,
        width), height);
      ctx.closePath();
      ctx.fill();
      ctx.globalAlpha = 1;
      ctx.beginPath();
      for (j = start; j < i; j++) {
        ctx[j === start ? "moveTo" : "lineTo"](
          pointX(j, values.length, times, useTime, minTime, maxTime, width),
          pointY(values[j], minValue, maxValue, height));
      }
      ctx.stroke();
    }
    if (last !== -1) {
      ctx.beginPath();
      ctx.arc(pointX(last, values.length, times, useTime, minTime, maxTime,
        width), pointY(values[last], minValue, maxValue, height), 2.5, 0,
        Math.PI * 2);
      ctx.fill();
    }
  }
  function makeRecord(state, key) {
    var panel = document.createElement("button");
    var label = document.createElement("span");
    var current = document.createElement("span");
    var value = document.createTextNode("");
    var unit = document.createElement("span");
    var record;
    panel.type = "button";
    panel.className = "qmulti-panel";
    label.className = "qmulti-label";
    current.className = "qmulti-cur";
    unit.className = "qmulti-unit";
    current.appendChild(value);
    current.appendChild(unit);
    panel.appendChild(label);
    panel.appendChild(current);
    record = { key: key, panel: panel, label: label, current: current,
      value: value, unit: unit, visual: null, used: true };
    panel.addEventListener("click", function () {
      var callback = state.model && state.model.onSelect;
      if (typeof callback === "function") { callback(key); }
    });
    return record;
  }
  function findRecord(records, key) {
    var i;
    for (i = 0; i < records.length; i++) {
      if (!records[i].used && records[i].key === key) { return records[i]; }
    }
    return null;
  }
  function setVisual(record, showCanvas) {
    var visual;
    var wanted = showCanvas ? "canvas" : "span";
    if (record.visual && record.visual.nodeName.toLowerCase() === wanted) {
      return;
    }
    if (record.visual) { record.panel.removeChild(record.visual); }
    visual = document.createElement(wanted);
    visual.className = showCanvas ? "qmulti-spark" : "qmulti-empty";
    if (showCanvas) {
      visual.setAttribute("aria-hidden", "true");
    } else {
      visual.textContent = "—";
    }
    record.visual = visual;
    record.panel.appendChild(visual);
  }
  function patchRecord(record, metric, selected) {
    var series = Array.isArray(metric.series) ? metric.series : [];
    var times = Array.isArray(series[0]) ? series[0] : [];
    var values = Array.isArray(series[1]) ? series[1] : [];
    var formatted = formatCurrent(metric.current, metric.unit);
    var stateClass = valueClass(metric);
    var nonNull = 0;
    var i;
    record.label.textContent = metric.label;
    record.value.nodeValue = formatted;
    record.unit.textContent = (metric.unit === "s" &&
      isNumber(metric.current) && metric.current >= 120)
      ? "" : metric.unit;
    record.current.className = "qmulti-cur" + (stateClass ? " " + stateClass : "");
    record.panel.className = "qmulti-panel" +
      (selected === metric.key ? " on" : "");
    record.panel.setAttribute("aria-pressed", selected === metric.key ?
      "true" : "false");
    record.panel.title = metric.label + " \u00b7 " + formatted +
      record.unit.textContent + " \u00b7 " + values.length + " samples";
    for (i = 0; i < values.length; i++) {
      if (isNumber(values[i])) { nonNull++; }
    }
    setVisual(record, nonNull >= 2);
    if (nonNull >= 2) { drawSpark(record.visual, times, values); }
  }
  function updateState(state, model) {
    var metrics;
    var record;
    var i;
    if (state.dead) { return; }
    state.model = model || {};
    metrics = Array.isArray(state.model.metrics) ? state.model.metrics : [];
    for (i = 0; i < state.records.length; i++) { state.records[i].used = false; }
    for (i = 0; i < metrics.length; i++) {
      record = findRecord(state.records, metrics[i].key);
      if (!record) {
        record = makeRecord(state, metrics[i].key);
        state.records.push(record);
      }
      record.used = true;
      state.root.appendChild(record.panel);
      patchRecord(record, metrics[i], state.model.selected);
    }
    for (i = state.records.length - 1; i >= 0; i--) {
      record = state.records[i];
      if (!record.used) {
        record.panel.parentNode.removeChild(record.panel);
        state.records.splice(i, 1);
      }
    }
  }
  function mount(el, model) {
    var root = document.createElement("div");
    var state = { root: root, records: [], model: null, dead: false };
    root.className = "qmulti";
    el.appendChild(root);
    updateState(state, model);
    return {
      update: function (nextModel) { updateState(state, nextModel); },
      destroy: function () {
        if (state.dead) { return; }
        state.dead = true;
        if (root.parentNode) { root.parentNode.removeChild(root); }
        state.records = [];
      }
    };
  }
  window.QNet = window.QNet || {};
  window.QNet.multi = { mount: mount };
})();
