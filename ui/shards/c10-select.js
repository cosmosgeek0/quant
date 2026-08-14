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
