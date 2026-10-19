// app.js — the small amount of hand-written JavaScript this app needs.
//
// TEACHING NOTE: because HTMX handles most interactivity from HTML attributes,
// there's very little custom JS here. What remains uses EVENT DELEGATION: instead
// of attaching a click handler to every tab button, we attach ONE listener to the
// whole document and check what was clicked. That keeps working even for elements
// HTMX swaps in later, which handlers bound at page load would miss.

// Tabs: clicking a [role=tab] button shows its panel and hides its siblings.
document.addEventListener("click", function (e) {
  const tab = e.target.closest("[role=tab]");   // did the click land on (or inside) a tab?
  if (!tab) return;
  const list = tab.closest("[role=tablist]");
  list.querySelectorAll("[role=tab]").forEach(function (t) {
    const selected = t === tab;
    // aria-selected drives the CSS underline AND tells screen readers which tab
    // is active — accessibility and styling from one attribute.
    t.setAttribute("aria-selected", selected ? "true" : "false");
    document.getElementById(t.getAttribute("aria-controls")).hidden = !selected;
  });
});

// After HTMX logs an activity successfully, reset the form so it's ready for the
// next entry. `htmx:afterRequest` fires on every HTMX request; we filter to the
// activity form and to successes only.
document.addEventListener("htmx:afterRequest", function (e) {
  const form = e.target.closest && e.target.closest("form.activity-form");
  // Skip if this response was a validation error (it carries an HX-Retarget
  // header telling HTMX to show the errors) — we don't want to wipe the input then.
  if (form && e.detail.successful && !e.detail.xhr.getResponseHeader("HX-Retarget")) {
    form.querySelector("textarea").value = "";
    const errs = form.querySelector(".form-errors");
    if (errs) errs.innerHTML = "";
    // QA-01: this used to remove the "Nothing logged yet" placeholder element
    // here. That's exactly the bug it was papering over — JS that deletes a node
    // on the first add can never put it back, so deleting the last entry left a
    // blank timeline with no explanation. The empty state is a CSS :has() rule
    // on #timeline now, which is recomputed every time the list changes and
    // needs nobody's permission to come back. Nothing to do here.
  }
});

// Kanban pipeline: SortableJS drag-and-drop with HTMX stage save.
// TEACHING NOTE: SortableJS makes the cards draggable. On drop, we find the new
// column, POST the stage change via HTMX, and move the card in the DOM. The
// server validates permissions and returns the updated card HTML plus out-of-band
// swaps for the two column headers and footers the move changed, so the numbers
// follow the card without a reload.
document.addEventListener("DOMContentLoaded", function () {
  const board = document.querySelector(".kanban");
  if (!board || typeof Sortable === "undefined") return;
  // Never trust the browser to enforce a permission. The view already decided
  // (its can_edit) and pipeline.html wrote the answer onto the board as
  // data-can-edit. Read it BEFORE creating a single Sortable instance, because
  // SortableJS ignores the HTML5 `draggable` attribute — without this gate a
  // read-only user got a board they could drag, and every cross-column move
  // bounced off the server while in-column ones stuck on screen (PIPE-5).
  if (board.getAttribute("data-can-edit") !== "true") return;

  // PIPE-7: the drop target needs to say so. The CSS rule for this class already
  // existed and nothing ever set it, so the highlight was dead code. Clear every
  // column first so a highlight can't survive a move from one column to another.
  const clearDropHighlights = function () {
    document.querySelectorAll(".kanban-col.drag-over").forEach(function (c) {
      c.classList.remove("drag-over");
    });
  };

  document.querySelectorAll(".kanban-col-body").forEach(function (col) {
    new Sortable(col, {
      group: "deals",
      animation: 150,
      // PIPE-6: there is no position column in the schema, so an in-column
      // reorder was never saved anywhere — it looked like it worked and then
      // reverted on the next reload. Switching sorting off leaves one honest
      // gesture: drag across columns, which is the move that actually persists.
      sort: false,
      // PIPE-9: at 375px a finger landing on a card began a drag instead of a
      // scroll, so trying to pan the board sideways silently moved deals and
      // saved real stage changes. Hold for a moment before a touch counts as a
      // drag — and only for touch, so dragging with a mouse still feels instant.
      delay: 200,
      delayOnTouchOnly: true,
      draggable: ".deal-card",
      onMove: function (evt) {
        clearDropHighlights();
        const target = evt.to.closest(".kanban-col");
        if (target) target.classList.add("drag-over");
      },
      onEnd: function (evt) {
        clearDropHighlights();
        if (evt.from === evt.to) return;  // reordered inside a column: not a stage change
        const card = evt.item;
        const form = card.querySelector(".deal-move-form");
        // Put the card back where it came from (a cancelled or not-allowed move).
        const putBack = function () {
          evt.from.insertBefore(card, evt.from.children[evt.oldIndex] || null);
        };
        if (!form) { putBack(); return; }  // no form = you can't move this deal
        const newStage = evt.to.closest(".kanban-col").dataset.stage;
        let reason = "";
        if (newStage === "lost") {
          // Same rule as the edit form: a Lost deal needs a reason. Cancel = no move.
          reason = (window.prompt("Why was this deal lost?") || "").trim();
          if (!reason) { putBack(); return; }
        }
        form.querySelector('input[name="stage"]').value = newStage;
        form.querySelector('input[name="lost_reason"]').value = reason;
        // HTMX POST to save the stage change; the server returns the updated card
        // and the refreshed column heads and feet.
        htmx.ajax("POST", form.action, { source: form, target: card, swap: "outerHTML" });
      },
    });
  });
});

// If the server refuses a move (not your deal, bad stage, failed validation),
// HTMX doesn't swap the card, which would leave it sitting in a column the
// database never agreed to. Say why, then reload so the board shows the truth.
document.addEventListener("htmx:responseError", function (e) {
  const elt = e.detail.elt;
  if (!elt || !elt.closest || !elt.closest(".deal-move-form")) return;
  const xhr = e.detail.xhr;
  // A validation refusal (422) is a plain-text sentence meant for people. A
  // 403/404/500 body is a full HTML error page: never dump that in an alert.
  const plain = (xhr.getResponseHeader("Content-Type") || "").startsWith("text/plain");
  const byStatus = {
    403: "You can't move that deal. Only its owner or an admin can.",
    404: "That deal or stage doesn't exist any more.",
  };
  window.alert((plain && xhr.responseText) || byStatus[xhr.status] || "That move didn't save. The board will refresh.");
  window.location.reload();
});

// A dropped connection is a DIFFERENT failure from a refusal, and it is not
// htmx:responseError. This event fires when the request never completed at all:
// the server is down, the wifi cut out, the phone went into a tunnel. HTMX
// swaps nothing, and the app.js handler above never runs, so the card stayed
// exactly where it was dropped while the database still had it in the old
// stage — a board quietly lying to its user with no error anywhere (PIPE-4).
document.addEventListener("htmx:sendError", function (e) {
  const elt = e.detail.elt;
  if (!elt || !elt.closest || !elt.closest(".deal-move-form")) return;
  window.alert("That move didn't save (no connection). The board will refresh.");
  window.location.reload();
});
