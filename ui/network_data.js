(function (root) {
  "use strict";

  var TIMEOUT_MS = 8000;
  var ALLOWED_INTERVALS = [2000, 5000, 10000, 30000];
  var listeners = { current: [], history: [], events: [], error: [] };
  var historyCache = Object.create(null), eventCursor = 0;
  var pollTimer = null, pollIntervalMs = 5000, pollUpdate = null;
  var pollingWanted = false, liveView = true, api;

  function subscribe(channel, listener) {
    if (!listeners[channel] || typeof listener !== "function") { return function () {}; }
    listeners[channel].push(listener);
    return function () {
      var index = listeners[channel].indexOf(listener);
      if (index !== -1) { listeners[channel].splice(index, 1); }
    };
  }

  function emit(channel, value) {
    if (!listeners[channel]) { return; }
    listeners[channel].slice().forEach(function (listener) {
      try { listener(value); } catch (ignored) {}
    });
  }

  function parseResponse(response) {
    return response.text().then(function (text) {
      var data = null;
      if (text) {
        try {
          data = JSON.parse(text);
        } catch (error) {
          if (response.ok) { error.status = response.status; throw error; }
        }
      }
      if (!response.ok) {
        var message = data && typeof data.error === "string"
          ? data.error : (text || response.statusText || "HTTP request failed");
        var httpError = new Error(message);
        httpError.status = response.status;
        throw httpError;
      }
      return data;
    });
  }

  function requestJson(path, options) {
    var controller = new root.AbortController();
    var timedOut = false;
    var settings = Object.assign({}, options, { signal: controller.signal });
    var timer = root.setTimeout(function () {
      timedOut = true; controller.abort();
    }, TIMEOUT_MS);
    return Promise.resolve().then(function () {
      return root.fetch(path, settings);
    }).then(parseResponse).then(function (data) {
      root.clearTimeout(timer);
      return data;
    }, function (error) {
      root.clearTimeout(timer);
      var status = error && typeof error.status === "number"
        ? error.status : null;
      var message = timedOut ? "Request timed out after 8000 ms"
        : (error && error.message ? error.message : "Network request failed");
      emit("error", { path: path, status: status, message: message });
      throw error;
    });
  }

  function getJson(path) {
    return requestJson(path, {
      method: "GET",
      cache: "no-store",
      headers: { Accept: "application/json" }
    });
  }

  function returnNull() {
    return null;
  }

  function fetchCurrent() {
    return getJson("/api/network").then(function (data) {
      var currentEvents = data && Array.isArray(data.events) ? data.events : [];
      var maxEventId = null;
      currentEvents.forEach(function (event) {
        var eventId = event ? Number(event.event_id) : NaN;
        if (isFinite(eventId) && eventId >= 0
            && (maxEventId === null || eventId > maxEventId)) {
          maxEventId = eventId;
        }
      });
      if (maxEventId !== null && maxEventId < eventCursor) { eventCursor = 0; }
      emit("current", data);
      return data;
    }).catch(returnNull);
  }

  function fetchHistory(edgeId, metric, range) {
    var key = String(edgeId) + "|" + String(metric) + "|" + String(range);
    var cached = historyCache[key];
    if (cached && cached.pending) { return cached.pending; }
    if (cached) {
      var cacheAge = root.performance.now() - cached.fetchedAt;
      if (cacheAge >= 0 && cacheAge < cached.ttlMs) {
        emit("history", cached.data);
        return Promise.resolve(cached.data);
      }
    }
    var path = "/api/network/history?edge_id=" + encodeURIComponent(edgeId)
      + "&metric=" + encodeURIComponent(metric) + "&range=" + encodeURIComponent(range);
    var pending = getJson(path).then(function (data) {
      var bucketSeconds = data ? Number(data.bucket_s) : null;
      historyCache[key] = { data: data, fetchedAt: root.performance.now(),
        ttlMs: bucketSeconds > 0 ? bucketSeconds * 1000 : 0 };
      emit("history", data);
      return data;
    }).catch(function () {
      delete historyCache[key];
      return null;
    });
    historyCache[key] = { pending: pending };
    return pending;
  }

  function boundedLimit(limit) {
    var value = Number(limit);
    if (!isFinite(value) || value < 1) { return 50; }
    return Math.min(200, Math.floor(value));
  }

  function fetchEvents(after, limit) {
    var cursor = after === undefined || after === null ? eventCursor : after;
    var path = "/api/network/events?after=" + encodeURIComponent(cursor)
      + "&limit=" + boundedLimit(limit);
    return getJson(path).then(function (data) {
      if (data && data.next_after !== null && data.next_after !== undefined) {
        var next = Number(data.next_after);
        if (isFinite(next) && next >= eventCursor) { eventCursor = next; }
      }
      emit("events", data);
      return data;
    }).catch(returnNull);
  }

  function postJson(path, body) {
    return requestJson(path, {
      method: "POST",
      cache: "no-store",
      headers: { Accept: "application/json", "Content-Type": "application/json" },
      body: JSON.stringify(body)
    });
  }

  function postErrorResult(error) {
    var message = error && error.message ? error.message : "Network request failed";
    return { ok: false, error: message };
  }

  function postConfig(key, value, group) {
    var body = { key: key, value: value };
    if (group !== undefined && group !== null) { body.group = group; }
    return postJson("/api/network/config", body).catch(postErrorResult);
  }

  function postProbe(edgeId) {
    return postJson("/api/network/probe", { edge_id: edgeId }).catch(postErrorResult);
  }

  function measureFetchMs() {
    var started = root.performance.now();
    return getJson("/api/network").then(function () {
      return root.performance.now() - started;
    }).catch(returnNull);
  }

  function clearPollTimer() {
    if (pollTimer !== null) {
      root.clearInterval(pollTimer); pollTimer = null;
    }
  }

  function pollOnce() {
    return fetchCurrent().then(function (data) {
      if (data !== null && typeof pollUpdate === "function") {
        try { pollUpdate(data); } catch (ignored) {}
      }
      return data;
    });
  }

  function restartPolling(runNow) {
    clearPollTimer();
    if (!pollingWanted || !liveView) { return; }
    pollTimer = root.setInterval(pollOnce, pollIntervalMs);
    if (runNow) { pollOnce(); }
  }

  function allowedInterval(ms) {
    var value = Number(ms);
    return ALLOWED_INTERVALS.indexOf(value) === -1 ? null : value;
  }

  function startPolling(intervalMs, onUpdate) {
    var value = intervalMs === undefined
      ? pollIntervalMs : allowedInterval(intervalMs);
    if (value === null) { return false; }
    pollIntervalMs = value;
    pollUpdate = typeof onUpdate === "function" ? onUpdate : null;
    pollingWanted = true;
    liveView = true;
    api.liveView = true;
    restartPolling(true);
    return true;
  }

  function stopPolling() {
    pollingWanted = false;
    clearPollTimer();
  }

  function setPollInterval(ms) {
    var value = allowedInterval(ms);
    if (value === null) { return false; }
    pollIntervalMs = value;
    restartPolling(false);
    return true;
  }

  function pause() {
    liveView = false;
    api.liveView = false;
    clearPollTimer();
  }

  function resume() {
    liveView = true;
    api.liveView = true;
    restartPolling(true);
  }

  api = {
    fetchCurrent: fetchCurrent,
    startPolling: startPolling,
    stopPolling: stopPolling,
    setInterval: setPollInterval,
    pause: pause,
    resume: resume,
    liveView: true,
    fetchHistory: fetchHistory,
    fetchEvents: fetchEvents,
    postConfig: postConfig,
    postProbe: postProbe,
    measureFetchMs: measureFetchMs,
    subscribe: subscribe,
    emit: emit
  };
  root.NetworkData = api;
}(window));
