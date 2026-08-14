"use strict";
/* Assembled component shards — sources in ui/shards/ (staging). */
window.QuantParts = window.QuantParts || {};

/* ===== shards/c1-statcard.js ===== */
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

/* ===== shards/c2-chips.js ===== */
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

/* ===== shards/c3-cells.js ===== */
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

/* ===== shards/c4-pills.js ===== */
(function registerPillParts(parts) {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = text;
    return node;
  }

  function displayNumber(value) {
    return String(value).replace(/^-/, "\u2212");
  }

  parts.pillBar = function (items, activeKey, onSelect) {
    var bar = el("div", "pillbar c4-pillbar");

    items.forEach(function (item) {
      var selected = item.key === activeKey;
      var pill = el(
        "button",
        "pill c4-pill" + (selected ? " active" : ""),
        item.label
      );

      pill.type = "button";
      pill.setAttribute("aria-pressed", selected ? "true" : "false");

      if (typeof item.count === "number" && Number.isFinite(item.count)) {
        pill.appendChild(
          el("span", "pill-n c4-pill-n", displayNumber(item.count))
        );
      }

      if (typeof onSelect === "function") {
        pill.addEventListener("click", function () {
          onSelect(item.key);
        });
      }

      bar.appendChild(pill);
    });

    return bar;
  };

  parts.btn = function (label, iconName, onClick) {
    var button = el("button", "c4-btn");
    var safeIconName =
      typeof iconName === "string" && /^[a-z0-9-]+$/.test(iconName);

    button.type = "button";

    if (safeIconName) {
      var icon = el("span", "ic c4-btn-ic");
      icon.setAttribute("aria-hidden", "true");
      icon.style.setProperty(
        "--ic",
        "url(vendor/icons/" + iconName + ".svg)"
      );
      button.appendChild(icon);
    }

    button.appendChild(el("span", "c4-btn-label", label));

    if (typeof onClick === "function") {
      button.addEventListener("click", onClick);
    }

    return button;
  };
})(window.QuantParts = window.QuantParts || {});

/* ===== shards/c5-daystrip.js ===== */
(function registerDayStrip(parts) {
  "use strict";

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

  function isNumber(value) {
    return typeof value === "number" && Number.isFinite(value);
  }

  function numberText(value) {
    if (!isNumber(value)) {
      return "\u2014";
    }
    return String(value).replace(/^-/, "\u2212");
  }

  parts.dayStrip = function dayStrip(days, activeIso, onSelect) {
    const strip = el("div", "c5-daystrip");
    const items = Array.isArray(days) ? days : [];

    strip.setAttribute("role", "navigation");
    strip.setAttribute("aria-label", "Journal day navigation");

    items.forEach(function appendDay(day) {
      const item = day || {};
      const isActive = item.iso === activeIso;
      const totalIsKnown = isNumber(item.total);
      const card = el("button", "c5-daystrip__day");
      const date = el("span", "c5-daystrip__date", item.label);
      const eventsRow = el("span", "c5-daystrip__row c5-daystrip__events");
      const eventsLabel = el("span", "c5-daystrip__events-label", "Events");
      const eventTotal = el(
        "span",
        "c5-daystrip__events-total",
        numberText(item.total)
      );
      const footer = el("span", "c5-daystrip__row c5-daystrip__footer");
      const footerLabel = el(
        "span",
        "c5-daystrip__footer-label",
        isActive ? "Current page" : "Select day"
      );

      card.type = "button";
      card.dataset.iso = item.iso || "";

      if (item.isToday === true) {
        date.classList.add("c5-daystrip__date--today");
      }
      if (isActive) {
        card.classList.add("c5-daystrip__day--active");
        card.setAttribute("aria-current", "page");
      }
      if (!totalIsKnown) {
        const unavailable = el(
          "span",
          "c5-daystrip__unavailable",
          "Unavailable"
        );
        eventsLabel.appendChild(unavailable);
        eventTotal.classList.add("c5-daystrip__events-total--absent");
        eventTotal.setAttribute("aria-label", "Event total unavailable");
      }

      eventsRow.append(eventsLabel, eventTotal);
      footer.appendChild(footerLabel);

      if (isActive) {
        if (item.counts && typeof item.counts === "object") {
          const counts = el("span", "c5-daystrip__counts");
          ["c", "w", "i", "d"].forEach(function appendCount(key) {
            const value = item.counts[key];
            if (!isNumber(value)) {
              return;
            }
            counts.appendChild(
              el(
                "span",
                "c5-daystrip__count c5-daystrip__count--" + key,
                key.toUpperCase() + " " + numberText(value)
              )
            );
          });
          if (counts.childNodes.length) {
            footer.appendChild(counts);
          }
        }
      } else {
        const footerTotal = el(
          "span",
          "c5-daystrip__footer-total",
          numberText(item.total)
        );
        if (!totalIsKnown) {
          footerTotal.classList.add("c5-daystrip__footer-total--absent");
          footerTotal.setAttribute("aria-label", "Event total unavailable");
        }
        footer.appendChild(footerTotal);
      }

      card.append(date, eventsRow, footer);
      card.addEventListener("click", function selectDay() {
        if (typeof onSelect === "function") {
          onSelect(item.iso);
        }
      });
      strip.appendChild(card);
    });

    return strip;
  };
}(window.QuantParts = window.QuantParts || {}));

/* ===== shards/c6-feed.js ===== */
(function () {
  'use strict';

  const parts = window.QuantParts || (window.QuantParts = {});
  const tones = new Set(['ok', 'warn', 'bad', 'info', 'off']);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function requireEvent(evt) {
    if (!evt || typeof evt !== 'object') {
      throw new TypeError('feedRow requires an event object');
    }
    if (evt.title === null || evt.title === undefined) {
      throw new TypeError('feedRow requires evt.title');
    }
    if (!tones.has(evt.tone)) {
      throw new TypeError('feedRow requires a supported evt.tone');
    }
    if (evt.onExpand !== null && evt.onExpand !== undefined && typeof evt.onExpand !== 'function') {
      throw new TypeError('feedRow evt.onExpand must be a function or null');
    }
  }

  parts.feedRow = function feedRow(evt) {
    requireEvent(evt);

    const expandable = typeof evt.onExpand === 'function';
    const timeMissing = evt.timeText === null || evt.timeText === undefined;
    const captionMissing = evt.caption === null || evt.caption === undefined;
    const root = el('div', `c6-feed-row c6-feed-tone-${evt.tone}`);
    const main = el(expandable ? 'button' : 'div', 'c6-feed-main');
    const time = el('span', 'c6-feed-time', timeMissing ? '—' : evt.timeText);
    const markSlot = el('span', 'c6-feed-mark-slot');
    const mark = el('span', 'c6-feed-mark');
    const event = el('span', 'c6-feed-event');
    const title = el('span', 'c6-feed-title', evt.title);
    const separator = el('span', 'c6-feed-separator', ' — ');
    const caption = el(
      'span',
      captionMissing ? 'c6-feed-caption c6-feed-caption-absent' : 'c6-feed-caption',
      captionMissing ? 'detail unavailable' : evt.caption
    );
    const chevronSlot = el('span', 'c6-feed-chevron');

    if (timeMissing) time.title = 'unrecorded';

    markSlot.setAttribute('role', 'img');
    markSlot.setAttribute('aria-label', `Status: ${evt.tone}`);
    markSlot.appendChild(mark);

    event.append(title, separator, caption);
    main.append(time, markSlot, event);

    if (evt.symbol !== null && evt.symbol !== undefined) {
      main.appendChild(el('span', 'c6-feed-symbol', evt.symbol));
    }

    if (expandable) {
      const chevron = el('span', 'ic c6-feed-chevron-ic');
      chevron.style.setProperty('--ic', 'url(vendor/icons/chevron-down.svg)');
      chevron.setAttribute('aria-hidden', 'true');
      chevronSlot.appendChild(chevron);
    }

    chevronSlot.setAttribute('aria-hidden', 'true');
    main.appendChild(chevronSlot);

    if (!expandable) {
      root.appendChild(main);
      return root;
    }

    main.type = 'button';
    main.classList.add('c6-feed-main-expandable');
    main.setAttribute('aria-expanded', 'false');

    let expanded = false;
    let expandBlock = null;

    main.addEventListener('click', function () {
      if (!expandBlock) {
        const detail = evt.onExpand();
        if (!detail || typeof detail.nodeType !== 'number') {
          throw new TypeError('feedRow evt.onExpand must return a DOM node');
        }

        expandBlock = el('div', 'c6-feed-expand');
        expandBlock.setAttribute('role', 'region');
        expandBlock.setAttribute('aria-label', `${evt.title} details`);
        expandBlock.appendChild(detail);
        root.appendChild(expandBlock);
      }

      expanded = !expanded;
      expandBlock.hidden = !expanded;
      root.classList.toggle('c6-feed-open', expanded);
      main.setAttribute('aria-expanded', String(expanded));
    });

    root.appendChild(main);
    return root;
  };

  parts.feedGroupHead = function feedGroupHead(dateText, summaryText) {
    const head = el('div', 'c6-feed-group-head');
    head.append(
      el('span', 'c6-feed-group-date', dateText),
      el('span', 'c6-feed-group-summary', summaryText)
    );
    return head;
  };
}());

/* ===== shards/c7-states.js ===== */
(function () {
  "use strict";

  const kinds = new Set(["loading", "empty", "unavailable", "stale", "error"]);

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function suppliedText(value) {
    if (value === null || value === undefined) return null;
    const text = String(value);
    return text.trim() ? text : null;
  }

  function clockIcon(cls) {
    const icon = el("span", `ic ${cls}`);
    icon.style.setProperty("--ic", "url(vendor/icons/clock.svg)");
    icon.setAttribute("aria-hidden", "true");
    return icon;
  }

  window.QuantParts = window.QuantParts || {};

  window.QuantParts.stateBlock = function (kind, title, detail) {
    const safeKind = kinds.has(kind) ? kind : "unavailable";
    const titleText = suppliedText(title) || (safeKind === "loading" ? "Loading…" : "—");
    const root = el("div", `c7-state c7-state--${safeKind}`);
    const content = el("div", "c7-state-content");
    const titleNode = el("div", "c7-state-title");

    root.setAttribute("role", "status");

    if (safeKind === "stale") {
      titleNode.appendChild(clockIcon("c7-state-icon"));
    }
    titleNode.appendChild(el("span", "c7-state-title-text", titleText));
    content.appendChild(titleNode);

    if (safeKind !== "loading") {
      const detailText = suppliedText(detail) || "No state detail was provided.";
      content.appendChild(el("div", "c7-state-detail", detailText));
    }

    root.appendChild(content);
    return root;
  };

  window.QuantParts.ageChip = function (ageSeconds, amberAfter, redAfter) {
    const knownAge = typeof ageSeconds === "number"
      && Number.isFinite(ageSeconds)
      && ageSeconds >= 0;
    let tone = "";
    let label = "age unknown";

    if (knownAge) {
      label = `${ageSeconds}s ago`;
      if (typeof redAfter === "number" && Number.isFinite(redAfter) && ageSeconds > redAfter) {
        tone = "red";
      } else if (
        typeof amberAfter === "number"
        && Number.isFinite(amberAfter)
        && ageSeconds > amberAfter
      ) {
        tone = "amber";
      }
    } else {
      tone = "unknown";
    }

    const toneClass = tone ? ` c7-age-chip--${tone}` : "";
    const chip = el("span", `c7-age-chip${toneClass}`);
    chip.appendChild(clockIcon("c7-age-chip-icon"));
    chip.appendChild(el("span", "c7-age-chip-text", label));
    return chip;
  };
}());

/* ===== shards/c8-surface.js ===== */
(function registerSurface(parts) {
  "use strict";

  const liveStates = new Set(["live", "updating", "unavailable"]);
  const liveLabels = {
    live: "Live",
    updating: "Updating",
    unavailable: "Unavailable",
  };

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function objectValue(value) {
    return value && typeof value === "object" ? value : {};
  }

  function readableText(value) {
    if (value === null || value === undefined) return null;
    const text = String(value);
    return text.trim() ? text : null;
  }

  function displayText(value) {
    return String(value).replace(/(^|[\s(])-(?=\d)/g, "$1\u2212");
  }

  function iconName(value) {
    const name = readableText(value);
    return name && /^[a-z0-9-]+$/.test(name) ? name : null;
  }

  function isNode(value) {
    return Boolean(value && typeof value.nodeType === "number");
  }

  function isElement(value) {
    return Boolean(isNode(value) && value.nodeType === 1 && value.classList);
  }

  function surfaceToolbar(opts) {
    const config = objectValue(opts);
    const liveConfig = objectValue(config.live);
    const knownState = liveStates.has(liveConfig.state);
    const state = knownState ? liveConfig.state : "unavailable";
    const ageText = knownState ? readableText(liveConfig.ageText) : null;
    const safeIconName = iconName(config.iconName);
    const titleText = readableText(config.title);
    const toolbar = el("div", "sfc-band sfc-toolbar");
    const main = el("div", "sfc-toolbar-main");
    const identity = el("div", "sfc-toolbar-identity");
    const titleWrap = el("div", "sfc-toolbar-title-wrap");
    const absenceReasons = [];

    if (safeIconName) {
      const icon = el("span", "ic sfc-toolbar-icon");
      icon.style.setProperty(
        "--ic",
        "url(vendor/icons/" + safeIconName + ".svg)"
      );
      icon.setAttribute("aria-hidden", "true");
      identity.appendChild(icon);
    } else {
      const absentIcon = el(
        "span",
        "sfc-toolbar-icon sfc-toolbar-icon--absent",
        "\u2014"
      );
      absentIcon.setAttribute("role", "img");
      absentIcon.setAttribute("aria-label", "Icon unavailable");
      identity.appendChild(absentIcon);
      absenceReasons.push("Icon unavailable");
    }

    titleWrap.appendChild(
      el("span", "sfc-toolbar-title", titleText === null ? "\u2014" : titleText)
    );
    if (titleText === null) absenceReasons.push("Title unavailable");
    if (absenceReasons.length) {
      titleWrap.appendChild(
        el("span", "sfc-toolbar-identity-note", absenceReasons.join(" \u00b7 "))
      );
    }
    identity.appendChild(titleWrap);

    const live = el("div", "sfc-live");
    const dot = el("span", "sfc-live-dot sfc-live-dot--" + state);
    const copy = el("span", "sfc-live-copy");
    dot.setAttribute("aria-hidden", "true");

    if (ageText !== null) {
      copy.textContent = liveLabels[state] + " \u00b7 " + displayText(ageText);
    } else {
      copy.appendChild(
        el("span", "sfc-live-value", liveLabels[state] + " \u00b7 \u2014")
      );
      copy.appendChild(
        el(
          "span",
          "sfc-live-note",
          knownState ? "Age unavailable" : "Status unavailable"
        )
      );
    }

    live.setAttribute("role", "status");
    live.append(dot, copy);
    main.append(identity, live);
    toolbar.appendChild(main);

    if (isNode(config.right)) {
      const right = el("div", "sfc-toolbar-right");
      right.appendChild(config.right);
      toolbar.appendChild(right);
    }

    return toolbar;
  }

  parts.surfaceToolbar = surfaceToolbar;

  parts.surface = function surface(opts) {
    const config = objectValue(opts);
    const root = el("div", "sfc");
    const toolbarSource = Object.prototype.hasOwnProperty.call(config, "toolbar")
      ? config.toolbar
      : config;
    const toolbar = isElement(toolbarSource)
      ? toolbarSource
      : surfaceToolbar(toolbarSource);
    const sourceBands = Array.isArray(config.bands)
      ? config.bands
      : isNode(config.bands)
        ? [config.bands]
        : [];
    const bands = [];

    toolbar.classList.add("sfc-band", "sfc-toolbar");
    root.appendChild(toolbar);

    sourceBands.forEach(function appendBand(source) {
      if (!isNode(source)) return;
      const band = isElement(source) ? source : el("div", "sfc-band");
      if (isElement(source)) band.classList.add("sfc-band");
      else band.appendChild(source);
      bands.push(band);
      root.appendChild(band);
    });

    return { root: root, toolbar: toolbar, bands: bands };
  };
}(window.QuantParts = window.QuantParts || {}));

/* ===== shards/c9-tape.js ===== */
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

/* ===== shards/c10-select.js ===== */
(function registerPillSelect() {
  "use strict";

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function readableLabel(value) {
    if (value === null || value === undefined) return null;
    const text = String(value);
    return text.trim() ? text : null;
  }

  window.QuantParts = window.QuantParts || {};
  window.QuantParts.pillSelect = function pillSelect(opts) {
    const config = opts && typeof opts === "object" ? opts : {};
    const sourceOptions = Array.isArray(config.options) ? config.options : [];
    const options = sourceOptions.map(function normalizeOption(option) {
      const record = option && typeof option === "object" ? option : {};
      return {
        key: record.key,
        label: readableLabel(record.label),
      };
    });
    const hasActiveKey = Object.prototype.hasOwnProperty.call(config, "activeKey");
    let selectedIndex = hasActiveKey
      ? options.findIndex(function findActive(option) {
          return Object.is(option.key, config.activeKey);
        })
      : -1;
    let isOpen = false;

    const root = el("div", "c10-select");
    const trigger = el("button", "c10-select__trigger");
    const triggerCopy = el("span", "c10-select__trigger-copy");
    const chevron = el("span", "ic c10-select__chevron");
    const menu = el("div", "c10-select__menu");
    const optionNodes = [];

    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "listbox");
    trigger.setAttribute("aria-expanded", "false");
    chevron.setAttribute("aria-hidden", "true");
    chevron.style.setProperty("--ic", "url(vendor/icons/chevron-down.svg)");
    menu.setAttribute("role", "listbox");
    menu.setAttribute("aria-label", "Selection options");
    menu.tabIndex = -1;
    menu.hidden = true;

    function renderTrigger(label, absenceReason) {
      triggerCopy.replaceChildren();
      if (label !== null) {
        triggerCopy.append(el("span", "c10-select__trigger-label", label));
        return;
      }
      triggerCopy.append(el("span", "c10-select__trigger-label", "—"));
      triggerCopy.append(
        el("span", "c10-select__trigger-note", absenceReason || "Selection unavailable")
      );
    }

    function syncSelection() {
      optionNodes.forEach(function syncOption(row, index) {
        const selected = index === selectedIndex;
        row.classList.toggle("c10-select__option--selected", selected);
        row.setAttribute("aria-selected", selected ? "true" : "false");
      });
    }

    function focusOption(index) {
      if (!optionNodes.length) {
        menu.focus();
        return;
      }
      const boundedIndex = Math.max(0, Math.min(index, optionNodes.length - 1));
      optionNodes[boundedIndex].focus();
    }

    function openMenu(preferredIndex) {
      if (isOpen) return;
      isOpen = true;
      menu.hidden = false;
      trigger.setAttribute("aria-expanded", "true");
      const fallbackIndex = selectedIndex >= 0 ? selectedIndex : 0;
      focusOption(Number.isInteger(preferredIndex) ? preferredIndex : fallbackIndex);
    }

    function closeMenu(returnFocus) {
      if (!isOpen) return;
      isOpen = false;
      menu.hidden = true;
      trigger.setAttribute("aria-expanded", "false");
      if (returnFocus) trigger.focus();
    }

    function selectOption(index) {
      const option = options[index];
      if (!option) return;
      selectedIndex = index;
      syncSelection();
      renderTrigger(option.label, "Label unavailable");
      closeMenu(true);
      if (typeof config.onSelect === "function") config.onSelect(option.key);
    }

    options.forEach(function buildOption(option, index) {
      const row = el("button", "c10-select__option");
      const copy = el("span", "c10-select__option-copy");
      const check = el("span", "ic c10-select__check");

      row.type = "button";
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");
      if (option.label === null) {
        copy.append(el("span", "c10-select__option-label", "—"));
        copy.append(el("span", "c10-select__option-note", "Label unavailable"));
      } else {
        copy.append(el("span", "c10-select__option-label", option.label));
      }
      check.setAttribute("aria-hidden", "true");
      check.style.setProperty("--ic", "url(vendor/icons/check.svg)");
      row.append(copy, check);
      row.addEventListener("click", function chooseOption() {
        selectOption(index);
      });
      optionNodes.push(row);
      menu.append(row);
    });

    if (!options.length) {
      const empty = el("div", "c10-select__empty");
      empty.setAttribute("role", "option");
      empty.setAttribute("aria-disabled", "true");
      empty.append(el("span", "c10-select__empty-value", "—"));
      empty.append(el("span", "c10-select__empty-note", "No options available"));
      menu.append(empty);
    }

    const configuredLabel = readableLabel(config.label);
    const activeLabel = selectedIndex >= 0 ? options[selectedIndex].label : null;
    renderTrigger(configuredLabel !== null ? configuredLabel : activeLabel, "No selection available");
    syncSelection();
    trigger.append(triggerCopy, chevron);
    root.append(trigger, menu);

    trigger.addEventListener("click", function toggleMenu() {
      if (isOpen) closeMenu(false);
      else openMenu(selectedIndex);
    });

    trigger.addEventListener("keydown", function navigateFromTrigger(event) {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        openMenu(selectedIndex >= 0 ? selectedIndex : 0);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        openMenu(selectedIndex >= 0 ? selectedIndex : optionNodes.length - 1);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(false);
      }
    });

    menu.addEventListener("keydown", function navigateMenu(event) {
      const focusedIndex = optionNodes.indexOf(document.activeElement);
      if (event.key === "ArrowDown" && optionNodes.length) {
        event.preventDefault();
        focusOption(focusedIndex < 0 ? 0 : (focusedIndex + 1) % optionNodes.length);
      } else if (event.key === "ArrowUp" && optionNodes.length) {
        event.preventDefault();
        focusOption(
          focusedIndex < 0
            ? optionNodes.length - 1
            : (focusedIndex - 1 + optionNodes.length) % optionNodes.length
        );
      } else if (event.key === "Home" && optionNodes.length) {
        event.preventDefault();
        focusOption(0);
      } else if (event.key === "End" && optionNodes.length) {
        event.preventDefault();
        focusOption(optionNodes.length - 1);
      } else if (event.key === "Enter" && focusedIndex >= 0) {
        event.preventDefault();
        selectOption(focusedIndex);
      } else if (event.key === "Escape") {
        event.preventDefault();
        closeMenu(true);
      }
    });

    document.addEventListener("click", function closeOnOutsideClick(event) {
      if (isOpen && !root.contains(event.target)) closeMenu(false);
    });

    return root;
  };
})();

/* ===== shards/m1-livetrail.js ===== */
(function () {
  'use strict';

  var MAX_ROWS = 40;
  var EVENT_TONES = {
    ok: 'OK',
    warn: 'Warning',
    bad: 'Attention',
    info: 'Information',
    off: 'Unavailable'
  };
  var LAMP_TONES = {
    ok: 'OK',
    warn: 'Warning',
    bad: 'Attention',
    off: 'Unavailable'
  };
  var BREATHING = {
    calm: true,
    urgent: true,
    none: true
  };

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function hasOwn(object, key) {
    return Object.prototype.hasOwnProperty.call(object, key);
  }

  function isAbsent(value) {
    return value === null || value === undefined ||
      (typeof value === 'string' && value.trim() === '') ||
      (typeof value === 'number' && !Number.isFinite(value));
  }

  function displayText(value) {
    return String(value).replace(
      /(^|[\s([{=:])-(?=\s?(?:[$€£₹]\s*)?(?:\d|\.\d))/g,
      '$1−'
    );
  }

  function field(value, name) {
    if (isAbsent(value)) {
      return { text: '—', why: name + ' unavailable' };
    }
    return { text: displayText(value), why: '' };
  }

  function normalizedTone(value, tones) {
    return typeof value === 'string' && hasOwn(tones, value) ? value : 'off';
  }

  function setText(record, key, node, text) {
    if (record.rendered[key] === text) return;
    node.textContent = text;
    record.rendered[key] = text;
  }

  function setExplanation(record, key, node, text) {
    /* Calm absence (integrator patch): the reason lives in a tooltip on
       the field, never as a visible second line on every row. */
    setText(record, key, node, text);
    node.hidden = true;
    if (node.parentNode) {
      if (text) node.parentNode.title = text;
      else node.parentNode.removeAttribute("title");
    }
  }

  function makeEventRow() {
    var root = el('div', 'm1-livetrail-row m1-livetrail-tone-off');
    var time = el('span', 'm1-livetrail-time');
    var timeValue = el('span', 'm1-livetrail-time-value');
    var timeWhy = el('span', 'm1-livetrail-field-why');
    var markSlot = el('span', 'm1-livetrail-mark-slot');
    var mark = el('span', 'm1-livetrail-mark');
    var event = el('span', 'm1-livetrail-event');
    var title = el('span', 'm1-livetrail-title');
    var titleWhy = el('span', 'm1-livetrail-field-why');
    var separator = el('span', 'm1-livetrail-separator', ' — ');
    var caption = el('span', 'm1-livetrail-caption');
    var captionWhy = el('span', 'm1-livetrail-field-why');
    var symbol = el('span', 'm1-livetrail-symbol');
    var symbolValue = el('span', 'm1-livetrail-symbol-value');
    var symbolWhy = el('span', 'm1-livetrail-field-why');

    root.setAttribute('role', 'listitem');
    markSlot.setAttribute('role', 'img');
    timeWhy.hidden = true;
    titleWhy.hidden = true;
    captionWhy.hidden = true;
    symbolWhy.hidden = true;

    time.append(timeValue, timeWhy);
    markSlot.appendChild(mark);
    event.append(title, titleWhy, separator, caption, captionWhy);
    symbol.append(symbolValue, symbolWhy);
    root.append(time, markSlot, event, symbol);

    return {
      root: root,
      timeValue: timeValue,
      timeWhy: timeWhy,
      markSlot: markSlot,
      title: title,
      titleWhy: titleWhy,
      caption: caption,
      captionWhy: captionWhy,
      symbolValue: symbolValue,
      symbolWhy: symbolWhy,
      tone: 'off',
      rendered: {}
    };
  }

  function eventModel(event) {
    return {
      time: field(event.timeText, 'Time'),
      title: field(event.title, 'Title'),
      caption: field(event.caption, 'Caption'),
      symbol: field(event.symbol, 'Symbol'),
      tone: normalizedTone(event.tone, EVENT_TONES),
      toneKnown: typeof event.tone === 'string' && hasOwn(EVENT_TONES, event.tone)
    };
  }

  function updateEventRow(record, event) {
    var model = eventModel(event);
    var toneLabel = model.toneKnown ?
      'Status: ' + EVENT_TONES[model.tone] : 'Status unavailable';

    setText(record, 'time', record.timeValue, model.time.text);
    setExplanation(record, 'timeWhy', record.timeWhy, model.time.why);
    setText(record, 'title', record.title, model.title.text);
    setExplanation(record, 'titleWhy', record.titleWhy, model.title.why);
    setText(record, 'caption', record.caption, model.caption.text);
    setExplanation(record, 'captionWhy', record.captionWhy, model.caption.why);
    setText(record, 'symbol', record.symbolValue, model.symbol.text);
    setExplanation(record, 'symbolWhy', record.symbolWhy, model.symbol.why);
    if (record.rendered.toneLabel !== toneLabel) {
      record.markSlot.setAttribute('aria-label', toneLabel);
      record.rendered.toneLabel = toneLabel;
    }

    if (record.tone !== model.tone) {
      record.root.classList.remove('m1-livetrail-tone-' + record.tone);
      record.root.classList.add('m1-livetrail-tone-' + model.tone);
      record.tone = model.tone;
    }
  }

  function preparedEvents(events) {
    var prepared = [];
    var keys = new Set();
    var index;

    if (!Array.isArray(events)) {
      throw new TypeError('liveTrail update requires an events array');
    }

    for (index = 0; index < events.length && prepared.length < MAX_ROWS; index += 1) {
      var event = events[index];
      if (!event || typeof event !== 'object') {
        throw new TypeError('liveTrail events must be objects');
      }

      var key = event.key;
      var keyIsString = typeof key === 'string' && key.trim() !== '';
      var keyIsNumber = typeof key === 'number' && Number.isFinite(key);
      if (!keyIsString && !keyIsNumber) {
        throw new TypeError('liveTrail events require a stable string or number key');
      }
      if (keys.has(key)) continue;

      keys.add(key);
      prepared.push({ key: key, event: event });
    }

    return prepared;
  }

  window.QuantParts = window.QuantParts || {};
  window.QuantParts.liveTrail = function liveTrail(container) {
    if (!container || typeof container.appendChild !== 'function') {
      throw new TypeError('liveTrail requires a DOM container');
    }

    var root = el('section', 'm1-livetrail');
    var now = el('div', 'm1-livetrail-now m1-livetrail-lamp-off');
    var lamp = el('span', 'm1-livetrail-lamp');
    var lampGlow = el('span', 'm1-livetrail-lamp-glow');
    var lampCore = el('span', 'm1-livetrail-lamp-core');
    var nowCopy = el('span', 'm1-livetrail-now-copy');
    var phaseLine = el('span', 'm1-livetrail-phase-line');
    var phase = el('span', 'm1-livetrail-phase');
    var phaseWhy = el('span', 'm1-livetrail-now-why');
    var detailLine = el('span', 'm1-livetrail-detail-line');
    var detail = el('span', 'm1-livetrail-detail');
    var detailWhy = el('span', 'm1-livetrail-now-why');
    var trail = el('div', 'm1-livetrail-trail');
    var nowRendered = {};
    var rows = new Map();
    var animatedKeys = new Set();
    var lampTone = 'off';
    var breathing = 'none';

    root.setAttribute('aria-label', 'Live trail');
    now.setAttribute('role', 'status');
    now.setAttribute('aria-live', 'polite');
    lamp.setAttribute('role', 'img');
    trail.setAttribute('role', 'list');
    trail.setAttribute('aria-label', 'Recent activity');
    trail.setAttribute('aria-live', 'polite');
    trail.setAttribute('aria-relevant', 'additions text');
    trail.hidden = true;
    phaseWhy.hidden = true;
    detailWhy.hidden = true;

    lamp.append(lampGlow, lampCore);
    phaseLine.append(phase, phaseWhy);
    detailLine.append(detail, detailWhy);
    nowCopy.append(phaseLine, detailLine);
    now.append(lamp, nowCopy);
    root.append(now, trail);
    container.appendChild(root);

    function updateNow(nowState) {
      var state = nowState && typeof nowState === 'object' ? nowState : {};
      var phaseModel = field(state.phase, 'Phase');
      var detailModel = field(state.detail, 'Detail');
      var nextTone = normalizedTone(state.lamp, LAMP_TONES);
      var nextBreathing = typeof state.breathing === 'string' &&
        hasOwn(BREATHING, state.breathing) ? state.breathing : 'none';
      var toneKnown = typeof state.lamp === 'string' && hasOwn(LAMP_TONES, state.lamp);
      var lampLabel = toneKnown ?
        'Status: ' + LAMP_TONES[nextTone] : 'Status lamp unavailable';

      setText({ rendered: nowRendered }, 'phase', phase, phaseModel.text);
      setExplanation({ rendered: nowRendered }, 'phaseWhy', phaseWhy, phaseModel.why);
      setText({ rendered: nowRendered }, 'detail', detail, detailModel.text);
      setExplanation({ rendered: nowRendered }, 'detailWhy', detailWhy, detailModel.why);

      if (lampTone !== nextTone) {
        now.classList.remove('m1-livetrail-lamp-' + lampTone);
        now.classList.add('m1-livetrail-lamp-' + nextTone);
        lampTone = nextTone;
      }
      if (breathing !== nextBreathing) {
        now.classList.remove('m1-livetrail-breath-' + breathing);
        now.classList.add('m1-livetrail-breath-' + nextBreathing);
        breathing = nextBreathing;
      }
      lamp.setAttribute('aria-label', lampLabel);
    }

    function update(nowState, events) {
      var prepared = preparedEvents(events);
      var nextKeys = new Set();
      var ordered = [];

      updateNow(nowState);

      prepared.forEach(function (item) {
        var record = rows.get(item.key);
        if (!record) {
          record = makeEventRow();
          rows.set(item.key, record);
          if (!animatedKeys.has(item.key)) {
            record.root.classList.add('m1-livetrail-row-arrive');
            animatedKeys.add(item.key);
          }
        }

        updateEventRow(record, item.event);
        nextKeys.add(item.key);
        ordered.push(record);
      });

      var cursor = trail.firstChild;
      ordered.forEach(function (record) {
        if (record.root === cursor) {
          cursor = cursor.nextSibling;
        } else {
          trail.insertBefore(record.root, cursor);
        }
      });

      rows.forEach(function (record, key) {
        if (nextKeys.has(key)) return;
        record.root.remove();
        rows.delete(key);
      });

      trail.hidden = ordered.length === 0;
    }

    updateNow(null);
    return { update: update };
  };
}());

/* ===== shards/m2-motion.js ===== */
// HONEST-MOTION LAW: motion signals arrival/change/state, never drama; money digits never tween; nothing loops except lamps.
(function (parts) {
  "use strict";

  parts.flashCell = function (node, direction) {
    Array.prototype.slice.call(node.classList).forEach(function (className) {
      if (className.indexOf("m2-flash") === 0) {
        node.classList.remove(className);
      }
    });

    void node.offsetWidth;

    node.classList.add(
      direction === "up" ? "m2-flash-good" :
        direction === "down" ? "m2-flash-bad" : "m2-flash"
    );
  };

  parts.markChanged = function (node, newText, direction) {
    var nextText = newText === null || newText === undefined ? "—" : String(newText);

    if (node.textContent === nextText) return false;

    node.textContent = nextText;
    parts.flashCell(node, direction);
    return true;
  };
}(window.QuantParts = window.QuantParts || {}));
