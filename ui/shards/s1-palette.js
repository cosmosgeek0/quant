(function registerPalette() {
  "use strict";

  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined && text !== null) node.textContent = String(text);
    return node;
  }

  function cleanText(value) {
    if (typeof value !== "string") return null;
    value = value.trim();
    return value ? value : null;
  }

  function safeIconName(value) {
    var name = cleanText(value);
    return name && /^[a-z0-9]+(?:-[a-z0-9]+)*$/i.test(name) ? name : null;
  }

  function activeElementIsTextInput() {
    var active = document.activeElement;
    var tagName = active && active.tagName;
    return tagName === "INPUT" || tagName === "TEXTAREA";
  }

  window.QuantParts = window.QuantParts || {};
  window.QuantParts.palette = function palette(getIndex) {
    var openState = false;
    var previousFocus = null;
    var selectedIndex = -1;
    var renderedItems = [];

    var overlay = el("div", "s1-palette");
    var modal = el("div", "s1-palette__modal");
    var inputRow = el("div", "s1-palette__input-row");
    var searchIcon = el("span", "ic s1-palette__search-icon");
    var input = el("input", "s1-palette__input");
    var list = el("div", "s1-palette__list");
    var footer = el(
      "div",
      "s1-palette__footer",
      "↑↓ navigate · enter open · esc close"
    );

    overlay.hidden = true;
    overlay.setAttribute("aria-hidden", "true");
    modal.setAttribute("role", "dialog");
    modal.setAttribute("aria-modal", "true");
    modal.setAttribute("aria-label", "Command palette");
    searchIcon.setAttribute("aria-hidden", "true");
    searchIcon.style.setProperty("--ic", "url(vendor/icons/search.svg)");
    input.type = "text";
    input.placeholder = "Search commands";
    input.autocomplete = "off";
    input.spellcheck = false;
    input.setAttribute("aria-label", "Search commands");
    input.setAttribute("aria-autocomplete", "list");
    input.setAttribute("aria-haspopup", "listbox");
    input.setAttribute("aria-expanded", "false");
    list.setAttribute("role", "listbox");
    list.setAttribute("aria-label", "Command results");
    footer.setAttribute("aria-hidden", "true");

    inputRow.append(searchIcon, input);
    modal.append(inputRow, list, footer);
    overlay.appendChild(modal);

    function mount() {
      var mountPoint;
      if (overlay.isConnected) return;
      mountPoint = document.body || document.documentElement;
      mountPoint.appendChild(overlay);
    }

    function currentIndex() {
      var source;

      if (typeof getIndex !== "function") {
        return { available: false, entries: [] };
      }

      try {
        source = getIndex();
      } catch (error) {
        return { available: false, entries: [] };
      }

      if (!Array.isArray(source)) {
        return { available: false, entries: [] };
      }

      return {
        available: true,
        entries: source.map(function normalizeEntry(entry, sourceIndex) {
          var record = entry && typeof entry === "object" ? entry : {};
          return {
            group: cleanText(record.group),
            label: cleanText(record.label),
            sub: cleanText(record.sub),
            iconName: safeIconName(record.iconName),
            action: typeof record.action === "function" ? record.action : null,
            sourceIndex: sourceIndex,
          };
        }),
      };
    }

    function filteredGroups(entries, rawQuery) {
      var query = rawQuery.trim().toLowerCase();
      var groups = [];
      var groupsByName = new Map();
      var remaining = 40;

      entries.forEach(function collectMatch(item) {
        var label = item.label ? item.label.toLowerCase() : "";
        var sub = item.sub ? item.sub.toLowerCase() : "";
        var haystack = label + " " + sub;
        var group;

        if (query && haystack.indexOf(query) === -1) return;

        group = groupsByName.get(item.group);
        if (!group) {
          group = { label: item.group, matches: [] };
          groupsByName.set(item.group, group);
          groups.push(group);
        }

        group.matches.push({
          item: item,
          rank:
            query && (label.indexOf(query) === 0 || sub.indexOf(query) === 0)
              ? 0
              : 1,
        });
      });

      groups.forEach(function rankMatches(group) {
        group.matches.sort(function compareMatches(left, right) {
          return left.rank - right.rank || left.item.sourceIndex - right.item.sourceIndex;
        });
      });

      return groups.reduce(function capGroups(capped, group) {
        var included;
        if (!remaining) return capped;
        included = group.matches.slice(0, remaining);
        remaining -= included.length;
        if (included.length) {
          capped.push({
            label: group.label,
            items: included.map(function unwrap(match) {
              return match.item;
            }),
          });
        }
        return capped;
      }, []);
    }

    function setSelection(index, scrollIntoView) {
      var selected;

      if (
        index < 0 ||
        index >= renderedItems.length ||
        typeof renderedItems[index].item.action !== "function"
      ) {
        index = -1;
      }

      selectedIndex = index;
      renderedItems.forEach(function syncSelection(rendered, renderedIndex) {
        var isSelected = renderedIndex === selectedIndex;
        rendered.node.classList.toggle(
          "s1-palette__item--selected",
          isSelected
        );
        rendered.node.setAttribute("aria-selected", isSelected ? "true" : "false");
      });

      selected = selectedIndex >= 0 ? renderedItems[selectedIndex] : null;
      if (selected && scrollIntoView) {
        selected.node.scrollIntoView({ block: "nearest" });
      }
    }

    function firstSelectableIndex() {
      return renderedItems.findIndex(function findSelectable(rendered) {
        return typeof rendered.item.action === "function";
      });
    }

    function appendEmpty(message) {
      var empty = el("div", "s1-palette__empty");
      empty.append(el("span", "s1-palette__empty-value", "—"));
      empty.append(el("span", "s1-palette__empty-note", message));
      list.appendChild(empty);
    }

    function buildItem(item) {
      var renderedIndex = renderedItems.length;
      var row = el("button", "s1-palette__item");
      var icon;
      var copy = el("span", "s1-palette__item-copy");
      var label = el(
        "span",
        "s1-palette__item-label",
        item.label || "—"
      );
      var rendered = { item: item, node: row };

      row.type = "button";
      row.tabIndex = -1;
      row.setAttribute("role", "option");
      row.setAttribute("aria-selected", "false");

      if (item.iconName) {
        icon = el("span", "ic s1-palette__item-icon");
        icon.style.setProperty(
          "--ic",
          "url(vendor/icons/" + item.iconName + ".svg)"
        );
      } else {
        icon = el(
          "span",
          "s1-palette__item-icon s1-palette__item-icon--empty"
        );
      }
      icon.setAttribute("aria-hidden", "true");

      copy.appendChild(label);
      if (!item.label) {
        copy.appendChild(
          el("span", "s1-palette__item-note", "Label unavailable")
        );
      }
      if (item.sub) {
        copy.appendChild(el("span", "s1-palette__item-sub", item.sub));
      }
      if (!item.action) {
        row.disabled = true;
        row.setAttribute("aria-disabled", "true");
        copy.appendChild(
          el("span", "s1-palette__item-note", "Action unavailable")
        );
      }

      row.append(icon, copy);
      row.addEventListener("mouseenter", function selectFromPointer() {
        if (item.action) setSelection(renderedIndex, false);
      });
      row.addEventListener("mousedown", function retainSearchFocus(event) {
        event.preventDefault();
      });
      row.addEventListener("click", function runFromPointer() {
        runAction(renderedIndex);
      });
      renderedItems.push(rendered);
      return row;
    }

    function renderResults(query) {
      var snapshot = currentIndex();
      var groups = snapshot.available
        ? filteredGroups(snapshot.entries, query)
        : [];
      var fragment = document.createDocumentFragment();

      renderedItems = [];
      selectedIndex = -1;
      list.replaceChildren();

      groups.forEach(function buildGroup(group) {
        var header = el("div", "s1-palette__group");

        header.setAttribute("role", "presentation");
        if (group.label) {
          header.appendChild(
            el("span", "s1-palette__group-label", group.label)
          );
        } else {
          header.appendChild(el("span", "s1-palette__group-label", "—"));
          header.appendChild(
            el("span", "s1-palette__group-note", "Group unavailable")
          );
        }
        fragment.appendChild(header);
        group.items.forEach(function appendItem(item) {
          fragment.appendChild(buildItem(item));
        });
      });

      list.appendChild(fragment);
      if (!snapshot.available) {
        appendEmpty("Command index unavailable");
      } else if (!snapshot.entries.length) {
        appendEmpty("No commands available");
      } else if (!renderedItems.length) {
        appendEmpty("No matching commands");
      }

      setSelection(firstSelectableIndex(), false);
      list.scrollTop = 0;
    }

    function moveSelection(direction) {
      var selectable = [];
      var currentPosition;
      var nextPosition;

      renderedItems.forEach(function collectSelectable(rendered, index) {
        if (rendered.item.action) selectable.push(index);
      });
      if (!selectable.length) return;

      currentPosition = selectable.indexOf(selectedIndex);
      if (currentPosition === -1) {
        nextPosition = direction > 0 ? 0 : selectable.length - 1;
      } else {
        nextPosition =
          (currentPosition + direction + selectable.length) % selectable.length;
      }
      setSelection(selectable[nextPosition], true);
    }

    function closePalette(restoreFocus) {
      var focusTarget = previousFocus;
      if (!openState) return;

      openState = false;
      overlay.hidden = true;
      overlay.setAttribute("aria-hidden", "true");
      input.setAttribute("aria-expanded", "false");
      input.value = "";
      list.replaceChildren();
      renderedItems = [];
      selectedIndex = -1;
      previousFocus = null;

      if (
        restoreFocus &&
        focusTarget &&
        focusTarget.isConnected &&
        typeof focusTarget.focus === "function"
      ) {
        focusTarget.focus({ preventScroll: true });
      }
    }

    function runAction(index) {
      var rendered = renderedItems[index];
      if (!rendered || typeof rendered.item.action !== "function") return;

      try {
        rendered.item.action();
      } finally {
        closePalette(false);
      }
    }

    function openPalette() {
      if (openState) return;

      mount();
      previousFocus = document.activeElement;
      input.value = "";
      renderResults("");
      openState = true;
      overlay.hidden = false;
      overlay.setAttribute("aria-hidden", "false");
      input.setAttribute("aria-expanded", "true");
      input.focus({ preventScroll: true });
    }

    input.addEventListener("input", function filterFromCurrentIndex() {
      renderResults(input.value);
    });

    document.addEventListener(
      "click",
      function closeFromOutsideClick(event) {
        if (openState && !modal.contains(event.target)) closePalette(true);
      },
      true
    );

    document.addEventListener("keydown", function handlePaletteKeys(event) {
      var key = typeof event.key === "string" ? event.key : "";

      if (event.defaultPrevented || event.isComposing) return;

      if (
        (event.metaKey || event.ctrlKey) &&
        !event.altKey &&
        key.toLowerCase() === "k"
      ) {
        event.preventDefault();
        if (openState) closePalette(true);
        else openPalette();
        return;
      }

      if (
        key === "/" &&
        !openState &&
        !event.metaKey &&
        !event.ctrlKey &&
        !event.altKey &&
        !activeElementIsTextInput()
      ) {
        event.preventDefault();
        openPalette();
        return;
      }

      if (!openState) return;

      if (key === "Escape") {
        event.preventDefault();
        closePalette(true);
      } else if (key === "ArrowDown") {
        event.preventDefault();
        moveSelection(1);
      } else if (key === "ArrowUp") {
        event.preventDefault();
        moveSelection(-1);
      } else if (key === "Enter") {
        event.preventDefault();
        runAction(selectedIndex);
      }
    });

    return {
      open: openPalette,
      close: function close() {
        closePalette(true);
      },
      isOpen: function isOpen() {
        return openState;
      },
    };
  };
})();
