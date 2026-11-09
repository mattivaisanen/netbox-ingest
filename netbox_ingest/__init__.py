"""
Pull data from JSON APIs into NetBox.

A source describes one API endpoint (URL, authentication, pagination). A
mapping matches the source's records to NetBox objects of one model, and its
fields say which value goes into which NetBox field. A run fetches, matches,
compares and writes, and keeps a report. See DEVELOPER.md.
"""
from netbox.plugins import PluginConfig


class IngestConfig(PluginConfig):
    name = 'netbox_ingest'
    verbose_name = 'Ingest'
    description = 'Pull data from JSON APIs into NetBox fields, with matching, transforms, dry runs and ownership tracking.'
    version = '0.1.0'
    author = 'Matti'
    base_url = 'ingest'
    # Only claim what CI tests (.github/workflows/test.yml).
    min_version = '4.7.0'

    # Limits an administrator sets in configuration.py. Sources can choose
    # lower values, never higher ones. See docs/configuration.md.
    default_settings = {
        # Requests per second to one source, and parallel requests per run.
        'max_requests_per_second': 5,
        'max_concurrency': 4,
        # Runs of different sources at the same time (one per source always).
        'max_concurrent_runs': 2,
        # Shortest schedule interval a source may use, in minutes.
        'min_interval': 5,
        # Per request: timeout in seconds, retries on 429/5xx, response size in MB.
        'request_timeout': 30,
        'max_retries': 2,
        'max_response_mb': 20,
        # Pages followed per fetch before the run stops paging.
        'max_pages': 500,
        # Host names sources may call (fnmatch patterns such as "*.example.net").
        # Empty: any host.
        'allowed_hosts': [],
        # Run history: total size kept and maximum age. The newest run of each
        # source is always kept.
        'retention_mb': 100,
        'retention_days': 90,
        # Report rows stored per run. Counts stay exact beyond this.
        'max_report_rows': 20000,
    }

    menu_items = 'navigation.menu_items'

    def ready(self):
        super().ready()
        # Connects the signal that keeps schedules in step with sources.
        # Importing it runs no database queries.
        from . import signals  # noqa: F401


config = IngestConfig
