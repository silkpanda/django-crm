"""
Response middleware — headers we add to every authenticated response.

TEACHING NOTE: middleware here does the OPPOSITE job from the settings
middleware. Settings middleware (sessions, auth, messages) prepares the request
on the way IN. This one is about the response on the way OUT, and it only exists
because the answer to "is this response allowed to be cached?" is different for a
logged-in rep than for an anonymous visitor — so it has to be decided once, for
every page, rather than remembered view by view.

QA-14 — signed-in pages are cacheable, so Back after sign-out shows data.
"""

from django.utils.cache import patch_cache_control


class NoStoreForSignedInMiddleware:
    """
    Mark every authenticated response `no-store, private`.

    SECURITY NOTE — the why, because "why bother, the data's already behind a
    login" is the exact reasoning this fixes. Signing out DOES work on the server
    side: hit /companies/ after a logout and it 302s you to the login page. But a
    browser is allowed to redraw the page it already has without asking the
    server anything. On a shared office PC — sign out, hand the keyboard to the
    next person, they press Back — the customer list comes straight back out of
    the disk cache, full of names and phone numbers, from a session that no
    longer exists.

    Two directives, and the second is not decoration:

      no-store  don't write this to disk, anywhere, ever
      private    don't let a shared cache (an office proxy, a CDN) keep a copy

    `no-store` alone still leaves the response eligible for a shared cache, which
    in a small office is a real box sitting between everyone and the VM.

    TEACHING NOTE — why middleware and not `never_cache` on the views. The app
    has 38 routes of its own (140 counting the Django admin), and the ones that
    matter most are easy to forget: the CSV export (the response most likely to
    be saved to someone's desktop) and the HTMX endpoints, which return fragments
    rather than pages and so never look like "a page" to whoever adds the next
    decorator. One line in the MIDDLEWARE list covers all of them, including
    anything added later.

    Anonymous responses are left completely alone. The login page is public
    information; caching it is fine, and a blanket rule would just be a slower
    login for no security gain.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)

        # `getattr` rather than `request.user` because this middleware is late in
        # the stack, but a response can still arrive from a path that never set
        # an authenticated user (an exception handled above the auth middleware,
        # or a 404 from URL resolution). A 500 that leaks a stack trace is not
        # made worse by this class raising AttributeError on the way out.
        user = getattr(request, "user", None)
        if user is not None and user.is_authenticated:
            patch_cache_control(response, no_store=True, private=True)

        return response