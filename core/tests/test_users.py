"""
Tests for Settings -> Users: who can reach it, what it creates, what it refuses.
"""

from django.contrib.auth import get_user_model
from django.test import TestCase
from django.urls import reverse

from core.permissions import role_of
from .factories import PASSWORD, make_user

User = get_user_model()
STRONG = "violet-anvil-sundial-42"


def payload(**over):
    data = {
        "username": "newrep", "first_name": "New", "last_name": "Rep",
        "email": "newrep@example.com", "is_active": "on", "role": "Rep",
        "password1": STRONG, "password2": STRONG,
    }
    data.update(over)
    return data


class UserAccessTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep")
        self.viewer = make_user("view1", "Viewer")
        self.urls = [
            ("get", reverse("user_list")),
            ("get", reverse("user_create")),
            ("post", reverse("user_create")),
            ("get", reverse("user_update", args=[self.rep.pk])),
            ("post", reverse("user_update", args=[self.rep.pk])),
            ("post", reverse("user_toggle_active", args=[self.rep.pk])),
        ]

    def test_non_admins_get_403_everywhere(self):
        for who in (self.rep, self.viewer):
            self.client.force_login(who)
            for method, url in self.urls:
                with self.subTest(user=who.username, method=method, url=url):
                    self.assertEqual(getattr(self.client, method)(url, payload()).status_code, 403)
        self.assertFalse(User.objects.filter(username="newrep").exists())

    def test_anonymous_is_redirected_to_login(self):
        for method, url in self.urls:
            with self.subTest(method=method, url=url):
                resp = getattr(self.client, method)(url, payload())
                self.assertEqual(resp.status_code, 302)
                self.assertIn("login", resp["Location"])

    def test_rep_cannot_promote_self(self):
        self.client.force_login(self.rep)
        self.client.post(reverse("user_update", args=[self.rep.pk]), payload(username="rep1", role="Admin"))
        self.assertEqual(role_of(User.objects.get(pk=self.rep.pk)), "Rep")

    def test_sidebar_link_admin_only(self):
        self.client.force_login(self.admin)
        self.assertContains(self.client.get(reverse("dashboard")), reverse("user_list"))
        self.client.force_login(self.rep)
        self.assertNotContains(self.client.get(reverse("dashboard")), reverse("user_list"))


class UserCreateTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", "Admin")
        self.client.force_login(self.admin)

    def test_admin_creates_user_who_can_log_in_with_role(self):
        resp = self.client.post(reverse("user_create"), payload())
        self.assertRedirects(resp, reverse("user_list"))
        new = User.objects.get(username="newrep")
        self.assertEqual(role_of(new), "Rep")
        self.assertNotEqual(new.password, STRONG)  # hashed, not stored
        self.client.logout()
        self.assertTrue(self.client.login(username="newrep", password=STRONG))

    def test_weak_password_rejected(self):
        resp = self.client.post(reverse("user_create"), payload(password1="short", password2="short"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="newrep").exists())

    def test_mismatched_password_rejected(self):
        resp = self.client.post(reverse("user_create"), payload(password2=STRONG + "x"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="newrep").exists())

    def test_duplicate_username_any_case_rejected(self):
        resp = self.client.post(reverse("user_create"), payload(username="BOSS"))
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(User.objects.filter(username__iexact="boss").count(), 1)

    def test_bogus_role_rejected(self):
        resp = self.client.post(reverse("user_create"), payload(role="Superuser"))
        self.assertEqual(resp.status_code, 200)
        self.assertFalse(User.objects.filter(username="newrep").exists())

    def test_form_cannot_grant_superuser(self):
        self.client.post(reverse("user_create"), payload(is_superuser="on", is_staff="on"))
        new = User.objects.get(username="newrep")
        self.assertFalse(new.is_superuser)
        self.assertFalse(new.is_staff)


class UserEditTests(TestCase):
    def setUp(self):
        self.admin = make_user("boss", "Admin")
        self.rep = make_user("rep1", "Rep", email="rep1@example.com")
        self.client.force_login(self.admin)

    def edit(self, user, **over):
        data = payload(username=user.username, email=user.email or "x@example.com")
        data.pop("password1"), data.pop("password2")
        data.update(over)
        return self.client.post(reverse("user_update", args=[user.pk]), data)

    def test_change_role_replaces_group(self):
        self.edit(self.rep, role="Viewer")
        rep = User.objects.get(pk=self.rep.pk)
        self.assertEqual(role_of(rep), "Viewer")
        self.assertEqual(rep.groups.count(), 1)

    def test_edit_does_not_touch_password(self):
        before = User.objects.get(pk=self.rep.pk).password
        self.edit(self.rep, first_name="Renamed")
        self.assertEqual(User.objects.get(pk=self.rep.pk).password, before)

    def test_cannot_demote_self(self):
        resp = self.edit(self.admin, role="Rep")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(role_of(User.objects.get(pk=self.admin.pk)), "Admin")

    def test_cannot_deactivate_self_via_form(self):
        data = payload(username="boss", email="boss@example.com", role="Admin")
        data.pop("is_active"), data.pop("password1"), data.pop("password2")
        resp = self.client.post(reverse("user_update", args=[self.admin.pk]), data)
        self.assertEqual(resp.status_code, 200)
        self.assertTrue(User.objects.get(pk=self.admin.pk).is_active)

    def test_toggle_deactivates_and_reactivates(self):
        url = reverse("user_toggle_active", args=[self.rep.pk])
        self.client.post(url)
        self.assertFalse(User.objects.get(pk=self.rep.pk).is_active)
        self.client.logout()
        self.assertFalse(self.client.login(username="rep1", password=PASSWORD))
        self.client.force_login(self.admin)
        self.client.post(url)
        self.assertTrue(User.objects.get(pk=self.rep.pk).is_active)

    def test_toggle_refuses_self(self):
        self.client.post(reverse("user_toggle_active", args=[self.admin.pk]))
        self.assertTrue(User.objects.get(pk=self.admin.pk).is_active)

    def test_toggle_requires_post(self):
        self.assertEqual(self.client.get(reverse("user_toggle_active", args=[self.rep.pk])).status_code, 405)

    def test_inactive_user_still_shows_stored_role(self):
        self.client.post(reverse("user_toggle_active", args=[self.rep.pk]))
        resp = self.client.get(reverse("user_update", args=[self.rep.pk]))
        self.assertEqual(resp.context["form"].fields["role"].initial, "Rep")

    def test_superuser_role_locked(self):
        root = User.objects.create_superuser("root", "root@example.com", PASSWORD)
        resp = self.edit(root, role="Viewer", email="root@example.com")
        self.assertEqual(role_of(User.objects.get(pk=root.pk)), "Admin")
        self.assertEqual(resp.status_code, 302)
