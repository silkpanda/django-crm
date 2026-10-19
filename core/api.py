"""
The read-only JSON API — a second front door onto data the HTML views already
serve. Seven GET routes, no writes, no framework.

TEACHING NOTE — why plain `JsonResponse` and not Django REST Framework. The
whole surface is three lists, three details and an index, all of which is a
queryset, a serializer function and a dict. DRF would add a dependency, a
settings block, a renderer stack, a throttling default and a pagination class
to produce the same seven responses — and then those four things would be
security surface somebody has to configure correctly on day one. The response
SHAPE is the part worth having from DRF and it is copied deliberately below
(`count`/`next`/`previous`/`results`), so if DRF ever arrives the wire format
does not change and no client has to be rewritten.

READ-ONLY, AND THE DISTANCE TO THE NEAREST WRITE. Every view here answers GET
and nothing else, because the guardrails a write needs — an audit trail, a
permission check per field rather than per record, a CSRF-equivalent for
non-cookie credentials, validation, and a way to undo a mistake made by a
script at 2am — are a second piece of work, not a method away. A POST to any
route here is a 405 rather than a half-built create.

WHY SESSION AUTH AND NOT A TOKEN. A token scheme is not one field, it is a
lifecycle: issue it, store it, rotate it, expire it, revoke it, and decide what
happens when a laptop full of tokens is stolen. Each of those is a security
surface this repo would then have to build and gate, on a workstream whose
entire justification is "let a script read some deals". The session cookie the
browser already carries does the same job, and it already comes with CSRF
protection and a logout that actually ends it. When a write ever needs a
non-browser client, THAT is the moment to design the token, with the revocation
story designed up front rather than bolted on.

SECURITY NOTE — the 403, and the bit of machinery under it. This app runs
`LoginRequiredMiddleware` globally, which is the right default: a view cannot
be left unprotected by forgetting a decorator. Its answer to an anonymous
request, though, is a 302 to `/accounts/login/?next=...` and an HTML page —
which is useless to a client. A JSON consumer that follows the redirect gets
markup where it asked for data, and the failure surfaces somewhere else
entirely: a parse error in whatever reads the response, or worse, a string of
HTML quietly treated as a value.

So these views opt OUT of the redirect (`login_not_required`, applied in
`ApiView.as_view`) and enforce the identical condition themselves in
`dispatch`. The condition has not been weakened — "no session, no data" is
still the rule — but the failure mode is now a 403 whose body is JSON. Doing it
in the base class means it is still stated once, not per view: the thing that
made `LoginRequiredMiddleware` worth having was that you could not forget, and
that is preserved as long as nobody adds a view here that does not inherit
`ApiView`. Test 2 in test_api.py pins both halves for all seven routes.

SECURITY NOTE — scope is the whole ballgame on /api/deals/. `scoped_deals()`
from core/scope.py is called exactly as the pipeline board calls it, so a Rep's
API results are the same set as the board they look at all day. An endpoint that
ignored scope would not be a missing feature; it would be the exfiltration path
with a JSON header on it. And a deal the caller may not see is a 404, never a
403, for the reason `views_notifications.py` gives: a 403 confirms the row is
real to somebody who should not know it exists.

KNOWN GAP — no rate limiting here. Deliberate, and worth saying out loud: a
read endpoint with no throttle is free for anyone holding a session cookie to
hammer, and a page size of 50 is a small enough unit that walking the whole
table is a handful of requests. The in-repo pattern to copy when this needs
closing is `views_leads.LEAD_RATE_LIMIT` — `cache.add` then `cache.incr` keyed
on the client address, against the database-backed cache so every worker counts
into the same place.

KNOWN GAP — a URL under /api/ that matches NO route still answers with the
app's `404.html`, because that page is chosen by a project-wide handler rather
than by anything in this file. A 404 for a pk that does not exist IS JSON (see
`DetailApiView.get`); a typo in the path is not. Fixing the second one means
editing `handler404` in crm_project/urls.py, which is a decision about every
404 in the app rather than about the API, so it is left to whoever owns that.
"""

from django.contrib.auth.decorators import login_not_required
from django.core.paginator import Paginator
from django.http import Http404, JsonResponse
from django.shortcuts import get_object_or_404, render
from django.urls import reverse
from django.utils.decorators import classonlymethod
from django.views.generic import View

from .models import Company, Contact, Deal
from .scope import scoped_deals
from .serializers import company_json, contact_json, deal_json

# Fifty rows is one screenful of a spreadsheet import and one page of a report.
# Bigger pages make the count cheap and the response slow; smaller ones make a
# client walk the whole table in a loop, which is the thing `next` exists to
# discourage.
API_PAGE_SIZE = 50


# ------------------------------------------------------------------ responses


def api_error(detail, status, **extra):
    """
    The one error shape. `{"detail": "..."}` is what DRF returns, so the day it
    arrives, the body a client parses is the body it parsed before.
    """
    return JsonResponse({"detail": detail, **extra}, status=status)


def page_url(request, number):
    """
    An absolute URL for page `number` of the URL being requested.

    TEACHING NOTE — `build_absolute_uri()`, because `next` has to be usable.
    A client handed "/api/deals/?page=2" has to know what to put in front of it,
    which means knowing its own scheme and host — and a client behind a proxy,
    or one whose base URL differs from the request that produced the link, will
    guess wrong. Handing back the whole URL removes the guess.

    The other query parameters ride along. There is nothing to preserve yet
    (filtering is not in this workstream), but a `next` that silently dropped
    `?scope=` the day someone adds it would page one request through a different
    scope than the one they asked for.
    """
    params = request.GET.copy()
    params["page"] = number
    return request.build_absolute_uri(f"{request.path}?{params.urlencode()}")


def paginated(request, queryset, serializer):
    """
    A list, in the shape DRF would have produced.

    TEACHING NOTE — the page number is read here rather than handed straight to
    `Paginator.page()`, because a page PAST THE END is a 200 with an empty
    `results` and not a 404, which is the opposite of what Django does with it.
    A client walking `count` rows in pages of 50 asks for the page after the last
    one the moment a row is deleted underneath it, and a 404 there means every
    such client carries an "is this the end?" special case forever. Empty is an
    answer; 404 is a question the client has to know how to ask.

    This is deliberately NOT what `RecordListView.paginate_queryset` does — that
    one serves the LAST page for an out-of-range number, because a human
    following a stale bookmark would rather see something than an error. A
    script wants the truth. Same paginator, opposite answer, different callers.

    `?page=abc` and `?page=0` land on page 1 rather than raising, for the same
    fat-finger reason: nobody is asking whether "abc" is a valid page, they have
    typed it by accident.
    """
    try:
        number = int(request.GET.get("page", 1))
    except (TypeError, ValueError):
        number = 1
    if number < 1:
        number = 1

    paginator = Paginator(queryset, API_PAGE_SIZE)
    # `num_pages` rather than a try/except around `page()`: an empty queryset
    # reports one (empty) page, so this needs no special case for "no rows".
    if number > paginator.num_pages:
        return {"count": paginator.count, "next": None, "previous": None, "results": []}

    page = paginator.page(number)
    return {
        "count": paginator.count,
        "next": page_url(request, page.next_page_number()) if page.has_next() else None,
        "previous": page_url(request, page.previous_page_number()) if page.has_previous() else None,
        "results": [serializer(obj) for obj in page.object_list],
    }


def wants_html(request):
    """
    Does this caller want the page or the object? `/api/` is both.

    TEACHING NOTE — the raw `Accept` header, not `request.accepts()`. `accepts()`
    picks the FIRST type you offer whenever the header is missing or `*/*`, and
    the callers that matter most here send exactly that: curl, a Python script,
    the test client. Offering HTML first would hand all three a web page. So the
    rule is stricter — "asked for HTML, and did not also ask for JSON" — which
    is right for all four kinds of caller and needs no configuration.
    """
    accept = request.headers.get("Accept", "")
    if "application/json" in accept:
        return False
    return "text/html" in accept


# --------------------------------------------------------------------- views


class ApiView(View):
    """
    The base every route here inherits: JSON, GET only, and a 403 that is JSON.

    `http_method_names` is the whole of "read-only" — a POST, PUT, PATCH or
    DELETE never reaches a handler, so there is no code path to review in which
    a write could have crept in.

    `http_method_not_allowed` exists so the 405 is a JSON body too. Django's
    default answers with `HttpResponseNotAllowed`, which is `text/html` and
    carries the allowed methods in a header rather than the body; a client that
    got that back has to guess, and `allow` in the body is the one thing it can
    act on.
    """

    http_method_names = ["get", "head", "options"]

    @classonlymethod
    def as_view(cls, **initkwargs):
        # The opt-out from `LoginRequiredMiddleware` — see the SECURITY NOTE in
        # the module docstring for why the gate cannot be the middleware's
        # redirect. It sets one attribute on the function it is handed, so
        # applying it here rather than in urls.py means a new route under /api/
        # is covered by writing `SomeView.as_view()` like everywhere else, with
        # no second thing to remember at the URL layer.
        return login_not_required(super().as_view(**initkwargs))

    def dispatch(self, request, *args, **kwargs):
        if not request.user.is_authenticated:
            return api_error(
                "Authentication required. This API uses the same session cookie as the web "
                "app; there is no token, and an HTML login page is not a usable answer.",
                status=403,
            )
        return super().dispatch(request, *args, **kwargs)

    def http_method_not_allowed(self, request, *args, **kwargs):
        return api_error(
            f"{request.method} is not allowed here. This API is read-only; every endpoint is GET.",
            status=405,
            allow=self._allowed_methods(),
        )


class PaginatedApiView(ApiView):
    """A list endpoint: queryset -> serializer -> the DRF-shaped envelope."""

    serializer = None

    def base_queryset(self):
        raise NotImplementedError

    def get_queryset(self):
        return self.base_queryset()

    def get(self, request, *args, **kwargs):
        return JsonResponse(paginated(request, self.get_queryset(), self.serializer))


class DetailApiView(ApiView):
    """A one-record endpoint. `get_queryset` IS the permission check."""

    model = None
    serializer = None

    def base_queryset(self):
        return self.model.objects.all()

    def get_queryset(self):
        return self.base_queryset()

    def get(self, request, pk, *args, **kwargs):
        # `get_object_or_404` against this view's OWN queryset rather than the
        # model: the queryset is where the scope filter lives, so fetching the
        # row first and checking permission afterwards would be the
        # fetch-then-check shape this repo has been bitten by twice. Filter
        # first, and a row the caller may not see is indistinguishable from a
        # row that was never there — the 404-over-403 rule from
        # views_notifications.py, kept by construction rather than by
        # remembering to apply it.
        try:
            obj = get_object_or_404(self.get_queryset(), pk=pk)
        except Http404:
            # As JSON, not the app's 404.html. The status code is unchanged and
            # is the whole of what this endpoint promises about a row the caller
            # may not have — but a client that asked for JSON and received a web
            # page fails to parse it somewhere less obvious than here, which is
            # the same argument that makes the 403 above a JSON 403. The message
            # says "or not yours" because from out here the two are the same
            # answer on purpose, and a client logging the difference would be
            # building the oracle this is shaped to withhold.
            return api_error("No such record, or not one you may read.", status=404)
        return JsonResponse(self.serializer(obj))


class ApiIndexView(ApiView):
    """The root: the route list, as an object or as a page, per the Accept header."""

    def get(self, request, *args, **kwargs):
        endpoints = endpoint_docs()
        if wants_html(request):
            return render(request, "api/index.html", {"endpoints": endpoints})
        return JsonResponse(
            {
                "read_only": True,
                "authentication": (
                    "Session cookie — the same one the web app uses. Sign in at "
                    f"{reverse('login')} and send the cookie; there is no token."
                ),
                "pagination": "?page=N, 50 per page. A page past the end is 200 with an empty results list.",
                "endpoints": endpoints,
            }
        )


class CompanyListApi(PaginatedApiView):
    """
    GET /api/companies/ — every company, 50 a page, as on the companies screen.
    """
    serializer = staticmethod(company_json)

    def base_queryset(self):
        # No owner filter, and that is copied rather than assumed:
        # `CompanyListView.base_queryset()` hands every company to every
        # signed-in user, because in this app only EDITS are ownership-gated
        # (`can_edit`) — a Rep is expected to be able to find any account in the
        # building. The API matches the screen.
        #
        # The screen's `prefetch_related("tags")` is deliberately NOT copied:
        # `company_json` reads no many-to-many and no foreign key, so prefetching
        # would be a query spent on data nobody receives.
        return Company.objects.all()


class CompanyDetailApi(DetailApiView):
    """
    GET /api/companies/<pk>/ — one company. Any signed-in user may read any company.
    """
    model = Company
    serializer = staticmethod(company_json)

    def base_queryset(self):
        # Every company, for the same reason — and matching
        # `CompanyDetailView.get_queryset()`, which filters by nothing either:
        # a Rep may open any account's page in the browser.
        return Company.objects.all()


class ContactListApi(PaginatedApiView):
    """
    GET /api/contacts/ — every contact, 50 a page, with the company name on each row.
    """
    serializer = staticmethod(contact_json)

    def base_queryset(self):
        # Copied from `ContactListView.base_queryset()`, which likewise hands
        # every contact to every signed-in user. Its `select_related("company")`
        # IS load-bearing here, unlike the company endpoint's prefetch:
        # `contact_json` emits `company_name`, so without the JOIN every row
        # would cost a second query to fetch the name it is about to print.
        # One query for the page instead of fifty.
        return Contact.objects.select_related("company")


class ContactDetailApi(DetailApiView):
    """
    GET /api/contacts/<pk>/ — one contact. Any signed-in user may read any contact.
    """
    model = Contact
    serializer = staticmethod(contact_json)

    def base_queryset(self):
        # `select_related("company")` for `company_name`, as the list above and
        # as `ContactDetailView.get_queryset()` already does.
        return Contact.objects.select_related("company")


class DealListApi(PaginatedApiView):
    """
    GET /api/deals/ — the deals the caller's scope allows, the same set as the pipeline board.
    """
    serializer = staticmethod(deal_json)

    def base_queryset(self):
        # The line the whole workstream turns on. `scoped_deals()` decides, from
        # the user's role, whether these are their own deals or the team's — the
        # same call `pipeline_columns()` makes, so the API and the board can
        # never quote different numbers.
        #
        # It also handles `?scope=` itself: an Admin's value is honoured and
        # remembered in the session, and a Rep's `?scope=team` is discarded
        # before it reaches a filter (see the SECURITY NOTE in core/scope.py).
        # So there is deliberately no scope handling anywhere else in this file.
        # A second implementation would be a second rule, and two rules is how
        # scope stops meaning anything.
        #
        # `select_related("company", "owner")` is `DealListView.base_queryset()`'s
        # and it is not optional: `deal_json` reads the company name and the
        # owner's username on every row.
        return scoped_deals(self.request, Deal.objects.select_related("company", "owner"))


class DealDetailApi(DetailApiView):
    """
    GET /api/deals/<pk>/ — one deal, inside the caller's scope; outside it is a 404.
    """
    model = Deal
    serializer = staticmethod(deal_json)

    def base_queryset(self):
        # `?scope=` belongs to /api/deals/ and nowhere else. `deal_scope()`
        # writes whatever it finds there into the session — that is how an
        # Admin's board remembers the switch between screens — so honouring it
        # on a DETAIL url would mean `/api/deals/5/?scope=mine` silently
        # re-scoping the pipeline board the next time that Admin opened it. The
        # parameter is dropped before the question is asked; the remembered
        # session value is still read, which is exactly what keeps a detail
        # fetch agreeing with the board it belongs to.
        if "scope" in self.request.GET:
            self.request.GET = self.request.GET.copy()
            del self.request.GET["scope"]
        # The same `scoped_deals()` call the list makes. Because
        # `get_object_or_404` is handed THIS queryset, a Rep asking for a
        # colleague's deal is told 404 — not 403, and not a 200 they had no
        # business having.
        return scoped_deals(self.request, Deal.objects.select_related("company", "owner"))


# ---------------------------------------------------------------------- index


def detail_path(route_name):
    """
    `/api/companies/<pk>/` for the index — not a real pk. The index documents
    shapes, and printing `/api/companies/0/` would be a URL that 404s.
    """
    return reverse(route_name, args=[0]).replace("/0/", "/<pk>/")


def endpoint_docs():
    """
    Every route under /api/, described once.

    TEACHING NOTE — the JSON index and the HTML page are rendered from this one
    function on purpose. Two hand-maintained lists is one more thing to forget,
    and the failure mode is a documentation page that is confidently wrong about
    which routes exist.
    """
    signed_in = "Any signed-in user (Rep, Viewer or Admin)."
    scoped = "Scoped: a Rep gets their own deals, a Viewer or Admin the team's."
    return [
        {"path": reverse("api_index"), "method": "GET", "returns": "This index.", "auth": signed_in},
        {"path": reverse("api_company_list"), "method": "GET", "returns": "Companies, 50 per page.", "auth": signed_in},
        {"path": detail_path("api_company_detail"), "method": "GET", "returns": "One company.", "auth": signed_in},
        {"path": reverse("api_contact_list"), "method": "GET", "returns": "Contacts, 50 per page.", "auth": signed_in},
        {"path": detail_path("api_contact_detail"), "method": "GET", "returns": "One contact.", "auth": signed_in},
        {"path": reverse("api_deal_list"), "method": "GET", "returns": "Deals, 50 per page.", "auth": scoped},
        {
            "path": detail_path("api_deal_detail"),
            "method": "GET",
            "returns": "One deal, or 404 if it is not yours to see.",
            "auth": scoped,
        },
    ]