"use strict";

/* Volume tab — interesting movers. Owned by the Volume lane.
   Sources: stream (selection contains top_surge), decisions (bounded
   last-50 window), momentum runner state (Open cross-reference and
   recorded pre-ignition/persistence fields). Observation is
   distinguished from execution eligibility: Spot-only symbols are
   Observe only, never tradeable. Move/Confirm/Age come only from
   recorded fields; an unrecorded value renders as an em dash. The
   browser never recalculates backend money math. */

(function () {

  /* ---------- pure row logic (exported for offline tests) ---------- */

  function movePct(currentPrice, referencePrice) {
    if (currentPrice === null || currentPrice === undefined ||
        currentPrice === "" || referencePrice === null ||
        referencePrice === undefined || referencePrice === "") {
      return null;
    }
    var cur = Number(currentPrice);
    var ref = Number(referencePrice);
    if (!Number.isFinite(cur) || !Number.isFinite(ref) || ref <= 0) {
      return null;
    }
    return ((cur - ref) / ref) * 100;
  }

  function ageSeconds(ignitionUtc, nowMs) {
    if (typeof ignitionUtc !== "string" || !ignitionUtc) return null;
    var t = Date.parse(ignitionUtc);
    if (!Number.isFinite(t)) return null;
    var s = (nowMs - t) / 1000;
    if (s < 0) return null; /* future timestamp: claim nothing */
    return s;
  }

  /* Conservative phase mapping (brief law): recorded runner-open wins;
     WATCH renders Quiet; any other recorded action is shown as
     recorded; no decision row renders "No decision". */
  function phaseInfo(decision, runnerOpen) {
    if (runnerOpen) return { text: "Open", kind: "open" };
    if (!decision) return { text: null, kind: "none" };
    var action = String(decision.action || "WATCH");
    if (action === "WATCH") return { text: "Quiet", kind: "quiet" };
    if (/UNKNOWN/i.test(action)) return { text: action, kind: "attention" };
    if (/REFUS/i.test(action)) return { text: action, kind: "refused" };
    if (/READY/i.test(action)) return { text: action, kind: "ready" };
    return { text: action, kind: "active" };
  }

  /* Actionable-first default order: phase rank, then surge inside the
     same rank (surge mapped to [0,1) so it never crosses ranks). */
  function actionableRank(kind, surge) {
    var base = { open: 5, ready: 4, active: 3, attention: 3, refused: 2,
      quiet: 1, none: 0 }[kind];
    if (base === undefined) base = 0;
    var s = Number(surge);
    var frac = Number.isFinite(s) && s > 0 ? s / (s + 1) : 0;
    return base + frac;
  }

  function decisionsBySymbol(decisionsPayload) {
    var map = {};
    if (decisionsPayload && Array.isArray(decisionsPayload.decisions)) {
      decisionsPayload.decisions.forEach(function (record) {
        if (!record || typeof record !== "object") return;
        var sym = record.symbol ||
          (record.lifecycle_state && record.lifecycle_state.symbol);
        if (sym) map[sym] = record;
      });
    }
    return map;
  }

  function runnerTokens(momentumPayload) {
    var st = momentumPayload && momentumPayload.state;
    var inner = st && st.state;
    var tokens = inner && inner.tokens;
    return tokens && typeof tokens === "object" ? tokens : {};
  }

  function runnerOpenSymbols(momentumPayload) {
    var st = momentumPayload && momentumPayload.state;
    var positions = st && st.positions;
    var out = {};
    if (positions && typeof positions === "object" &&
        !Array.isArray(positions)) {
      Object.keys(positions).forEach(function (key) {
        if (positions[key] && typeof positions[key] === "object") {
          out[key] = true;
        }
      });
    }
    return out;
  }

  function buildRows(streamPayload, decisionsPayload, momentumPayload,
      nowMs) {
    if (!streamPayload || !Array.isArray(streamPayload.tokens)) return [];
    var decisions = decisionsBySymbol(decisionsPayload);
    var tokens = runnerTokens(momentumPayload);
    var openSymbols = runnerOpenSymbols(momentumPayload);
    var out = [];
    streamPayload.tokens.forEach(function (token) {
      if (!token || typeof token !== "object") return;
      var sel = token.selection || [];
      if (sel.indexOf("top_surge") < 0) return;
      var data = token.data || {};
      var decision = decisions[token.symbol] || null;
      var ls = (decision && decision.lifecycle_state) || {};
      var runner = tokens[token.symbol] || null;
      var market = data.venues === "both" ? "Both" :
        (data.venues === "spot" ? "Spot" : "Perp");
      var price = data.mark !== undefined && data.mark !== null ?
        data.mark : (data.spot_ask !== undefined &&
          data.spot_ask !== null ? data.spot_ask :
          (decision ? decision.price : null));
      var preIgnition = ls.pre_ignition_price !== undefined &&
        ls.pre_ignition_price !== null ? ls.pre_ignition_price :
        (runner ? runner.pre_ignition_price : null);
      var ignitionUtc = (decision && decision.ignition_utc) ||
        ls.ignition_utc || (runner && runner.ignition_utc) || null;
      var persistence = ls.persistence_count;
      if (typeof persistence !== "number" ||
          !Number.isFinite(persistence)) {
        persistence = null;
      }
      var open = Object.prototype.hasOwnProperty.call(
        openSymbols, token.symbol);
      var phase = phaseInfo(decision, open);
      var surge = data.surge_ratio !== undefined ? data.surge_ratio : null;
      out.push({
        key: token.symbol,
        symbol: token.symbol,
        market: market,
        route: (data.venues === "both" || data.venues === "perp") ?
          "Futures long" : "Observe only",
        price: price,
        surge: surge,
        move: movePct(price, preIgnition),
        confirm: persistence !== null &&
          (persistence > 0 || ignitionUtc) ? persistence : null,
        age: ageSeconds(ignitionUtc, nowMs),
        ignitionUtc: ignitionUtc,
        preIgnition: preIgnition,
        phase: phase.text,
        phaseKind: phase.kind,
        actionable: actionableRank(phase.kind, surge),
        decision: decision,
        runner: runner,
        data: data
      });
    });
    return out;
  }

  function applyFilter(rows, name, query) {
    var out = rows;
    if (name === "ready") {
      out = out.filter(function (r) { return r.phaseKind === "ready"; });
    } else if (name === "open") {
      out = out.filter(function (r) { return r.phaseKind === "open"; });
    } else if (name === "refused") {
      out = out.filter(function (r) { return r.phaseKind === "refused"; });
    } else if (name === "both") {
      out = out.filter(function (r) { return r.market === "Both"; });
    } else if (name === "spot") {
      out = out.filter(function (r) { return r.market === "Spot"; });
    } else if (name === "perp") {
      out = out.filter(function (r) { return r.market === "Perp"; });
    }
    var q = String(query || "").trim().toUpperCase();
    if (q) {
      out = out.filter(function (r) {
        return r.symbol.indexOf(q) >= 0;
      });
    }
    return out;
  }

  /* Summary facts from real rows only — nothing invented. */
  function summarize(rows) {
    var top = null;
    var venues = { Both: 0, Spot: 0, Perp: 0 };
    var open = 0;
    rows.forEach(function (r) {
      if (venues[r.market] !== undefined) venues[r.market] += 1;
      if (r.phaseKind === "open") open += 1;
      var s = Number(r.surge);
      if (Number.isFinite(s) &&
          (top === null || s > Number(top.surge))) {
        top = r;
      }
    });
    return { count: rows.length, top: top, venues: venues, open: open };
  }

  /* ---------- DOM module ---------- */

  if (typeof window !== "undefined" && window.QuantUI) {
    var UI = window.QuantUI;
    var table = null;
    var filter = "interesting";

    var FILTERS = [
      ["interesting", "Interesting"], ["ready", "Ready"],
      ["open", "Open"], ["refused", "Refused"],
      ["both", "Both venues"], ["spot", "Spot"], ["perp", "Perp"]
    ];

    var init = function () {
      var root = document.getElementById("root-volume");
      root.textContent = "";
      var block = UI.el("section", "block");
      block.appendChild(UI.el("h2", null, "Volume — interesting movers"));

      var summary = UI.el("div", "daystrip");
      summary.id = "volume-summary";
      block.appendChild(summary);

      var toolbar = UI.el("div", "toolbar");
      toolbar.id = "volume-toolbar";
      var search = document.createElement("input");
      search.type = "search";
      search.id = "volume-search";
      search.placeholder = "symbol";
      search.setAttribute("aria-label", "Search symbol");
      toolbar.appendChild(search);
      FILTERS.forEach(function (pair, index) {
        var btn = UI.el("button", "fbtn", pair[1]);
        btn.setAttribute("data-filter", pair[0]);
        btn.setAttribute("aria-pressed", index === 0 ? "true" : "false");
        toolbar.appendChild(btn);
      });
      block.appendChild(toolbar);

      var wrap = UI.el("div", "tablewrap");
      var tableNode = document.createElement("table");
      tableNode.className = "data";
      tableNode.id = "volume-table";
      tableNode.innerHTML =
        "<thead><tr>" +
        '<th data-sort="symbol">Asset</th>' +
        '<th data-sort="market">Market</th>' +
        '<th data-sort="route">Route</th>' +
        '<th data-sort="price">Price</th>' +
        '<th data-sort="surge">Surge</th>' +
        '<th data-sort="move">Move</th>' +
        '<th data-sort="confirm">Confirm</th>' +
        '<th data-sort="age">Age</th>' +
        '<th data-sort="actionable" class="sorted">Phase</th>' +
        "</tr></thead><tbody></tbody>";
      wrap.appendChild(tableNode);
      block.appendChild(wrap);

      var chipRow = UI.el("p", "rankchip");
      chipRow.id = "volume-rankchip";
      chipRow.appendChild(document.createTextNode(
        "Ranks changed while inspecting. "));
      chipRow.appendChild(UI.el("button", null, "Apply new order"));
      block.appendChild(chipRow);
      var meta = UI.el("p", "meta");
      meta.id = "volume-meta";
      block.appendChild(meta);
      block.appendChild(UI.el("p", "meta",
        "Interesting movers only. The complete All-markets view " +
        "arrives with a later data contract."));
      root.appendChild(block);

      toolbar.addEventListener("click", function (ev) {
        var btn = ev.target.closest(".fbtn");
        if (!btn) return;
        toolbar.querySelectorAll(".fbtn").forEach(function (node) {
          node.setAttribute("aria-pressed",
            node === btn ? "true" : "false");
        });
        filter = btn.getAttribute("data-filter");
        render();
      });
      search.addEventListener("input", render);

      table = new UI.KeyedTable({
        tableId: "volume-table",
        rankchipId: "volume-rankchip",
        sortKey: "actionable",
        expandRender: expand,
        columns: [
          { key: "symbol", cls: "sym",
            render: function (r) { return { text: r.symbol }; } },
          { key: "market",
            render: function (r) { return { text: r.market }; } },
          { key: "route", render: function (r) {
            return { text: r.route,
              cls: r.route === "Observe only" ? "dim" : "" };
          } },
          { key: "price", cls: "num", render: function (r) {
            return { text: UI.fmtNum(r.price) || "—" };
          } },
          { key: "surge", cls: "num", render: function (r) {
            var text = UI.fmtNum(r.surge, 1);
            return { text: text === null ? "—" : text + "×" };
          } },
          { key: "move", cls: "num", render: function (r) {
            var text = UI.fmtNum(r.move, 2);
            if (text === null) return { text: "—", cls: "dim" };
            return { text: (r.move > 0 ? "+" : "") + text + "%" };
          } },
          { key: "confirm", cls: "num", render: function (r) {
            return r.confirm === null ? { text: "—", cls: "dim" } :
              { text: String(r.confirm) };
          } },
          { key: "age", render: function (r) {
            return r.age === null ? { text: "—", cls: "dim" } :
              { text: UI.ageText(r.age) };
          } },
          { key: "actionable", render: function (r) {
            if (r.phase === null) {
              return { text: "No decision", cls: "dim" };
            }
            if (r.phaseKind === "quiet") {
              return { text: "Quiet", cls: "dim" };
            }
            var cls = r.phaseKind === "open" ? "b-green" :
              (r.phaseKind === "attention" ? "b-red" :
                (r.phaseKind === "refused" ? "b-amber" : "b-blue"));
            return { node: UI.el("span", "badge " + cls, r.phase) };
          } }
        ]
      });
    };

    var kvRow = function (dl, label, value) {
      if (value === null || value === undefined || value === "") return;
      dl.appendChild(UI.el("dt", null, label));
      dl.appendChild(UI.el("dd", null, String(value)));
    };

    var expand = function (row) {
      var box = UI.el("div");

      /* Eligibility: observation versus execution, never blurred. */
      if (row.route === "Observe only") {
        box.appendChild(UI.el("p", "meta",
          "This symbol has no perpetual future, so the machine can " +
          "only observe it. It will never trade it on the current " +
          "route."));
      } else {
        box.appendChild(UI.el("p", "meta",
          "Execution route: USD-M Futures long, where the machine's " +
          "own recorded rules pass."));
      }

      /* Decision chain in owner language, from the recorded row. */
      var decision = row.decision;
      if (decision) {
        var line = "Latest decision: " + (decision.action || "?");
        if (decision.job_eligible === true) {
          line += " · job-eligible";
        } else if (decision.job_eligible === false) {
          line += " · not job-eligible";
        }
        if (decision.job_reason) line += " — " + decision.job_reason;
        var when = UI.shortTime(decision.decision_utc || "");
        if (when) line += " · " + when;
        box.appendChild(UI.el("p", "meta", line));
      } else {
        box.appendChild(UI.el("p", "unavail",
          "Decision detail unavailable — partial source (this symbol " +
          "is outside the bounded recent-decision window)."));
      }

      /* Recorded signal facts; unrecorded rows are simply absent. */
      var data = row.data || {};
      var runner = row.runner || {};
      var dl = UI.el("dl", "kv");
      kvRow(dl, "1m quote volume", UI.fmtNum(data.vol_1m_quote, 0));
      kvRow(dl, "Median 1m volume", UI.fmtNum(data.vol_med_1m, 0));
      var surgeText = UI.fmtNum(data.surge_ratio, 1);
      kvRow(dl, "Surge", surgeText === null ? null : surgeText + "×");
      kvRow(dl, "Open interest", UI.fmtNum(data.oi, 0));
      var oiChange = UI.fmtNum(data.oi_change_pct, 2);
      kvRow(dl, "OI change", oiChange === null ? null : oiChange + "%");
      var spread = UI.fmtNum(runner.last_spread_pct, 3);
      kvRow(dl, "Spread", spread === null ? null : spread + "%");
      var gap = UI.fmtNum(data.basis_gap_pct, 3);
      kvRow(dl, "Basis gap", gap === null ? null : gap + "%");
      kvRow(dl, "Pre-ignition reference", UI.fmtNum(row.preIgnition));
      if (row.ignitionUtc) {
        kvRow(dl, "Ignition", UI.shortTime(row.ignitionUtc));
      }
      if (row.confirm !== null) {
        kvRow(dl, "Persistence checks", row.confirm);
      }
      if (dl.childNodes.length > 0) box.appendChild(dl);

      /* Open position facts come only from the runner's own state. */
      if (row.phaseKind === "open" && runner) {
        var pos = "Open position (runner truth)";
        var qty = UI.fmtNum(runner.qty);
        if (qty !== null) pos += ": quantity " + qty;
        var avg = UI.fmtNum(runner.avg_entry_price);
        if (avg !== null) pos += " · average entry " + avg;
        box.appendChild(UI.el("p", "meta", pos));
        box.appendChild(UI.el("p", "unavail",
          "Open P&L unavailable — no recorded valuation."));
      }

      box.appendChild(UI.el("p", "unavail",
        "History collecting — no measured series exists yet."));
      box.appendChild(UI.techDrawer(decision || row.runner || row.data));
      return box;
    };

    var renderSummary = function (allRows, streamPresent) {
      var host = document.getElementById("volume-summary");
      host.textContent = "";
      if (!streamPresent) return;
      var facts = summarize(allRows);
      var card = function (title, rows) {
        var node = UI.el("div", "daycard");
        node.style.cursor = "default";
        node.appendChild(UI.el("p", "dc-day", title));
        rows.forEach(function (pair) {
          var line = UI.el("div", "dc-row");
          line.appendChild(UI.el("span", null, pair[0]));
          line.appendChild(UI.el("span", "dc-num", pair[1]));
          node.appendChild(line);
        });
        return node;
      };
      host.appendChild(card("Movers", [
        ["Interesting", String(facts.count)],
        ["Open now", String(facts.open)]
      ]));
      var topRows = facts.top === null ?
        [["None recorded", "—"]] :
        [[facts.top.symbol,
          (UI.fmtNum(facts.top.surge, 1) || "—") + "×"]];
      host.appendChild(card("Top surge", topRows));
      host.appendChild(card("Venues", [
        ["Both", String(facts.venues.Both)],
        ["Spot", String(facts.venues.Spot)],
        ["Perp", String(facts.venues.Perp)]
      ]));
    };

    var render = function () {
      if (!table) return;
      var stream = UI.state.data.stream;
      var all = buildRows(stream, UI.state.data.decisions,
        UI.state.data.momentum, Date.now());
      var query = document.getElementById("volume-search").value;
      var list = applyFilter(all, filter, query);
      table.update(list);
      renderSummary(all, !!(stream && stream.present));
      var meta = document.getElementById("volume-meta");
      if (!stream || !stream.present) {
        meta.textContent = "Market data unavailable — nothing has " +
          "been delivered to this page yet.";
      } else if (all.length === 0) {
        meta.textContent = "No interesting movers in the current " +
          "data window.";
      } else if (list.length === 0) {
        meta.textContent = "No movers match this filter in the " +
          "current data window.";
      } else {
        meta.textContent = list.length + " movers, actionable phase " +
          "first, then surge · phases come from the machine's own " +
          "recent decisions (bounded last-50 window — partial source).";
      }
    };

    window.QuantTabs.volume = {
      needs: ["stream", "decisions", "momentum"],
      init: init,
      render: render
    };
  }

  /* Pure row logic exported for offline tests. */
  if (typeof module !== "undefined" && module.exports) {
    module.exports = {
      movePct: movePct,
      ageSeconds: ageSeconds,
      phaseInfo: phaseInfo,
      actionableRank: actionableRank,
      buildRows: buildRows,
      applyFilter: applyFilter,
      summarize: summarize
    };
  }
})();
