"use strict";

/* Overview tab — the money page. Owned by the Overview lane.
   Sources: strip/position/momentum/failures (core poll) + pnl, decisions.
   Iteration 1 truth: no account contract and no normalized ledger exist,
   so Net today, equity and capital render Unavailable — never zero,
   never a sample. Partial realized records stay visibly separate; the
   browser recalculates no money value. */

(function () {
  var UI = window.QuantUI;
  var basisTable = null;
  var volumeTable = null;
  var lastExposureJson = "";
  var lastActivityJson = "";
  var REALIZED_SHOWN = 8;

  /* ---------- init: static skeleton, built once ---------- */

  function statCard(id, label) {
    var card = UI.el("div", "daycard");
    card.style.cursor = "default";
    var cell = UI.el("div", "gcell");
    cell.id = id;
    cell.appendChild(UI.el("p", "glabel", label));
    cell.appendChild(UI.el("p", "gvalue skel", "loading"));
    cell.appendChild(UI.el("p", "gsub"));
    card.appendChild(cell);
    return card;
  }

  function sectionBlock(bodyId, title) {
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, title));
    var body = UI.el("div");
    body.id = bodyId;
    body.appendChild(UI.el("p", "empty skel", "loading"));
    block.appendChild(body);
    return block;
  }

  function recordTable(prefix, title, emptyText, metaText) {
    var block = UI.el("section", "block");
    block.appendChild(UI.el("h2", null, title));

    var empty = UI.el("p", "empty", emptyText);
    empty.id = prefix + "-empty";
    block.appendChild(empty);

    var wrap = UI.el("div", "tablewrap");
    wrap.id = prefix + "-wrap";
    wrap.hidden = true;
    var tableNode = document.createElement("table");
    tableNode.className = "data";
    tableNode.id = prefix + "-table";
    tableNode.innerHTML =
      "<thead><tr>" +
      '<th data-sort="ts" class="sorted">Time</th>' +
      '<th data-sort="symbol">Symbol</th>' +
      '<th data-sort="verdict">Verdict</th>' +
      '<th data-sort="net">Net</th>' +
      "</tr></thead><tbody></tbody>";
    wrap.appendChild(tableNode);
    block.appendChild(wrap);

    var chipRow = UI.el("p", "rankchip");
    chipRow.id = prefix + "-rankchip";
    chipRow.appendChild(document.createTextNode(
      "Order changed while inspecting. "));
    chipRow.appendChild(UI.el("button", null, "Apply new order"));
    block.appendChild(chipRow);

    var meta = UI.el("p", "meta", metaText);
    meta.id = prefix + "-meta";
    block.appendChild(meta);
    return block;
  }

  function recordColumns() {
    return [
      { key: "ts", cls: "num", render: function (r) {
        var text = UI.shortTime(r.ts || "");
        return text ? { text: text } :
          { text: "time unrecorded", cls: "dim" };
      } },
      { key: "symbol", cls: "sym",
        render: function (r) { return { text: r.symbol }; } },
      { key: "verdict", render: function (r) {
        var verdict = String(r.verdict || "");
        var cls = /UNKNOWN|FROZEN/.test(verdict.toUpperCase()) ?
          "b-red" : "b-gray";
        return { node: UI.el("span", "badge " + cls, verdict) };
      } },
      { key: "net", cls: "num", render: function (r) {
        if (r.net === null) return { text: "not recorded", cls: "dim" };
        var cls = Number(r.net) > 0 ? "pos" :
          (Number(r.net) < 0 ? "neg" : "");
        return { text: UI.fmtUsdt(r.net), cls: cls };
      } }
    ];
  }

  function expandRecord(row) {
    var box = UI.el("div");
    box.appendChild(UI.el("p", "meta",
      "Receipt record exactly as recorded — fees and funding " +
      "completeness depend on this receipt, not on this page."));
    var drawer = UI.techDrawer(row.record);
    drawer.open = true;
    box.appendChild(drawer);
    return box;
  }

  function init() {
    var root = document.getElementById("root-overview");
    root.textContent = "";

    var band = UI.el("div", "daystrip");
    band.id = "ov-statband";
    band.appendChild(statCard("ov-c-net", "Net today"));
    band.appendChild(statCard("ov-c-expo", "Exposure"));
    band.appendChild(statCard("ov-c-risk", "Risk"));
    band.appendChild(statCard("ov-c-data", "Data"));
    root.appendChild(band);

    var net = UI.el("section", "block");
    net.appendChild(UI.el("h2", null, "Net profit and loss — today"));
    net.appendChild(UI.el("p", "bignum unavailable",
      "Unavailable — ledger incomplete"));
    var stateLine = UI.el("p", "meta");
    stateLine.appendChild(UI.el("span", "badge b-gray", "Unreconciled"));
    stateLine.appendChild(document.createTextNode(
      " Account-wide accounting does not exist yet. The partial records " +
      "below are real but incomplete, and are never summed into a total."));
    net.appendChild(stateLine);
    net.appendChild(UI.el("p", "unavail",
      "P&L history not available yet — no normalized ledger exists."));
    root.appendChild(net);

    var grid1 = UI.el("div", "grid2");
    grid1.appendChild(sectionBlock("ov-exposure", "Current exposure"));
    grid1.appendChild(sectionBlock("ov-capital", "Capital"));
    root.appendChild(grid1);

    var grid2 = UI.el("div", "grid2");
    grid2.appendChild(recordTable("ov-basis",
      "Realized records — Basis (partial)",
      "No completed Basis accounting records in the delivered data.",
      "Partial: Basis accounting records only — not account-wide P&L."));
    grid2.appendChild(recordTable("ov-volume",
      "Realized records — Volume (partial)",
      "No realized Volume results in the delivered data.",
      "Partial: runner realized results only — not account-wide profit."));
    root.appendChild(grid2);

    var grid3 = UI.el("div", "grid2");
    grid3.appendChild(sectionBlock("ov-risk", "Risk usage"));
    grid3.appendChild(sectionBlock("ov-activity", "Recent activity"));
    root.appendChild(grid3);

    var capital = UI.clearTo("ov-capital");
    var capitalKv = UI.el("dl", "kv");
    ["Total equity", "Free Spot USDT", "Free Futures USDT",
     "Capital exposed"].forEach(function (label) {
      capitalKv.appendChild(UI.el("dt", null, label));
      capitalKv.appendChild(UI.el("dd", "unavail", "Unavailable"));
    });
    capital.appendChild(capitalKv);
    capital.appendChild(UI.el("p", "meta",
      "No account read contract exists yet. Equity and balances appear " +
      "when the account snapshot endpoint lands; they are never inferred " +
      "and never reused from documentation."));

    var risk = UI.clearTo("ov-risk");
    var riskKv = UI.el("dl", "kv");
    [["Entries today", "ov-r-entries"],
     ["Loss+fees today", "ov-r-loss"],
     ["Hard ceiling", "ov-r-ceiling"],
     ["Leverage", "ov-r-lev"],
     ["Entry lock", "ov-r-lock"]].forEach(function (pair) {
      riskKv.appendChild(UI.el("dt", null, pair[0]));
      var dd = UI.el("dd");
      dd.id = pair[1];
      riskKv.appendChild(dd);
    });
    risk.appendChild(riskKv);
    risk.appendChild(UI.el("p", "meta",
      "Recorded governor usage — incomplete for Basis. Caps are code " +
      "law; this page cannot change them."));

    basisTable = new UI.KeyedTable({
      tableId: "ov-basis-table",
      rankchipId: "ov-basis-rankchip",
      sortKey: "ts",
      expandRender: expandRecord,
      columns: recordColumns()
    });
    volumeTable = new UI.KeyedTable({
      tableId: "ov-volume-table",
      rankchipId: "ov-volume-rankchip",
      sortKey: "ts",
      expandRender: expandRecord,
      columns: recordColumns()
    });
  }

  /* ---------- per-poll facts (same truth the strip shows) ---------- */

  function setStat(id, value, cls, sub, title) {
    var cell = document.getElementById(id);
    var valueNode = cell.querySelector(".gvalue");
    valueNode.classList.remove("skel", "s-green", "s-amber", "s-red",
      "s-gray");
    if (cls) valueNode.classList.add(cls);
    if (valueNode.textContent !== value) {
      valueNode.textContent = value;
      valueNode.classList.remove("flash");
      void valueNode.offsetWidth;
      valueNode.classList.add("flash");
    }
    var subNode = cell.querySelector(".gsub");
    if (subNode.textContent !== (sub || "")) subNode.textContent = sub || "";
    cell.parentElement.title = title || "";
  }

  function setDd(id, text) {
    var node = document.getElementById(id);
    if (node.textContent === text) return;
    node.textContent = text;
    node.classList.remove("flash");
    void node.offsetWidth;
    node.classList.add("flash");
  }

  function renderStatBand() {
    var strip = UI.state.data.strip || {};
    var gov = strip.governor || {};
    var pull = strip.pull || {};
    var market = strip.market_pass || {};

    setStat("ov-c-net", "Unavailable", "s-gray", "Ledger incomplete",
      "Net realized P&L appears when the normalized ledger exists. " +
      "It is never derived in the browser.");

    var expo = UI.exposureLines();
    if (expo.length === 0) {
      setStat("ov-c-expo", "None", "", "Flat");
    } else {
      var first = expo[0];
      setStat("ov-c-expo", first.symbol, "s-amber",
        first.strategy + " · " + first.st +
        (expo.length > 1 ? " · +" + (expo.length - 1) + " more" : ""));
    }

    if (gov.present) {
      var riskCls = gov.entries_used >= gov.entries_cap ? "s-red" :
        (gov.entries_used > 0 ? "s-amber" : "");
      setStat("ov-c-risk", gov.entries_used + " of " + gov.entries_cap,
        riskCls, "loss+fees " + gov.loss_fees_used_usdt + " of " +
        gov.loss_fees_cap_usdt + " USDT");
    } else {
      setStat("ov-c-risk", "0 of 5", "", "No entries recorded today",
        "The governor day ledger has no rows today; caps are code law.");
    }

    var dataValue, dataCls, dataSub;
    if (!pull.present) {
      dataValue = "Unavailable"; dataCls = "s-gray";
      dataSub = "No data delivery yet";
    } else if (pull.ok === false || pull.level === "red") {
      dataValue = "Stale"; dataCls = "s-red";
      dataSub = "Delivery stopped " + UI.ageText(pull.age_seconds);
    } else if (pull.level === "amber") {
      dataValue = "Delayed"; dataCls = "s-amber";
      dataSub = "Updated " + UI.ageText(pull.age_seconds);
    } else {
      dataValue = "Live"; dataCls = "s-green";
      dataSub = market.state === "sealed" ?
        "Market feed sealed while paused" :
        "Updated " + UI.ageText(pull.age_seconds);
    }
    setStat("ov-c-data", dataValue, dataCls, dataSub,
      "Delivery age: " + UI.ageText(pull.age_seconds) +
      " · market pass: " + (market.state || "unknown"));
  }

  function renderExposure() {
    var expo = UI.exposureLines();
    var json = JSON.stringify(expo.map(function (line) {
      return [line.strategy, line.symbol, line.st];
    }));
    if (json === lastExposureJson) return;
    lastExposureJson = json;

    var root = UI.clearTo("ov-exposure");
    if (expo.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No standing position. The account is flat."));
      return;
    }
    expo.forEach(function (line) {
      var p = UI.el("p", null);
      var phase = UI.ownerPhaseForState(line.st);
      p.appendChild(UI.el("span", "badge " +
        (phase === "Attention" ? "b-red" : "b-amber"), phase));
      p.appendChild(document.createTextNode(
        " " + line.strategy + " · " + line.symbol + " "));
      var open = UI.el("button", "fbtn",
        "Open " + line.strategy + " →");
      open.addEventListener("click", function () {
        UI.selectTab(line.tab);
      });
      p.appendChild(open);
      root.appendChild(p);
      root.appendChild(UI.techDrawer(line.record));
    });
  }

  /* ---------- realized records (bounded, newest first) ---------- */

  function realizedRows(records, prefix) {
    var rows = records.map(function (record, index) {
      var net = record.realized && record.realized.net_usdt;
      if (net === undefined) net = record.net_usdt;
      return {
        key: prefix + "|" + (record.utc || "t") + "|" +
          (record.symbol || index) + "|" + index,
        ts: record.utc || null,
        symbol: record.symbol || "(unrecorded)",
        verdict: record.verdict || "record",
        net: net === undefined ? null : net,
        record: record
      };
    });
    rows.sort(function (a, b) {
      if (a.ts === null && b.ts === null) return 0;
      if (a.ts === null) return 1;
      if (b.ts === null) return -1;
      return a.ts < b.ts ? 1 : -1;
    });
    return rows;
  }

  function renderRecordTable(table, prefix, allRows, partialNote) {
    var empty = document.getElementById(prefix + "-empty");
    var wrap = document.getElementById(prefix + "-wrap");
    var meta = document.getElementById(prefix + "-meta");
    if (allRows.length === 0) {
      empty.hidden = false;
      wrap.hidden = true;
      meta.textContent = partialNote;
      return;
    }
    empty.hidden = true;
    wrap.hidden = false;
    var shown = allRows.slice(0, REALIZED_SHOWN);
    table.update(shown);
    meta.textContent = (allRows.length > REALIZED_SHOWN ?
      "Showing the latest " + REALIZED_SHOWN + " of " + allRows.length +
      " records — the full history belongs to Journal. " : "") + partialNote;
  }

  function renderRealized() {
    var pnl = UI.state.data.pnl;
    var basisRecords = (pnl && pnl.present && Array.isArray(pnl.records)) ?
      pnl.records : [];
    renderRecordTable(basisTable, "ov-basis",
      realizedRows(basisRecords, "b"),
      "Partial: Basis accounting records only — not account-wide P&L; " +
      "fees and funding completeness depend on each receipt.");

    var mom = UI.state.data.momentum;
    var volumeRecords = (mom && mom.realized &&
      Array.isArray(mom.realized.records)) ? mom.realized.records : [];
    renderRecordTable(volumeTable, "ov-volume",
      realizedRows(volumeRecords, "v"),
      "Partial: runner realized results only — not account-wide profit.");
  }

  function renderRisk() {
    var strip = UI.state.data.strip || {};
    var gov = strip.governor || {};
    setDd("ov-r-entries", (gov.present ? gov.entries_used : 0) + " of " +
      (gov.entries_cap || 5));
    setDd("ov-r-loss", (gov.present ? gov.loss_fees_used_usdt : "0") +
      " of " + (gov.loss_fees_cap_usdt || "1.00") + " USDT");
    setDd("ov-r-ceiling", "8 USDT first trade (Basis per leg)");
    setDd("ov-r-lev", "1× — locked");
    setDd("ov-r-lock", UI.ownerLockLine(gov.entry_lock_present));
  }

  function renderActivity() {
    var events = UI.buildEventList().slice(0, 5);
    var json = JSON.stringify(events.map(function (event) {
      return [event.ts, event.strat, event.sym, event.text];
    }));
    if (json === lastActivityJson) return;
    lastActivityJson = json;

    var root = UI.clearTo("ov-activity");
    if (events.length === 0) {
      root.appendChild(UI.el("p", "empty",
        "No completed trades yet. The machine is watching markets."));
      return;
    }
    var trail = UI.el("div", "trail");
    events.forEach(function (event) {
      trail.appendChild(UI.eventLine(event));
    });
    root.appendChild(trail);
    root.appendChild(UI.el("p", "meta", "Recent — partial sources."));
  }

  function render() {
    if (!basisTable) return;
    renderStatBand();
    renderExposure();
    renderRealized();
    renderRisk();
    renderActivity();
  }

  window.QuantTabs.overview = {
    needs: ["pnl", "decisions"],
    init: init,
    render: render
  };
})();
