"""
The Reports page.

TEACHING NOTE — one view, and it is a `TemplateView` because a report has no form
and no object. The whole of the work is in `core/reports.py`; this file's only
jobs are calling it with the current user and putting the result where the
template can see it. That separation is the point: the definitions of "win rate"
and "advanced" are in a module, so a second report surface — a CSV export, a
management command, an API endpoint — gets the same numbers by calling the same
function rather than by copying the arithmetic.

WHY THE SAME `scope.py` CALL AS THE BOARD. A report that ignored scope would not
be a missing filter, it would be a way to read your colleagues' win rates. An
Admin's report is the whole office because an Admin's BOARD is the whole office;
a Rep's report is their own book because a Rep's board is their own book. The
reports page and the pipeline are looking at the same data through the same door,
and a Rep seeing their own win rate while their board shows someone else's deal
would be a bug in one of them.
"""

from django.contrib.auth.mixins import LoginRequiredMixin
from django.views.generic import TemplateView

from .reports import report_for


class ReportView(LoginRequiredMixin, TemplateView):
    """Pipeline, conversion, rep performance and forecast, for this user."""

    template_name = "core/reports.html"

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["title"] = "Reports"
        context.update(report_for(self.request.user, self.request))
        # The dashboard already computes a pipeline by stage with this exact
        # shape. Rendering it twice is deliberate: if the two ever disagree, the
        # disagreement is the bug, and it is much cheaper to find when both are
        # on screen than when somebody notices the forecast in a meeting.
        context["stages"] = [stage for stage, _ in context["pipeline"].items()]
        return context