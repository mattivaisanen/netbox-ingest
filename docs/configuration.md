# Configuration

Administrator limits go in NetBox's `configuration.py`. A source can choose
lower values for its own rate, parallel requests and timeout, never higher
ones.

```python
PLUGINS_CONFIG = {
    'netbox_ingest': {
        'max_requests_per_second': 2,
        'allowed_hosts': ['*.example.net'],
    },
}
```

| Key | Type | Default | Description |
| --- | --- | --- | --- |
| `max_requests_per_second` | float | `5` | Requests per second to one source, across all its parallel requests. |
| `max_concurrency` | int | `4` | Parallel requests in one run (per-object sources). |
| `max_concurrent_runs` | int | `2` | Runs of different sources at the same time. A source never runs twice at once. A run over the limit is skipped and says so; a scheduled one tries again at its next time. |
| `min_interval` | int | `5` | Shortest schedule a source may use, in minutes. |
| `request_timeout` | int | `30` | Seconds per request. |
| `max_retries` | int | `2` | Retries of a request that failed with HTTP 429, a 5xx status or a network error. `Retry-After` is honoured, up to 60 seconds. |
| `max_response_mb` | int | `20` | Largest response read, in megabytes. A larger one fails the request. |
| `max_pages` | int | `500` | Pages followed in one fetch. The run log says when paging stopped early. |
| `allowed_hosts` | list | `[]` | Host names sources may call, as patterns (`api.example.net`, `*.example.net`). Empty allows any host. |
| `retention_mb` | int | `100` | Total size of run history kept. The oldest runs are deleted first; the newest run of each source is always kept. |
| `retention_days` | int | `90` | Runs older than this are deleted. `0` keeps them until `retention_mb` is reached. |
| `max_report_rows` | int | `20000` | Report rows stored per run. Counts stay exact; planned writes of a dry run are always stored. |

## Permissions

| Permission | Allows |
| --- | --- |
| `netbox_ingest.view_source`, `add_source`, `change_source`, `delete_source` | Managing sources (and their schedules) |
| `netbox_ingest.*_mapping`, `netbox_ingest.*_mappingfield` | Managing mappings and their fields |
| `netbox_ingest.run_source` | Test requests, dry runs, runs and applying dry runs |
| `netbox_ingest.view_run`, `delete_run` | Reading and deleting run reports |

Running a source or applying a dry run also needs the change permission on
every model the source's mappings write (for example `dcim.change_device`).
Scheduled runs have no user and are not checked this way, so give the
permissions to edit sources and mappings only to people trusted to write
those models.

## Secrets

A source's secret (password, token, API key, OAuth2 client secret) is
stored encrypted with a key derived from NetBox's `SECRET_KEY`, and is never
shown again in the UI or the REST API. If `SECRET_KEY` changes, re-enter
the secrets. Alternatively, name an environment variable of the NetBox
worker on the source; nothing is stored then.
