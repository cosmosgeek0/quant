"use strict";

/* Journal — curated durable history (contract §5.5, iteration-1).
   Day-strip navigation over the bounded records the machine exposes
   today; severity pills; 46px event tape with inline expansion.
   Sources are BOUNDED — a day card counts only delivered records, and
   the page says so. No raw dumps, no fabricated history. */

(function () {
  var U = window.QuantUI;
  var P = window.QuantParts;

  var view = { day: null, filter: "all" };

  function feedTime(iso) {
    var text = U.shortTime(iso);
    return text ? text.replace(" UTC", "Z") : null;
  }

  function utcDayIso(offsetDays) {
    var now = new Date();
    var day = new Date(Date.UTC(now.getUTCFullYear(), now.getUTCMonth(),
      now.getUTCDate() - offsetDays));
    return day.toISOString().slice(0, 10);
  }

  function dayLabel(iso) {
    var names = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"];
    var date = new Date(iso + "T00:00:00Z");
    return names[date.getUTCDay()] + " " + date.getUTCDate();
  }

  function fullDayLabel(iso) {
    var days = ["Sunday", "Monday", "Tuesday", "Wednesday", "Thursday",
      "Friday", "Saturday"];
    var months = ["January", "February", "March", "April", "May", "June",
      "July", "August", "September", "October", "November", "December"];
    var date = new Date(iso + "T00:00:00Z");
    return days[date.getUTCDay()] + ", " + months[date.getUTCMonth()] +
      " " + date.getUTCDate();
  }

  function sevOf(event) {
    if (event.sev === "attention") return "c";
    if (event.sev === "refused") return "w";
    return "i";
  }

  function matchesFilter(event) {
    if (view.filter === "all") return true;
    if (view.filter === "trades") return event.sev === "trade";
    if (view.filter === "failures") {
      return event.sev === "attention" || event.sev === "refused";
    }
    if (view.filter === "decisions") return event.sev === "info";
    return true;
  }

  function eventsByDay() {
    var map = {};
    U.buildEventList().forEach(function (event) {
      var day = U.shortDay(event.ts);
      if (!day) day = "undated";
      if (!map[day]) map[day] = [];
      map[day].push(event);
    });
    return map;
  }

  function render() {
    var root = U.clearTo("root-journal");
    var byDay = eventsByDay();
    if (view.day === null) view.day = utcDayIso(0);

    var days = [];
    for (var i = 6; i >= 0; i--) {
      var iso = utcDayIso(i);
      var list = byDay[iso] || [];
      var counts = { c: 0, w: 0, i: 0, d: 0 };
      list.forEach(function (event) { counts[sevOf(event)] += 1; });
      days.push({
        iso: iso,
        label: dayLabel(iso),
        total: list.length,
        counts: counts,
        isToday: i === 0
      });
    }

    var box = U.el("section", "card");
    var head = U.el("div", "card-head");
    head.appendChild(U.el("span", "card-title", "Journal"));
    head.appendChild(U.el("span", "card-meta",
      "bounded delivered records — not yet the full durable history"));
    box.appendChild(head);
    var body = U.el("div", "card-body");
    box.appendChild(body);

    body.appendChild(P.dayStrip(days, view.day, function (iso) {
      view.day = iso;
      render();
    }));

    body.appendChild(P.pillBar([
      { key: "all", label: "All" },
      { key: "trades", label: "Trades" },
      { key: "decisions", label: "Decisions" },
      { key: "failures", label: "Failures" }
    ], view.filter, function (key) {
      view.filter = key;
      render();
    }));

    var selected = (byDay[view.day] || []).filter(matchesFilter);
    var counts = { c: 0, w: 0, i: 0, d: 0 };
    (byDay[view.day] || []).forEach(function (event) {
      counts[sevOf(event)] += 1;
    });
    body.appendChild(P.feedGroupHead(fullDayLabel(view.day),
      (byDay[view.day] || []).length + " events · " + counts.c +
      " attention · " + counts.w + " refused · " + counts.i +
      " routine · UTC"));

    if (selected.length === 0) {
      body.appendChild(P.stateBlock("empty",
        "No delivered records for this day" +
          (view.filter === "all" ? "." : " under this filter."),
        "Bounded sources only — the full durable journal arrives with " +
        "the journal read contract."));
    } else {
      selected.forEach(function (event) {
        body.appendChild(P.feedRow({
          timeText: feedTime(event.ts),
          title: event.strat + (event.sym ? " · " + event.sym : ""),
          caption: event.text,
          tone: event.sev === "attention" ? "bad" :
            (event.sev === "refused" ? "warn" :
              (event.sev === "trade" ? "ok" : "info")),
          symbol: null,
          onExpand: function () {
            var wrap = U.el("div");
            var facts = [
              { label: "Recorded", value: event.ts || "—" },
              { label: "Strategy", value: event.strat || "—" },
              { label: "Symbol", value: event.sym || "—" },
              { label: "Event", value: event.text || "—" }
            ];
            if (event.money) {
              facts.push({ label: "Money effect", value: event.money });
            }
            wrap.appendChild(P.tapeWaterfall(facts));
            wrap.appendChild(U.techDrawer(event.raw || {}));
            return wrap;
          }
        }));
      });
    }

    root.appendChild(box);
  }

  window.QuantTabs.journal = {
    needs: ["pnl", "decisions", "events"],
    init: function () {},
    render: render
  };
})();
