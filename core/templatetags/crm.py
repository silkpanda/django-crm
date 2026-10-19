"""
Custom template tags and filters.

TEACHING NOTE: Django templates deliberately can't run arbitrary Python — that
keeps logic out of the HTML. When a template genuinely needs a small computation
(here: "can this user edit this object?", or building a sort link), you register
a `filter` or `simple_tag` in a file like this, inside a `templatetags/` package.
The template then does `{% load crm %}` and uses them. A filter is used with a
pipe (`value|editable_by:user`); a tag is called (`{% sort_link ... %}`).
"""

from django import template

from ..models import STAGE_PROBABILITY
from ..permissions import can_delete, can_edit

register = template.Library()  # every custom tag/filter attaches to this


@register.filter
def editable_by(obj, user):
    # Lets a template ask the real permission function: {% if obj|editable_by:user %}.
    # Same rule the view enforces, so the button and the server never disagree.
    return can_edit(user, obj)


@register.filter
def deletable_by(obj, user):
    return can_delete(user, obj)


@register.simple_tag
def sort_link(sort_base, current, key):
    """Build the URL for a sortable column header.

    First click sorts ascending; clicking the already-active column flips it to
    descending (the "-" prefix). `sort_base` carries the other query params so
    sorting doesn't wipe your filters.
    """
    direction = f"-{key}" if current == key else key
    prefix = f"{sort_base}&" if sort_base else ""
    return f"?{prefix}sort={direction}"


@register.simple_tag
def sort_arrow(current, key):
    # The little ▲/▼ next to the currently-sorted column header.
    if current == key:
        return "▲"
    if current == f"-{key}":
        return "▼"
    return ""


@register.filter
def stage_default_probability(stage):
    return STAGE_PROBABILITY.get(stage, 0)


@register.filter
def safe_href(url):
    """Only allow http(s) links through.

    SECURITY NOTE: a user-supplied "website" could be `javascript:alert(1)` or a
    `data:` URL. Rendering that straight into an href is an XSS foothold. This
    filter returns the URL only if it starts with http:// or https://, and an
    empty string otherwise, so the template can decide not to make it a link.
    """
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else ""
