"""
Settings -> Users: list, create, edit and (de)activate login users. Admin only.

TEACHING NOTE: the queue in views_leads.py uses the same gate. The sidebar link
is politeness; this `test_func` is the lock. Every view here inherits
`AdminRequiredMixin`, so there is no user URL that forgot the check.

Users are deactivated, never deleted. Records point at their owner with SET_NULL
and the history tables point at who changed what; deleting a person would blank
"who owned this deal" across the whole database. An inactive user can't log in
and has no role (role_of returns None), which is everything deletion would give
you without the amnesia.
"""

from django.contrib import messages
from django.contrib.auth import get_user_model
from django.contrib.auth.mixins import LoginRequiredMixin, UserPassesTestMixin
from django.core.exceptions import PermissionDenied
from django.shortcuts import get_object_or_404, redirect
from django.urls import reverse_lazy
from django.views.decorators.http import require_POST
from django.views.generic import CreateView, ListView, UpdateView

from .forms_users import UserForm, stored_role
from .permissions import is_admin

User = get_user_model()


class AdminRequiredMixin(LoginRequiredMixin, UserPassesTestMixin):
    def test_func(self):
        if not is_admin(self.request.user):
            raise PermissionDenied
        return True


class UserFormMixin(AdminRequiredMixin):
    model = User
    form_class = UserForm
    template_name = "core/form.html"
    success_url = reverse_lazy("user_list")

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs["acting_user"] = self.request.user
        return kwargs

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        context["model_name"] = "user"
        context["cancel_url"] = self.success_url
        return context


class UserListView(AdminRequiredMixin, ListView):
    model = User
    template_name = "core/user_list.html"
    context_object_name = "users"

    def get_queryset(self):
        return User.objects.order_by("-is_active", "username").prefetch_related("groups")

    def get_context_data(self, **kwargs):
        context = super().get_context_data(**kwargs)
        for u in context["users"]:
            u.shown_role = "Admin" if u.is_superuser else stored_role(u)
        return context


class UserCreateView(UserFormMixin, CreateView):
    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Created {self.object.username} as {form.cleaned_data['role']}.")
        return response


class UserUpdateView(UserFormMixin, UpdateView):
    def form_valid(self, form):
        response = super().form_valid(form)
        messages.success(self.request, f"Saved {self.object.username}.")
        return response


@require_POST
def user_toggle_active(request, pk):
    # Function view, so the Admin check is spelled out rather than inherited.
    if not request.user.is_authenticated:
        raise PermissionDenied
    if not is_admin(request.user):
        raise PermissionDenied
    target = get_object_or_404(User, pk=pk)
    if target.pk == request.user.pk:
        messages.error(request, "You can't deactivate your own account.")
    else:
        target.is_active = not target.is_active
        target.save(update_fields=["is_active"])
        state = "Reactivated" if target.is_active else "Deactivated"
        messages.success(request, f"{state} {target.username}.")
    return redirect("user_list")
