(function registerSearchField(parts) {
  "use strict";

  function el(tag, cls, text) {
    const node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  parts.searchField = function searchField(onActivate) {
    const trigger = el("button", "s2-search");
    const icon = el("span", "ic s2-search__icon");
    const placeholder = el(
      "span",
      "s2-search__placeholder",
      "Search markets, records\u2026"
    );
    const keycap = el("span", "s2-search__keycap", "\u2318K");

    trigger.type = "button";
    trigger.setAttribute("aria-label", "Search markets and records");
    trigger.setAttribute("aria-keyshortcuts", "Meta+K");
    icon.setAttribute("aria-hidden", "true");
    icon.style.setProperty("--ic", "url(vendor/icons/search.svg)");
    keycap.setAttribute("aria-hidden", "true");
    trigger.append(icon, placeholder, keycap);

    function activate() {
      if (typeof onActivate === "function") onActivate();
    }

    trigger.addEventListener("focus", activate);
    trigger.addEventListener("click", activate);

    return trigger;
  };
}(window.QuantParts = window.QuantParts || {}));
