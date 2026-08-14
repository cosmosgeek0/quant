(function () {
  "use strict";

  var SYMBOL_TINTS = [
    "rgba(47, 107, 255, 0.13)",
    "rgba(10, 155, 100, 0.13)",
    "rgba(214, 161, 29, 0.13)",
    "rgba(79, 125, 243, 0.13)",
    "rgba(224, 65, 75, 0.13)",
    "rgba(154, 106, 8, 0.13)",
    "rgba(44, 49, 58, 0.13)",
    "rgba(105, 113, 128, 0.13)"
  ];

  var parts = window.QuantParts || {};
  window.QuantParts = parts;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function isAbsent(value) {
    return value === null || value === undefined ||
      (typeof value === "string" && value.trim() === "");
  }

  function isNode(value) {
    return value && typeof value === "object" &&
      typeof value.nodeType === "number";
  }

  function normalizeMinus(value) {
    return String(value).replace(/(^|[\s(])-(?=\d|\.)/g, "$1−");
  }

  function appendValue(node, value, normalizeNumericMinus) {
    if (isNode(value)) {
      node.appendChild(value);
      return;
    }
    node.textContent = normalizeNumericMinus ?
      normalizeMinus(value) : String(value);
  }

  function tintIndex(symbol) {
    var hash = 2166136261;
    var index;
    for (index = 0; index < symbol.length; index += 1) {
      hash ^= symbol.charCodeAt(index);
      hash = Math.imul(hash, 16777619);
    }
    return (hash >>> 0) % SYMBOL_TINTS.length;
  }

  function monogram(symbol) {
    var letters = symbol.match(/[A-Za-z]/g);
    if (!letters || letters.length === 0) letters = symbol.trim().split("");
    return letters.slice(0, 2).join("").toUpperCase();
  }

  parts.cell2 = function (primary, sub, opts) {
    var options = opts || {};
    var absent = isAbsent(primary);
    var root = el("div", "cell2 c3-cell2");
    var primaryNode = el("div", "c2p c3-cell2__primary");
    var subText = isAbsent(sub) ?
      (absent ? "Value not provided" : "Provenance not provided") :
      normalizeMinus(sub);
    var subNode = el("div", "c2s c3-cell2__sub", subText);
    var toneClasses = {
      good: "c3-cell2--good",
      bad: "c3-cell2--bad",
      warn: "c3-cell2--warn"
    };

    if (toneClasses[options.tone]) root.classList.add(toneClasses[options.tone]);
    if (options.mono) root.classList.add("c3-cell2--mono");
    if (options.num) root.classList.add("c3-cell2--num");
    if (absent) root.classList.add("c3-cell2--absent");

    appendValue(primaryNode, absent ? "—" : primary, !options.mono);
    subNode.title = subText;
    root.appendChild(primaryNode);
    root.appendChild(subNode);
    return root;
  };

  parts.symCell = function (symbol, subline) {
    var absent = isAbsent(symbol);
    var symbolText = absent ? "—" : String(symbol);
    var root = el("div", "sym c3-sym");
    var copy = el("div", "c3-sym__copy");
    var name = el("div", "c3-sym__name", symbolText);
    var subText = isAbsent(subline) ?
      (absent ? "Symbol not provided" : null) : String(subline);

    if (absent) {
      root.classList.add("c3-sym--absent");
    } else {
      var badge = el("span", "c3-sym__badge", monogram(symbolText));
      badge.style.setProperty(
        "--c3-sym-tint",
        SYMBOL_TINTS[tintIndex(symbolText)]
      );
      badge.setAttribute("aria-hidden", "true");
      root.appendChild(badge);
    }

    copy.appendChild(name);
    if (subText !== null) {
      var sub = el("div", "c3-sym__sub", normalizeMinus(subText));
      sub.title = normalizeMinus(subText);
      copy.appendChild(sub);
    }
    root.appendChild(copy);
    return root;
  };

  parts.moneyText = function (valueString, unit) {
    var absent = isAbsent(valueString);
    var root = el("span", "c3-money");
    var value = el(
      "span",
      "c3-money__value",
      absent ? "—" : normalizeMinus(valueString)
    );

    root.appendChild(value);
    if (!isAbsent(unit)) {
      root.appendChild(el("span", "unit c3-money__unit", String(unit)));
    }
    if (absent) {
      root.classList.add("c3-money--absent");
      root.appendChild(el("span", "c3-money__absence", "· Not provided"));
    }
    return root;
  };
}());
