"use strict";

/* Quant tab — the machine's trader page. Owned by the Quant lane.
   Left: deterministic decision trail (never called AI thinking; only
   sentences mechanically derivable from structured records; honest
   Activity detail unavailable otherwise), grouped into episodes where
   symbol + day link events. Right: authority (Unavailable until the
   operator contract exists — never inferred, never a control) and
   read-only policy facts that are code law.

   Live-update rule: the trail updates in place. An episode block is
   rebuilt only when its underlying records change; open Technical
   drawers are remembered by event key and restored, so polling never
   collapses what the owner is inspecting. */

(function () {
  var UI = window.QuantUI;

  var EVENT_LIMIT = 40;
  var STRAT_TAB = { Basis: "basis", Volume: "volume" };

  /* Kept across renders: episode nodes by key, their content JSON,
     and which Technical drawers the owner has open. */
  var trail = {
    nodes: {},
    json: {},
    open: {},
    metaNode: null,
    emptyShown: false
  };
  var policyJson = "";

  /* ---------- init: static skeleton, built once ---------- */

  function init() {
    var root = document.getElementById("root-quant");
    root.textContent = "";
    var grid = UI.el("div", "quantgrid");

    var trailSection = UI.el("section", "block trail");
    trailSection.appendChild(UI.el("h2", null, "Decision trail"));
    var stateLine = UI.el("p", "meta");
    stateLine.id = "quant-state";
    trailSection.appendChild(stateLine);
    var trailBody = UI.el("div");
    trailBody.id = "quant-trail";
    trailBody.appendChild(UI.el("p", "empty skel", "loading"));
    trailSection.appendChild(trailBody);
    grid.appendChild(trailSection);

    var right = UI.el("div");
    var auth = UI.el("section", "block");
    auth.appendChild(UI.el("h2", null, "Authority"));
    /* Contract 5.4: this prose is law until /api/operator/state exists.
       Static — built once, never rebuilt, never a control. */
    auth.appendChild(UI.el("p", "unavail",
      "Unavailable — the operator contract does not exist yet. Entry " +
      "authority cannot be read, granted, or revoked from this page, " +
      "and nothing here can pause or arm the machine."));
    right.appendChild(auth);

    var policy = UI.el("section", "block");
    policy.appendChild(UI.el("h2", null, "Policy — effective values"));
    var policyBody = UI.el("div");
    policyBody.id = "quant-policy";
    policy.appendChild(policyBody);
    policy.appendChild(UI.el("p", "meta",
      "These are code law, shown read-only. Changing any of them is a " +
      "reviewed code decision, never a dashboard action."));
    right.appendChild(policy);
    grid.appendChild(right);

    root.appendChild(grid);
  }

  /* ---------- trail: episode grouping ---------- */

  /* Same symbol + same UTC day = one episode; events without a symbol
     stand alone. Events arrive newest-first from the shared builder,
     so first appearance orders the episodes. */
  function groupEpisodes(events) {
    var order = [];
    var byKey = {};
    events.forEach(function (event) {
      var day = UI.shortDay(event.ts);
      var key = event.sym ? "ep|" + event.sym + "|" + day :
        "solo|" + event.ts + "|" + event.strat + "|" + event.text;
      var episode = byKey[key];
      if (!episode) {
        episode = { key: key, sym: event.sym, day: day, events: [] };
        byKey[key] = episode;
        order.push(episode);
      }
      episode.events.push(event);
    });
    return order;
  }

  function eventKey(event) {
    return [event.ts, event.strat, event.sym, event.text].join("|");
  }

  function symbolLink(event) {
    var tab = STRAT_TAB[event.strat];
    var link = UI.el("a", "symlink", event.sym);
    link.href = "#" + (tab || "");
    link.title = "Open " + event.sym + " on the " + event.strat + " tab";
    link.addEventListener("click", function (clickEvent) {
      clickEvent.preventDefault();
      if (tab) UI.selectTab(tab);
    });
    return link;
  }

  function severityClass(sev) {
    if (sev === "attention") return "b-red";
    if (sev === "refused") return "b-amber";
    if (sev === "trade") return "b-green";
    return "b-gray";
  }

  function eventRow(event, insideEpisode) {
    var wrap = UI.el("div", "evt");
    var line = UI.el("div", "jrow");
    var when = UI.shortTime(event.ts) || "time unrecorded";
    if (!insideEpisode && UI.shortDay(event.ts)) {
      when = UI.shortDay(event.ts) + " " + when;
    }
    line.appendChild(UI.el("span", "ts", when));
    line.appendChild(UI.el("span", "badge " + severityClass(event.sev),
      event.strat));
    var body = UI.el("span");
    if (event.sym && !insideEpisode) {
      body.appendChild(symbolLink(event));
      body.appendChild(document.createTextNode(" "));
    }
    body.appendChild(document.createTextNode(event.text));
    line.appendChild(body);
    wrap.appendChild(line);

    /* Raw evidence stays one drill-down away — collapsed drawer,
       remembered across polls by event key. */
    var key = eventKey(event);
    var drawer = UI.techDrawer(event.raw);
    if (trail.open[key]) drawer.open = true;
    drawer.addEventListener("toggle", function () {
      if (drawer.open) trail.open[key] = true;
      else delete trail.open[key];
    });
    wrap.appendChild(drawer);
    return wrap;
  }

  function episodeNode(episode) {
    var node = UI.el("div", "episode");
    node.setAttribute("data-key", episode.key);
    if (episode.sym) {
      var head = UI.el("div", "jrow ephead");
      var strong = UI.el("strong");
      strong.appendChild(symbolLink(episode.events[0]));
      head.appendChild(strong);
      var strategies = [];
      episode.events.forEach(function (event) {
        if (strategies.indexOf(event.strat) < 0) {
          strategies.push(event.strat);
        }
      });
      strategies.forEach(function (name) {
        head.appendChild(UI.el("span", "badge b-gray", name));
      });
      head.appendChild(UI.el("span", "meta", episode.day + " · " +
        episode.events.length +
        (episode.events.length === 1 ? " event" : " events")));
      node.appendChild(head);
      episode.events.forEach(function (event) {
        node.appendChild(eventRow(event, true));
      });
    } else {
      node.appendChild(eventRow(episode.events[0], false));
    }
    return node;
  }

  function renderTrail() {
    var container = document.getElementById("quant-trail");
    var events = UI.buildEventList().slice(0, EVENT_LIMIT);

    if (events.length === 0) {
      if (!trail.emptyShown) {
        container.textContent = "";
        trail.nodes = {}; trail.json = {}; trail.metaNode = null;
        container.appendChild(UI.el("p", "unavail",
          "Activity detail unavailable — the machine has not recorded " +
          "trail-worthy events in the delivered data, and the " +
          "structured activity contract does not exist yet."));
        trail.emptyShown = true;
      }
      return;
    }
    if (trail.emptyShown) {
      container.textContent = "";
      trail.emptyShown = false;
    }

    if (!trail.metaNode) {
      trail.metaNode = UI.el("p", "meta", "Recent — partial. Built only " +
        "from the bounded records the machine exposes today.");
      container.appendChild(trail.metaNode);
    } else {
      var skeleton = container.querySelector(".skel");
      if (skeleton) skeleton.remove();
    }

    var episodes = groupEpisodes(events);
    var seen = {};
    episodes.forEach(function (episode) {
      seen[episode.key] = true;
      var json = JSON.stringify(episode.events.map(function (event) {
        return [event.ts, event.strat, event.sym, event.sev, event.text];
      }));
      var node = trail.nodes[episode.key];
      if (!node || trail.json[episode.key] !== json) {
        var fresh = episodeNode(episode);
        if (node) node.replaceWith(fresh);
        else container.appendChild(fresh);
        trail.nodes[episode.key] = fresh;
        trail.json[episode.key] = json;
      }
    });

    Object.keys(trail.nodes).forEach(function (key) {
      if (seen[key]) return;
      trail.nodes[key].remove();
      delete trail.nodes[key];
      delete trail.json[key];
    });

    /* Order: newest episode first, after the meta line. Moving an
       already-attached node preserves its open drawers. */
    episodes.forEach(function (episode) {
      container.appendChild(trail.nodes[episode.key]);
    });
  }

  /* ---------- right column: policy facts ---------- */

  function kvRow(kv, term, valueText, badgeText, badgeCls) {
    kv.appendChild(UI.el("dt", null, term));
    var dd = UI.el("dd");
    dd.appendChild(document.createTextNode(valueText));
    if (badgeText) {
      dd.appendChild(document.createTextNode(" "));
      dd.appendChild(UI.el("span", "badge " + (badgeCls || "b-gray"),
        badgeText));
    }
    kv.appendChild(dd);
    return kv;
  }

  function renderPolicy() {
    var fees = UI.state.data.fees;
    var gov = (UI.state.data.strip || {}).governor || {};
    var json = JSON.stringify([fees, gov.entry_lock_present]);
    if (json === policyJson) return;
    policyJson = json;

    var root = UI.clearTo("quant-policy");
    var kv = UI.el("dl", "kv");
    kvRow(kv, "Leverage", "1×", "locked");
    kvRow(kv, "First-trade ceiling", "8 USDT (Basis: per leg)", "locked");
    kvRow(kv, "Later ceiling",
      "32 USDT — after one clean machine-managed close " +
      "(Momentum only today)", "locked");
    kvRow(kv, "Entries per day", "5", "locked");
    kvRow(kv, "Daily loss+fees", "1.00 USDT", "locked");
    kvRow(kv, "Adverse stop", "2% realized incl. fees", "Volume runner");

    var commission = null;
    if (fees && fees.present && fees.rates) {
      commission = UI.fmtPct(fees.rates.round_trip_taker_fees_pct);
    }
    if (commission) {
      kvRow(kv, "Measured commissions", commission + " round trip",
        fees.rates.fetched_utc ?
          UI.shortDay(fees.rates.fetched_utc) : null);
    } else {
      kvRow(kv, "Measured commissions", "Unavailable");
    }
    kvRow(kv, "Entry lock", UI.ownerLockLine(gov.entry_lock_present));
    root.appendChild(kv);
  }

  /* ---------- render ---------- */

  function render() {
    var strip = UI.state.data.strip || {};
    var stopLine = UI.ownerStopLine(strip.stop_present,
      strip.stop_jobs_present);
    document.getElementById("quant-state").textContent =
      "Current state: " + UI.currentPhase() +
      (stopLine ? " · " + stopLine : "");

    renderTrail();
    renderPolicy();
  }

  window.QuantTabs.quant = {
    needs: ["decisions", "pnl", "fees"],
    init: init,
    render: render
  };
})();
