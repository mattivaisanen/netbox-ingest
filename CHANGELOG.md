# Changelog

## Unreleased

New:

- Live tests in the source, mapping and field forms, on the values being
  edited (saved or not): test the endpoint (status, time, size, first
  record and its paths), test the authentication (clear messages for
  rejected credentials, 401 and 403, and a warning when the API answers
  without credentials too), and test a mapping or a field against one
  NetBox object to see what a run would write. Nothing is written.
- The source Test page also shows the response size and content type, and
  says when nothing is found at the records path.

## 0.1.0 (2026-11-09)

New:

- Sources: JSON APIs with HTTP Basic, bearer token, API key (header or
  query string) or OAuth2 client credentials authentication; encrypted
  secrets or secrets from environment variables; paging by next-page URL,
  `Link` header, page number or offset.
- Bulk fetching with matching, per-object fetching with URL placeholders,
  and a lookup list that translates an object's key into the API's own ID.
- Mappings to any NetBox model, filtered with NetBox's own filters, matched
  by JSON path and NetBox field with optional regular expressions.
- Fields with regular expression, lookup table and Jinja2 transforms,
  written into model fields, foreign keys or custom fields.
- Ownership: only empty fields and values the plugin wrote are updated;
  everything else is skipped and reported.
- Dry runs with Apply, scheduled runs, run reports with history limits.
- A Test page that calls the API once and lists the paths in a record.
- REST API for sources, mappings, fields and runs.
