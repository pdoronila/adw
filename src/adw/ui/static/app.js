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
    // Ticket cards open the detail modal, fetched on demand. Clicks inside the
    // action forms (Start/Requeue/Remove) submit as usual and never open it.
    var card = target.closest("[data-ticket-detail]");
    if (card && !target.closest("form") && !document.querySelector("dialog[open]")) {
      fetch("/fragments/tickets/" + encodeURIComponent(card.getAttribute("data-ticket-detail")))
        .then(function (resp) { return resp.ok ? resp.text() : null; })
        .then(function (html) {
          if (!html) return;
          var dialog = document.getElementById("ticket-detail-modal");
          dialog.innerHTML = html;
          openModal("ticket-detail-modal");
        });
    }
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

  // Board drag-and-drop: drag a queue card onto the In-progress column to
  // start it. Document-level delegation so handlers survive htmx swaps; the
  // board poll checks window.adwDragging so a swap can't clobber a drag.
  window.adwDragging = false;

  document.addEventListener("dragstart", function (event) {
    var card = event.target instanceof HTMLElement && event.target.closest(".ticket[data-ticket-id]");
    if (!card) return;
    window.adwDragging = true;
    event.dataTransfer.setData("text/plain", card.getAttribute("data-ticket-id"));
    event.dataTransfer.effectAllowed = "move";
  });

  document.addEventListener("dragend", function () {
    window.adwDragging = false;
    document.querySelectorAll(".drop-target").forEach(function (el) { el.classList.remove("drop-target"); });
  });

  document.addEventListener("dragover", function (event) {
    var col = event.target instanceof HTMLElement && event.target.closest("[data-drop-state='in_progress']");
    if (!col || !window.adwDragging) return;
    event.preventDefault(); // required to allow drop
    event.dataTransfer.dropEffect = "move";
    col.classList.add("drop-target");
  });

  document.addEventListener("dragleave", function (event) {
    var col = event.target instanceof HTMLElement && event.target.closest("[data-drop-state='in_progress']");
    if (col) col.classList.remove("drop-target");
  });

  document.addEventListener("drop", function (event) {
    var col = event.target instanceof HTMLElement && event.target.closest("[data-drop-state='in_progress']");
    if (!col) return;
    event.preventDefault();
    window.adwDragging = false;
    var id = event.dataTransfer.getData("text/plain");
    if (!id) return;
    // Plain POST → 303 → toast, same as the button actions.
    var form = document.createElement("form");
    form.method = "post";
    form.action = "/tickets/" + encodeURIComponent(id) + "/start";
    document.body.appendChild(form);
    form.submit();
  });
})();
