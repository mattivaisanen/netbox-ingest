"""Keeps each source's recurring job in step with its settings."""
import logging

from django.db import transaction
from django.db.models.signals import post_delete, post_save
from django.dispatch import receiver

from .models import Source

logger = logging.getLogger('netbox_ingest')

#: Fields that change the schedule.
SCHEDULE_FIELDS = ('enabled', 'interval', 'name')


@receiver(post_save, sender=Source)
def source_saved(sender, instance, created, update_fields=None, **kwargs):
    if update_fields and not set(update_fields) & set(SCHEDULE_FIELDS):
        return
    pk = instance.pk

    def sync():
        from .jobs import sync_schedule

        try:
            source = Source.objects.filter(pk=pk).first()
            if source is not None:
                sync_schedule(source)
        except Exception:  # noqa: BLE001 - saving a source must not fail on Redis trouble
            logger.exception('Could not update the schedule of source %s', pk)

    transaction.on_commit(sync)


@receiver(post_delete, sender=Source)
def source_deleted(sender, instance, **kwargs):
    pk = instance.pk

    def drop():
        from .jobs import drop_schedule

        try:
            drop_schedule(pk)
        except Exception:  # noqa: BLE001
            logger.exception('Could not remove the schedule of source %s', pk)

    transaction.on_commit(drop)
