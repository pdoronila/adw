/* adw dashboard — toast dismissal, modals, keyboard shortcuts. No dependencies. */
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

  // Modals: [data-modal-open] opens the named <dialog>, [data-modal-close]
  // closes its dialog, clicking the backdrop closes too. Escape is native.
  function openModal(id) {
    if (document.querySelector("dialog[open]")) return false;
    var dialog = document.getElementById(id);
    if (!dialog) return false;
    dialog.showModal();
    var field = dialog.querySelector("textarea, input");
    if (field) field.focus();
    return true;
  }

  document.addEventListener("click", function (event) {
    var target = event.target;
    if (!(target instanceof HTMLElement)) return;
    var opener = target.closest("[data-modal-open]");
    if (opener) {
      openModal(opener.getAttribute("data-modal-open"));
      return;
    }
    if (target.closest("[data-modal-close]")) {
      var dialog = target.closest("dialog");
      if (dialog) dialog.close();
      return;
    }
    // A click on the dialog element itself (not its contents) is the backdrop.
    if (target instanceof HTMLDialogElement && target.open) target.close();
  });

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
    r: function () { return openModal("start-run-modal"); },
    n: function () { return openModal("new-ticket-modal"); },
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
      var chords = { d: "/", r: "/runs", t: "/tickets" };
      if (chords[event.key]) {
        event.preventDefault();
        window.location.href = chords[event.key];
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
