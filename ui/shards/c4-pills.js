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
