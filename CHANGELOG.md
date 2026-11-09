# Changelog

## Unreleased

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
