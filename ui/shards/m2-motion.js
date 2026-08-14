// HONEST-MOTION LAW: motion signals arrival/change/state, never drama; money digits never tween; nothing loops except lamps.
(function (parts) {
  "use strict";

  parts.flashCell = function (node, direction) {
    Array.prototype.slice.call(node.classList).forEach(function (className) {
      if (className.indexOf("m2-flash") === 0) {
        node.classList.remove(className);
      }
    });

    void node.offsetWidth;

    node.classList.add(
      direction === "up" ? "m2-flash-good" :
        direction === "down" ? "m2-flash-bad" : "m2-flash"
    );
  };

  parts.markChanged = function (node, newText, direction) {
    var nextText = newText === null || newText === undefined ? "—" : String(newText);

    if (node.textContent === nextText) return false;

    node.textContent = nextText;
    parts.flashCell(node, direction);
    return true;
  };
}(window.QuantParts = window.QuantParts || {}));
