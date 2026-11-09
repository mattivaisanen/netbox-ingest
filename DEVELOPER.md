# Developer guide

How the plugin works and why, for anyone changing it.

## Layout

| Module | Does |
| --- | --- |
| `models.py` | Source, Mapping, MappingField, WrittenValue, Run, RunItem |
| `http.py` | Requests: authentication, rate limit, retries, size limit, paging, parallel calls |
| `parsers.py` | Response bytes to Python data. Only JSON; register another format here |
| `jsonpath.py` | The small path language used for records, keys and values |
| `matching.py` | Keys for records and objects, NetBox filters on the object side |
| `transforms.py` | Regex, lookup table and Jinja2 transforms |
| `targets.py` | Reading, converting, validating and bulk-writing NetBox fields |
| `engine.py` | A run from fetch to report; the ownership rule; applying a dry run |
| `jobs.py`, `signals.py` | Background jobs and schedules |
| `retention.py` | Run history limits, clean-up of ownership rows |

## Removable without planning

Removing the plugin must never leave NetBox broken. NetBox core tables that
point at a model by content type (change log, tags, journal, bookmarks,
subscriptions, notifications, jobs with an object) break their pages once
that model's code is gone. So:

- No model uses NetBox's feature mixins. They are `BaseModel` (restricted
  querysets) plus timestamps. Changes to sources and mappings are not
  change-logged; that is the price.
- Jobs are enqueued without an instance and carry the run or source by ID
  in their kwargs. Schedules are found by job name: `Ingest: <name> (#<pk>)`.
- The plugin does not create custom fields, tags or anything else in NetBox.
  Users write into fields they chose.

Leftover rows after removal are harmless: the plugin's own tables (until
`migrate netbox_ingest zero`) and scheduled job rows whose function can't be
imported any more (they fail quietly in the worker).

## A run

1. `IngestJob` (NetBox `JobRunner`) calls `engine.execute(run)`.
2. A PostgreSQL advisory lock per source (`pg_try_advisory_lock`) keeps a
   source from running twice. `max_concurrent_runs` is a count of running
   runs, checked after the lock.
3. Fetch: the lookup list first if there is one, then either the full
   record list (bulk) or, per mapping, one request per object (per object).
   Identical per-object requests across mappings are made once.
4. Match: bulk builds `{key: record}` and `{key: [pk]}` and joins them.
   Duplicate keys on either side are reported as ambiguous and not written.
5. Values: JSON path, transform, then `Target.convert()` (the model field's
   own `to_python`, `validate` and validators, or the custom field's
   `validate`). Invalid values are reported, never written.
6. Compare and write in chunks of 500 objects: rows locked with
   `select_for_update`, current values read with `values()`, the ownership
   rule applied, then one `UPDATE` per (field, value) group.
7. Report rows are saved with one `bulk_create`; history is pruned.

## The ownership rule

`engine.decide(current, new, written, overwrite)`:

| Current value | Plugin's last write | Result |
| --- | --- | --- |
| equals new | any | unchanged (claimed if not owned) |
| equals last write | present | write |
| differs from last write | present | skip: edited in NetBox |
| empty | none | write |
| anything else | none | skip: not written by the plugin |

`overwrite_foreign` on a field turns both skips into writes. `WrittenValue`
holds the last written value per (content type, object, field). "Forget
written values" on a mapping deletes its rows.

A value equal to what the API sends is claimed (a `WrittenValue` row is
added), so a later change in the API updates it. This is safe: the plugin
would have written the same value.

## Patched or bypassed NetBox behaviour

The plugin patches nothing in NetBox. It deliberately bypasses `save()`:

- `QuerySet.update()` for model fields and foreign keys.
- `jsonb_set()` on `custom_field_data` for custom fields, so other custom
  field values on the object are untouched even if they change at the same
  moment. The value is sent as JSON text and cast, because a SQL `NULL`
  would make `jsonb_set` return `NULL` for the whole column.
- No change log, no `post_save` signals, no event rules or webhooks,
  `last_updated` unchanged.
- The global search index is refreshed explicitly with
  `search_backend.cache()` for written objects.

## Dry run and Apply

A dry run stores every planned write as a `RunItem` (status `planned`) with
the current and new value, and these rows are never cut by
`max_report_rows`. Apply re-reads each object with the row locked and writes
only where the field still holds the dry run's current value; otherwise the
row becomes `stale`. Apply does not call the API again.

## Secrets

`secrets.py`: Fernet with a key of SHA-256(`netbox-ingest:` + `SECRET_KEY`).
Forms and the API take the secret write-only. URLs in logs and reports have
the query-string API key replaced by `***`.

## Tests

`netbox_ingest/tests/`, run from a NetBox checkout:

```bash
python manage.py test netbox_ingest
```

HTTP is patched (`http.Client.request`); tests never call the network. CI
runs on pull requests and manual dispatch only, against the oldest supported
and the latest tested NetBox release.
