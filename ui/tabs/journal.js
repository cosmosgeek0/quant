"use strict";

/* Journal — curated durable history (contract §5.5, iteration-1).
   Day-strip over the bounded delivered records; severity pills; the
   event tape with inline facts and collapsed technical evidence.
   Bounded sources are labeled as such — never presented as the full
   durable journal. */

(function () {
  var U = window.QuantUI;
  var view = { day: null, filter: "all" };

  function utcDayIso(offsetDays) {
    var now = new Date();
    return new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
      now.getUTCDate() - offsetDays)).toISOString().slice(0, 10);
  }
  function dayLabel(iso) {
    var names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var date = new Date(iso + "T00:00:00Z");
    return names[date.getUTCDay()] + " " + date.getUTCDate();
  }
  function fullDay(iso) {
    var days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
      "Friday", "Saturday"];
    var months = ["January", "February", "March", "April", "May",
      "June", "July", "August", "September", "October", "November",
      "December"];
    var date = new Date(iso + "T00:00:00Z");
    return days[date.getUTCDay()] + ", " + months[date.getUTCMonth()] +
      " " + date.getUTCDate();
  }
  function sevKey(event) {
    if (event.sev === "attention") return "c";
    if (event.sev === "refused") return "w";
    return "i";
  }
  function matches(event) {
    if (view.filter === "all") return true;
    if (view.filter === "trades") return event.sev === "trade";
    if (view.filter === "failures") {
      return event.sev === "attention" || event.sev === "refused";
    }
    if (view.filter === "decisions") return event.sev === "info";
    return true;
  }
  function byDay() {
    var map = {};
    U.buildEventList().forEach(function (event) {
      var day = U.shortDay(event.ts) || "undated";
      (map[day] = map[day] || []).push(event);
    });
    return map;
  }

  function render() {
    var root = U.clearTo("pageroot");
    var frame = U.el("div", "frame");
    var map = byDay();
    if (view.day === null) view.day = utcDayIso(0);

    var days = [];
    for (var i = 6; i >= 0; i--) {
      var iso = utcDayIso(i);
      var list = map[iso] || [];
      var counts = { c: 0, w: 0, i: 0, d: 0 };
      list.forEach(function (e) { counts[sevKey(e)] += 1; });
      days.push({ iso: iso, label: dayLabel(iso), total: list.length,
        counts: counts, isToday: i === 0 });
    }
    var head = U.band("Journal",
      "bounded delivered records — not yet the full durable history");
    frame.appendChild(head);
    frame.appendChild(U.dayStrip(days, view.day, function (iso) {
      view.day = iso; render();
    }));
    frame.appendChild(U.pillBar([
      { key: "all", label: "All" },
      { key: "trades", label: "Trades" },
      { key: "decisions", label: "Decisions" },
      { key: "failures", label: "Failures" }
    ], view.filter, function (key) { view.filter = key; render(); }));

    var dayList = map[view.day] || [];
    var counts = { c: 0, w: 0, i: 0, d: 0 };
    dayList.forEach(function (e) { counts[sevKey(e)] += 1; });
    frame.appendChild(U.groupHead(fullDay(view.day),
      dayList.length + " events · " + counts.c + " attention · " +
      counts.w + " refused · " + counts.i + " routine · UTC"));

    var selected = dayList.filter(matches);
    if (selected.length === 0) {
      frame.appendChild(U.stateBlock("empty",
        "No delivered records for this day" +
          (view.filter === "all" ? "." : " under this filter."),
        "Bounded sources only — the full durable journal arrives " +
        "with the journal read contract."));
    } else {
      selected.forEach(function (event) {
        frame.appendChild(U.feedRow({
          timeText: U.shortTime(event.ts) || null,
          title: event.strat + (event.sym ? " · " + event.sym : ""),
          caption: event.text,
          tone: U.eventTone(event),
          symbol: null,
          onExpand: function () {
            var wrap = U.el("div");
            var pairs = [
              ["Recorded", event.ts || "—"],
              ["Strategy", event.strat || "—"],
              ["Symbol", event.sym || "—"],
              ["Event", event.text || "—"]
            ];
            if (event.money) pairs.push(["Money effect", event.money]);
            wrap.appendChild(U.facts(pairs));
            wrap.appendChild(U.techDrawer(event.raw || {}));
            return wrap;
          }
        }));
      });
    }
    root.appendChild(frame);
  }

  window.QuantTabs.journal = {
    needs: ["pnl", "decisions", "events"],
    init: function () {}, render: render
  };
})();
