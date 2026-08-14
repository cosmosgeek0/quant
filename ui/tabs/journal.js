"use strict";

/* Journal tab — curated durable history. Owned by the Journal lane.
   Sources: the shared bounded event list (Volume action receipts and
   realized results, Basis P&L records, refusal/unknown results,
   non-watch decisions) plus the archived events fixture, clearly
   labeled historical. The complete journal contract is a later backend
   milestone, so every row and every day-count here reflects DELIVERED
   records only and says so. The browser recalculates no money value;
   missing pieces render as "—", never invented. */

(function () {
  var UI = window.QuantUI;
  var table = null;
  var filter = "all";
  var dayFilter = null;
  var stripDates = [];
  var cards = {};
  var lastHistoryPayload = false;

  /* Severity is the owner-facing class of each record; the filter
     categories map onto it exactly (failures = refused + attention). */
  var SEV = {
    trade: { label: "Trade", badge: "b-green", cat: "trades" },
    info: { label: "Decision", badge: "b-gray", cat: "decisions" },
    refused: { label: "Refused", badge: "b-amber", cat: "failures" },
    attention: { label: "Attention", badge: "b-red", cat: "failures" }
  };
  var FILTER_LABELS = {
    all: "All", trades: "Trades", decisions: "Decisions",
    failures: "Failures"
  };
  var MONTHS = ["Jan", "Feb", "Mar", "Apr", "May", "Jun", "Jul", "Aug",
    "Sep", "Oct", "Nov", "Dec"];
  var WEEKDAYS = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];

  /* Facts shown in plain language when the record carries them.
     A field absent from the record is simply not shown. */
  var FACT_FIELDS = [
    ["verdict", "Verdict"], ["reason", "Reason"],
    ["note", "Recorded note"], ["action", "Action"],
    ["op", "Operation"], ["job_reason", "Job reason"],
    ["state", "State"]
  ];

  function init() {
    var root = document.getElementById("root-journal");
    root.textContent = "";

    var records = UI.el("section", "block");
    records.appendChild(UI.el("h2", null,
      "Journal — durable records (partial)"));
    records.appendChild(UI.el("p", "meta",
      "The complete journal contract does not exist yet. Rows and day " +
      "counts below are built only from the bounded records the machine " +
      "already delivers — they are real, but they are not complete " +
      "history."));

    var strip = UI.el("div", "daystrip");
    strip.id = "journal-daystrip";
    records.appendChild(strip);

    var toolbar = UI.el("div", "toolbar");
    toolbar.id = "journal-toolbar";
    [["all", "All"], ["trades", "Trades"], ["decisions", "Decisions"],
     ["failures", "Failures"]].forEach(function (pair, index) {
      var btn = UI.el("button", "fbtn", pair[1]);
      btn.setAttribute("data-filter", pair[0]);
      btn.setAttribute("aria-pressed", index === 0 ? "true" : "false");
      toolbar.appendChild(btn);
    });
    [["Controls", "No control records exist yet — controls become real " +
      "only after the operator contract lands."],
     ["System", "No system records are delivered to this page yet."]
    ].forEach(function (pair) {
      var btn = UI.el("button", "fbtn", pair[0]);
      btn.disabled = true;
      btn.setAttribute("aria-disabled", "true");
      btn.title = pair[1];
      btn.style.opacity = "0.45";
      btn.style.cursor = "not-allowed";
      toolbar.appendChild(btn);
    });
    records.appendChild(toolbar);

    var dayNote = UI.el("p", "meta");
    dayNote.id = "journal-daynote";
    dayNote.style.display = "none";
    var dayNoteText = UI.el("span");
    dayNoteText.id = "journal-daynote-text";
    dayNote.appendChild(dayNoteText);
    dayNote.appendChild(document.createTextNode(" "));
    var clearBtn = UI.el("button", "fbtn", "Show all days");
    clearBtn.addEventListener("click", function () {
      dayFilter = null;
      render();
    });
    dayNote.appendChild(clearBtn);
    records.appendChild(dayNote);

    var wrap = UI.el("div", "tablewrap");
    var tableNode = document.createElement("table");
    tableNode.className = "data";
    tableNode.id = "journal-table";
    tableNode.innerHTML =
      "<thead><tr>" +
      '<th data-sort="ts" class="sorted">Time</th>' +
      '<th data-sort="sev">Severity</th>' +
      '<th data-sort="strat">Strategy</th>' +
      '<th data-sort="symbol">Symbol</th>' +
      "<th>Event</th>" +
      '<th data-sort="moneyVal">Money</th>' +
      '<th data-sort="outcome">Outcome</th>' +
      "</tr></thead><tbody></tbody>";
    wrap.appendChild(tableNode);
    records.appendChild(wrap);

    var chipRow = UI.el("p", "rankchip");
    chipRow.id = "journal-rankchip";
    chipRow.appendChild(document.createTextNode(
      "Order changed while inspecting. "));
    chipRow.appendChild(UI.el("button", null, "Apply new order"));
    records.appendChild(chipRow);

    var empty = UI.el("p", "empty");
    empty.id = "journal-empty";
    empty.style.display = "none";
    records.appendChild(empty);

    var meta = UI.el("p", "meta");
    meta.id = "journal-meta";
    records.appendChild(meta);
    root.appendChild(records);

    var history = UI.el("section", "block");
    history.appendChild(UI.el("h2", null,
      "Historical evidence — archived spot runs"));
    history.appendChild(UI.el("p", "meta",
      "Archived record of the first real spot runs. Historical " +
      "evidence, not live data."));
    var historyBody = UI.el("div");
    historyBody.id = "journal-history";
    historyBody.appendChild(UI.el("p", "empty skel", "loading"));
    history.appendChild(historyBody);
    root.appendChild(history);

    toolbar.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".fbtn");
      if (!btn || btn.disabled || !btn.getAttribute("data-filter")) return;
      toolbar.querySelectorAll(".fbtn[data-filter]").forEach(
        function (node) {
          node.setAttribute("aria-pressed",
            node === btn ? "true" : "false");
        });
      filter = btn.getAttribute("data-filter");
      render();
    });

    table = new UI.KeyedTable({
      tableId: "journal-table",
      rankchipId: "journal-rankchip",
      sortKey: "ts",
      expandRender: expand,
      columns: [
        { key: "ts", cls: "num", render: function (r) {
          return { text: r.ts ?
            r.ts.slice(5, 10) + " " + r.ts.slice(11, 19) :
            "time unrecorded" };
        } },
        { key: "sev", render: function (r) {
          var s = SEV[r.sevKey] || SEV.info;
          return { node: UI.el("span", "badge " + s.badge, s.label) };
        } },
        { key: "strat", render: function (r) {
          return { text: r.strat || "—" };
        } },
        { key: "symbol", cls: "sym", render: function (r) {
          return { text: r.symbol || "—", cls: r.symbol ? "" : "dim" };
        } },
        { key: "text", render: function (r) { return { text: r.text }; } },
        { key: "money", cls: "num", render: function (r) {
          if (!r.money) return { text: "—", cls: "dim" };
          return { text: r.money, cls: r.moneyVal > 0 ? "pos" :
            (r.moneyVal < 0 ? "neg" : "") };
        } },
        { key: "outcome", render: function (r) {
          return { text: r.outcome || "—", cls: r.outcome ? "" : "dim" };
        } }
      ]
    });
  }

  /* ---------- rows from the shared bounded event list ---------- */

  function moneyNumber(event) {
    if (!event.money) return null;
    var raw = event.raw || {};
    var net = raw.realized && raw.realized.net_usdt;
    if (net === undefined) net = raw.net_usdt;
    var value = Number(net);
    return Number.isFinite(value) ? value : null;
  }

  function buildRows() {
    var counters = {};
    return UI.buildEventList().map(function (event) {
      var raw = event.raw || {};
      var base = (event.ts || "no-ts") + "|" + event.sev + "|" +
        event.strat + "|" + (event.sym || "") + "|" + event.text;
      counters[base] = (counters[base] || 0) + 1;
      var sev = SEV[event.sev] || SEV.info;
      return {
        key: base + "#" + counters[base],
        ts: event.ts || "",
        day: UI.shortDay(event.ts || "") || null,
        sev: sev.label,
        sevKey: event.sev,
        cat: sev.cat,
        strat: event.strat || "",
        symbol: event.sym || "",
        text: event.text,
        money: event.money || null,
        moneyVal: moneyNumber(event),
        outcome: typeof raw.verdict === "string" && raw.verdict ?
          raw.verdict : null,
        raw: raw
      };
    });
  }

  function visibleRows(all) {
    var out = all;
    if (filter !== "all") {
      out = out.filter(function (row) { return row.cat === filter; });
    }
    if (dayFilter) {
      out = out.filter(function (row) { return row.day === dayFilter; });
    }
    return out;
  }

  /* ---------- rolling 7-day strip (delivered-record counts) ---------- */

  function dayLabel(dateStr) {
    var d = new Date(dateStr + "T00:00:00Z");
    return MONTHS[d.getUTCMonth()] + " " + d.getUTCDate();
  }

  function toggleDay(dateStr) {
    dayFilter = dayFilter === dateStr ? null : dateStr;
    render();
  }

  function makeCard(dateStr, todayStr) {
    var card = UI.el("div", "daycard");
    card.setAttribute("role", "button");
    card.tabIndex = 0;
    card.title = "Counts of records delivered for " + dateStr +
      " (UTC) — bounded sources only, not complete history. Click to " +
      "show only this day's rows.";
    var day = UI.el("div", "dc-day", dayLabel(dateStr));
    day.appendChild(UI.el("span", null, dateStr === todayStr ? "today" :
      WEEKDAYS[new Date(dateStr + "T00:00:00Z").getUTCDay()]));
    card.appendChild(day);
    var rowTotal = UI.el("div", "dc-row");
    rowTotal.appendChild(UI.el("span", null, "Delivered"));
    var total = UI.el("span", "dc-num", "0");
    rowTotal.appendChild(total);
    card.appendChild(rowTotal);
    var rowSev = UI.el("div", "dc-row");
    var hml = UI.el("span", "hml", "—");
    rowSev.appendChild(hml);
    card.appendChild(rowSev);
    function activate() { toggleDay(dateStr); }
    card.addEventListener("click", activate);
    card.addEventListener("keydown", function (ev) {
      if (ev.key === "Enter" || ev.key === " ") {
        ev.preventDefault();
        activate();
      }
    });
    return { card: card, total: total, hml: hml, lastCounts: "" };
  }

  function renderDayStrip(allRows) {
    var todayStr = new Date().toISOString().slice(0, 10);
    var dates = [];
    for (var i = 6; i >= 0; i--) {
      dates.push(new Date(Date.now() - i * 86400000)
        .toISOString().slice(0, 10));
    }
    var strip = document.getElementById("journal-daystrip");
    if (dates.join(",") !== stripDates.join(",")) {
      stripDates = dates;
      cards = {};
      strip.textContent = "";
      if (dayFilter && dates.indexOf(dayFilter) < 0) dayFilter = null;
      dates.forEach(function (dateStr) {
        cards[dateStr] = makeCard(dateStr, todayStr);
        strip.appendChild(cards[dateStr].card);
      });
    }

    var byDay = {};
    allRows.forEach(function (row) {
      if (!row.day || !cards[row.day]) return;
      var c = byDay[row.day] ||
        (byDay[row.day] = { total: 0, t: 0, r: 0, a: 0 });
      c.total += 1;
      if (row.sevKey === "trade") c.t += 1;
      else if (row.sevKey === "refused") c.r += 1;
      else if (row.sevKey === "attention") c.a += 1;
    });

    dates.forEach(function (dateStr) {
      var entry = cards[dateStr];
      var counts = byDay[dateStr] || { total: 0, t: 0, r: 0, a: 0 };
      var json = JSON.stringify(counts);
      if (entry.lastCounts !== json) {
        entry.lastCounts = json;
        entry.total.textContent = String(counts.total);
        entry.hml.textContent = "";
        /* trade = green by the color grammar; .hml .t is a requested
           shared style — until it lands, "m" renders the accent color,
           which claims nothing. Refused = amber (l), attention = red
           (h). Zero counts are omitted, not shown as colored zeros. */
        if (counts.t > 0) {
          var t = UI.el("span", "m t", "T" + counts.t);
          t.title = counts.t + " trade record" +
            (counts.t === 1 ? "" : "s");
          entry.hml.appendChild(t);
        }
        if (counts.r > 0) {
          var r = UI.el("span", "l", " R" + counts.r);
          r.title = counts.r + " refused record" +
            (counts.r === 1 ? "" : "s");
          entry.hml.appendChild(r);
        }
        if (counts.a > 0) {
          var a = UI.el("span", "h", " A" + counts.a);
          a.title = counts.a + " attention record" +
            (counts.a === 1 ? "" : "s");
          entry.hml.appendChild(a);
        }
        if (!entry.hml.firstChild) entry.hml.textContent = "—";
      }
      entry.card.classList.toggle("active", dateStr === dayFilter);
      entry.card.setAttribute("aria-pressed",
        dateStr === dayFilter ? "true" : "false");
    });
  }

  /* ---------- expanded event ---------- */

  function expand(row) {
    var box = UI.el("div");
    box.appendChild(UI.el("p", "meta",
      "Recorded " + (row.ts ?
        row.ts.replace("T", " ").replace("Z", " UTC") :
        "at an unrecorded time") +
      " · " + (row.strat || "strategy unrecorded") +
      (row.symbol ? " · " + row.symbol : "")));
    box.appendChild(UI.el("p", null, row.text));

    var facts = FACT_FIELDS.filter(function (pair) {
      var value = row.raw[pair[0]];
      return typeof value === "string" && value !== "";
    });
    if (facts.length > 0) {
      var kv = UI.el("dl", "kv");
      facts.forEach(function (pair) {
        kv.appendChild(UI.el("dt", null, pair[1]));
        kv.appendChild(UI.el("dd", null, row.raw[pair[0]]));
      });
      box.appendChild(kv);
    }

    if (row.money) {
      box.appendChild(UI.el("p", "meta",
        "Recorded money effect: " + row.money + " — this is the " +
        "record's own accounting. Account-wide fee, funding, and P&L " +
        "reconciliation does not exist yet, so completeness beyond " +
        "this record is unverified."));
    } else {
      box.appendChild(UI.el("p", "meta",
        "No money effect recorded for this event."));
    }
    box.appendChild(UI.el("p", "unavail",
      "Related-episode links unavailable — the episode contract does " +
      "not exist yet."));
    box.appendChild(UI.techDrawer(row.raw));
    return box;
  }

  /* ---------- render ---------- */

  function render() {
    if (!table) return;
    var all = buildRows();
    renderDayStrip(all);
    var visible = visibleRows(all);
    table.update(visible);

    var dayNote = document.getElementById("journal-daynote");
    if (dayFilter) {
      document.getElementById("journal-daynote-text").textContent =
        "Showing only " + dayLabel(dayFilter) + " (" + dayFilter +
        " UTC).";
      dayNote.style.display = "";
    } else {
      dayNote.style.display = "none";
    }

    var empty = document.getElementById("journal-empty");
    if (all.length === 0) {
      empty.textContent = "No durable trade, decision, or failure " +
        "records in the delivered data yet.";
      empty.style.display = "";
    } else if (visible.length === 0) {
      var parts = [];
      if (filter !== "all") parts.push(FILTER_LABELS[filter]);
      if (dayFilter) parts.push(dayLabel(dayFilter) + " UTC");
      empty.textContent = "No delivered records match this view (" +
        parts.join(", ") + "). The delivered windows are bounded — " +
        "absence here does not prove nothing happened.";
      empty.style.display = "";
    } else {
      empty.style.display = "none";
    }

    document.getElementById("journal-meta").textContent =
      visible.length + " of " + all.length + " delivered records " +
      "shown · sources: Volume action receipts and realized results, " +
      "Basis P&L records, refusal and unknown results, non-watch " +
      "decisions · bounded windows, not complete history.";

    renderHistory();
  }

  /* ---------- archived spot runs (historical fixture) ---------- */

  function renderHistory() {
    var payload = UI.state.data.events;
    if (payload === undefined) return; /* still loading; keep skeleton */
    if (payload === lastHistoryPayload) return;
    lastHistoryPayload = payload;
    var root = UI.clearTo("journal-history");
    if (!payload || !payload.present || !Array.isArray(payload.events) ||
        payload.events.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "The archived events source is unavailable — no runs to show."));
      return;
    }
    var runs = {};
    var order = [];
    payload.events.forEach(function (event) {
      var id = event.run_id || "(no run)";
      if (!runs[id]) { runs[id] = []; order.push(id); }
      runs[id].push(event);
    });
    order.forEach(function (id) {
      var events = runs[id];
      var verdictEvent = null;
      events.forEach(function (event) {
        if (event.kind === "run_verdict") verdictEvent = event;
      });
      var verdict = verdictEvent && verdictEvent.verdict;
      var row = UI.el("div", "jrow");
      row.appendChild(UI.el("span", "ts",
        ((UI.shortDay(events[0].ts || "") + " " +
          UI.shortTime(events[0].ts || "")).trim()) || "time unrecorded"));
      row.appendChild(UI.el("span", "badge " +
        (verdict === "CLOSED" ? "b-green" : "b-amber"),
        verdict || "no verdict"));
      row.appendChild(UI.el("span", null,
        (events[0].symbol || "symbol unrecorded") +
        " — archived spot run · " + events.length + " recorded steps"));
      root.appendChild(row);
      if (verdictEvent && typeof verdictEvent.note === "string" &&
          verdictEvent.note) {
        root.appendChild(UI.el("p", "meta",
          "Recorded note: " + verdictEvent.note));
      }
      root.appendChild(UI.techDrawer(events));
    });
  }

  window.QuantTabs.journal = {
    needs: ["decisions", "pnl"],
    init: init,
    render: render
  };
})();
