# netbox-ingest

A NetBox plugin that keeps NetBox fields filled with data from other
systems' JSON APIs.

You point it at an API endpoint, tell it which NetBox objects the records
belong to and which value goes into which field. It fetches the data on a
schedule or on demand, matches each record to its NetBox object, and writes
the values: into a custom field, or into a built-in field such as
`description` or `status`. Before anything is written you can see exactly
what would change.

Typical uses: a warranty end date from an asset system into a device custom
field, a firmware version reported by a management API, an owner or cost
center from a CMDB, an operational state from any tool that has a REST API.

## What it does

- **Sources**: one API endpoint each. HTTP Basic, bearer tokens, API keys
  in a header or query string, or OAuth2 client credentials. Secrets are
  stored encrypted or read from an environment variable. Follows paging
  (next-page URL, `Link` header, page number, offset).
- **Two ways to fetch**: *bulk* reads the whole list once and matches it to
  NetBox objects; *per object* calls a URL built from each object, such as
  `https://api.example.com/assets/{key}`. A *lookup* list can translate an
  object's name into the API's own ID first.
- **Mappings**: which objects (one model, optionally filtered with NetBox's
  usual filters) and how they match: a JSON path in the record against a
  NetBox field, with an optional regular expression on either side.
- **Fields**: a JSON path in the record and the NetBox field it goes into,
  with an optional transform (regular expression, lookup table, or a Jinja2
  template).
- **Dry run and Apply**: a dry run shows every value that would change,
  current and new, side by side. Apply writes what the dry run showed, and
  skips anything someone changed in between.
- **Runs**: every run has a report: what was written, what was skipped and
  why, records that matched no object, objects with no record, failed
  requests.

## What it never overwrites

The plugin only writes into a field when the field is empty, or still holds
exactly the value the plugin itself wrote last time. A value set by a person
or another tool is left alone, and the run report says so. If someone edits
a value the plugin wrote, the plugin stops updating it. You can allow
overwriting per field when you really want the API to win.

When the API has no value for a field, the field is left as it is. The
plugin never creates or deletes NetBox objects.

## No change log rows

Writes are made in bulk, outside NetBox's normal save path, so a scheduled
run over thousands of objects does not add thousands of change log entries.
The plugin's own run reports are the record of what it wrote. As a result,
these writes also do not trigger NetBox event rules or webhooks, and do not
change an object's "last updated" time.

## Requirements

- NetBox **4.7** or later. Tested with NetBox 4.7.0 and 4.7.4.
- No other plugins or Python packages.
- NetBox's background worker (`manage.py rqworker`) for runs.

## Installation

Not on PyPI; install from GitHub into NetBox's Python environment:

```bash
pip install git+https://github.com/mattivaisanen/netbox-ingest.git
```

Add it to `configuration.py` and run the migrations:

```python
PLUGINS = ['netbox_ingest']
```

```bash
cd /opt/netbox/netbox && python manage.py migrate
```

Restart NetBox and its worker. The plugin appears under **Plugins**:
Ingest Sources, Ingest Mappings and Ingest Runs.

Limits for administrators (request rate, parallel requests, allowed hosts,
history size): [docs/configuration.md](docs/configuration.md).

## Upgrading

```bash
pip install --upgrade git+https://github.com/mattivaisanen/netbox-ingest.git
cd /opt/netbox/netbox && python manage.py migrate
```

Then restart NetBox and its worker.

## Removing

Remove `netbox_ingest` from `PLUGINS`, restart NetBox and its worker, and
`pip uninstall netbox-ingest`. Nothing else is needed: the plugin keeps all
its records in its own tables and adds nothing to NetBox's change log,
tags, journal or job objects. Values it wrote stay in NetBox as ordinary
data.

Optional: `python manage.py migrate netbox_ingest zero` before removing it
drops the plugin's tables. Scheduled runs left in NetBox's job list show as
scheduled until you delete them; they do nothing.

## License

Apache License 2.0, see [LICENSE](LICENSE).
