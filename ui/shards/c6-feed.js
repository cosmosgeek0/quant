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
