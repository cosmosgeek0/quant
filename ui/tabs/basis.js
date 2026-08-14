"use strict";

/* Basis tab — both-venue opportunity product. Owned by the Basis lane.
   Sources: stream (selection contains basis_gap, venues both), candidates
   (real proposals with the full equation), position (Open state).
   Honesty: Edge exists only where a real proposal was evaluated; the
   browser never recalculates the kernel. The ONLY client arithmetic is
   the contract-sanctioned approximate leg/gross notionals, derived from
   the proposal's own frozen quantity and prices and always ≈-labeled.
   Charts say History collecting until a real history endpoint exists;
   no time selector or chart toggle ships before then (no dead controls).
   Phase claims only Observed / Proposal / Open — Focused, Confirming and
   Refused wait for the watcher decision read model. */

(function () {
  var UI = window.QuantUI;
  var table = null;
  var filter = "all";
  var stats = null;

  var EMPTY_LINES = {
    all: "No both-venue symbols in the current delivery.",
    edge: "No positive net edge now.",
    proposal: "No standing proposals.",
    open: "No open Basis position.",
    search: "No symbols match the search."
  };

  /* ---------- pure helpers ---------- */

  function finite(raw) {
    if (raw === null || raw === undefined || raw === "") return null;
    var value = Number(raw);
    return Number.isFinite(value) ? value : null;
  }

  /* Approximate notionals from the proposal's own frozen fields only.
     quantity is the shared base-asset quantity proven per leg by the
     watcher; spot_ask/perp_bid are the prices it evaluated. Display
     scale only — never part of the edge equation. */
  function legNotionals(proposal) {
    if (!proposal) return null;
    var quantity = finite(proposal.quantity);
    var spot = finite(proposal.spot_ask);
    var perp = finite(proposal.perp_bid);
    if (quantity === null || spot === null || perp === null) return null;
    return {
      spot: quantity * spot,
      perp: quantity * perp,
      gross: quantity * (spot + perp)
    };
  }

  function ownerExpiryText(iso) {
    var ttl = UI.secondsUntil(iso);
    if (ttl === null) return "Expiry unrecorded";
    if (ttl > 0) return "Opportunity expires in " + ttl + "s";
    return "Proposal expired " + (-ttl) + "s ago";
  }

  function fundingPeriodHours(ms) {
    var value = finite(ms);
    if (value === null || value <= 0) return null;
    return UI.fmtNum(value / 3600000, 2);
  }

  /* ---------- dom ---------- */

  function makeStat(label) {
    var node = UI.el("span", "chip");
    var dot = UI.el("span", "cdot c-gray");
    var sub = UI.el("span", "cmuted", "loading");
    node.appendChild(dot);
    node.appendChild(UI.el("span", null, label));
    node.appendChild(sub);
    return { node: node, dot: dot, sub: sub };
  }

  function setStat(stat, level, text) {
    stat.dot.className = "cdot c-" + level;
    if (stat.sub.textContent !== text) stat.sub.textContent = text;
  }

  function init() {
    var root = document.getElementById("root-basis");
    root.textContent = "";
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, "Basis — both-venue opportunities"));

    stats = {
      universe: makeStat("Both-venue"),
      proposals: makeStat("Proposals"),
      best: makeStat("Best edge")
    };
    var band = UI.el("div", "toolbar");
    band.id = "basis-band";
    band.appendChild(stats.universe.node);
    band.appendChild(stats.proposals.node);
    band.appendChild(stats.best.node);
    block.appendChild(band);

    var toolbar = UI.el("div", "toolbar");
    toolbar.id = "basis-toolbar";
    var search = document.createElement("input");
    search.type = "search";
    search.id = "basis-search";
    search.placeholder = "symbol";
    search.setAttribute("aria-label", "Search symbol");
    toolbar.appendChild(search);
    [["all", "All"], ["edge", "Positive edge"], ["proposal", "Proposal"],
     ["open", "Open"]].forEach(function (pair, index) {
      var btn = UI.el("button", "fbtn", pair[1]);
      btn.setAttribute("data-filter", pair[0]);
      btn.setAttribute("aria-pressed", index === 0 ? "true" : "false");
      toolbar.appendChild(btn);
    });
    block.appendChild(toolbar);

    var wrap = UI.el("div", "tablewrap");
    var tableNode = document.createElement("table");
    tableNode.className = "data";
    tableNode.id = "basis-table";
    tableNode.innerHTML =
      "<thead><tr>" +
      '<th data-sort="symbol">Asset</th>' +
      '<th data-sort="spot">Spot</th>' +
      '<th data-sort="perp">Perp</th>' +
      '<th data-sort="gap">Gap</th>' +
      '<th data-sort="edge" class="sorted">Edge</th>' +
      '<th data-sort="funding">Funding</th>' +
      '<th data-sort="planned">Planned</th>' +
      '<th data-sort="phase">Phase</th>' +
      "</tr></thead><tbody></tbody>";
    wrap.appendChild(tableNode);
    block.appendChild(wrap);

    var emptyLine = UI.el("p", "empty", "");
    emptyLine.id = "basis-empty";
    emptyLine.style.display = "none";
    block.appendChild(emptyLine);

    var chipRow = UI.el("p", "rankchip");
    chipRow.id = "basis-rankchip";
    chipRow.appendChild(document.createTextNode(
      "Ranks changed while inspecting. "));
    chipRow.appendChild(UI.el("button", null, "Apply new order"));
    block.appendChild(chipRow);
    var meta = UI.el("p", "meta");
    meta.id = "basis-meta";
    block.appendChild(meta);
    root.appendChild(block);

    toolbar.addEventListener("click", function (ev) {
      var btn = ev.target.closest(".fbtn");
      if (!btn) return;
      toolbar.querySelectorAll(".fbtn").forEach(function (node) {
        node.setAttribute("aria-pressed", node === btn ? "true" : "false");
      });
      filter = btn.getAttribute("data-filter");
      render();
    });
    search.addEventListener("input", render);

    table = new UI.KeyedTable({
      tableId: "basis-table",
      rankchipId: "basis-rankchip",
      sortKey: "edge",
      expandRender: expand,
      columns: [
        { key: "symbol", cls: "sym",
          render: function (r) { return { text: r.symbol }; } },
        { key: "spot", cls: "num", render: function (r) {
          return { text: UI.fmtNum(r.spot) || "—" };
        } },
        { key: "perp", cls: "num", render: function (r) {
          return { text: UI.fmtNum(r.perp) || "—" };
        } },
        { key: "gap", cls: "num", render: function (r) {
          var text = UI.fmtPct(r.gap);
          return { text: text || "—",
            cls: Number(r.gap) > 0 ? "pos" : "neg" };
        } },
        { key: "edge", cls: "num", render: function (r) {
          if (r.edge === null) return { text: "Not evaluated", cls: "dim" };
          return { text: UI.fmtPct(r.edge),
            cls: Number(r.edge) > 0 ? "pos" : "neg" };
        } },
        { key: "funding", cls: "num", render: function (r) {
          var text = UI.fmtPct(r.funding);
          if (text === null) return { text: "—", cls: "dim" };
          var carry = r.proposal ? UI.fmtPct(r.proposal.carry_pct) : null;
          return { text: carry === null ? text :
            text + " · carry " + carry };
        } },
        { key: "planned", cls: "num", render: function (r) {
          return r.planned === null ? { text: "—", cls: "dim" } :
            { text: "≈ " + UI.fmtNum(r.planned, 2) + " USDT /leg" };
        } },
        { key: "phase", render: function (r) {
          var cls = r.phase === "Open" ? "b-green" :
            (r.phase === "Proposal" ? "b-blue" : "b-gray");
          return { node: UI.el("span", "badge " + cls, r.phase) };
        } }
      ]
    });
  }

  /* ---------- row assembly ---------- */

  function proposalsBySymbol() {
    var map = {};
    var payload = UI.state.data.candidates;
    if (payload && Array.isArray(payload.candidates)) {
      payload.candidates.forEach(function (candidate) {
        var record = candidate.proposal || candidate;
        if (record && record.symbol) map[record.symbol] = record;
      });
    }
    return map;
  }

  /* Open only when the persisted state actually reads as holding — the
     position file may hold a finished (CLOSED) receipt. */
  function openBasisSymbol() {
    var pos = UI.state.data.position;
    if (!pos || !pos.position || typeof pos.position !== "object") {
      return null;
    }
    if (UI.ownerPhaseForState(pos.position.state) !== "Holding") return null;
    return pos.position.symbol || null;
  }

  var lastTotal = 0;

  function rows() {
    var stream = UI.state.data.stream;
    if (!stream || !Array.isArray(stream.tokens)) { lastTotal = 0; return []; }
    var proposals = proposalsBySymbol();
    var openSymbol = openBasisSymbol();
    var out = [];
    stream.tokens.forEach(function (token) {
      var sel = token.selection || [];
      if (sel.indexOf("basis_gap") < 0) return;
      var data = token.data || {};
      if (data.venues !== "both") return;
      if (data.basis_gap_pct === null ||
          data.basis_gap_pct === undefined) return;
      var proposal = proposals[token.symbol] || null;
      var legs = legNotionals(proposal);
      out.push({
        key: token.symbol,
        symbol: token.symbol,
        spot: data.spot_ask,
        perp: data.perp_bid,
        gap: data.basis_gap_pct,
        funding: data.funding_rate,
        edge: proposal ? proposal.net_edge_pct : null,
        planned: legs ? legs.spot : null,
        phase: openSymbol === token.symbol ? "Open" :
          (proposal ? "Proposal" : "Observed"),
        proposal: proposal,
        data: data
      });
    });
    lastTotal = out.length;
    if (filter === "edge") {
      out = out.filter(function (row) {
        return row.edge !== null && Number(row.edge) > 0;
      });
    } else if (filter === "proposal") {
      out = out.filter(function (row) { return row.proposal; });
    } else if (filter === "open") {
      out = out.filter(function (row) { return row.phase === "Open"; });
    }
    var query = document.getElementById("basis-search").value.trim()
      .toUpperCase();
    if (query) {
      out = out.filter(function (row) {
        return row.symbol.indexOf(query) >= 0;
      });
    }
    return out;
  }

  /* ---------- expanded row ---------- */

  function expand(row) {
    var box = UI.el("div");
    box.appendChild(UI.el("p", "unavail", "History collecting — no " +
      "measured price series exists yet. Gap and price charts appear " +
      "when a real history endpoint delivers points; nothing is drawn " +
      "from snapshots."));
    if (row.proposal) {
      var proposal = row.proposal;
      box.appendChild(UI.el("p", "meta", "Cost waterfall — values exactly " +
        "as the Basis backend supplied them; this page recalculates " +
        "nothing."));
      var waterfall = document.createElement("table");
      waterfall.className = "waterfall";
      function wf(label, value, total) {
        var tr = UI.el("tr", total ? "total" : "");
        tr.appendChild(UI.el("td", null, label));
        tr.appendChild(UI.el("td", null,
          value === null ? "not supplied" : value));
        waterfall.appendChild(tr);
      }
      wf("Raw entry gap", UI.fmtPct(proposal.entry_gap_pct));
      wf("Spot taker fee", UI.fmtPct(proposal.spot_taker_commission_pct));
      wf("Perp taker fee", UI.fmtPct(proposal.perp_taker_commission_pct));
      wf("Entry slippage", UI.fmtPct(proposal.entry_slippage_pct));
      wf("Exit slippage", UI.fmtPct(proposal.exit_slippage_pct));
      wf("Funding carry", UI.fmtPct(proposal.carry_pct));
      wf("Exit gap assumption", UI.fmtPct(proposal.exit_gap_pct));
      wf("Safety buffer", UI.fmtPct(proposal.safety_buffer_pct));
      wf("Executable net edge", UI.fmtPct(proposal.net_edge_pct), true);
      box.appendChild(waterfall);
      box.appendChild(UI.el("p", "meta",
        "Intended action: Buy Spot / Short Perp — shared quantity " +
        (UI.fmtNum(proposal.quantity) || "not supplied") + " per leg."));
      var legs = legNotionals(proposal);
      if (legs) {
        box.appendChild(UI.el("p", "meta",
          "≈ " + UI.fmtNum(legs.spot, 2) + " USDT spot leg · ≈ " +
          UI.fmtNum(legs.perp, 2) + " USDT perp leg · ≈ " +
          UI.fmtNum(legs.gross, 2) + " USDT gross across both legs " +
          "(approximate — derived only from the proposal's own frozen " +
          "quantity and prices)."));
      } else {
        box.appendChild(UI.el("p", "unavail",
          "Leg notionals not derivable — the proposal record is missing " +
          "quantity or price fields."));
      }
      var period = fundingPeriodHours(proposal.funding_period_ms);
      box.appendChild(UI.el("p", "meta",
        "Funding " + (UI.fmtPct(proposal.funding_rate) || "not supplied") +
        (period ? " per " + period + "h period" : "") +
        " · next-settlement countdown and projected credit/debit stay " +
        "unavailable until the backend emits those fields."));
      var proposalLine = UI.el("p", "meta",
        "Proposal " + (proposal.proposal_id || "(id unrecorded)") +
        " · standing · ");
      var expiry = UI.el("span", null,
        ownerExpiryText(proposal.expires_utc));
      expiry.setAttribute("data-expires", proposal.expires_utc || "");
      proposalLine.appendChild(expiry);
      box.appendChild(proposalLine);
      box.appendChild(UI.el("p", "unavail",
        "Persistence, stability and exact pass reasoning are not exposed " +
        "to this page yet — they arrive with the watcher decision read " +
        "model."));
    } else {
      box.appendChild(UI.el("p", "unavail",
        "Not evaluated — the watcher’s decision rows are not exposed to " +
        "this page yet. Only symbols with a current proposal show a real " +
        "net edge."));
    }
    var data = row.data || {};
    box.appendChild(UI.el("p", "meta",
      "Exchange facts: Spot step " + (UI.fmtNum(data.spot_step) || "?") +
      ", min " + (UI.fmtNum(data.spot_min_notional) || "?") +
      " USDT · Perp step " + (UI.fmtNum(data.perp_step) || "?") +
      ", min " + (UI.fmtNum(data.perp_min_notional) || "?") +
      " USDT · funding " + (UI.fmtPct(data.funding_rate) || "?")));
    box.appendChild(UI.techDrawer(row.proposal || row.data));
    return box;
  }

  /* ---------- render ---------- */

  function renderBand() {
    var stream = UI.state.data.stream;
    var candidates = UI.state.data.candidates;
    if (stream && stream.present) {
      var count = finite(stream.basis_gap_count);
      setStat(stats.universe, "green",
        (count === null ? "?" : count) + " symbols");
    } else {
      setStat(stats.universe, "gray", "no delivery");
    }
    if (!candidates || !candidates.present) {
      setStat(stats.proposals, "gray", "no delivery yet");
      setStat(stats.best, "gray", "none evaluated");
      return;
    }
    var list = Array.isArray(candidates.candidates) ?
      candidates.candidates : [];
    setStat(stats.proposals, list.length > 0 ? "green" : "gray",
      list.length + " standing");
    var first = list.length > 0 ?
      (list[0].proposal || list[0]) : null;
    var best = first ? UI.fmtPct(first.net_edge_pct) : null;
    if (best !== null) {
      setStat(stats.best, "green", best + " " + (first.symbol || ""));
    } else {
      setStat(stats.best, "gray", "none evaluated");
    }
  }

  function render() {
    if (!table) return;
    var meta = document.getElementById("basis-meta");
    var emptyLine = document.getElementById("basis-empty");
    var stream = UI.state.data.stream;
    renderBand();

    if (!UI.state.lastGoodAt && !stream) {
      meta.textContent = "Loading market data…";
      emptyLine.style.display = "none";
      return;
    }

    var list = rows();
    table.update(list);

    /* In-place countdown refresh: expiry text ticks every poll without
       rebuilding the expansion (live-update rule: cells, not sections). */
    document.querySelectorAll("#basis-table [data-expires]")
      .forEach(function (node) {
        var text = ownerExpiryText(node.getAttribute("data-expires"));
        if (node.textContent !== text) node.textContent = text;
      });

    if (!stream || !stream.present) {
      meta.textContent = "Market data unavailable — nothing has been " +
        "delivered to this page yet.";
      emptyLine.style.display = "none";
      return;
    }

    if (list.length === 0) {
      var query = document.getElementById("basis-search").value.trim();
      emptyLine.textContent = query ? EMPTY_LINES.search :
        EMPTY_LINES[filter] || EMPTY_LINES.all;
      emptyLine.style.display = "";
    } else {
      emptyLine.style.display = "none";
    }

    meta.textContent = lastTotal + " both-venue symbols in the " +
      "current delivery" +
      (list.length === lastTotal ? "" : " · " + list.length + " shown") +
      " · data " + (UI.shortTime(stream.as_of || "") || "age unknown") +
      " · Edge exists only where a real proposal was evaluated.";
  }

  window.QuantTabs.basis = {
    needs: ["stream", "candidates"],
    init: init,
    render: render
  };
})();
