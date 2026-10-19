"""
The quote page, the line editor, and the status-change endpoint.

TEACHING NOTE — a separate module from views.py, matching views_leads.py and
views_notifications.py: one file per feature once a feature outgrows a screen. It
also buys the thing that matters here, which is that the whole quote feature has
one ownership story in one place, instead of four views in a 1,400-line file each
remembering to call `can_edit`.

Every URL in this feature is keyed on the DEAL's pk, not the quote's. The quote
hangs off the deal, so the deal is a complete key for "which quote", and
`Quote.get_absolute_url` therefore returns a deal-keyed URL. No caller ever has to
know which of the two pk's it happens to be holding.

SECURITY NOTE — the ownership check is `can_edit(request.user, deal)`, on the
DEAL, not on the quote. Two reasons, and the second is the one that bites. First,
`can_edit` is the app's single answer to "may this person edit this record", and a
quote is a quote OF a deal: asking the question of the deal means a quote cannot
be edited by somebody who cannot edit the deal it prices. Second, `can_edit` has
no branch for a Quote at all — permissions.py knows about Company, Contact, Deal,
Activity and Task — so asking it about a quote returns False for every role but
Admin, which would quietly make quotes Admin-only. A new model's ownership is its
parent's ownership, stated once, here.
"""

from django.contrib import messages
from django.contrib.auth.mixins import LoginRequiredMixin
from django.core.exceptions import PermissionDenied
from django.db import transaction
from django.shortcuts import get_object_or_404, redirect, render
from django.views.decorators.http import require_POST
from django.views.generic import TemplateView, View

from .forms_quotes import QuoteForm, QuoteLineFormSet, live_line_forms
from .models import Deal, Quote
from .permissions import can_edit
from .quotes import (
    LOCKED_MESSAGE,
    allowed_next_statuses,
    expired,
    line_amount,
    locked,
    quote_total,
    set_status,
)


class QuoteDetailView(LoginRequiredMixin, TemplateView):
    """
    Read-only: the quote for a deal, its lines, and the total, computed on the way out.

    TEACHING NOTE — the total is `quote_total(quote)` called HERE, once, rather than
    a `{{ line.quantity|multiply:line.unit_price }}` per row plus a sum in the
    template. Two reasons, both of which have bitten this app before: arithmetic in
    a template cannot be tested, and the per-line figures and the whole-quote figure
    have to come from the same code or they can disagree. A template multiplying
    through `floatformat` would round every line before adding and print a total
    that is not the sum of the rows printed above it.

    A deal with no quote REDIRECTS to the editor rather than 404ing. The quote is
    not missing, it has not been written yet — and the page the user was trying to
    reach has a perfectly good "write it" version one click away. A 404 would say
    this deal has no quote, which is a statement about a fact that does not exist.

    `TemplateView` rather than `DetailView`: DetailView exists to fetch one object
    by its own pk, and here the object is found by the DEAL's pk and may be absent
    in a way that has to become a redirect. Overriding both of DetailView's
    contributing methods to get that behaviour is more code than the base class
    saves. One query either way.
    """

    template_name = "core/quote_detail.html"

    def get(self, request, *args, **kwargs):
        # select_related: the template prints the deal's name and owner on every
        # page, and this is the page a rep opens to find out what a deal is worth.
        self.quote = (
            Quote.objects.select_related("deal", "deal__owner").filter(deal__pk=kwargs["pk"]).first()
        )
        if self.quote is None:
            return redirect("quote_edit", pk=kwargs["pk"])
        return super().get(request, *args, **kwargs)

    def get_context_data(self, **kwargs):
        ctx = super().get_context_data(**kwargs)
        quote = self.quote
        ctx["quote"] = quote
        ctx["deal"] = quote.deal
        ctx["total"] = quote_total(quote)
        ctx["expired"] = expired(quote)
        ctx["locked"] = locked(quote)
        ctx["locked_message"] = LOCKED_MESSAGE
        ctx["can_edit"] = can_edit(self.request.user, quote.deal)
        ctx["next_statuses"] = allowed_next_statuses(quote)
        # TEACHING NOTE — `rows` rather than letting the template loop
        # `quote.lines.all()` itself, for the same reason RecordDetailView builds
        # `task_rows`: a row's amount is an ARITHMETIC RESULT, and a template
        # cannot do Decimal multiplication that survives a fraction (see
        # quotes.line_amount). Deciding it here also means one query for all the
        # lines rather than one per row if the template ever starts calling
        # anything on them.
        ctx["rows"] = [
            {"line": line, "amount": line_amount(line)} for line in quote.lines.all()
        ]
        return ctx


class QuoteEditView(LoginRequiredMixin, View):
    """
    The line editor. Add a row, fill it in, save — the whole quote at once.

    TEACHING NOTE — the permission check is in `dispatch`, which every request
    passes through before `get` or `post` runs. Checking in each of them instead
    is the classic version of this bug: the GET gets the check added, the POST does
    not, and the editor is open in a browser but not defensible against a script.
    One chokepoint, both verbs.

    The accepted-quote freeze is checked here too, once, for the same reason.
    `locked()` is the rule (in core/quotes.py); this is the door. It covers the
    GET that would show the editor and the POST that would save it, which means the
    delete-every-line case is refused by the same line of code rather than by a
    second guard that could one day disagree with the first.
    """

    template_name = "core/quote_form.html"

    def dispatch(self, request, *args, **kwargs):
        self.deal = get_object_or_404(Deal, pk=kwargs["pk"])
        if not can_edit(request.user, self.deal):
            raise PermissionDenied
        # `get_or_create` rather than a 404: the editor is how a quote comes into
        # existence, so arriving here on a deal that has none is the normal case,
        # not a broken link. The OneToOneField guarantees the `create` half can
        # never fire twice — a second attempt is an IntegrityError from the
        # database, not a second quote.
        self.quote, _created = Quote.objects.get_or_create(deal=self.deal)
        if locked(self.quote):
            messages.error(request, LOCKED_MESSAGE)
            return redirect("quote_detail", pk=self.deal.pk)
        return super().dispatch(request, *args, **kwargs)

    def get_context_data(self, formset, quote_form):
        return {
            "deal": self.deal,
            "quote": self.quote,
            "formset": formset,
            "quote_form": quote_form,
            # The figure printed above the rows is the sum of what is IN THE
            # DATABASE, not of what is on screen — a row being typed into is not a
            # line item yet. Which is why the template has to call it the saved
            # total, or it reads as a live preview that does not update.
            "total": quote_total(self.quote),
            "expired": expired(self.quote),
        }

    def get(self, request, *args, **kwargs):
        formset = QuoteLineFormSet(instance=self.quote)
        return self.render(formset, QuoteForm(instance=self.quote))

    def post(self, request, *args, **kwargs):
        formset = QuoteLineFormSet(request.POST, instance=self.quote)
        quote_form = QuoteForm(request.POST, instance=self.quote)
        if not (formset.is_valid() and quote_form.is_valid()):
            return self.render(formset, quote_form)
        # The quote's own fields and all of its lines save together. A quote whose
        # valid_until was set while its new lines bounced off a missing unit_price
        # is a quote claiming a deadline it was never priced against, and the flash
        # message would have said it saved.
        with transaction.atomic():
            quote_form.save()
            self.save_lines(formset)
        messages.success(request, f"Saved the quote for {self.deal.name}.")
        return redirect("quote_detail", pk=self.deal.pk)

    def save_lines(self, formset):
        """
        Write the lines: delete the ticked ones, number the rest by formset index,
        save.

        TEACHING NOTE — this does NOT use `formset.save()`, and that is the whole
        lesson. Two documented behaviours of `save(commit=False)` make it the
        wrong call here, and both of them fail without an error:

          - `delete_existing()` only deletes when `commit=True`. With
            `commit=False` a ticked DELETE box does nothing whatsoever.
          - `saved_forms` holds only the forms `has_changed()`. Reorder two rows
            without editing either and that list is empty, so a formset whose job
            is POSITION has nothing to number and the order you just asked for is
            silently not saved.

        So the deletions are done explicitly and the numbering walks
        `live_line_forms(formset)` — see core/forms_quotes.py for why that helper
        and not `saved_forms`.

        `start=1` rather than 0, because position is an ordinal and not an index:
        "line 1 of 5" and "position 0" disagree to anybody reading the column, and
        0 is also what a row created outside the editor lands on, which is exactly
        the collision that makes 0 a bad value to start at.

        Each row's position is written in the SAME save as the rest of that row, so
        there is never a moment when a line exists carrying a stale number — which
        is what would happen if the formset saved first and numbered afterwards.

        The whole thing is one transaction. A quote whose lines were deleted and
        then half-written is worse than one that refused to save, and both halves
        are a single act as far as the person clicking Save is concerned.

        `form.save(commit=False)` is called per row because that is what copies
        the submitted values onto a NEW instance — `formset.save(commit=False)`
        does it for changed rows only, which is the same limitation again. There
        are no many-to-many fields on QuoteLine, so there is no `save_m2m()` left
        to run afterwards, and this would be where to run it if there were.
        """
        with transaction.atomic():
            for form in formset.deleted_forms:
                form.instance.delete()
            for position, form in enumerate(live_line_forms(formset), start=1):
                line = form.save(commit=False)
                line.position = position
                line.save()

    def render(self, formset, quote_form):
        return render(self.request, self.template_name, self.get_context_data(formset, quote_form))


@require_POST
def quote_status_change(request, pk):
    """
    Move a quote to the status the POST asked for — if that move is legal.

    TEACHING NOTE — `@require_POST`, so a GET is a 405 and the status never changes
    on a link click, a prefetch, or a browser's connection reuse. There is no
    `login_required` decorator here because the global `LoginRequiredMiddleware`
    already handles it, and a logged-out visitor gets the login page rather than a
    405 — which, as views_leads.py puts it, is free reconnaissance.

    The `ValueError` from `set_status` is caught and shown as a flash message
    rather than a 500. The caller is a person who clicked a button the page
    offered them, and a traceback is not a way of telling somebody the button was
    wrong — the message is. Note what is NOT in the except branch: no save, no
    partial state. `set_status` validates before it writes, so a refused move
    leaves the stored status exactly as it was.
    """
    quote = get_object_or_404(Quote, deal__pk=pk)
    if not can_edit(request.user, quote.deal):
        raise PermissionDenied

    new_status = request.POST.get("status", "")
    try:
        set_status(quote, new_status)
    except ValueError as problem:
        messages.error(request, str(problem))
    else:
        messages.success(request, f"Quote for {quote.deal.name} is now {quote.get_status_display()}.")
    # Both outcomes land on the same page: the point of that page is the quote,
    # and a status change that was refused has not changed it.
    return redirect("quote_detail", pk=pk)