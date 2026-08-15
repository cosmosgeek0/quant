/* net_v5.js — Network V5 proposal (branch lane/ui-network-v5).

   What changes and why (each move traced to a measured reference):
   - The rainbow overview dies. Replaced by a SCHEDULE RASTER (loop × time,
     worst sample per slice — the uptime-bar grammar the page already
     ships, stacked so "when was anything late" is one glance).
   - A WORST-FIRST TABLE carries the numbers a chart cannot: cadence, age,
     ×schedule, p95, late% (SmokePing / Wikimedia probes pattern: chart
     answers when, table answers who and how bad).
   - Small multiples become DISTRIBUTION panels: min–max envelope over
     time slices + median line + the loop's own cadence as a dashed guide
     (SmokePing's smoke, drawn in this page's ink).
   - A PATH STRIP states the real topology (MAC → TRANSPORT → HOST) with
     one dot per loop — ESnet's idea at instrument scale.
   - Color obeys DESIGN.md: ink everywhere; amber/red only on deviating
     values; history marks use the shipped bar palette; green is a dot,
     never a text.

   Self-contained: own fetches, own timers, own DOM. shell.js hands over
   the page container when this file is present; nothing else changes. */
"use strict";

(function (root) {
  if (root.NetV5) { return; }

  var RANGES = { "1h": 3600, "6h": 21600, "24h": 86400 };
  var BAR = { ok: "#24c19a", warn: "#fbbf24", bad: "#f87171" };
  var WARN_TXT = "#d4a406";
  var BAD_TXT = "#cc2f3c";

  /* the eight loops — names, details and tiers are the page's existing
     vocabulary (CONNECTIONS/NET_METRICS in shell.js), not new voices */
  var LOOPS = [
    { key: "server", name: "Web server", detail: "serve.py · loopback probe",
      tier: "MAC", age: "rtt_ms", beat: null, unit: "ms",
      fallback: { warn: 350, bad: 800 } },
    { key: "newsloop", name: "News fetch loop", detail: "104 public sources",
      tier: "MAC", age: "news_s", beat: "news_beat_s", unit: "s",
      fallback: { warn: 300, bad: 900 } },
    { key: "mirror", name: "Data mirror", detail: "host → Mac · SSM pull",
      tier: "TRANSPORT", age: "mirror_s", beat: "mirror_beat_s", unit: "s",
      fallback: { warn: 90, bad: 180 } },
    { key: "market", name: "Market feed", detail: "exchange stream pass · host",
      tier: "HOST", age: "market_s", beat: "market_beat_s", unit: "s",
      fallback: { warn: 45, bad: 120 } },
    { key: "supervisor", name: "Supervisor", detail: "machine heartbeat · host",
      tier: "HOST", age: "supervisor_s", beat: "supervisor_beat_s", unit: "s",
      fallback: { warn: 360, bad: 720 } },
    { key: "basiswatch", name: "Basis watch", detail: "basis pass loop · host",
      tier: "HOST", age: "basis_watch_s", beat: "basis_watch_beat_s",
      unit: "s", fallback: { warn: 360, bad: 720 } },
    { key: "basissup", name: "Basis supervisor",
      detail: "position supervision · host",
      tier: "HOST", age: "basis_sup_s", beat: "basis_sup_beat_s", unit: "s",
      fallback: { warn: 360, bad: 720 } },
    { key: "jobrunner", name: "Job runner", detail: "restricted jobs · 1m cron",
      tier: "HOST", age: "jobs_s", beat: "jobs_beat_s", unit: "s",
      fallback: { warn: 120, bad: 240 } }
  ];
  var PATH_TIERS = [
    ["MAC", ["server", "newsloop"]],
    ["TRANSPORT", ["mirror"]],
    ["HOST", ["market", "supervisor", "basiswatch", "basissup", "jobrunner"]]
  ];

  var state = null;

  /* ---------- tiny DOM + math helpers ---------- */

  function el(tag, cls, text) {
    var n = document.createElement(tag);
    if (cls) { n.className = cls; }
    if (text != null) { n.textContent = text; }
    return n;
  }

  function num(v) {
    return (typeof v === "number" && isFinite(v)) ? v : null;
  }

  function sorted(list) {
    var c = list.slice();
    c.sort(function (a, b) { return a - b; });
    return c;
  }

  function quantile(sortedVals, p) {
    if (!sortedVals.length) { return null; }
    var idx = Math.min(sortedVals.length - 1,
      Math.max(0, Math.round(p * (sortedVals.length - 1))));
    return sortedVals[idx];
  }

  function fmtDur(s) {
    if (s == null) { return "—"; }
    if (s < 0) { s = 0; }
    if (s < 100) { return Math.round(s) + "s"; }
    var m = Math.floor(s / 60);
    var r = Math.round(s - m * 60);
    if (r === 60) { m += 1; r = 0; }
    if (m < 60) { return m + "m " + r + "s"; }
    var h = Math.floor(m / 60);
    return h + "h " + (m - h * 60) + "m";
  }

  function fmtVal(v, unit) {
    if (v == null) { return "—"; }
    if (unit === "ms") { return Math.round(v) + " ms"; }
    return fmtDur(v);
  }

  function fmtRatio(r) {
    if (r == null) { return "—"; }
    /* two decimals always: an instrument never lets ×1.95 read as ×2 */
    return "×" + r.toFixed(2);
  }

  function statusWord(cls) {
    return cls === "ok" ? "Operational"
      : cls === "warn" ? "Degraded"
      : cls === "bad" ? "Outage" : "Measuring";
  }

  function css(name, fb) {
    var v = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return v || fb;
  }

  function sizeCanvas(cv) {
    var r = Math.max(1, root.devicePixelRatio || 1);
    var w = Math.max(10, cv.clientWidth);
    var h = Math.max(10, cv.clientHeight);
    if (cv.width !== Math.round(w * r)) { cv.width = Math.round(w * r); }
    if (cv.height !== Math.round(h * r)) { cv.height = Math.round(h * r); }
    var ctx = cv.getContext("2d");
    ctx.setTransform(r, 0, 0, r, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx: ctx, w: w, h: h };
  }

  /* ---------- data ---------- */

  function getJSON(url, cb) {
    fetch(url, { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) { cb(d); })
      .catch(function () { cb(null); });
  }

  function onNetworkPage() {
    return (location.hash || "").slice(1) === "network";
  }

  function fetchAll() {
    if (!state) { return; }
    getJSON("/api/telemetry?range=" + state.range, function (d) {
      if (!state || !onNetworkPage()) { return; }
      state.telem = d;
      state.points = (d && d.present && Array.isArray(d.points))
        ? d.points : [];
      update();
    });
    getJSON("/api/poll_config", function (d) {
      if (!state) { return; }
      if (d && d.ok !== false) {
        state.pollConf = {
          allowed: Array.isArray(d.allowed) && d.allowed.length
            ? d.allowed : [5, 10, 15, 30, 60, 120],
          default_s: num(d.default_s) || 15,
          metrics: (d && typeof d.metrics === "object" && d.metrics) || {}
        };
        syncSelects();
      }
    });
  }

  function postCadence(key, sec) {
    fetch("/api/poll_config", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ metrics: (function () {
        var m = {}; m[key] = sec; return m;
      })() })
    }).then(function () { fetchAll(); })
      .catch(function () { fetchAll(); });
  }

  /* per-loop series from the interleaved telemetry rows */
  function seriesOf(field) {
    var out = [[], []];
    var pts = state.points;
    for (var i = 0; i < pts.length; i += 1) {
      var v = num(pts[i][field]);
      if (v == null) { continue; }
      var t = Date.parse(pts[i].ts);
      if (!isFinite(t)) { continue; }
      out[0].push(t / 1000);
      out[1].push(v);
    }
    return out;
  }

  function derive() {
    var m = {};
    var nowS = Date.now() / 1000;
    var winS = RANGES[state.range];
    var t0 = nowS - winS;
    var i;

    for (i = 0; i < LOOPS.length; i += 1) {
      var lp = LOOPS[i];
      var ages = seriesOf(lp.age);
      var beats = lp.beat ? seriesOf(lp.beat) : ages;
      var beatVals = sorted(beats[1]);
      var cadence = null;
      if (lp.beat && beatVals.length >= 5) {
        var med = quantile(beatVals, 0.5);
        if (med >= 5 && med <= 3600) { cadence = med; }
      }
      var thr = cadence
        ? { warn: cadence * 2, bad: cadence * 4 }
        : lp.fallback;

      var ageNow = null;
      if (ages[1].length) {
        var lastT = ages[0][ages[0].length - 1];
        var lastV = ages[1][ages[1].length - 1];
        ageNow = lp.unit === "ms" ? lastV
          : lastV + Math.max(0, nowS - lastT);
      }
      var ratio = null;
      if (ageNow != null && lp.unit === "s" && cadence) {
        ratio = ageNow / cadence;
      }
      var cls = "";
      if (ageNow != null && thr) {
        cls = ageNow >= thr.bad ? "bad"
          : ageNow >= thr.warn ? "warn" : "ok";
      }
      var late = null;
      if (ages[1].length && thr) {
        var over = 0;
        for (var a = 0; a < ages[1].length; a += 1) {
          if (ages[1][a] >= thr.warn) { over += 1; }
        }
        late = over / ages[1].length;
      }
      var ageVals = sorted(ages[1]);
      m[lp.key] = {
        loop: lp, ages: ages, beats: beats, cadence: cadence, thr: thr,
        ageNow: ageNow, ratio: ratio, cls: cls, late: late,
        stat: {
          min: beatVals.length ? beatVals[0] : null,
          med: quantile(beatVals, 0.5),
          p95: quantile(beatVals, 0.95),
          max: beatVals.length ? beatVals[beatVals.length - 1] : null
        },
        ageStat: {
          p50: quantile(ageVals, 0.5),
          p95: quantile(ageVals, 0.95),
          max: ageVals.length ? ageVals[ageVals.length - 1] : null
        },
        t0: t0, t1: nowS
      };
    }

    /* page aggregates */
    var rtt = sorted(seriesOf("rtt_ms")[1]);
    var onSched = 0;
    var loopsGraded = 0;
    var worst = null;
    for (i = 0; i < LOOPS.length; i += 1) {
      var lk = LOOPS[i];
      if (lk.unit !== "s") { continue; }
      var mm = m[lk.key];
      loopsGraded += 1;
      if (mm.cls === "ok") { onSched += 1; }
      if (mm.ratio != null && (!worst || mm.ratio > worst.ratio)) {
        worst = { name: lk.name === "News fetch loop" ? "News loop" : lk.name,
          ageNow: mm.ageNow, ratio: mm.ratio };
      }
    }
    var pts = state.points;
    var freshest = null;
    if (pts.length) {
      var lt = Date.parse(pts[pts.length - 1].ts);
      if (isFinite(lt)) { freshest = Math.max(0, (Date.now() - lt) / 1000); }
    }
    var span = null;
    if (pts.length >= 2) {
      var f = Date.parse(pts[0].ts);
      var l = Date.parse(pts[pts.length - 1].ts);
      if (isFinite(f) && isFinite(l)) { span = (l - f) / 1000; }
    }
    var changes = 0;
    var prevMachine = null;
    for (i = 0; i < pts.length; i += 1) {
      var mc = pts[i].machine;
      if (typeof mc !== "string" || !mc) { continue; }
      if (prevMachine != null && mc !== prevMachine) { changes += 1; }
      prevMachine = mc;
    }
    return {
      loops: m,
      pingP50: quantile(rtt, 0.5),
      pingP95: quantile(rtt, 0.95),
      onSched: onSched, loopsGraded: loopsGraded, worst: worst,
      samples: pts.length, freshest: freshest, span: span,
      stateChanges: changes, t0: t0, t1: nowS
    };
  }

  /* ---------- canvas renderers ---------- */

  function bucketize(series, t0, t1, nb, m) {
    var buckets = [];
    var bi;
    for (bi = 0; bi < nb; bi += 1) { buckets.push([]); }
    for (var i = 0; i < series[0].length; i += 1) {
      var t = series[0][i];
      if (t < t0 || t > t1) { continue; }
      bi = Math.min(nb - 1, Math.floor((t - t0) / (t1 - t0) * nb));
      buckets[bi].push(series[1][i]);
    }
    return buckets;
  }

  function drawRaster(cv, model) {
    var g = sizeCanvas(cv);
    var nb = Math.max(32, Math.min(90, Math.floor(g.w / 9)));
    var buckets = bucketize(model.ages, model.t0, model.t1, nb);
    var thr = model.thr;
    var slot = g.w / nb;
    var bw = slot * 0.62;
    var neutral = css("--st-line", "#e5e7eb");
    for (var b = 0; b < nb; b += 1) {
      var worstCls = null;
      var vals = buckets[b];
      for (var v = 0; v < vals.length; v += 1) {
        var c = !thr ? "ok"
          : vals[v] >= thr.bad ? "bad"
          : vals[v] >= thr.warn ? "warn" : "ok";
        if (worstCls == null || c === "bad" ||
            (c === "warn" && worstCls === "ok")) { worstCls = c; }
        if (worstCls === "bad") { break; }
      }
      /* nominal history whispers (DESIGN.md §2.4); deviation is loud */
      g.ctx.globalAlpha = worstCls == null ? 0.15
        : worstCls === "ok" ? 0.32 : 1;
      g.ctx.fillStyle = worstCls == null ? neutral : BAR[worstCls];
      var x = b * slot + (slot - bw) / 2;
      g.ctx.beginPath();
      g.ctx.rect(x, 1, bw, g.h - 2);
      g.ctx.fill();
    }
    g.ctx.globalAlpha = 1;
  }

  function niceCeil(v) {
    if (v <= 0) { return 1; }
    var pow = Math.pow(10, Math.floor(Math.log(v) / Math.LN10));
    var f = v / pow;
    var steps = [1, 1.2, 1.5, 2, 2.5, 3, 4, 5, 6, 8, 10];
    for (var i = 0; i < steps.length; i += 1) {
      if (f <= steps[i]) { return steps[i] * pow; }
    }
    return 10 * pow;
  }

  function drawEnvelope(cv, model) {
    var g = sizeCanvas(cv);
    var lp = model.loop;
    var gutter = 40;
    var plotW = g.w - gutter;
    var plotH = g.h - 16;
    /* the panel plots AGE — how late the loop is right now, breathing
       between 0 and its cadence; the table owns beat statistics */
    var nb = Math.max(30, Math.min(84, Math.floor(plotW / 9)));
    var buckets = bucketize(model.ages, model.t0, model.t1, nb);
    var ink = css("--st-ink", "#161618");
    var mid = css("--st-mid", "#787882");
    var faint = css("--st-faint", "#95959d");

    /* y-scale: room for the cadence guide and the warn band edge */
    var maxV = 0;
    var bx;
    for (bx = 0; bx < nb; bx += 1) {
      for (var q = 0; q < buckets[bx].length; q += 1) {
        if (buckets[bx][q] > maxV) { maxV = buckets[bx][q]; }
      }
    }
    /* ms loops scale to their own data; beat loops leave headroom for
       the cadence guide and the 2× warn edge (SmokePing keeps the
       threshold visible without drowning the signal) */
    var target;
    if (lp.unit === "ms") {
      target = Math.max(maxV * 1.3, 10);
    } else {
      target = maxV * 1.15;
      if (model.cadence) { target = Math.max(target, model.cadence * 2.25); }
    }
    var yMax = niceCeil(target || 1);
    function Y(v) { return plotH - (v / yMax) * (plotH - 4) - 0; }
    function X(b) { return (b + 0.5) / nb * plotW; }

    /* deviation zones — faint, only where thresholds exist on-scale */
    if (model.thr) {
      if (model.thr.warn < yMax) {
        g.ctx.fillStyle = WARN_TXT;
        g.ctx.globalAlpha = 0.04;
        g.ctx.fillRect(0, Y(Math.min(yMax, model.thr.bad)), plotW,
          Y(model.thr.warn) - Y(Math.min(yMax, model.thr.bad)));
      }
      if (model.thr.bad < yMax) {
        g.ctx.fillStyle = BAD_TXT;
        g.ctx.globalAlpha = 0.04;
        g.ctx.fillRect(0, 0, plotW, Y(model.thr.bad));
      }
      g.ctx.globalAlpha = 1;
    }

    /* cadence guide — dashed, quiet */
    if (model.cadence && model.cadence < yMax) {
      g.ctx.strokeStyle = faint;
      g.ctx.globalAlpha = 0.8;
      g.ctx.setLineDash([3, 4]);
      g.ctx.beginPath();
      g.ctx.moveTo(0, Y(model.cadence) + 0.5);
      g.ctx.lineTo(plotW, Y(model.cadence) + 0.5);
      g.ctx.stroke();
      g.ctx.setLineDash([]);
      g.ctx.globalAlpha = 1;
    }

    /* min–max envelope + median line, gap-honest */
    var envAlpha = 0.10;
    function runs(fn) {
      var open = false;
      for (var b2 = 0; b2 < nb; b2 += 1) {
        if (!buckets[b2].length) {
          if (open) { fn(null, b2); open = false; }
          continue;
        }
        fn(sorted(buckets[b2]), b2, !open);
        open = true;
      }
      if (open) { fn(null, nb); }
    }
    /* envelope */
    var lo = [];
    var hi = [];
    var segStart = null;
    runs(function (vals, b2, isStart) {
      if (vals == null) {
        if (lo.length > 1) {
          g.ctx.fillStyle = ink;
          g.ctx.globalAlpha = envAlpha;
          g.ctx.beginPath();
          g.ctx.moveTo(X(segStart), Y(lo[0]));
          for (var k = 1; k < lo.length; k += 1) {
            g.ctx.lineTo(X(segStart + k), Y(lo[k]));
          }
          for (var k2 = hi.length - 1; k2 >= 0; k2 -= 1) {
            g.ctx.lineTo(X(segStart + k2), Y(hi[k2]));
          }
          g.ctx.closePath();
          g.ctx.fill();
          g.ctx.globalAlpha = 1;
        }
        lo = []; hi = []; segStart = null;
        return;
      }
      if (isStart) { segStart = b2; }
      lo.push(vals[0]);
      hi.push(vals[vals.length - 1]);
    });
    /* median */
    g.ctx.strokeStyle = ink;
    g.ctx.lineWidth = 1.5;
    g.ctx.lineJoin = "round";
    var pen = false;
    runs(function (vals, b2, isStart) {
      if (vals == null) { pen = false; return; }
      var y = Y(quantile(vals, 0.5));
      if (!pen) {
        g.ctx.beginPath();
        g.ctx.moveTo(X(b2), y);
        pen = true;
      } else if (isStart) {
        g.ctx.moveTo(X(b2), y);
      } else {
        g.ctx.lineTo(X(b2), y);
      }
    });
    if (pen) { g.ctx.stroke(); }

    /* right-edge y labels: 0 · cadence (or mid) · top */
    g.ctx.fillStyle = mid;
    g.ctx.font = "10.5px InterVariable, Inter, sans-serif";
    g.ctx.textAlign = "left";
    g.ctx.textBaseline = "middle";
    var unitTxt = lp.unit === "ms" ? "ms" : "s";
    function yLbl(v, txt) {
      g.ctx.fillText(txt, plotW + 6, Math.max(7, Math.min(plotH - 5, Y(v))));
    }
    yLbl(yMax, Math.round(yMax) + unitTxt);
    if (model.cadence && model.cadence < yMax * 0.85) {
      yLbl(model.cadence, Math.round(model.cadence) + unitTxt);
    }
    yLbl(0, "0");

    /* bottom time labels */
    g.ctx.textAlign = "center";
    g.ctx.textBaseline = "alphabetic";
    g.ctx.fillStyle = faint;
    for (var tm = 0; tm < 4; tm += 1) {
      var frac = tm / 3;
      var tt = model.t0 + frac * (model.t1 - model.t0);
      var d = new Date(tt * 1000);
      var lbl = (d.getUTCHours() < 10 ? "0" : "") + d.getUTCHours() +
        ":" + (d.getUTCMinutes() < 10 ? "0" : "") + d.getUTCMinutes();
      var xx = Math.max(14, Math.min(plotW - 14, frac * plotW));
      g.ctx.fillText(lbl, xx, g.h - 3);
    }
  }

  function drawSpark(cv, model) {
    var g = sizeCanvas(cv);
    var nb = 48;
    var buckets = bucketize(model.beats, model.t0, model.t1, nb);
    var ink = css("--st-mid", "#787882");
    var maxV = 0;
    var minV = Infinity;
    var b;
    for (b = 0; b < nb; b += 1) {
      for (var q = 0; q < buckets[b].length; q += 1) {
        if (buckets[b][q] > maxV) { maxV = buckets[b][q]; }
        if (buckets[b][q] < minV) { minV = buckets[b][q]; }
      }
    }
    if (!isFinite(minV) || maxV <= 0) { return; }
    var pad = (maxV - minV) * 0.2 + 0.001;
    var loV = Math.max(0, minV - pad);
    var hiV = maxV + pad;
    g.ctx.strokeStyle = ink;
    g.ctx.lineWidth = 1;
    var pen = false;
    for (b = 0; b < nb; b += 1) {
      if (!buckets[b].length) { pen = false; continue; }
      var med = quantile(sorted(buckets[b]), 0.5);
      var x = (b + 0.5) / nb * g.w;
      var y = g.h - 2 - (med - loV) / (hiV - loV) * (g.h - 4);
      if (!pen) { g.ctx.beginPath(); g.ctx.moveTo(x, y); pen = true; }
      else { g.ctx.lineTo(x, y); }
    }
    if (pen) { g.ctx.stroke(); }
  }

  /* ---------- skeleton ---------- */

  function statTile(label) {
    var t = el("div", "st-stat");
    t.appendChild(el("div", "st-t-meta", label));
    var v = el("div", "st-t-datum st-stat-val", "—");
    t.appendChild(v);
    return { root: t, val: v };
  }

  function iconBtn(title, pathD, onClick) {
    var b = el("button", "st-iconbtn");
    b.setAttribute("type", "button");
    b.setAttribute("title", title);
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    svg.setAttribute("width", "13");
    svg.setAttribute("height", "13");
    svg.setAttribute("viewBox", "0 0 16 16");
    var p = document.createElementNS("http://www.w3.org/2000/svg", "path");
    p.setAttribute("d", pathD);
    p.setAttribute("fill", "currentColor");
    svg.appendChild(p);
    b.appendChild(svg);
    b.addEventListener("click", onClick);
    return b;
  }
  var REFRESH_D = "M8 2.5a5.5 5.5 0 1 0 5.35 4.19.75.75 0 0 1 1.46-.35" +
    "A7 7 0 1 1 8 1c1.73 0 3.32.63 4.55 1.67V1.75a.75.75 0 0 1 1.5 0v3.5" +
    "a.75.75 0 0 1-.75.75h-3.5a.75.75 0 0 1 0-1.5h1.72A5.48 5.48 0 0 0 8 2.5Z";

  function mkSwitch(on, onChange) {
    var b = el("button", "switch" + (on ? " on" : ""));
    b.setAttribute("type", "button");
    b.setAttribute("role", "switch");
    b.setAttribute("aria-checked", on ? "true" : "false");
    b.appendChild(el("span", "knob"));
    b.addEventListener("click", function () {
      var now = !b.classList.contains("on");
      b.classList.toggle("on", now);
      b.setAttribute("aria-checked", now ? "true" : "false");
      onChange(now);
    });
    return b;
  }

  function readPollLocal() {
    try {
      var raw = localStorage.getItem("quant.poll");
      if (raw) {
        var p = JSON.parse(raw);
        if (p && typeof p === "object") { return p; }
      }
    } catch (e) { /* fresh default below */ }
    return { strip: 10, news: 20, paused: false };
  }

  function savePollLocal(cfg) {
    try { localStorage.setItem("quant.poll", JSON.stringify(cfg)); }
    catch (e) { /* storage may be unavailable; live toggle still works */ }
  }

  function build(container) {
    destroy();
    var localPoll = readPollLocal();
    state = {
      range: "1h", points: [], telem: null,
      pollConf: { allowed: [5, 10, 15, 30, 60, 120], default_s: 15,
        metrics: {} },
      paused: !!localPoll.paused,
      localPoll: localPoll,
      els: { tiles: {}, path: {}, rasterRows: {}, tableBody: null,
        panels: {} },
      timer: null
    };

    var page = el("div", "st-page" + (state.paused ? " paused" : ""));

    /* ---- head: existing grammar (title · refresh · master) ---- */
    var head = el("div", "st-head");
    head.appendChild(el("div", "st-t-title", "Network"));
    head.appendChild(el("span", "st-spacer"));
    var seg = el("div", "st-seg");
    Object.keys(RANGES).forEach(function (rg) {
      var b = el("button", state.range === rg ? "on" : "",
        rg.toUpperCase());
      b.setAttribute("type", "button");
      b.addEventListener("click", function () {
        if (state.range === rg) { return; }
        state.range = rg;
        var bs = seg.querySelectorAll("button");
        for (var i = 0; i < bs.length; i += 1) {
          bs[i].classList.toggle("on",
            bs[i].textContent === rg.toUpperCase());
        }
        fetchAll();
      });
      seg.appendChild(b);
    });
    head.appendChild(seg);
    head.appendChild(iconBtn("refresh everything now", REFRESH_D,
      function () { fetchAll(); }));
    var master = el("span", "st-master" + (state.paused ? " stopped" : ""));
    var masterWord = el("span", "st-t-mid",
      state.paused ? "Network stopped" : "Network live");
    master.appendChild(masterWord);
    master.appendChild(mkSwitch(!state.paused, function (on) {
      state.paused = !on;
      state.localPoll.paused = !on;
      savePollLocal(state.localPoll);
      master.classList.toggle("stopped", !on);
      masterWord.textContent = on ? "Network live" : "Network stopped";
      page.classList.toggle("paused", !on);
      if (on) { fetchAll(); }
    }));
    head.appendChild(master);
    page.appendChild(head);

    page.appendChild(el("div", "v5-sub",
      "8 loops · lag measured against each loop's own cadence · " +
      "15s samples · UTC"));

    /* ---- tile band: the page's eight existing numbers ---- */
    var band = el("div", "st-stats");
    ["Loops on rhythm", "Worst lag", "Ping p50", "Ping p95",
      "Freshest sample", "Samples", "History held", "State changes"]
      .forEach(function (lbl) {
        var t = statTile(lbl);
        state.els.tiles[lbl] = t.val;
        band.appendChild(t.root);
      });
    page.appendChild(band);

    /* ---- path strip: the topology as it is wired ---- */
    var path = el("div", "v5-path");
    PATH_TIERS.forEach(function (tier) {
      var col = el("div", "v5-path-col");
      col.appendChild(el("div", "v5-path-tier", tier[0]));
      tier[1].forEach(function (k) {
        var lp = null;
        for (var i = 0; i < LOOPS.length; i += 1) {
          if (LOOPS[i].key === k) { lp = LOOPS[i]; }
        }
        var row = el("div", "v5-node");
        var dot = el("span", "v5-dot off");
        row.appendChild(dot);
        row.appendChild(el("span", "v5-node-name", lp.name));
        row.appendChild(el("span", "st-spacer"));
        var val = el("span", "v5-node-val", "—");
        row.appendChild(val);
        col.appendChild(row);
        state.els.path[k] = { dot: dot, val: val };
      });
      path.appendChild(col);
    });
    page.appendChild(path);

    /* ---- schedule raster card ---- */
    var rasterCard = el("div", "st-card");
    var rHead = el("div", "v5-card-head");
    rHead.appendChild(el("span", "v5-card-lbl", "Schedule history"));
    rHead.appendChild(el("span", "st-spacer"));
    var rasterMeta = el("span", "v5-note", "");
    rasterMeta.style.marginTop = "0";
    rHead.appendChild(rasterMeta);
    state.els.rasterMeta = rasterMeta;
    rasterCard.appendChild(rHead);
    var rows = el("div", "v5-raster-rows");
    LOOPS.forEach(function (lp) {
      var row = el("div", "v5-r-row");
      row.appendChild(el("div", "v5-r-name",
        lp.name === "News fetch loop" ? "News loop" : lp.name));
      var cv = document.createElement("canvas");
      cv.className = "v5-r-canvas";
      cv.setAttribute("aria-hidden", "true");
      row.appendChild(cv);
      rows.appendChild(row);
      state.els.rasterRows[lp.key] = cv;
    });
    rasterCard.appendChild(rows);
    var axis = el("div", "v5-r-axis");
    state.els.rasterAxis = axis;
    rasterCard.appendChild(axis);
    rasterCard.appendChild(el("div", "v5-note",
      "each cell = the worst sample in its time slice · " +
      "green on schedule · amber past 2× cadence · " +
      "red past 4×"));
    page.appendChild(rasterCard);

    /* ---- worst-first table card ---- */
    var tableCard = el("div", "st-card");
    var tHead = el("div", "v5-card-head");
    tHead.appendChild(el("span", "v5-card-lbl", "Loops, worst first"));
    tableCard.appendChild(tHead);
    var tbl = el("table", "v5-table");
    var thead = el("thead");
    var hr = el("tr");
    [["Loop", "v5-td-name"], ["Tier", ""], ["Status", "c-status"],
      ["Cadence", ""], ["Age", ""], ["×Sched", ""],
      ["p95 beat", ""], ["Late", ""], ["Trend", ""]]
      .forEach(function (h) {
        var th = el("th", h[1], h[0]);
        hr.appendChild(th);
      });
    thead.appendChild(hr);
    tbl.appendChild(thead);
    var tbody = el("tbody");
    state.els.tableBody = tbody;
    tbl.appendChild(tbody);
    tableCard.appendChild(tbl);
    tableCard.appendChild(el("div", "v5-note",
      "late = share of window samples past 2× cadence · " +
      "beats are the loop's measured tick intervals"));
    page.appendChild(tableCard);

    /* ---- envelope panels ---- */
    var grid = el("div", "v5-grid");
    LOOPS.forEach(function (lp) {
      var card = el("div", "v5-panel");
      card.id = "v5-panel-" + lp.key;
      var ph = el("div", "v5-p-head");
      var dot = el("span", "v5-dot off");
      ph.appendChild(dot);
      ph.appendChild(el("span", "v5-p-name", lp.name));
      ph.appendChild(el("span", "v5-p-tier", lp.tier));
      ph.appendChild(el("span", "v5-p-detail", lp.detail));
      ph.appendChild(el("span", "st-spacer"));
      var word = el("span", "v5-word", "Measuring");
      ph.appendChild(word);
      var sel = el("select", "v5-select");
      sel.setAttribute("data-poll", lp.key);
      sel.setAttribute("title", "sample cadence");
      sel.addEventListener("change", function () {
        postCadence(lp.key, parseInt(sel.value, 10));
      });
      ph.appendChild(sel);
      ph.appendChild(iconBtn("refresh now", REFRESH_D, function () {
        fetchAll();
      }));
      card.appendChild(ph);
      var cv = document.createElement("canvas");
      cv.className = "v5-env";
      cv.setAttribute("aria-hidden", "true");
      card.appendChild(cv);
      var stats = el("div", "v5-p-stats");
      card.appendChild(stats);
      grid.appendChild(card);
      state.els.panels[lp.key] = { canvas: cv, stats: stats, dot: dot,
        word: word, select: sel };
    });
    page.appendChild(grid);

    container.appendChild(page);
    state.pageEl = page;

    /* lifecycle */
    state.timer = setInterval(function () {
      if (!onNetworkPage()) { return; }
      if (state.paused) { return; }
      fetchAll();
    }, 15000);
    state.onResize = function () { update(); };
    root.addEventListener("resize", state.onResize);
    state.onHash = function () {
      if (!onNetworkPage()) { destroy(); }
    };
    root.addEventListener("hashchange", state.onHash);

    fetchAll();
  }

  function syncSelects() {
    if (!state) { return; }
    LOOPS.forEach(function (lp) {
      var p = state.els.panels[lp.key];
      if (!p) { return; }
      var sel = p.select;
      var want = num(state.pollConf.metrics[lp.key]) ||
        state.pollConf.default_s;
      sel.textContent = "";
      state.pollConf.allowed.forEach(function (s) {
        var o = document.createElement("option");
        o.value = String(s);
        o.textContent = s + "s";
        sel.appendChild(o);
      });
      sel.value = String(want);
    });
  }

  function setDot(dotEl, cls) {
    dotEl.className = dotEl.className.split(" ")[0] +
      (cls ? " " + (cls === "ok" ? "" : cls) : " off");
  }

  function update() {
    if (!state || !state.pageEl) { return; }
    var d = derive();
    var tiles = state.els.tiles;
    tiles["Loops on rhythm"].textContent =
      d.onSched + "/" + d.loopsGraded;
    tiles["Worst lag"].textContent = d.worst
      ? d.worst.name + " " + Math.round(d.worst.ageNow) + "s " +
        fmtRatio(d.worst.ratio)
      : "—";
    tiles["Ping p50"].textContent =
      d.pingP50 != null ? Math.round(d.pingP50) + " ms" : "—";
    tiles["Ping p95"].textContent =
      d.pingP95 != null ? Math.round(d.pingP95) + " ms" : "—";
    tiles["Freshest sample"].textContent =
      d.freshest != null ? Math.round(d.freshest) + "s" : "—";
    tiles["Samples"].textContent = String(d.samples);
    tiles["History held"].textContent =
      d.span != null ? (Math.round(d.span / 360) / 10) + "h" : "—";
    tiles["State changes"].textContent = String(d.stateChanges);

    /* deviation coloring on the worst tile value only when deviating */
    var worstCls = "";
    if (d.worst && d.worst.ratio >= 4) { worstCls = "bad"; }
    else if (d.worst && d.worst.ratio >= 2) { worstCls = "warn"; }
    tiles["Worst lag"].style.color = worstCls === "bad"
      ? "var(--st-bad-text)" : worstCls === "warn"
      ? "var(--st-warn-text)" : "";

    /* path strip */
    LOOPS.forEach(function (lp) {
      var m = d.loops[lp.key];
      var p = state.els.path[lp.key];
      if (!p) { return; }
      setDot(p.dot, m.cls || null);
      p.val.textContent = fmtVal(m.ageNow, lp.unit);
      p.val.className = "v5-node-val" +
        (m.cls === "warn" || m.cls === "bad" ? " " + m.cls : "");
    });

    /* raster */
    LOOPS.forEach(function (lp) {
      var cv = state.els.rasterRows[lp.key];
      if (cv) { drawRaster(cv, d.loops[lp.key]); }
    });
    var axis = state.els.rasterAxis;
    axis.textContent = "";
    for (var tm = 0; tm < 5; tm += 1) {
      var tt = d.t0 + tm / 4 * (d.t1 - d.t0);
      var dt = new Date(tt * 1000);
      axis.appendChild(el("span", "",
        (dt.getUTCHours() < 10 ? "0" : "") + dt.getUTCHours() + ":" +
        (dt.getUTCMinutes() < 10 ? "0" : "") + dt.getUTCMinutes()));
    }
    state.els.rasterMeta.textContent =
      state.range.toUpperCase() + " window · newest right";

    /* table, worst first */
    var order = LOOPS.slice();
    order.sort(function (a, b) {
      var ra = d.loops[a.key].ratio;
      var rb = d.loops[b.key].ratio;
      if (ra == null && rb == null) { return 0; }
      if (ra == null) { return 1; }
      if (rb == null) { return -1; }
      return rb - ra;
    });
    var tbody = state.els.tableBody;
    tbody.textContent = "";
    order.forEach(function (lp) {
      var m = d.loops[lp.key];
      var tr = el("tr");
      tr.addEventListener("click", function () {
        var card = document.getElementById("v5-panel-" + lp.key);
        if (!card) { return; }
        card.scrollIntoView({ behavior: "smooth", block: "center" });
        card.classList.add("flash");
        setTimeout(function () { card.classList.remove("flash"); }, 1200);
      });
      var tdName = el("td", "v5-td-name");
      tdName.appendChild(el("span", "nm", lp.name));
      tr.appendChild(tdName);
      tr.appendChild(el("td", "v5-td-tier", lp.tier));
      var tdSt = el("td", "v5-td-status");
      var w = el("span", "v5-word" +
        (m.cls && m.cls !== "ok" ? " " + m.cls : ""), "");
      var wd = el("span", "v5-dot" + (m.cls === "warn" ? " warn"
        : m.cls === "bad" ? " bad" : m.cls === "ok" ? "" : " off"));
      w.appendChild(wd);
      w.appendChild(document.createTextNode(statusWord(m.cls)));
      tdSt.appendChild(w);
      tr.appendChild(tdSt);
      tr.appendChild(el("td", "", lp.unit === "ms" ? "—"
        : m.cadence ? fmtDur(m.cadence) : "—"));
      var tdAge = el("td", "", fmtVal(m.ageNow, lp.unit));
      if (m.cls === "warn" || m.cls === "bad") {
        tdAge.style.color = m.cls === "bad"
          ? "var(--st-bad-text)" : "var(--st-warn-text)";
      }
      tr.appendChild(tdAge);
      var tdR = el("td", "v5-td-ratio" +
        (m.cls === "warn" || m.cls === "bad" ? " " + m.cls : ""),
        lp.unit === "ms" ? "—" : fmtRatio(m.ratio));
      tr.appendChild(tdR);
      tr.appendChild(el("td", "",
        m.stat.p95 != null ? fmtVal(m.stat.p95, lp.unit) : "—"));
      tr.appendChild(el("td", "", m.late == null ? "—"
        : (Math.round(m.late * 1000) / 10) + "%"));
      var tdTr = el("td");
      var sp = document.createElement("canvas");
      sp.className = "v5-spark";
      tdTr.appendChild(sp);
      tr.appendChild(tdTr);
      tbody.appendChild(tr);
      drawSpark(sp, m);
    });

    /* panels */
    LOOPS.forEach(function (lp) {
      var m = d.loops[lp.key];
      var p = state.els.panels[lp.key];
      if (!p) { return; }
      setDot(p.dot, m.cls || null);
      p.word.className = "v5-word" +
        (m.cls && m.cls !== "ok" ? " " + m.cls : "");
      p.word.textContent = statusWord(m.cls);
      if (state.points.length >= 2) {
        drawEnvelope(p.canvas, m);
      }
      var s = m.stat;
      p.stats.textContent = "";
      function statBit(lbl, txt, cls) {
        var span = el("span", "", lbl + " ");
        var b = el("b", cls || "", txt);
        span.appendChild(b);
        p.stats.appendChild(span);
      }
      var as = m.ageStat;
      if (lp.unit === "ms") {
        statBit("p50", fmtVal(as.p50, "ms"));
        statBit("p95", fmtVal(as.p95, "ms"));
        statBit("max", fmtVal(as.max, "ms"));
        statBit("now", fmtVal(m.ageNow, "ms"),
          m.cls !== "ok" && m.cls ? m.cls : "");
      } else {
        statBit("cadence", m.cadence ? fmtDur(m.cadence) : "—");
        statBit("lag p50", fmtVal(as.p50, "s"));
        statBit("p95", fmtVal(as.p95, "s"));
        statBit("max", fmtVal(as.max, "s"));
        statBit("now",
          fmtVal(m.ageNow, "s") + " " +
          (m.ratio != null ? fmtRatio(m.ratio) : ""),
          m.cls !== "ok" && m.cls ? m.cls : "");
      }
    });
  }

  function destroy() {
    if (!state) { return; }
    if (state.timer) { clearInterval(state.timer); }
    if (state.onResize) { root.removeEventListener("resize", state.onResize); }
    if (state.onHash) { root.removeEventListener("hashchange", state.onHash); }
    state = null;
  }

  root.NetV5 = { build: build, destroy: destroy };
})(window);
