"""
Phase 6, workstream 2 — the notifications table.

TEACHING NOTE — two CreateModels, and the first one is not a model anybody asked
for. `HistoricalRecords` on Notification (see core/models.py) makes
simple-history generate its own paired table, so this migration is half a
migration you did not write. That is the price of having history on every record
in this app, and it is the same price paid by 0001_initial for every other model
with a `history` field.

The file is named `0005_notification.py` rather than the
`0005_historicalnotification_notification.py` that `makemigrations` picked: a
migration's name is arbitrary as long as nothing depends on the old one, and the
generated name reads like a rendering fault. Nothing references it — the next
migration will point at `0005_notification` and the squashed history stays
honest either way.
"""

import django.db.models.deletion
import simple_history.models
from django.conf import settings
from django.db import migrations, models


class Migration(migrations.Migration):

    dependencies = [
        ('core', '0004_lead_teams'),
        migrations.swappable_dependency(settings.AUTH_USER_MODEL),
    ]

    operations = [
        migrations.CreateModel(
            name='HistoricalNotification',
            fields=[
                ('id', models.BigIntegerField(auto_created=True, blank=True, db_index=True, verbose_name='ID')),
                ('kind', models.CharField(choices=[('lead_assigned', 'Lead assigned'), ('task_assigned', 'Task assigned'), ('task_due', 'Task due'), ('deal_moved', 'Deal moved')], max_length=20)),
                ('text', models.CharField(max_length=200)),
                ('url', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(blank=True, editable=False)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('history_id', models.AutoField(primary_key=True, serialize=False)),
                ('history_date', models.DateTimeField(db_index=True)),
                ('history_change_reason', models.CharField(max_length=100, null=True)),
                ('history_type', models.CharField(choices=[('+', 'Created'), ('~', 'Changed'), ('-', 'Deleted')], max_length=1)),
                ('history_user', models.ForeignKey(null=True, on_delete=django.db.models.deletion.SET_NULL, related_name='+', to=settings.AUTH_USER_MODEL)),
                ('user', models.ForeignKey(blank=True, db_constraint=False, null=True, on_delete=django.db.models.deletion.DO_NOTHING, related_name='+', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'verbose_name': 'historical notification',
                'verbose_name_plural': 'historical notifications',
                'ordering': ('-history_date', '-history_id'),
                'get_latest_by': ('history_date', 'history_id'),
            },
            bases=(simple_history.models.HistoricalChanges, models.Model),
        ),
        migrations.CreateModel(
            name='Notification',
            fields=[
                ('id', models.BigAutoField(auto_created=True, primary_key=True, serialize=False, verbose_name='ID')),
                ('kind', models.CharField(choices=[('lead_assigned', 'Lead assigned'), ('task_assigned', 'Task assigned'), ('task_due', 'Task due'), ('deal_moved', 'Deal moved')], max_length=20)),
                ('text', models.CharField(max_length=200)),
                ('url', models.CharField(blank=True, max_length=200)),
                ('created_at', models.DateTimeField(auto_now_add=True)),
                ('read_at', models.DateTimeField(blank=True, null=True)),
                ('user', models.ForeignKey(on_delete=django.db.models.deletion.CASCADE, related_name='notifications', to=settings.AUTH_USER_MODEL)),
            ],
            options={
                'ordering': ['-created_at', '-pk'],
            },
        ),
    ]
