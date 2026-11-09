"""
Live tests: call the real API once, on demand, and show what happened.

These back the "Live test" panel of the source, mapping and field forms.
They work on unsaved form values, so a change can be tried before it is
saved. Nothing here writes to NetBox or stores anything: a test builds its
report in memory and throws it away.

- endpoint(): one request, its status, timing, size and a sample record.
- authentication(): whether the API accepts the credentials, with a clear
  message for 401/403, and whether it would also answer without them.
- object_preview(): one NetBox object through the whole run (fetch, match,
  transform, convert, ownership check), without the write.
"""
import copy
import json
import time

from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext as _

from . import engine, http, jsonpath, matching, targets
from .choices import AuthTypeChoices, FetchModeChoices, ItemStatusChoices as S, PaginationChoices
from .models import Mapping, Run
from .secrets import SecretError

#: Characters of the sample record shown.
SAMPLE_CHARS = 20000


def _client(source, secret=None):
    """A client that answers quickly: no retries in a live test."""
    client = http.Client(source, secret=secret)
    client.max_retries = 0
    return client


def _report():
    """A report that is never saved."""
    return engine.Report(Run(pk=0, dry_run=True))


def _request_values(source, key):
    """URL and body for a test request, with {key}, {pk} and {lookup} filled in."""
    values = {'key': key, 'pk': ''}
    url = source.url
    if source.fetch_mode == FetchModeChoices.PER_OBJECT:
        url = http.render_template(source.url, values)
    body = http.body_for(source)
    if body is not None:
        body = http.render_template(body, values, for_json=True)
    return url, body


def needs_key(source):
    """True when the request URL or body cannot be built without an object key."""
    return source.fetch_mode == FetchModeChoices.PER_OBJECT and (
        http.uses_placeholder(source.url + source.body, 'key') or
        http.uses_placeholder(source.url + source.body, 'lookup'))


#
# Endpoint
#

def endpoint(source, key='', secret=None):
    """
    Calls the source's URL once (first page only) and describes the answer.
    Returns a dict for the template; 'error' is set when the call failed.
    """
    out = {'test': 'endpoint'}
    if needs_key(source) and not key:
        out['error'] = _('This URL has a placeholder: enter an object key to test it.')
        return out
    client = None
    try:
        client = _client(source, secret)
        values = {'key': key, 'pk': ''}
        if source.fetch_mode == FetchModeChoices.PER_OBJECT and source.lookup_url:
            pairs = engine.fetch_lookup(source, client, _report())
            mapping = source.mappings.filter(enabled=True).first() if source.pk else None
            mapping = mapping or Mapping(ignore_case=True)
            found = engine.lookup_index(pairs, mapping).get(matching.make_key(key, ignore_case=mapping.ignore_case))
            out['lookup_count'] = len(pairs)
            out['lookup_value'] = found
            values['lookup'] = found
        url = http.render_template(source.url, values) if source.fetch_mode == FetchModeChoices.PER_OBJECT \
            else source.url
        body = http.body_for(source)
        if body is not None:
            body = http.render_template(body, values, for_json=True)

        started = time.monotonic()
        data, response = client.request(url, body=body)
        out['seconds'] = round(time.monotonic() - started, 2)
        out['url'] = client.redact(response.url)
        out['status_code'] = response.status_code
        out['size'] = getattr(response, 'size', None)
        out['content_type'] = response.headers.get('Content-Type', '')
        out['redirected'] = client.redact(url) != out['url']

        # Where the records are, and a sample of the first one.
        if source.records_path and jsonpath.get(data, source.records_path, default=None) is None:
            out['records_warning'] = _('Nothing at the records path "{path}".').format(path=source.records_path)
            if isinstance(data, dict):
                out['records_warning'] += ' ' + _('The response has: {keys}.').format(
                    keys=', '.join(str(k) for k in list(data)[:30]))
        records = client.records_of(data, source.records_path)
        out['record_count'] = len(records)
        sample = records[0] if records else data
        out['sample'] = json.dumps(sample, indent=2, default=str)[:SAMPLE_CHARS]
        out['paths'] = jsonpath.paths(sample)
        if source.pagination != PaginationChoices.NONE:
            out['paging_note'] = _('Only the first page was read.')
    except (http.FetchError, SecretError) as exc:
        out['error'] = str(exc)
        out['status_code'] = getattr(exc, 'status', None)
    finally:
        if client is not None:
            client.close()
    return out


#
# Authentication
#

def _explain(exc):
    """A plain message for a failed request, focused on credentials."""
    status = getattr(exc, 'status', None)
    if getattr(exc, 'token', False):
        if status in (400, 401, 403):
            return _('The token URL refused the client ID or secret (HTTP {status}).').format(status=status)
        return _('The token request failed: {error}').format(error=exc)
    if status == 401:
        return _('The API rejected the credentials (HTTP 401). Check the username, the secret and, for API keys, '
                 'the header or parameter name.')
    if status == 403:
        return _('Access denied (HTTP 403). The credentials are wrong, or they are not allowed to read this '
                 'endpoint.')
    return str(exc)


def authentication(source, key='', secret=None):
    """
    Checks the credentials with one request (plus one without credentials,
    to see whether the API needs them at all). Returns a dict for the
    template with 'ok' True, False or None (no verdict).
    """
    out = {'test': 'authentication', 'auth_type': source.get_auth_type_display(), 'steps': []}

    # Pick a URL that needs no object: the lookup list is fine for per-object sources.
    url, body = _request_values(source, key)
    if needs_key(source) and not key:
        if source.lookup_url:
            url, body = source.lookup_url, None
        else:
            out['ok'] = None
            out['message'] = _('This URL has a placeholder: enter an object key to test it.')
            return out
    out['url'] = url

    # The secret itself: readable, environment variable set?
    if source.auth_type != AuthTypeChoices.NONE:
        try:
            if secret is None:
                from .secrets import get_secret

                secret = get_secret(source)
        except SecretError as exc:
            out['ok'] = False
            out['message'] = str(exc)
            return out
        if not secret:
            out['steps'].append(('warning', _('No secret is set; the request is sent with an empty one.')))

    client = None
    try:
        client = _client(source, secret)
        out['url'] = client.redact(url)
        if source.auth_type == AuthTypeChoices.OAUTH2:
            started = time.monotonic()
            client._oauth_token()
            out['steps'].append(('success', _('Token received from the token URL in {s} s.').format(
                s=round(time.monotonic() - started, 2))))
        started = time.monotonic()
        _data, response = client.request(url, body=body)
        out['seconds'] = round(time.monotonic() - started, 2)
        out['status_code'] = response.status_code
    except (http.FetchError, SecretError) as exc:
        out['ok'] = False
        out['status_code'] = getattr(exc, 'status', None)
        out['message'] = _explain(exc)
        if not isinstance(exc, SecretError) and str(exc) != out['message']:
            out['detail'] = str(exc)
        return out
    finally:
        if client is not None:
            client.close()

    if source.auth_type == AuthTypeChoices.NONE:
        out['ok'] = True
        out['message'] = _('The API answered HTTP {status} without credentials.').format(
            status=out['status_code'])
        return out

    # Accepted. Does the API need credentials at all?
    anonymous = copy.copy(source)
    anonymous.auth_type = AuthTypeChoices.NONE
    client = None
    try:
        client = _client(anonymous)
        client.request(url, body=body)
        out['ok'] = None
        out['message'] = _('The API accepted the request, but it also answers without credentials, so this does '
                           'not prove they are right.')
    except http.FetchError as exc:
        out['ok'] = True
        out['message'] = _('The credentials work.')
        if exc.status in (401, 403):
            out['steps'].append(('success', _('Without them the API answers HTTP {status}.').format(
                status=exc.status)))
    finally:
        if client is not None:
            client.close()
    return out


#
# One object, through the whole run
#

def find_object(mapping, text, user=None):
    """
    The NetBox object a test runs against: "#<ID>" or a key as the mapping
    reads it from its object key field. Returns (object, error message).
    """
    model = mapping.model
    if model is None:
        return None, _('The model of this mapping is not installed.')
    text = (text or '').strip()
    if not text:
        return None, _('Enter an object to test against.')
    try:
        queryset = matching.filtered_queryset(model, mapping.object_filter)
    except matching.FilterError as exc:
        return None, str(exc)
    if user is not None and hasattr(queryset, 'restrict'):
        queryset = queryset.restrict(user, 'view')
    if text.startswith('#') and text[1:].isdigit():
        obj = queryset.filter(pk=int(text[1:])).first()
        if obj is None:
            return None, _('No {model} with ID {id} passes the object filter.').format(
                model=model._meta.verbose_name, id=text[1:])
        return obj, ''
    wanted = matching.make_key(text, ignore_case=mapping.ignore_case)
    keys = matching.object_keys(queryset, mapping.object_key_field, mapping.object_key_regex, mapping.ignore_case)
    pks = keys.get(wanted, [])
    if not pks:
        return None, _('No {model} has the key "{key}" in {field} (after the object filter).').format(
            model=model._meta.verbose_name, key=text, field=mapping.object_key_field)
    if len(pks) > 1:
        return None, _('{n} objects have this key; use #<ID> to pick one.').format(n=len(pks))
    return queryset.get(pk=pks[0]), ''


def object_preview(mapping, fields, obj, secret=None):
    """
    Runs one object through fetch, match, transform and the ownership rule,
    and describes what a run would do to it. Nothing is written.
    fields are MappingField instances, saved or not.
    """
    source = mapping.source
    model = mapping.model
    object_type = ContentType.objects.get_for_model(model)
    out = {'test': 'object', 'object': obj, 'fields': [], 'log': []}
    report = _report()
    queryset = model.objects.filter(pk=obj.pk)
    client = None
    try:
        client = _client(source, secret)
        started = time.monotonic()
        lookup = engine.fetch_lookup(source, client, report) if source.lookup_url else None
        if source.fetch_mode == FetchModeChoices.BULK:
            records = client.fetch_all(source.url, source.records_path, body=http.body_for(source), log=report.log)
            report.log('info', _('Fetched {n} records.').format(n=len(records)))
            matched = engine._match_bulk(mapping, object_type, queryset, records, report)
        else:
            matched = engine._match_per_object(mapping, object_type, queryset, source, client, lookup, {}, report)
        out['seconds'] = round(time.monotonic() - started, 2)
        out['requests'] = client.stats['requests']
    except (http.FetchError, SecretError, matching.FilterError) as exc:
        out['error'] = str(exc)
        return out
    finally:
        if client is not None:
            client.close()

    out['key'] = matching.object_key_by_pk(queryset, mapping.object_key_field, mapping.object_key_regex,
                                           ignore_case=False).get(obj.pk)
    # Why the object did not match, from the report rows about it.
    if obj.pk not in matched:
        reasons = [i.message for i in report.items if i.object_id == obj.pk]
        out['error'] = reasons[0] if reasons else _('No record matched this object.')
        out['log'] = report.log_entries
        return out

    key, record = matched[obj.pk]
    out['record'] = json.dumps(record, indent=2, default=str)[:SAMPLE_CHARS]

    # Transform and compare each field, exactly as a dry run would.
    resolved = []
    for field in fields:
        try:
            resolved.append((field, targets.resolve(model, field.target)))
        except targets.TargetError as exc:
            out['fields'].append({'target': field.target, 'value_path': field.value_path,
                                  'label': _('Invalid field'), 'color': 'red', 'message': str(exc)})
    desired = engine._desired_values(mapping, object_type, resolved, matched, report)
    if desired:
        engine._write(Run(pk=0, dry_run=True), mapping, model, object_type, resolved, desired, report)

    rows = {i.target: i for i in report.items if i.object_id == obj.pk and i.target}
    current_values = model.objects.filter(pk=obj.pk).values(
        'pk', *sorted({c for _f, t in resolved for c in t.columns})).first() or {}
    labels = {value: label for value, label, _color in S.CHOICES}
    for field, target in resolved:
        raw = jsonpath.get(record, field.value_path)
        row = rows.get(field.target)
        entry = {
            'target': field.target,
            'value_path': field.value_path,
            'raw': None if raw is jsonpath.MISSING else json.dumps(targets.jsonable(raw), default=str),
            'current': json.dumps(targets.jsonable(target.read(current_values)), default=str),
        }
        if row is not None:
            entry.update(status=row.status, label=labels.get(row.status, row.status),
                         color=S.colors.get(row.status), message=row.message,
                         new=json.dumps(row.new, default=str) if row.new is not None else None)
        else:
            entry.update(status='unchanged', label=_('Unchanged'), color='green',
                         message=_('NetBox already has this value.'), new=entry['current'])
        out['fields'].append(entry)
    out['log'] = report.log_entries
    return out
