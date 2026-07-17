/* adw dashboard — toast dismissal + keyboard shortcuts. No dependencies. */
(function () {
  "use strict";

  // Toast: auto-dismiss and strip the ?toast= param so refresh doesn't re-toast.
  var toast = document.getElementById("toast");
  if (toast) {
    var url = new URL(window.location.href);
    if (url.searchParams.has("toast")) {
      url.searchParams.delete("toast");
      history.replaceState(null, "", url.pathname + url.search + url.hash);
    }
    setTimeout(function () {
      toast.classList.add("hide");
      setTimeout(function () { toast.remove(); }, 350);
    }, 4000);
  }

  // Keyboard shortcuts. Skipped while typing or with modifier keys held.
  function focusEl(id) {
    var el = document.getElementById(id);
    if (!el) return false;
    el.scrollIntoView({ block: "center", behavior: "smooth" });
    el.focus({ preventScroll: true });
    return true;
  }

  var shortcuts = {
    "/": function () { return focusEl("run-search"); },
    r: function () { return focusEl("run-task"); },
    n: function () { return focusEl("ticket-title"); },
  };

  var chordPending = false;
  var chordTimer = null;

  document.addEventListener("keydown", function (event) {
    if (event.metaKey || event.ctrlKey || event.altKey) return;
    var target = event.target;
    var typing =
      target instanceof HTMLElement &&
      (target.tagName === "INPUT" || target.tagName === "TEXTAREA" ||
       target.tagName === "SELECT" || target.isContentEditable);

    if (event.key === "Escape") {
      if (typing) target.blur();
      return;
    }
    if (typing) return;

    if (chordPending) {
      chordPending = false;
      clearTimeout(chordTimer);
      if (event.key === "d") {
        event.preventDefault();
        window.location.href = "/";
      }
      return;
    }
    if (event.key === "g") {
      chordPending = true;
      chordTimer = setTimeout(function () { chordPending = false; }, 500);
      return;
    }

    var handler = shortcuts[event.key];
    if (handler && handler()) event.preventDefault();
  });
})();
