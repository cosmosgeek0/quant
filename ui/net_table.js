/* Classes emitted: qnet-table, qnet-header, qnet-heading, qnet-group,
   qnet-row, qnet-component, qnet-name, qnet-detail, qnet-status,
   qnet-status-pill, qnet-status-dot, qnet-status-text, qnet-status-ok,
   qnet-status-warn, qnet-status-bad, qnet-value, qnet-trend, qnet-faint,
   qnet-cadence, qnet-cadence-select, qnet-cadence-fixed,
   qnet-refresh-cell, qnet-refresh, qnet-spin-kick, qnet-active,
   qnet-uptime; house classes: switch, knob. */
"use strict";

(function () {
  var QNet = window.QNet = window.QNet || {};

  function text(value) {
    return value === null || typeof value === "undefined"
      ? "" : String(value);
  }

  function rowKey(id) {
    return "$" + String(id);
  }

  function statusClass(value) {
    if (value === "ok" || value === "warn" || value === "bad") {
      return " qnet-status-" + value;
    }
    return "";
  }

  function makeCell(className, role) {
    var cell = document.createElement("div");
    cell.className = className;
    cell.setAttribute("role", role || "cell");
    return cell;
  }

  function makeRefreshIcon() {
    var svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    var path;

    svg.setAttribute("viewBox", "0 0 24 24");
    svg.setAttribute("width", "13");
    svg.setAttribute("height", "13");
    svg.setAttribute("fill", "none");
    svg.setAttribute("stroke", "currentColor");
    svg.setAttribute("stroke-linecap", "round");
    svg.setAttribute("stroke-linejoin", "round");
    svg.setAttribute("aria-hidden", "true");

    path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d",
      "M3 12a9 9 0 1 0 9-9 9.75 9.75 0 0 0-6.74 2.74L3 8");
    svg.appendChild(path);

    path = document.createElementNS("http://www.w3.org/2000/svg", "path");
    path.setAttribute("d", "M3 3v5h5");
    svg.appendChild(path);
    return svg;
  }

  function renderTrend(host, samples) {
    host.className = "qnet-trend";
    if (samples && QNet.bars && typeof QNet.bars.render === "function") {
      QNet.bars.render(host, samples);
      return;
    }
    host.textContent = "";
    host.removeAttribute("title");
    host.className += " qnet-faint";
    host.textContent = "\u2014";
  }

  function build(el, model) {
    var callbacks = model && model.callbacks ? model.callbacks : {};
    var rowElements = Object.create(null);
    var table = document.createElement("div");
    var header = document.createElement("div");
    var headings = ["COMPONENT", "STATUS", "LAST TICK", "TREND",
      "UPTIME", "CADENCE", "", "ACTIVE"];
    var groups = model && model.groups ? model.groups : [];
    var i;

    table.className = "qnet-table";
    table.setAttribute("role", "table");
    header.className = "qnet-header";
    header.setAttribute("role", "row");
    for (i = 0; i < headings.length; i += 1) {
      var heading = makeCell("qnet-heading", "columnheader");
      heading.textContent = headings[i];
      if (i === 5) {
        heading.setAttribute("aria-label", "Refresh");
      }
      header.appendChild(heading);
    }
    table.appendChild(header);

    function patchRow(record, row) {
      record.id = row.id;
      record.status.className = "qnet-status";
      record.pill.className = "qnet-status-pill" + statusClass(row.statusCls);
      record.statusText.textContent = text(row.statusText);
      record.value.textContent = text(row.value);
      renderTrend(record.trend, row.bars);
      record.uptime.className = "qnet-uptime" +
        (row.uptime == null ? " qnet-faint" : "");
      record.uptime.textContent = row.uptime == null
        ? "\u2014" : row.uptime.toFixed(row.uptime >= 99.95 ? 0 : 1) + "%";
      record.refresh.hidden = !row.canRefresh;
      record.refresh.setAttribute("aria-hidden", row.canRefresh ? "false" : "true");
      if (record.toggle) {
        record.toggle.setAttribute("aria-checked",
          row.active ? "true" : "false");
      }
    }

    function makeCadence(row, record) {
      var cadence = row.cadence;
      var select;
      var option;
      var fixed;
      var j;

      if (cadence && cadence.kind === "select") {
        select = document.createElement("select");
        select.className = "qnet-cadence-select";
        select.setAttribute("aria-label", text(row.name) + " cadence");
        for (j = 0; j < cadence.options.length; j += 1) {
          option = document.createElement("option");
          option.value = String(cadence.options[j]);
          option.textContent = String(cadence.options[j]) + "s";
          select.appendChild(option);
        }
        select.value = String(cadence.value);
        select.addEventListener("change", function () {
          var seconds = parseInt(select.value, 10);
          if (typeof callbacks.onCadence === "function") {
            callbacks.onCadence(record.id, seconds);
          }
        });
        record.cadence.appendChild(select);
        record.select = select;
        return;
      }

      fixed = document.createElement("span");
      if (cadence && cadence.kind === "fixed") {
        fixed.className = "qnet-cadence-fixed";
        fixed.textContent = text(cadence.text);
      } else {
        fixed.className = "qnet-cadence-fixed qnet-faint";
        fixed.textContent = "\u2014";
      }
      record.cadence.appendChild(fixed);
    }

    function makeRow(row) {
      var record = {};
      var rowEl = document.createElement("div");
      var name = document.createElement("div");
      var detail = document.createElement("div");
      var pill = document.createElement("span");
      var dot = document.createElement("span");
      var statusText = document.createElement("span");
      var refresh = document.createElement("button");
      var toggle = document.createElement("button");
      var knob = document.createElement("span");
      var refreshTimer = null;

      rowEl.className = "qnet-row";
      rowEl.setAttribute("role", "row");
      rowEl.setAttribute("data-qnet-row", text(row.id));

      record.id = row.id;
      record.row = rowEl;
      record.component = makeCell("qnet-component");
      name.className = "qnet-name";
      name.textContent = text(row.name);
      detail.className = "qnet-detail";
      detail.textContent = text(row.detail);
      record.component.appendChild(name);
      record.component.appendChild(detail);

      record.status = makeCell("qnet-status");
      record.pill = pill;
      pill.className = "qnet-status-pill";
      dot.className = "qnet-status-dot";
      statusText.className = "qnet-status-text";
      record.statusText = statusText;
      pill.appendChild(dot);
      pill.appendChild(statusText);
      record.status.appendChild(pill);

      record.value = makeCell("qnet-value");
      record.trend = makeCell("qnet-trend");
      record.uptime = makeCell("qnet-uptime");
      record.cadence = makeCell("qnet-cadence");
      makeCadence(row, record);

      record.refreshCell = makeCell("qnet-refresh-cell");
      refresh.className = "qnet-refresh";
      refresh.type = "button";
      refresh.setAttribute("aria-label", "Refresh " + text(row.name));
      refresh.appendChild(makeRefreshIcon());
      refresh.addEventListener("click", function () {
        refresh.classList.remove("qnet-spin-kick");
        void refresh.offsetWidth;
        refresh.classList.add("qnet-spin-kick");
        if (refreshTimer !== null) { window.clearTimeout(refreshTimer); }
        refreshTimer = window.setTimeout(function () {
          refresh.classList.remove("qnet-spin-kick");
          refreshTimer = null;
        }, 650);
        if (typeof callbacks.onRefresh === "function") {
          callbacks.onRefresh(record.id);
        }
      });
      record.refresh = refresh;
      record.refreshCell.appendChild(refresh);

      record.active = makeCell("qnet-active");
      /* a switch exists only where flipping it does something real
         today (DESIGN.md §4) — other rows leave the cell empty */
      if (row.hasControl) {
        toggle.className = "switch";
        toggle.type = "button";
        toggle.setAttribute("role", "switch");
        toggle.setAttribute("aria-label", text(row.name) + " active");
        knob.className = "knob";
        toggle.appendChild(knob);
        toggle.addEventListener("click", function () {
          var on = toggle.getAttribute("aria-checked") !== "true";
          toggle.setAttribute("aria-checked", on ? "true" : "false");
          if (typeof callbacks.onToggle === "function") {
            callbacks.onToggle(record.id, on);
          }
        });
        record.toggle = toggle;
        record.active.appendChild(toggle);
      } else {
        record.toggle = null;
        var noCtl = document.createElement("span");
        noCtl.className = "qnet-nocontrol";
        noCtl.textContent = "—";
        record.active.appendChild(noCtl);
      }

      rowEl.appendChild(record.component);
      rowEl.appendChild(record.status);
      rowEl.appendChild(record.value);
      rowEl.appendChild(record.trend);
      rowEl.appendChild(record.uptime);
      rowEl.appendChild(record.cadence);
      rowEl.appendChild(record.refreshCell);
      rowEl.appendChild(record.active);
      return record;
    }

    el.textContent = "";
    el.appendChild(table);

    for (i = 0; i < groups.length; i += 1) {
      var group = groups[i];
      var groupEl = document.createElement("div");
      var rows = group.rows || [];
      var j;

      groupEl.className = "qnet-group";
      groupEl.setAttribute("role", "rowheader");
      groupEl.setAttribute("data-qnet-group", text(group.id));
      groupEl.textContent = text(group.label);
      table.appendChild(groupEl);
      for (j = 0; j < rows.length; j += 1) {
        var record = makeRow(rows[j]);
        rowElements[rowKey(rows[j].id)] = record;
        table.appendChild(record.row);
        patchRow(record, rows[j]);
      }
    }

    return {
      update: function (nextModel) {
        var nextGroups = nextModel && nextModel.groups
          ? nextModel.groups : [];
        var gi;

        callbacks = nextModel && nextModel.callbacks
          ? nextModel.callbacks : {};
        for (gi = 0; gi < nextGroups.length; gi += 1) {
          var nextRows = nextGroups[gi].rows || [];
          var ri;
          for (ri = 0; ri < nextRows.length; ri += 1) {
            var found = rowElements[rowKey(nextRows[ri].id)];
            if (found) { patchRow(found, nextRows[ri]); }
          }
        }
      }
    };
  }

  QNet.table = QNet.table || {};
  QNet.table.build = build;
}());
