(function registerSettings(parts) {
  "use strict";

  var STORAGE_KEY = "quant-ui-prefs";
  var DEFAULT_PREFS = {
    theme: "light",
    accent: "blue",
    zoom: 1,
    clockZone: "UTC",
    motion: "on",
  };
  var ACCENTS = [
    { key: "red", label: "Red", color: "#e5484d", ink: "#b42318", rgb: [229, 72, 77] },
    { key: "orange", label: "Orange", color: "#f2760c", ink: "#b45309", rgb: [242, 118, 12] },
    { key: "yellow", label: "Yellow", color: "#eab308", ink: "#8a6d00", rgb: [234, 179, 8] },
    { key: "green", label: "Green", color: "#12a150", ink: "#0a7c3d", rgb: [18, 161, 80] },
    { key: "blue", label: "Blue", color: "#2f6bff", ink: "#1d4ed8", rgb: [47, 107, 255] },
    { key: "indigo", label: "Indigo", color: "#5b5bef", ink: "#4338ca", rgb: [91, 91, 239] },
    { key: "violet", label: "Violet", color: "#9333ea", ink: "#7c22ce", rgb: [147, 51, 234] },
  ];
  var ZOOMS = [0.9, 1, 1.1, 1.25];
  var volatilePrefs = null;
  var instanceCount = 0;

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function copyPrefs(prefs) {
    return {
      theme: prefs.theme,
      accent: prefs.accent,
      zoom: prefs.zoom,
      clockZone: prefs.clockZone,
      motion: prefs.motion,
    };
  }

  function accentFor(key) {
    var match = ACCENTS.find(function findAccent(accent) {
      return accent.key === key;
    });
    return match || ACCENTS[4];
  }

  function normalizePrefs(value) {
    var source = value && typeof value === "object" && !Array.isArray(value) ? value : {};
    var accent = accentFor(source.accent);

    return {
      theme: source.theme === "dark" ? "dark" : DEFAULT_PREFS.theme,
      accent: accent.key,
      zoom: ZOOMS.indexOf(source.zoom) >= 0 ? source.zoom : DEFAULT_PREFS.zoom,
      clockZone: source.clockZone === "IST" ? "IST" : DEFAULT_PREFS.clockZone,
      motion: source.motion === "off" ? "off" : DEFAULT_PREFS.motion,
    };
  }

  function readPrefs() {
    if (volatilePrefs) return copyPrefs(volatilePrefs);

    try {
      var stored = window.localStorage.getItem(STORAGE_KEY);
      if (stored !== null) return normalizePrefs(JSON.parse(stored));
    } catch (error) {
      return copyPrefs(DEFAULT_PREFS);
    }

    return copyPrefs(DEFAULT_PREFS);
  }

  function rgba(rgb, alpha) {
    return "rgba(" + rgb.join(", ") + ", " + alpha + ")";
  }

  function applyRootPrefs(prefs) {
    var root = document.documentElement;
    var accent = accentFor(prefs.accent);
    var zoomPercent = {
      "0.9": "90%",
      "1": "100%",
      "1.1": "110%",
      "1.25": "125%",
    }[String(prefs.zoom)];

    root.dataset.theme = prefs.theme;
    root.style.setProperty("--blue", accent.color);
    root.style.setProperty("--blue-ink", accent.ink);
    root.style.setProperty("--blue-soft", rgba(accent.rgb, "0.11"));
    root.style.setProperty("--blue-line", rgba(accent.rgb, "0.42"));
    root.style.fontSize = zoomPercent;

    if (prefs.motion === "off") root.dataset.motion = "off";
    else delete root.dataset.motion;
  }

  parts.getPrefs = function getPrefs() {
    return readPrefs();
  };

  parts.settings = function settings() {
    var controls = [];
    var isOpen = false;
    var panelId = "s3-settings-panel-" + String(++instanceCount);
    var trigger = el("button", "s3-settings__trigger");
    var triggerIcon = el("span", "ic s3-settings__trigger-icon");
    var triggerLabel = el("span", "s3-settings__trigger-label", "Appearance");
    var panel = el("div", "s3-settings__panel");

    trigger.type = "button";
    trigger.setAttribute("aria-haspopup", "dialog");
    trigger.setAttribute("aria-expanded", "false");
    trigger.setAttribute("aria-controls", panelId);
    triggerIcon.setAttribute("aria-hidden", "true");
    triggerIcon.style.setProperty("--ic", "url(vendor/icons/settings.svg)");
    trigger.append(triggerIcon, triggerLabel);

    panel.id = panelId;
    panel.hidden = true;
    panel.setAttribute("role", "dialog");
    panel.setAttribute("aria-label", "Appearance settings");

    function syncControls(prefs) {
      controls.forEach(function syncControl(control) {
        var selected = prefs[control.pref] === control.value;
        control.node.classList.toggle(control.selectedClass, selected);
        control.node.setAttribute("aria-pressed", selected ? "true" : "false");
      });
    }

    function apply() {
      var prefs = readPrefs();
      applyRootPrefs(prefs);
      syncControls(prefs);
      return copyPrefs(prefs);
    }

    function savePreference(pref, value) {
      var next = readPrefs();
      var persisted = false;

      next[pref] = value;
      next = normalizePrefs(next);

      try {
        window.localStorage.setItem(STORAGE_KEY, JSON.stringify(next));
        persisted = true;
        volatilePrefs = null;
      } catch (error) {
        volatilePrefs = copyPrefs(next);
      }

      if (persisted) apply();
      else {
        applyRootPrefs(next);
        syncControls(next);
      }
    }

    function addSegmentedRow(label, pref, options) {
      var row = el("div", "s3-settings__row");
      var rowLabel = el("span", "s3-settings__label", label);
      var segments = el("div", "s3-settings__segments");

      segments.setAttribute("role", "group");
      segments.setAttribute("aria-label", label);

      options.forEach(function addOption(option) {
        var button = el("button", "s3-settings__pill", option.label);
        button.type = "button";
        button.setAttribute("aria-pressed", "false");
        button.addEventListener("click", function selectOption() {
          savePreference(pref, option.value);
        });
        controls.push({
          node: button,
          pref: pref,
          value: option.value,
          selectedClass: "s3-settings__pill--selected",
        });
        segments.appendChild(button);
      });

      row.append(rowLabel, segments);
      panel.appendChild(row);
    }

    function addAccentRow() {
      var row = el("div", "s3-settings__row");
      var rowLabel = el("span", "s3-settings__label", "Accent");
      var accents = el("div", "s3-settings__accents");

      accents.setAttribute("role", "group");
      accents.setAttribute("aria-label", "Accent");

      ACCENTS.forEach(function addAccent(accent) {
        var button = el("button", "s3-settings__accent");
        button.type = "button";
        button.setAttribute("aria-label", accent.label + " accent");
        button.setAttribute("aria-pressed", "false");
        button.style.setProperty("--s3-settings-swatch", accent.color);
        button.addEventListener("click", function selectAccent() {
          savePreference("accent", accent.key);
        });
        controls.push({
          node: button,
          pref: "accent",
          value: accent.key,
          selectedClass: "s3-settings__accent--selected",
        });
        accents.appendChild(button);
      });

      row.append(rowLabel, accents);
      panel.appendChild(row);
    }

    addSegmentedRow("Theme", "theme", [
      { label: "Light", value: "light" },
      { label: "Dark", value: "dark" },
    ]);
    addAccentRow();
    addSegmentedRow("Zoom", "zoom", [
      { label: "90%", value: 0.9 },
      { label: "100%", value: 1 },
      { label: "110%", value: 1.1 },
      { label: "125%", value: 1.25 },
    ]);
    addSegmentedRow("Clock", "clockZone", [
      { label: "UTC", value: "UTC" },
      { label: "IST", value: "IST" },
    ]);
    addSegmentedRow("Motion", "motion", [
      { label: "On", value: "on" },
      { label: "Off", value: "off" },
    ]);

    function positionPanel() {
      var triggerRect = trigger.getBoundingClientRect();
      var panelRect = panel.getBoundingClientRect();
      var viewportWidth = window.innerWidth || document.documentElement.clientWidth;
      var viewportHeight = window.innerHeight || document.documentElement.clientHeight;
      var width = panelRect.width || 260;
      var height = panelRect.height || 210;
      var maxLeft = Math.max(10, viewportWidth - width - 10);
      var maxTop = Math.max(10, viewportHeight - height - 10);
      var left = Math.max(10, Math.min(triggerRect.right + 8, maxLeft));
      var top = Math.max(10, Math.min(triggerRect.bottom - height, maxTop));

      panel.style.left = Math.round(left) + "px";
      panel.style.top = Math.round(top) + "px";
    }

    function setOpen(next, returnFocus) {
      isOpen = Boolean(next);
      panel.hidden = !isOpen;
      trigger.setAttribute("aria-expanded", isOpen ? "true" : "false");
      if (isOpen) positionPanel();
      else if (returnFocus) trigger.focus();
    }

    trigger.addEventListener("click", function togglePanel() {
      setOpen(!isOpen, false);
    });

    document.addEventListener("click", function closeOnOutsideClick(event) {
      if (isOpen && !trigger.contains(event.target) && !panel.contains(event.target)) {
        setOpen(false, false);
      }
    });

    document.addEventListener("keydown", function closeOnEscape(event) {
      if (isOpen && event.key === "Escape") {
        event.preventDefault();
        setOpen(false, true);
      }
    });

    window.addEventListener("resize", function repositionOnResize() {
      if (isOpen) positionPanel();
    });
    window.addEventListener(
      "scroll",
      function repositionOnScroll() {
        if (isOpen) positionPanel();
      },
      true
    );

    apply();

    return { trigger: trigger, panel: panel, apply: apply };
  };
})(window.QuantParts = window.QuantParts || {});
