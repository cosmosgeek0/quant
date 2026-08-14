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
