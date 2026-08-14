(function () {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function fact(row) {
    if (Array.isArray(row)) {
      return {
        label: row[0],
        value: row[1],
        explanation: row[2],
        numeric: row[3]
      };
    }
    return row && typeof row === "object" ? row : {};
  }

  function isAbsent(value) {
    return value === null || value === undefined ||
      (typeof value === "string" && value.trim() === "") ||
      (typeof value === "number" && !Number.isFinite(value));
  }

  function displayText(value) {
    return String(value).replace(/^(\s*)-(?=\s?(?:\d|\.\d))/, "$1−");
  }

  function isNumericLike(value, explicit) {
    if (typeof explicit === "boolean") return explicit;
    if (typeof value === "number") return Number.isFinite(value);
    if (typeof value !== "string") return false;
    return /^[+\-−]?(?:[$€£₹]\s*)?(?:\d[\d,]*(?:\.\d+)?|\.\d+)(?:\s*(?:%|[A-Za-z][A-Za-z0-9./_-]*))?$/.test(
      value.trim()
    );
  }

  window.QuantParts = window.QuantParts || {};
  window.QuantParts.tapeWaterfall = function (rows) {
    var root = el("div", "tape-waterfall");
    var list = Array.isArray(rows) ? rows : [];

    list.forEach(function (source) {
      var item = fact(source);
      var absent = isAbsent(item.value);
      var row = el("div", "tape-waterfall-row");
      var label = isAbsent(item.label) ? "—" : String(item.label);
      var numeric = isNumericLike(item.value, item.numeric);
      var valueBox = el(
        "div",
        "tape-waterfall-valuebox" +
          (numeric ? " tape-waterfall-valuebox-num" : "")
      );
      var value = el(
        "span",
        "tape-waterfall-value",
        absent ? "—" : displayText(item.value)
      );

      row.appendChild(el("span", "tape-waterfall-label", label));
      valueBox.appendChild(value);
      if (absent) {
        var explanation = isAbsent(item.explanation) ?
          "No value provided" : String(item.explanation);
        valueBox.appendChild(el("span", "tape-waterfall-why", explanation));
      }
      row.appendChild(valueBox);
      root.appendChild(row);
    });

    return root;
  };
}());
