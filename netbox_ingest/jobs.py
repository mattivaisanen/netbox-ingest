"""
Background jobs, in NetBox's own job queue.

Jobs are created without a NetBox object (instance=None) on purpose: a job
row pointing at a plugin model would break NetBox's Jobs list once the
plugin is removed. The run is passed by ID instead, and the job is named
after its source.

A source with an interval gets one recurring job ("Ingest: <name>"). Saving
the source replaces it (signals.py). A recurring job whose source is gone
or disabled stops itself.
"""
import logging
from datetime import timedelta

from django.utils import timezone

from netbox.jobs import JobRunner

logger = logging.getLogger('netbox_ingest.jobs')

SCHEDULE_PREFIX = 'Ingest: '


def job_name(source):
    return f'{SCHEDULE_PREFIX}{source.name} (#{source.pk})'


class IngestJob(JobRunner):
    """Runs a source, or applies a dry run."""

    class Meta:
        name = 'Ingest'

    def run(self, *args, source_id=None, run_id=None, dry_run=False, action='run', **kwargs):
        from . import engine
        from .choices import RunStatusChoices, TriggerChoices
        from .models import Run, Source

        if action == 'apply':
            run = Run.objects.select_related('source').get(pk=run_id)
            counts = engine.apply(run, self.job.user)
            self.logger.info(f'Applied {run}: {counts.get("written", 0)} written, '
                             f'{counts.get("stale", 0)} changed since the dry run.')
            return

        if run_id is not None:
            run = Run.objects.select_related('source').get(pk=run_id)
        else:
            # A scheduled run: the source may have changed or gone since.
            source = Source.objects.filter(pk=source_id).first()
            if source is None or not source.enabled or not source.interval:
                self.logger.info('The source is gone, disabled or unscheduled; this schedule ends.')
                self.job.interval = None
                return
            if self.job.interval != source.interval:
                self.job.interval = source.interval
            run = Run.objects.create(source=source, trigger=TriggerChoices.SCHEDULE, job_id=self.job.job_id)
        run.job_id = self.job.job_id
        Run.objects.filter(pk=run.pk).update(job_id=self.job.job_id)
        engine.execute(run)
        self.logger.info(f'{run}: {run.get_status_display()}. {counts_text(run.counts)}')
        if run.status == RunStatusChoices.FAILED:
            self.logger.error(run.error)


def counts_text(counts):
    return ', '.join(f'{n} {name}' for name, n in sorted((counts or {}).items()) if n) or 'nothing to do'


class QueueError(Exception):
    pass


def enqueue_run(run, user=None):
    """Queues a run made by a person or the API."""
    try:
        job = IngestJob.enqueue(name=job_name(run.source), user=user, run_id=run.pk)
    except Exception as exc:  # noqa: BLE001 - shown instead of a server error
        logger.exception('Could not queue run %s', run.pk)
        raise QueueError(f'NetBox could not queue the job ({exc}). Check that Redis and the NetBox worker '
                         f'are running.') from exc
    from .models import Run

    Run.objects.filter(pk=run.pk).update(job_id=job.job_id)
    return job


def enqueue_apply(run, user):
    try:
        return IngestJob.enqueue(name=job_name(run.source), user=user, run_id=run.pk, action='apply')
    except Exception as exc:  # noqa: BLE001
        logger.exception('Could not queue apply of run %s', run.pk)
        raise QueueError(f'NetBox could not queue the job ({exc}).') from exc


def scheduled_jobs(source_id):
    from core.choices import JobStatusChoices
    from core.models import Job

    return Job.objects.filter(
        name__startswith=SCHEDULE_PREFIX, name__endswith=f'(#{source_id})', interval__isnull=False,
        status__in=JobStatusChoices.ENQUEUED_STATE_CHOICES,
    )


def next_run(source):
    job = scheduled_jobs(source.pk).order_by('scheduled').first()
    return job.scheduled if job else None


def sync_schedule(source):
    """Makes the source's recurring job match its interval (or removes it)."""
    for job in scheduled_jobs(source.pk):
        job.delete()
    if source.enabled and source.interval:
        IngestJob.enqueue(
            name=job_name(source), schedule_at=timezone.now() + timedelta(minutes=source.interval),
            interval=source.interval, notifications='on_failure', source_id=source.pk,
        )


def drop_schedule(source_id):
    for job in scheduled_jobs(source_id):
        job.delete()
