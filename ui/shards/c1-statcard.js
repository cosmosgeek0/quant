(function registerStatCard(parts) {
  "use strict";

  const tones = new Set(["good", "warn", "bad", "accent"]);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) {
      node.className = cls;
    }
    if (text !== undefined && text !== null) {
      node.textContent = String(text);
    }
    return node;
  }

  function hasValue(value) {
    return value !== undefined && value !== null && value !== "";
  }

  parts.statCard = function statCard(opts) {
    const config = opts || {};
    const isAbsent = config.absent === true || !hasValue(config.value);
    const tone = tones.has(config.tone) ? config.tone : null;
    const card = el("div", "stat c1-statcard");
    const label = el("div", "stat-l c1-statcard__label", config.label);
    const value = el(
      "div",
      "stat-v c1-statcard__value",
      isAbsent ? "\u2014" : config.value
    );
    const sub = el(
      "div",
      "stat-s c1-statcard__sub",
      isAbsent ? config.absentWhy : config.sub
    );

    if (tone && !isAbsent) {
      card.classList.add(tone, "c1-statcard--" + tone);
    }
    if (isAbsent) {
      card.classList.add("absent", "c1-statcard--absent");
    }
    if (typeof config.title === "string" && config.title) {
      card.title = config.title;
    }

    card.append(label, value, sub);
    return card;
  };

  parts.statRow = function statRow(cards) {
    const row = el("div", "stat-row c1-stat-row");
    const items = Array.isArray(cards) ? cards : [];

    items.forEach(function appendCard(card) {
      if (card && typeof card.nodeType === "number") {
        row.appendChild(card);
      }
    });

    return row;
  };
}(window.QuantParts = window.QuantParts || {}));
