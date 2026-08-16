"use strict";

(function () {
  /* bar grammar measured off status.openai.com (2026-08-15): bars
     fill 68% of an even slot across the full row, with 15px height and
     full state colors — bars are history DATA, so they carry the real palette */
  var BAR_FILL = 0.68;
  var BAR_HEIGHT = 15;
  var BAR_RADIUS = 0.5;
  var CAPACITY = 90;
  var COLORS = {
    ok: "#24c19a",
    warn: "#fbbf24",
    bad: "#f87171"
  };

  function drawBar(ctx, x, y, width, color, alpha) {
    var right = x + width;
    var bottom = y + BAR_HEIGHT;

    ctx.fillStyle = color;
    ctx.globalAlpha = alpha;
    ctx.beginPath();
    ctx.moveTo(x + BAR_RADIUS, y);
    ctx.lineTo(right - BAR_RADIUS, y);
    ctx.quadraticCurveTo(right, y, right, y + BAR_RADIUS);
    ctx.lineTo(right, bottom - BAR_RADIUS);
    ctx.quadraticCurveTo(right, bottom, right - BAR_RADIUS, bottom);
    ctx.lineTo(x + BAR_RADIUS, bottom);
    ctx.quadraticCurveTo(x, bottom, x, bottom - BAR_RADIUS);
    ctx.lineTo(x, y + BAR_RADIUS);
    ctx.quadraticCurveTo(x, y, x + BAR_RADIUS, y);
    ctx.closePath();
    ctx.fill();
  }

  function render(el, samples) {
    var values = Array.isArray(samples) ? samples : [];
    var start = Math.max(0, values.length - CAPACITY);
    var count = values.length - start;
    var canvas = null;
    var width;
    var height;
    var ratio;
    var ctx;
    var neutral;
    var slotWidth;
    var barWidth;
    var gap;
    var x;
    var y;
    var i;
    var sample;

    width = Math.max(0, Math.floor(el.clientWidth));
    height = Math.max(BAR_HEIGHT, Math.floor(el.clientHeight));
    el.title = "last " + count + " samples · newest right";

    if (el.childNodes.length === 1 &&
        el.firstChild.nodeName.toLowerCase() === "canvas" &&
        el.firstChild.getAttribute("data-qnet-bars") === "") {
      canvas = el.firstChild;
    } else {
      el.textContent = "";
      canvas = document.createElement("canvas");
      canvas.setAttribute("data-qnet-bars", "");
      canvas.setAttribute("aria-hidden", "true");
      canvas.style.display = "block";
      el.appendChild(canvas);
    }

    ratio = Math.max(1, window.devicePixelRatio || 1);

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

    neutral = window.getComputedStyle(el)
      .getPropertyValue("--st-line").trim() || "#e5e7eb";
    slotWidth = count ? width / count : 0;
    barWidth = slotWidth * BAR_FILL;
    gap = count > 1 ? (width - count * barWidth) / (count - 1) : 0;
    x = count === 1 ? (width - barWidth) / 2 : 0;
    y = Math.floor((height - BAR_HEIGHT) / 2);

    for (i = start; i < values.length; i++) {
      sample = values[i];
      drawBar(ctx, x, y, barWidth, COLORS[sample] || neutral,
        sample === null ? 0.25 : 1);
      x += barWidth + gap;
    }
    ctx.globalAlpha = 1;
  }

  window.QNet = window.QNet || {};
  window.QNet.bars = { render: render };
})();
