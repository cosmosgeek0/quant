/* Quant network stat-panel sparklines, drawn by the vendored uPlot. */
"use strict";

(function (root) {
  var QNet = root.QNet = root.QNet || {};

  if (QNet.spark && typeof QNet.spark.mount === "function") { return; }

  var instances = [];

  function two(n) { return n < 10 ? "0" + n : "" + n; }

  function cleanCeil(value) {
    if (!(value > 0) || !isFinite(value)) { return 1; }
    var power = Math.pow(10, Math.floor(Math.log(value) / Math.LN10));
    var scaled = value / power;
    var step = scaled <= 1 ? 1 : (scaled <= 2 ? 2 : (scaled <= 5 ? 5 : 10));
    return step * power;
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

    return alpha === 0 ? "transparent" : value;
  }

  function checkedData(data) {
    if (!Array.isArray(data) || data.length !== 2 ||
        !Array.isArray(data[0]) || !Array.isArray(data[1]) ||
        data[0].length !== data[1].length) {
      throw new TypeError("QNet.spark data must be two equal-length arrays");
    }
    return data;
  }

  function removeInstance(handle) {
    for (var i = instances.length - 1; i >= 0; i--) {
      if (instances[i].handle === handle) { instances.splice(i, 1); }
    }
  }

  function mount(el, cfg) {
    if (!el || el.nodeType !== 1) {
      throw new TypeError("QNet.spark.mount requires an element");
    }
    if (!root.uPlot) { throw new Error("QNet.spark requires window.uPlot"); }

    cfg = cfg || {};
    var data = checkedData(cfg.data);
    var unit = cfg.unit;
    if (unit !== "ms" && unit !== "s") {
      throw new TypeError("QNet.spark unit must be ms or s");
    }

    var height = cfg.height == null ? 86 : cfg.height;
    if (typeof height !== "number" || !isFinite(height) || height < 1) {
      throw new TypeError("QNet.spark height must be a positive number");
    }
    height = Math.round(height);

    for (var i = instances.length - 1; i >= 0; i--) {
      if (instances[i].el === el) { instances[i].handle.destroy(); }
    }

    var css = root.getComputedStyle(el);
    var accent = css.getPropertyValue("--focus").replace(/^\s+|\s+$/g, "");
    if (!accent) {
      accent = document.documentElement.getAttribute("data-theme") === "dark"
        ? "#7d9bff" : "#2557d6";
    }
    var fillTop = alphaColor(accent, 0.18);
    var fillBottom = alphaColor(accent, 0);
    var width = Math.max(200, Math.round(el.clientWidth || 0));
    var currentData = data;
    var destroyed = false;
    var hovering = false;
    var overEl = null;
    var tooltipHost = null;
    var plot = null;

    var tooltip = document.createElement("div");
    tooltip.className = "qnet-spark-tooltip";
    tooltip.setAttribute("role", "tooltip");
    tooltip.setAttribute("aria-hidden", "true");
    tooltip.style.position = "absolute";
    tooltip.style.zIndex = "3";
    tooltip.style.display = "none";
    tooltip.style.pointerEvents = "none";
    tooltip.style.whiteSpace = "nowrap";
    tooltip.style.padding = "3px 6px";
    tooltip.style.border = "1px solid var(--line)";
    tooltip.style.borderRadius = "6px";
    tooltip.style.background = "var(--panel)";
    tooltip.style.color = "var(--ink)";
    tooltip.style.fontFamily = "var(--mono)";
    tooltip.style.fontSize = "10.5px";
    tooltip.style.fontWeight = "400";
    tooltip.style.lineHeight = "1.35";
    tooltip.style.fontVariantNumeric = "tabular-nums lining-nums";

    function hideTooltip() {
      tooltip.style.display = "none";
      tooltip.style.visibility = "hidden";
      tooltip.setAttribute("aria-hidden", "true");
    }

    function enterPlot() { hovering = true; }

    function leavePlot() {
      hovering = false;
      hideTooltip();
    }

    function updateTooltip(u) {
      var idx = u.cursor.idx;
      var times = currentData[0];
      var values = currentData[1];
      if (!hovering || idx == null || idx < 0 || idx >= values.length ||
          values[idx] == null || !isFinite(values[idx]) ||
          times[idx] == null || !isFinite(times[idx])) {
        hideTooltip();
        return;
      }

      var at = new Date(times[idx] * 1000);
      if (isNaN(at.getTime())) { hideTooltip(); return; }
      tooltip.textContent = two(at.getUTCHours()) + ":" +
        two(at.getUTCMinutes()) + ":" + two(at.getUTCSeconds()) +
        " \u00b7 " + values[idx] + unit;
      tooltip.style.display = "block";
      tooltip.style.visibility = "hidden";
      tooltip.setAttribute("aria-hidden", "false");

      var hostWidth = tooltipHost.clientWidth || width;
      var hostHeight = tooltipHost.clientHeight || height;
      var anchorLeft = overEl.offsetLeft + u.cursor.left;
      var anchorTop = overEl.offsetTop + u.cursor.top;
      var left = anchorLeft + 10;
      var top = anchorTop - tooltip.offsetHeight - 8;
      if (left + tooltip.offsetWidth > hostWidth - 4) {
        left = anchorLeft - tooltip.offsetWidth - 10;
      }
      if (top < 4) { top = anchorTop + 8; }
      left = Math.max(4, Math.min(left, hostWidth - tooltip.offsetWidth - 4));
      top = Math.max(4, Math.min(top, hostHeight - tooltip.offsetHeight - 4));
      tooltip.style.left = Math.round(left) + "px";
      tooltip.style.top = Math.round(top) + "px";
      tooltip.style.visibility = "visible";
    }

    function drawLastPoint(u) {
      var times = currentData[0];
      var values = currentData[1];
      var idx = values.length - 1;
      while (idx >= 0 && (values[idx] == null || !isFinite(values[idx]))) {
        idx--;
      }
      if (idx < 0 || times[idx] == null || !isFinite(times[idx])) { return; }

      var ratio = root.uPlot.pxRatio || root.devicePixelRatio || 1;
      var x = u.valToPos(times[idx], "x", true);
      var y = u.valToPos(values[idx], "y", true);
      u.ctx.save();
      u.ctx.beginPath();
      u.ctx.fillStyle = accent;
      u.ctx.arc(x, y, 3 * ratio, 0, Math.PI * 2);
      u.ctx.fill();
      u.ctx.restore();
    }

    var options = {
      width: width,
      height: height,
      padding: [5, 5, 5, 5],
      legend: { show: false },
      cursor: {
        x: false,
        y: false,
        points: { show: false },
        drag: { x: false, y: false, setScale: false }
      },
      scales: {
        x: { time: true },
        y: {
          range: function (u, dataMin, dataMax) {
            var paddedMax = dataMax * 1.25 || 1;
            return [0, cleanCeil(paddedMax)];
          }
        }
      },
      axes: [{ show: false }, { show: false }],
      series: [
        {},
        {
          stroke: accent,
          width: 1.6,
          spanGaps: false,
          points: { show: false },
          fill: function (u, seriesIdx) {
            var gradient = u.ctx.createLinearGradient(
              0, u.bbox.top, 0, u.bbox.top + u.bbox.height);
            gradient.addColorStop(0, fillTop);
            gradient.addColorStop(1, fillBottom);
            return gradient;
          }
        }
      ],
      hooks: {
        ready: [function (u) {
          overEl = u.over;
          tooltipHost = overEl.parentNode;
          tooltipHost.appendChild(tooltip);
          overEl.addEventListener("mouseenter", enterPlot);
          overEl.addEventListener("mouseleave", leavePlot);
        }],
        setCursor: [updateTooltip],
        draw: [drawLastPoint]
      }
    };

    plot = new root.uPlot(options, currentData, el);

    var handle = {
      setData: function (nextData) {
        if (destroyed) { return; }
        currentData = checkedData(nextData);
        plot.setData(currentData);
      },
      resize: function () {
        if (destroyed) { return; }
        width = Math.max(200, Math.round(el.clientWidth || 0));
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
      }
    };

    instances.push({ el: el, handle: handle });
    return handle;
  }

  QNet.spark = { mount: mount };
})(window);
