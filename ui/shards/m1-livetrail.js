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
