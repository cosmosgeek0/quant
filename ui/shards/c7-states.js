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
