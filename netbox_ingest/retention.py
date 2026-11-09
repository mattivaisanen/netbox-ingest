"""
Keeping run history in bounds.

After every run: runs older than retention_days go, then the oldest runs go
until all runs together fit in retention_mb (by each run's estimated size).
The newest run of each source is always kept. Ownership records of objects
deleted from NetBox are cleaned up at the same time.
"""
from datetime import timedelta

from django.db.models import Max
from django.utils import timezone

from .choices import RunStatusChoices
from .models import Run, WrittenValue
from .settings import get_setting


def prune():
    keep = set(Run.objects.values('source_id').annotate(newest=Max('pk')).values_list('newest', flat=True))
    finished = Run.objects.exclude(status__in=RunStatusChoices.ACTIVE).exclude(pk__in=keep)
    # A dry run nobody applied yet is kept as long as it fits.
    days = get_setting('retention_days')
    deleted = 0
    if days:
        deleted += finished.filter(created__lt=timezone.now() - timedelta(days=int(days))).delete()[0]
    limit = int(get_setting('retention_mb') or 0) * 1024 * 1024
    if limit:
        total = 0
        doomed = []
        for pk, size in Run.objects.order_by('-pk').values_list('pk', 'size'):
            total += size
            if total > limit and pk not in keep:
                doomed.append(pk)
        if doomed:
            deleted += finished.filter(pk__in=doomed).delete()[0]
    _prune_written_values()
    return deleted


def _prune_written_values():
    """Forgets values written into objects that no longer exist."""
    for type_id in WrittenValue.objects.values_list('object_type_id', flat=True).distinct():
        rows = WrittenValue.objects.filter(object_type_id=type_id)
        model = rows.first().object_type.model_class()
        if model is None:
            rows.delete()
            continue
        rows.exclude(object_id__in=model.objects.values('pk')).delete()
