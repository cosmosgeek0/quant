/* Quant network time-series instrument, drawn by the vendored uPlot. */
"use strict";

(function (root) {
  var QNet = root.QNet = root.QNet || {};

  if (QNet.chart && typeof QNet.chart.mount === "function") { return; }

  var OK = "#16a34a";
  var WARN = "#d4a406";
  var BAD = "#cc2f3c";
  var UTC_STEPS = [
    0.001, 0.002, 0.005, 0.01, 0.02, 0.05, 0.1, 0.2, 0.5,
    1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800,
    3600, 7200, 10800, 21600, 43200, 86400, 172800, 604800,
    1209600, 2592000, 7776000, 15552000, 31536000
  ];
  var instances = [];

  function two(n) { return n < 10 ? "0" + n : "" + n; }

  function finiteNumber(value) {
    return typeof value === "number" && isFinite(value);
  }

  function checkedPoints(points) {
    if (!Array.isArray(points) || points.length !== 2 ||
        !Array.isArray(points[0]) || !Array.isArray(points[1]) ||
        points[0].length !== points[1].length) {
      throw new TypeError("QNet.chart points must be two equal-length arrays");
    }
    return points;
  }

  function checkedThreshold(value, name) {
    if (value == null) { return null; }
    if (!finiteNumber(value)) {
      throw new TypeError("QNet.chart " + name + " threshold must be a number or null");
    }
    return value;
  }

  function checkedThresholds(thresholds) {
    thresholds = thresholds || {};
    return {
      warn: checkedThreshold(thresholds.warn, "warn"),
      bad: checkedThreshold(thresholds.bad, "bad")
    };
  }

  function alphaColor(color, alpha) {
    var value = String(color || "").replace(/^\s+|\s+$/g, "");
    var match = /^#([0-9a-f]{3})$/i.exec(value);
    var r;
    var g;
    var b;

    if (match) {
      r = parseInt(match[1].charAt(0) + match[1].charAt(0), 16);
      g = parseInt(match[1].charAt(1) + match[1].charAt(1), 16);
      b = parseInt(match[1].charAt(2) + match[1].charAt(2), 16);
      return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
    }

    match = /^#([0-9a-f]{6})$/i.exec(value);
    if (match) {
      r = parseInt(match[1].slice(0, 2), 16);
      g = parseInt(match[1].slice(2, 4), 16);
      b = parseInt(match[1].slice(4, 6), 16);
      return "rgba(" + r + "," + g + "," + b + "," + alpha + ")";
    }

    match = /^rgba?\(\s*([\d.]+)\s*,\s*([\d.]+)\s*,\s*([\d.]+)/i.exec(value);
    if (match) {
      return "rgba(" + match[1] + "," + match[2] + "," + match[3] +
        "," + alpha + ")";
    }

    return value;
  }

  function cssValue(styles, token, fallback) {
    var value = styles.getPropertyValue(token).replace(/^\s+|\s+$/g, "");
    return value || fallback;
  }

  function cleanMax(value) {
    if (!(value > 0) || !isFinite(value)) { return 1; }
    var power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var scaled = value / power;
    if (scaled >= 10 - 1e-12) {
      power *= 10;
      scaled = value / power;
    }
    var step = scaled < 1 ? 1 : (scaled < 2 ? 2 : (scaled < 5 ? 5 : 10));
    return step * power;
  }

  function numberLabel(value) {
    if (!finiteNumber(value)) { return ""; }
    if (value === 0) { return "0"; }
    return String(Number(value.toPrecision(4)));
  }

  function utcHm(value) {
    var date = new Date(value * 1000);
    if (isNaN(date.getTime())) { return ""; }
    return two(date.getUTCHours()) + ":" + two(date.getUTCMinutes());
  }

  function utcHms(value) {
    var date = new Date(value * 1000);
    if (isNaN(date.getTime())) { return ""; }
    return two(date.getUTCHours()) + ":" + two(date.getUTCMinutes()) +
      ":" + two(date.getUTCSeconds());
  }

  function removeInstance(handle) {
    for (var i = instances.length - 1; i >= 0; i--) {
      if (instances[i].handle === handle) { instances.splice(i, 1); }
    }
  }

  function mount(el, cfg) {
    if (!el || el.nodeType !== 1) {
      throw new TypeError("QNet.chart.mount requires an element");
    }
    if (!root.uPlot) { throw new Error("QNet.chart requires window.uPlot"); }

    cfg = cfg || {};
    var currentPoints = checkedPoints(cfg.points);
    var currentThresholds = checkedThresholds(cfg.thresholds);
    var unit = cfg.unit;
    if (unit !== "ms" && unit !== "s") {
      throw new TypeError("QNet.chart unit must be ms or s");
    }

    var height = cfg.height == null ? 220 : cfg.height;
    if (!finiteNumber(height) || height < 1 || Math.floor(height) !== height) {
      throw new TypeError("QNet.chart height must be a positive integer");
    }

    for (var i = instances.length - 1; i >= 0; i--) {
      if (instances[i].el === el) { instances[i].handle.destroy(); }
    }

    var styles = root.getComputedStyle(el);
    var ink = cssValue(styles, "--ink", styles.color);
    var muted = cssValue(styles, "--muted", alphaColor(ink, 0.62));
    var line = cssValue(styles, "--line", alphaColor(ink, 0.07));
    var line2 = cssValue(styles, "--line-2", alphaColor(ink, 0.12));
    var mono = cssValue(styles, "--mono", styles.fontFamily);
    /* one sage data line with a soft body under it — the owner's
       reference chart grammar; state colors stay on the guides */
    var seriesColor = cssValue(styles, "--st-data", "#57b47a");
    var seriesFill = cssValue(styles, "--st-data-fill",
      "rgba(87,180,122,0.10)");
    var warnGuide = alphaColor(WARN, 0.45);
    var badGuide = alphaColor(BAD, 0.45);
    var axisFont = "12px " + cssValue(styles, "--sans",
      "InterVariable, system-ui, sans-serif");
    var width = Math.max(280, Math.round(el.clientWidth || 0));
    var destroyed = false;
    var hovering = false;
    var overEl = null;
    var tooltipHost = null;
    var crosshairEl = null;
    var plot = null;

    var tooltip = document.createElement("div");
    var tooltipText = document.createTextNode("");
    var tooltipState = document.createElement("span");
    tooltip.className = "qnet-chart-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    tooltip.style.position = "absolute";
    tooltip.style.zIndex = "4";
    tooltip.style.display = "none";
    tooltip.style.pointerEvents = "none";
    tooltip.style.whiteSpace = "nowrap";
    tooltip.style.padding = "4px 7px";
    tooltip.style.border = "1px solid var(--line-2)";
    tooltip.style.borderRadius = "6px";
    tooltip.style.background = "var(--panel)";
    tooltip.style.color = "var(--ink)";
    tooltip.style.fontFamily = "var(--mono)";
    tooltip.style.fontSize = "10.5px";
    tooltip.style.fontWeight = "400";
    tooltip.style.lineHeight = "1.35";
    tooltip.style.fontVariantNumeric = "tabular-nums lining-nums";
    tooltip.appendChild(tooltipText);
    tooltip.appendChild(tooltipState);

    function highestValue(dataMax) {
      var highest = finiteNumber(dataMax) ? Math.max(0, dataMax) : 0;
      var values = currentPoints[1];
      for (var j = 0; j < values.length; j++) {
        if (finiteNumber(values[j]) && values[j] > highest) {
          highest = values[j];
        }
      }
      return highest;
    }

    /* the axis belongs to the DATA (owner law, 2026-08-15): fit the
       range to what was measured plus headroom. Thresholds never own
       the scale — a healthy 20ms series must fill the panel, not sit
       crushed under an empty 1000ms "safety" ceiling. Guides draw
       only when they fall inside the fitted range. */
    function yRange(u, dataMin, dataMax) {
      return [0, cleanMax(highestValue(dataMax) * 1.15)];
    }

    function xValues(u, splits) {
      var labels = [];
      for (var j = 0; j < splits.length; j++) {
        labels.push(finiteNumber(splits[j]) ? utcHm(splits[j]) : "");
      }
      return labels;
    }

    function xSplits(u, axisIdx, scaleMin, scaleMax) {
      if (!finiteNumber(scaleMin) || !finiteNumber(scaleMax)) { return []; }
      if (scaleMax <= scaleMin) { return [scaleMin]; }

      var chosen = null;
      var bestScore = Infinity;
      var j;
      for (j = 0; j < UTC_STEPS.length; j++) {
        var step = UTC_STEPS[j];
        var tolerance = step * 1e-9;
        var first = Math.ceil((scaleMin - tolerance) / step) * step;
        var count = Math.floor((scaleMax - first + tolerance) / step) + 1;
        if (count >= 4 && count <= 6 && Math.abs(count - 5) < bestScore) {
          chosen = { first: first, step: step, count: count };
          bestScore = Math.abs(count - 5);
          if (bestScore === 0) { break; }
        }
      }

      var splits = [];
      if (chosen) {
        for (j = 0; j < chosen.count; j++) {
          splits.push(Number((chosen.first + j * chosen.step).toPrecision(15)));
        }
        return splits;
      }

      var interval = (scaleMax - scaleMin) / 4;
      for (j = 0; j < 5; j++) {
        splits.push(j === 4 ? scaleMax : scaleMin + interval * j);
      }
      return splits;
    }

    function ySplits(u, axisIdx, scaleMin, scaleMax) {
      if (!finiteNumber(scaleMax) || scaleMax <= 0) { return [0, 0.5, 1]; }
      return [0, scaleMax / 2, scaleMax];
    }

    function yValues(u, splits) {
      var labels = [];
      var top = -1;
      var j;
      for (j = splits.length - 1; j >= 0; j--) {
        if (finiteNumber(splits[j])) { top = j; break; }
      }
      for (j = 0; j < splits.length; j++) {
        labels.push(finiteNumber(splits[j])
          ? numberLabel(splits[j]) + (j === top ? unit : "") : "");
      }
      return labels;
    }

    function xSpace(u, axisIdx, scaleMin, scaleMax, plotDim) {
      return Math.max(42, plotDim / 5);
    }

    function hideTooltip() {
      tooltip.style.display = "none";
      tooltip.style.visibility = "hidden";
      tooltip.setAttribute("aria-hidden", "true");
    }

    function stateFor(value) {
      if (currentThresholds.bad != null && value >= currentThresholds.bad) {
        return { label: "OUTAGE", color: BAD };
      }
      if (currentThresholds.warn != null && value >= currentThresholds.warn) {
        return { label: "DEGRADED", color: WARN };
      }
      return { label: "OK", color: OK };
    }

    function enterPlot() { hovering = true; }

    function leavePlot() {
      hovering = false;
      hideTooltip();
    }

    function updateTooltip(u) {
      var idx = u.cursor.idx;
      var times = currentPoints[0];
      var values = currentPoints[1];
      if (!hovering || idx == null || idx < 0 || idx >= values.length ||
          !finiteNumber(times[idx]) || !finiteNumber(values[idx])) {
        hideTooltip();
        return;
      }

      var timeText = utcHms(times[idx]);
      if (!timeText) { hideTooltip(); return; }
      var state = stateFor(values[idx]);
      tooltipText.nodeValue = timeText + " UTC \u00b7 " + values[idx] + unit +
        " \u00b7 ";
      tooltipState.textContent = state.label;
      tooltipState.style.color = state.color;
      tooltip.style.display = "block";
      tooltip.style.visibility = "hidden";
      tooltip.setAttribute("aria-hidden", "false");

      var hostWidth = tooltipHost.clientWidth || width;
      var hostHeight = tooltipHost.clientHeight || height;
      var anchorLeft = overEl.offsetLeft + u.cursor.left;
      var anchorTop = overEl.offsetTop + u.valToPos(values[idx], "y");
      var left = anchorLeft + 12;
      var top = anchorTop - tooltip.offsetHeight - 9;
      if (left + tooltip.offsetWidth > hostWidth - 4) {
        left = anchorLeft - tooltip.offsetWidth - 12;
      }
      if (top < 4) { top = anchorTop + 9; }
      left = Math.max(4, Math.min(left, hostWidth - tooltip.offsetWidth - 4));
      top = Math.max(4, Math.min(top, hostHeight - tooltip.offsetHeight - 4));
      tooltip.style.left = Math.round(left) + "px";
      tooltip.style.top = Math.round(top) + "px";
      tooltip.style.visibility = "visible";
    }

    function thresholdY(u, value) {
      /* honest position only — a guide outside the fitted scale is
         skipped by the callers, never clamped to a fake edge line */
      return u.valToPos(value, "y", true);
    }

    function drawThresholds(u) {
      var warn = currentThresholds.warn;
      var bad = currentThresholds.bad;
      if (warn == null && bad == null) { return; }

      var ctx = u.ctx;
      var box = u.bbox;
      var ratio = root.uPlot.pxRatio || root.devicePixelRatio || 1;
      var top = box.top;
      var bottom = box.top + box.height;
      var warnY = warn == null ? null : thresholdY(u, warn);
      var badY = bad == null ? null : thresholdY(u, bad);

      ctx.save();
      ctx.beginPath();
      ctx.rect(box.left, box.top, box.width, box.height);
      ctx.clip();

      ctx.lineWidth = ratio;
      ctx.setLineDash([4 * ratio, 3 * ratio]);
      if (warnY != null && warnY >= top && warnY <= bottom) {
        ctx.beginPath();
        ctx.strokeStyle = warnGuide;
        ctx.moveTo(box.left, warnY);
        ctx.lineTo(box.left + box.width, warnY);
        ctx.stroke();
      }
      if (badY != null && badY >= top && badY <= bottom) {
        ctx.beginPath();
        ctx.strokeStyle = badGuide;
        ctx.moveTo(box.left, badY);
        ctx.lineTo(box.left + box.width, badY);
        ctx.stroke();
      }
      ctx.restore();
    }

    function drawLastPoint(u) {
      var times = currentPoints[0];
      var values = currentPoints[1];
      var idx = values.length - 1;
      while (idx >= 0 && !finiteNumber(values[idx])) { idx--; }
      if (idx < 0 || !finiteNumber(times[idx])) { return; }

      var ratio = root.uPlot.pxRatio || root.devicePixelRatio || 1;
      var x = u.valToPos(times[idx], "x", true);
      var y = u.valToPos(values[idx], "y", true);
      u.ctx.save();
      u.ctx.beginPath();
      u.ctx.fillStyle = seriesColor;
      u.ctx.arc(x, y, 3 * ratio, 0, Math.PI * 2);
      u.ctx.fill();
      u.ctx.restore();
    }

    var options = {
      width: width,
      height: height,
      padding: [8, 10, 0, 6],
      legend: { show: false },
      tzDate: function (timestamp) {
        var date = new Date(timestamp * 1000);
        return typeof root.uPlot.tzDate === "function"
          ? root.uPlot.tzDate(date, "UTC") : date;
      },
      cursor: {
        show: true,
        x: true,
        y: false,
        points: { show: false },
        drag: { x: false, y: false, setScale: false }
      },
      scales: {
        x: { time: true },
        y: { range: yRange }
      },
      axes: [
        {
          stroke: muted,
          font: axisFont,
          size: 30,
          space: xSpace,
          splits: xSplits,
          values: xValues,
          grid: { show: true, stroke: line, width: 1 },
          ticks: { show: false },
          border: { show: false }
        },
        {
          stroke: muted,
          font: axisFont,
          size: 56,
          splits: ySplits,
          values: yValues,
          grid: { show: true, stroke: line, width: 1 },
          ticks: { show: false },
          border: { show: false }
        }
      ],
      series: [
        {},
        {
          stroke: seriesColor,
          fill: seriesFill,
          width: 1.8,
          spanGaps: false,
          points: { show: false }
        }
      ],
      hooks: {
        ready: [function (u) {
          overEl = u.over;
          tooltipHost = overEl.parentNode;
          tooltipHost.appendChild(tooltip);
          crosshairEl = overEl.querySelector(".u-cursor-x");
          if (crosshairEl) {
            crosshairEl.style.borderRight = "1px solid " + line2;
          }
          overEl.addEventListener("mouseenter", enterPlot);
          overEl.addEventListener("mouseleave", leavePlot);
        }],
        setCursor: [updateTooltip],
        drawClear: [drawThresholds],
        draw: [drawLastPoint]
      }
    };

    plot = new root.uPlot(options, currentPoints, el);

    var handle = {
      setData: function (points, thresholds) {
        if (destroyed) { return; }
        var nextPoints = checkedPoints(points);
        var nextThresholds = checkedThresholds(thresholds);
        currentPoints = nextPoints;
        currentThresholds = nextThresholds;
        plot.setData(currentPoints);
      },
      resize: function () {
        if (destroyed) { return; }
        width = Math.max(280, Math.round(el.clientWidth || 0));
        plot.setSize({ width: width, height: height });
      },
      destroy: function () {
        if (destroyed) { return; }
        destroyed = true;
        if (overEl) {
          overEl.removeEventListener("mouseenter", enterPlot);
          overEl.removeEventListener("mouseleave", leavePlot);
        }
        hideTooltip();
        if (tooltip.parentNode) { tooltip.parentNode.removeChild(tooltip); }
        plot.destroy();
        removeInstance(handle);
        plot = null;
        overEl = null;
        tooltipHost = null;
        crosshairEl = null;
      }
    };

    instances.push({ el: el, handle: handle });
    return handle;
  }

  /* ---- overview overlay: every loop on one granular instrument.
     Two honest scales (ages in s left, RTT in ms right), full
     resolution, cursor readout listing every visible series value
     at the hovered moment. ---- */
  function mountMulti(el, cfg) {
    if (!el) { throw new TypeError("QNet.chart.mountMulti needs a host"); }
    var defs = cfg.series || [];
    var height = cfg.height || 260;
    var styles = root.getComputedStyle(el);
    var ink = cssValue(styles, "--st-ink", styles.color);
    var muted = cssValue(styles, "--st-mid", ink);
    var line = cssValue(styles, "--st-divider", "#f0f0f2");
    var sans = cssValue(styles, "--sans", "InterVariable, sans-serif");
    var axisFont = "12px " + sans;
    var width = Math.max(320, Math.round(el.clientWidth || 0));

    var data = [cfg.times || []];
    for (var i = 0; i < defs.length; i += 1) { data.push(defs[i].values); }

    function rangeUp(u, dataMin, dataMax) {
      var hi = (typeof dataMax === "number" && isFinite(dataMax) &&
        dataMax > 0) ? dataMax * 1.12 : 1;
      return [0, hi];
    }

    var series = [{}];
    for (var d = 0; d < defs.length; d += 1) {
      series.push({
        label: defs[d].label,
        stroke: defs[d].color,
        width: 1.4,
        scale: defs[d].unit === "ms" ? "ms" : "s",
        spanGaps: false,
        points: { show: false }
      });
    }

    var tooltip = document.createElement("div");
    tooltip.className = "st-graph-tip";
    tooltip.style.display = "none";
    tooltip.setAttribute("aria-hidden", "true");

    var plot = null;
    var destroyed = false;

    function fmtVal(v, unit) {
      if (v == null || !isFinite(v)) { return null; }
      return (Math.round(v * 10) / 10) + " " + unit;
    }

    function renderTip(u) {
      var idx = u.cursor.idx;
      if (idx == null || idx < 0 || idx >= data[0].length) {
        tooltip.style.display = "none";
        return;
      }
      var t = data[0][idx];
      if (!isFinite(t)) { tooltip.style.display = "none"; return; }
      var dt = new Date(t * 1000);
      var stamp = two(dt.getUTCHours()) + ":" + two(dt.getUTCMinutes()) +
        ":" + two(dt.getUTCSeconds());
      var rows = [];
      for (var s = 0; s < defs.length; s += 1) {
        if (!u.series[s + 1].show) { continue; }
        var val = fmtVal(data[s + 1][idx], defs[s].unit);
        if (val == null) { continue; }
        rows.push({ color: defs[s].color, label: defs[s].label, val: val });
      }
      if (!rows.length) { tooltip.style.display = "none"; return; }
      tooltip.textContent = "";
      var head = document.createElement("div");
      head.className = "st-graph-tip-time";
      head.textContent = stamp + " UTC";
      tooltip.appendChild(head);
      for (var r2 = 0; r2 < rows.length; r2 += 1) {
        var rowEl = document.createElement("div");
        rowEl.className = "st-graph-tip-row";
        var key = document.createElement("span");
        key.className = "st-graph-tip-key";
        key.style.background = rows[r2].color;
        rowEl.appendChild(key);
        var lbl = document.createElement("span");
        lbl.className = "st-graph-tip-label";
        lbl.textContent = rows[r2].label;
        rowEl.appendChild(lbl);
        var vv = document.createElement("span");
        vv.className = "st-graph-tip-val";
        vv.textContent = rows[r2].val;
        rowEl.appendChild(vv);
        tooltip.appendChild(rowEl);
      }
      var left = u.cursor.left;
      var top = u.cursor.top;
      if (left == null || top == null || left < 0) {
        tooltip.style.display = "none";
        return;
      }
      tooltip.style.display = "block";
      var host = u.over.parentNode;
      var hw = host.clientWidth;
      var tw = tooltip.offsetWidth;
      var x = u.over.offsetLeft + left + 14;
      if (x + tw > hw - 6) { x = u.over.offsetLeft + left - tw - 14; }
      tooltip.style.left = Math.max(6, Math.round(x)) + "px";
      var y = u.over.offsetTop + top - 10;
      var hh = host.clientHeight;
      var th = tooltip.offsetHeight;
      if (y + th > hh - 4) { y = hh - th - 4; }
      tooltip.style.top = Math.max(6, Math.round(y)) + "px";
    }

    var hasS = false;
    var hasMs = false;
    for (var ud = 0; ud < defs.length; ud += 1) {
      if (defs[ud].unit === "ms") { hasMs = true; } else { hasS = true; }
    }
    function unitAxis(scaleName, unitText, side) {
      var axis = {
        scale: scaleName, stroke: muted, font: axisFont,
        size: 52,
        space: cfg.compact ? 34 : 40,
        values: function (u, splits) {
          var out = [];
          for (var j = 0; j < splits.length; j += 1) {
            out.push(splits[j] + unitText);
          }
          return out;
        },
        grid: side ? { show: false }
          : { show: true, stroke: line, width: 1 },
        ticks: { show: false }, border: { show: false }
      };
      if (side) { axis.side = 1; }
      return axis;
    }
    var axes = [
      { stroke: muted, font: axisFont, size: cfg.compact ? 26 : 30,
        space: cfg.compact ? 88 : 78,
        grid: { show: true, stroke: line, width: 1 },
        ticks: { show: false }, border: { show: false } }
    ];
    if (hasS) { axes.push(unitAxis("s", "s", false)); }
    if (hasMs) { axes.push(unitAxis("ms", "ms", hasS)); }

    var options = {
      width: width,
      height: height,
      padding: [10, 8, 0, 0],
      legend: { show: false },
      tzDate: function (timestamp) {
        var date = new Date(timestamp * 1000);
        return typeof root.uPlot.tzDate === "function"
          ? root.uPlot.tzDate(date, "UTC") : date;
      },
      cursor: {
        show: true, x: true, y: false,
        points: { show: false },
        drag: { x: false, y: false, setScale: false }
      },
      scales: {
        x: { time: true },
        s: { range: rangeUp },
        ms: { range: rangeUp }
      },
      axes: axes,
      series: series,
      hooks: {
        ready: [function (u) {
          u.over.parentNode.appendChild(tooltip);
          u.over.addEventListener("mouseleave", function () {
            tooltip.style.display = "none";
          });
        }],
        setCursor: [renderTip]
      }
    };

    el.textContent = "";
    plot = new root.uPlot(options, data, el);

    function handleResize() {
      if (destroyed) { return; }
      plot.setSize({
        width: Math.max(320, Math.round(el.clientWidth || 0)),
        height: height
      });
    }
    root.addEventListener("resize", handleResize);

    return {
      setData: function (times, defsNext) {
        data = [times];
        for (var q = 0; q < defsNext.length; q += 1) {
          data.push(defsNext[q].values);
        }
        plot.setData(data);
      },
      setShow: function (idx, on) {
        plot.setSeries(idx + 1, { show: !!on });
      },
      destroy: function () {
        destroyed = true;
        root.removeEventListener("resize", handleResize);
        if (tooltip.parentNode) {
          tooltip.parentNode.removeChild(tooltip);
        }
        plot.destroy();
      }
    };
  }

  QNet.chart = { mount: mount, mountMulti: mountMulti };
})(window);
