(function () {
  "use strict";

  var parts = window.QuantParts || (window.QuantParts = {});
  var chipTones = ["ok", "warn", "bad", "info", "off"];
  var severityLabels = {
    critical: "Critical severity",
    warning: "Warning severity",
    info: "Info severity",
    debug: "Debug severity",
    off: "Severity unavailable"
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function hasText(value) {
    return value !== undefined && value !== null && String(value).trim() !== "";
  }

  function normalizedTone(tone) {
    var candidate = hasText(tone) ? String(tone).trim().toLowerCase() : "off";
    return chipTones.indexOf(candidate) === -1 ? "off" : candidate;
  }

  function normalizedSeverity(level) {
    var candidate = hasText(level) ? String(level).trim().toLowerCase() : "off";
    return Object.prototype.hasOwnProperty.call(severityLabels, candidate)
      ? candidate
      : "off";
  }

  function signedNumber(value) {
    var raw;
    var unsigned;
    var mantissa;
    var digits;
    var negative;
    var zero;

    if (typeof value !== "string") return null;
    raw = value.trim();
    if (!/^[+\-\u2212]?(?:(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d*)?|\.\d+)(?:[eE][+\-\u2212]?\d+)?$/.test(raw)) {
      return null;
    }

    negative = raw.charAt(0) === "-" || raw.charAt(0) === "\u2212";
    unsigned = /^[+\-\u2212]/.test(raw) ? raw.slice(1) : raw;
    mantissa = unsigned.split(/[eE]/, 1)[0];
    digits = mantissa.replace(/[,.]/g, "");
    zero = /^0+$/.test(digits);

    return {
      display: raw.replace(/-/g, "\u2212"),
      direction: zero ? "zero" : negative ? "negative" : "positive"
    };
  }

  parts.stateChip = function (text, tone, sub) {
    var missing = !hasText(text) || String(text).trim() === "\u2014";
    var safeTone = missing ? "off" : normalizedTone(tone);
    var chip = el("span", "chip c2-chip " + safeTone + " c2-chip-" + safeTone);
    var main = el("span", "c2-chip-main", missing ? "\u2014" : String(text).trim());
    var detail = hasText(sub) ? String(sub).trim() : missing ? "State unavailable" : "";

    chip.appendChild(main);
    if (detail) chip.appendChild(el("span", "c2-chip-sub", detail));
    return chip;
  };

  parts.sevMark = function (level) {
    var safeLevel = normalizedSeverity(level);
    var mark = el("span", "c2-sev-mark c2-sev-" + safeLevel);

    mark.setAttribute("role", "img");
    mark.setAttribute("aria-label", severityLabels[safeLevel]);
    mark.appendChild(el("span", "c2-sev-bar c2-sev-bar-low"));
    mark.appendChild(el("span", "c2-sev-bar c2-sev-bar-mid"));
    mark.appendChild(el("span", "c2-sev-bar c2-sev-bar-high"));
    return mark;
  };

  parts.deltaText = function (signedNumberString, unit) {
    var parsed = signedNumber(signedNumberString);
    var direction = parsed ? parsed.direction : "off";
    var delta = el("span", "c2-delta c2-delta-" + direction);
    var main = el("span", "c2-delta-main");
    var icon;

    if (!parsed) {
      main.appendChild(el("span", "c2-delta-value", "\u2014"));
      main.appendChild(el("span", "c2-delta-note", "Value unavailable"));
      delta.appendChild(main);
      return delta;
    }

    if (direction !== "zero") {
      icon = el("span", "ic c2-delta-icon");
      icon.style.setProperty(
        "--ic",
        "url(vendor/icons/" + (direction === "positive" ? "trending-up" : "trending-down") + ".svg)"
      );
      icon.setAttribute("aria-hidden", "true");
      delta.appendChild(icon);
    }

    main.appendChild(el("span", "c2-delta-value", parsed.display));
    if (hasText(unit)) main.appendChild(el("span", "unit c2-delta-unit", String(unit).trim()));
    delta.appendChild(main);
    return delta;
  };
}());
