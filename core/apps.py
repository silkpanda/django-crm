"""
App registration for `core`, the one Django app that holds the whole CRM.

TEACHING NOTE: this is how Django finds the app. `INSTALLED_APPS` in
settings.py lists "core", and Django looks here for its name and defaults.

`default_auto_field` is the type Django gives every model's automatic `id`
column. BigAutoField is a 64-bit integer, so ids can't run out at about 2.1
billion the way the old 32-bit default could. Changing it later would need a
migration (a script that changes the database's structure) on every table,
which is why it gets set once, here, at the start.
"""

from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = 'core'
