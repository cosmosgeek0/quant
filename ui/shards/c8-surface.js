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
