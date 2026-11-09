"""
A run, start to finish: fetch, match, transform, compare, write, report.

The ownership rule decides every write (see decide()): the plugin fills
empty fields and updates values it wrote itself, and leaves everything else
alone, saying why in the report. A field's "overwrite values not written by
the plugin" switch is the only way past that.

Writes are bulk updates (targets.py), done in chunks inside a transaction
with the rows locked, so a person saving the same object at the same moment
either goes first (and the plugin sees the edit) or waits. A dry run does
everything except the write and stores the planned writes; applying it later
writes only what still holds the value the dry run saw.
"""
import json
import logging
import time
from collections import defaultdict

from django.contrib.contenttypes.models import ContentType
from django.db import connection, transaction
from django.db.models import Q
from django.utils import timezone

from . import http, jsonpath, matching, targets, transforms
from .choices import FetchModeChoices, ItemStatusChoices as S, RunStatusChoices
from .models import Run, RunItem, WrittenValue
from .secrets import SecretError
from .settings import get_setting

logger = logging.getLogger('netbox_ingest')

#: Objects written per transaction.
CHUNK = 500
#: Key for PostgreSQL advisory locks (one per source): "ingest" in hex-ish.
LOCK_CLASS = 0x1A6E57

UNCHANGED = 'unchanged'
WRITE = 'write'


class RunError(Exception):
    """Stops a run with a message for its report."""


#
# The ownership rule
#

def decide(current, new, written, overwrite_foreign=False):
    """
    (outcome, message) for writing new over current. written is the
    WrittenValue the plugin left in this field, or None.
    """
    if targets.same(current, new):
        return UNCHANGED, ''
    if written is not None:
        if targets.same(current, written.value):
            return WRITE, ''
        if overwrite_foreign:
            return WRITE, 'Replaced a value edited in NetBox (overwrite is on).'
        return S.EDITED, f'Edited in NetBox since the plugin wrote {json.dumps(written.value)}.'
    if targets.is_empty(current):
        return WRITE, ''
    if overwrite_foreign:
        return WRITE, 'Replaced a value the plugin had not written (overwrite is on).'
    return S.FOREIGN, 'The current value was not written by the plugin.'


#
# The report
#

class Report:
    """Collects a run's rows, counts and log; saves them once at the end."""

    def __init__(self, run):
        self.run = run
        self.items = []
        self.counts = defaultdict(int)
        self.log_entries = []
        self.max_rows = int(get_setting('max_report_rows'))
        self.truncated = False

    def log(self, level, message):
        self.log_entries.append({'level': level, 'message': str(message)[:2000]})
        getattr(logger, 'warning' if level == 'warning' else level if level in ('info', 'error') else 'info')(
            'Run %s: %s', self.run.pk, message)

    def add(self, status, **fields):
        self.counts[status] += 1
        # Planned writes are always kept: applying the dry run needs them.
        if len(self.items) >= self.max_rows and status != S.PLANNED:
            if not self.truncated:
                self.truncated = True
                self.log('warning', f'The report keeps {self.max_rows} rows (max_report_rows); '
                                    f'the counts include everything.')
            return None
        item = RunItem(run=self.run, status=status, **fields)
        self.items.append(item)
        return item

    def fill_reprs(self, model, object_type):
        """Names objects in this mapping's rows, a few queries for all of them."""
        pending = [i for i in self.items if i.object_type_id == object_type.pk and i.object_id and not i.object_repr]
        ids = list({i.object_id for i in pending})
        names = {}
        for start in range(0, len(ids), 2000):
            for obj in model.objects.filter(pk__in=ids[start:start + 2000]):
                names[obj.pk] = str(obj)[:200]
        for item in pending:
            item.object_repr = names.get(item.object_id, f'#{item.object_id}')

    def save(self):
        size = 0
        for item in self.items:
            size += 150 + len(item.object_repr) + len(item.key) + len(item.message) + \
                len(json.dumps(item.old, default=str)) + len(json.dumps(item.new, default=str))
        RunItem.objects.bulk_create(self.items, batch_size=2000)
        self.run.counts = dict(self.counts)
        self.run.log = self.log_entries
        self.run.size = size + len(json.dumps(self.log_entries))


#
# Running a source
#

def _try_lock(source_id):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_try_advisory_lock(%s, %s)', [LOCK_CLASS, source_id])
        return cursor.fetchone()[0]


def _unlock(source_id):
    with connection.cursor() as cursor:
        cursor.execute('SELECT pg_advisory_unlock(%s, %s)', [LOCK_CLASS, source_id])


def execute(run):
    """Runs a queued run (dry or live). Called by the background job."""
    source = run.source
    report = Report(run)
    Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.RUNNING, started=timezone.now())
    run.started = timezone.now()

    if not _try_lock(source.pk):
        return _finish(run, report, RunStatusChoices.SKIPPED, 'Another run of this source is still going.')
    try:
        limit = int(get_setting('max_concurrent_runs'))
        if Run.objects.filter(status=RunStatusChoices.RUNNING).exclude(pk=run.pk).count() >= limit:
            return _finish(run, report, RunStatusChoices.SKIPPED,
                           f'{limit} runs were already going (max_concurrent_runs).')
        started = time.monotonic()
        client = None
        try:
            client = http.Client(source)
            _execute(run, source, client, report)
            status, error = (RunStatusChoices.DRY_RUN if run.dry_run else RunStatusChoices.COMPLETED), ''
        except (RunError, http.FetchError, SecretError, matching.FilterError) as exc:
            status, error = RunStatusChoices.FAILED, str(exc)
        except Exception as exc:  # noqa: BLE001 - recorded on the run, then re-raised for the job log
            _finish(run, report, RunStatusChoices.FAILED, f'{exc.__class__.__name__}: {exc}')
            raise
        finally:
            if client is not None:
                run.stats = {**client.stats, 'seconds': round(time.monotonic() - started, 2),
                             **run.stats}
                client.close()
        return _finish(run, report, status, error)
    finally:
        _unlock(source.pk)


def _finish(run, report, status, error=''):
    if error:
        report.log('error', error)
    report.save()
    run.status = status
    run.error = error
    run.finished = timezone.now()
    run.save()
    from . import retention

    try:
        retention.prune()
    except Exception:  # noqa: BLE001 - retention must never fail a run
        logger.exception('Pruning old runs failed')
    return run


def _execute(run, source, client, report):
    mappings = list(source.mappings.filter(enabled=True).select_related('object_type').prefetch_related('fields'))
    if not mappings:
        raise RunError('The source has no enabled mappings.')
    body = http.body_for(source)

    lookup = None
    if source.lookup_url:
        lookup = fetch_lookup(source, client, report)

    records = None
    if source.fetch_mode == FetchModeChoices.BULK:
        records = client.fetch_all(source.url, source.records_path, body=body, log=report.log)
        run.stats = {'records': len(records)}
        report.log('info', f'Fetched {len(records)} records.')

    # Per-object responses, shared between mappings that ask for the same URL.
    cache = {}
    for mapping in mappings:
        model = mapping.model
        if model is None:
            report.log('error', f'{mapping.name}: its model is no longer installed; skipped.')
            continue
        fields = [f for f in mapping.fields.all() if f.enabled]
        resolved = []
        for field in fields:
            try:
                resolved.append((field, targets.resolve(model, field.target)))
            except targets.TargetError as exc:
                report.log('error', f'{mapping.name}: {exc} The field is skipped.')
        if not resolved:
            report.log('warning', f'{mapping.name}: no enabled fields; skipped.')
            continue
        object_type = ContentType.objects.get_for_model(model)
        queryset = matching.filtered_queryset(model, mapping.object_filter)
        if records is not None:
            matched = _match_bulk(mapping, object_type, queryset, records, report)
        else:
            matched = _match_per_object(mapping, object_type, queryset, source, client, lookup, cache, report)
        desired = _desired_values(mapping, object_type, resolved, matched, report)
        _write(run, mapping, model, object_type, resolved, desired, report)
        report.fill_reprs(model, object_type)


def fetch_lookup(source, client, report):
    """[(key as the API writes it, value)] from the lookup URL."""
    rows = client.fetch_all(source.lookup_url, source.lookup_records_path, log=report.log)
    pairs = []
    for row in rows:
        key = jsonpath.get(row, source.lookup_key_path, default=None)
        value = jsonpath.get(row, source.lookup_value_path, default=None)
        if key is not None and value is not None and not isinstance(value, (dict, list)):
            pairs.append((key, value))
    report.log('info', f'Lookup: {len(pairs)} usable records of {len(rows)}.')
    return pairs


def lookup_index(pairs, mapping):
    """
    {key: value} for one mapping: the lookup's keys go through the mapping's
    record key regex and case rule, like bulk records do.
    """
    index = {}
    for raw, value in pairs:
        key = matching.make_key(raw, mapping.record_key_regex, mapping.ignore_case)
        if key is not None:
            index.setdefault(key, value)
    return index


def _match_bulk(mapping, object_type, queryset, records, report):
    """{pk: (key, record)} for objects with exactly one matching record."""
    index, duplicates, keyless = matching.record_index(
        records, mapping.record_key_path, mapping.record_key_regex, mapping.ignore_case)
    objects = matching.object_keys(queryset, mapping.object_key_field, mapping.object_key_regex,
                                   mapping.ignore_case)
    matched = {}
    for key, pks in objects.items():
        if key in duplicates:
            for pk in pks:
                report.add(S.AMBIGUOUS, mapping=mapping, object_type=object_type, object_id=pk, key=key,
                           message='More than one record has this key.')
        elif len(pks) > 1 and key in index:
            for pk in pks:
                report.add(S.AMBIGUOUS, mapping=mapping, object_type=object_type, object_id=pk, key=key,
                           message=f'{len(pks)} objects have this key.')
        elif key in index:
            matched[pks[0]] = (key, index[key])
        else:
            for pk in pks:
                report.add(S.NO_DATA, mapping=mapping, object_type=object_type, object_id=pk, key=key,
                           message='No record has this key; the object is left alone.')
    for key in index.keys() - objects.keys():
        report.add(S.UNMATCHED, mapping=mapping, key=key[:300], message='No object has this key.')
    if keyless:
        report.log('warning', f'{mapping.name}: {len(keyless)} records have no key at '
                              f'"{mapping.record_key_path}" and were not matched.')
    report.counts['matched'] += len(matched)
    report.log('info', f'{mapping.name}: {len(matched)} objects matched.')
    return matched


def _match_per_object(mapping, object_type, queryset, source, client, lookup, cache, report):
    """{pk: (key, record)}: one request per object, URL built from its key."""
    raw_keys = matching.object_key_by_pk(queryset, mapping.object_key_field, mapping.object_key_regex,
                                         ignore_case=False)
    needs_key = http.uses_placeholder(source.url + source.body, 'key') or lookup is not None
    index = lookup_index(lookup, mapping) if lookup is not None else None
    body = http.body_for(source)
    wanted, pending = {}, []
    for pk, key in raw_keys.items():
        if needs_key and key is None:
            report.add(S.NO_DATA, mapping=mapping, object_type=object_type, object_id=pk,
                       message=f'The object has no key in "{mapping.object_key_field}".')
            continue
        values = {'key': key, 'pk': pk}
        if lookup is not None:
            values['lookup'] = index.get(matching.make_key(key, ignore_case=mapping.ignore_case))
            if values['lookup'] is None:
                report.add(S.NO_DATA, mapping=mapping, object_type=object_type, object_id=pk, key=key,
                           message='The key is not in the lookup.')
                continue
        url = http.render_template(source.url, values)
        request_body = http.render_template(body, values, for_json=True) if body is not None else None
        wanted[pk] = (key, (url, request_body))
        if (url, request_body) not in cache:
            cache[(url, request_body)] = None
            pending.append(((url, request_body), url, request_body))
    for request_key, result in client.fetch_many(pending).items():
        cache[request_key] = result

    matched = {}
    for pk, (key, request_key) in wanted.items():
        data, error = cache[request_key]
        if error:
            report.add(S.FAILED, mapping=mapping, object_type=object_type, object_id=pk, key=key or '',
                       message=error[:500])
            continue
        records = http.Client.records_of(data, source.records_path)
        if mapping.record_key_path and len(records) > 1:
            wanted_key = matching.make_key(key, ignore_case=mapping.ignore_case)
            records = [r for r in records if matching.make_key(
                jsonpath.get(r, mapping.record_key_path), mapping.record_key_regex, mapping.ignore_case) == wanted_key]
        if not records:
            report.add(S.NO_DATA, mapping=mapping, object_type=object_type, object_id=pk, key=key or '',
                       message='The response has no record.')
        elif len(records) > 1:
            report.add(S.AMBIGUOUS, mapping=mapping, object_type=object_type, object_id=pk, key=key or '',
                       message=f'The response has {len(records)} records; set the record key path to pick one.')
        else:
            matched[pk] = (key, records[0])
    report.counts['matched'] += len(matched)
    report.log('info', f'{mapping.name}: {len(pending)} requests, {len(matched)} objects matched.')
    return matched


def _desired_values(mapping, object_type, resolved, matched, report):
    """{pk: {target: value}}: converted and validated values from the records."""
    from django.core.exceptions import ValidationError

    desired = {}
    for pk, (key, record) in matched.items():
        values = {}
        for field, target in resolved:
            raw = jsonpath.get(record, field.value_path)
            common = dict(mapping=mapping, object_type=object_type, object_id=pk, key=str(key or '')[:300],
                          target=field.target)
            if raw is jsonpath.MISSING or raw is None:
                report.add(S.NO_VALUE, message=f'"{field.value_path}" is empty or missing; left alone.', **common)
                continue
            try:
                value = transforms.apply(field, raw, record=record, key=key)
            except transforms.TransformError as exc:
                report.add(S.INVALID, new=targets.jsonable(raw), message=str(exc)[:500], **common)
                continue
            if value is transforms.SKIP:
                report.add(S.NO_VALUE, new=targets.jsonable(raw),
                           message='The transform gave no value; left alone.', **common)
                continue
            try:
                values[field.target] = target.convert(value)
            except ValidationError as exc:
                report.add(S.INVALID, new=targets.jsonable(value), message='; '.join(exc.messages)[:500], **common)
        if values:
            desired[pk] = values
    return desired


def _write(run, mapping, model, object_type, resolved, desired, report):
    """Compares with NetBox and writes (or plans) in chunks."""
    by_target = {field.target: (field, target) for field, target in resolved}
    columns = sorted({c for _field, target in resolved for c in target.columns})
    pks = list(desired)
    written_pks = set()
    for start in range(0, len(pks), CHUNK):
        chunk = pks[start:start + CHUNK]
        with transaction.atomic():
            rows = model.objects.filter(pk__in=chunk)
            if not run.dry_run:
                rows = rows.select_for_update(of=('self',))
            rows = {row['pk']: row for row in rows.values('pk', *columns)}
            owned = {
                (w.object_id, w.target): w for w in WrittenValue.objects.filter(
                    object_type=object_type, object_id__in=chunk, target__in=list(by_target))
            }
            writes = {}  # (target, value as JSON) -> (value, [pk])
            ownership = []
            for pk in chunk:
                row = rows.get(pk)
                if row is None:
                    continue  # deleted meanwhile
                for name, new in desired[pk].items():
                    field, target = by_target[name]
                    current = target.read(row)
                    written = owned.get((pk, name))
                    outcome, message = decide(current, new, written, field.overwrite_foreign)
                    common = dict(mapping=mapping, object_type=object_type, object_id=pk, target=name,
                                  old=targets.jsonable(current), new=targets.jsonable(new), message=message)
                    if outcome == UNCHANGED:
                        report.counts[UNCHANGED] += 1
                        # Same value: the plugin now answers for it, so later
                        # API changes can update it.
                        if written is None and not run.dry_run and not targets.is_empty(new):
                            ownership.append((pk, name, new))
                    elif outcome == WRITE:
                        if run.dry_run:
                            report.add(S.PLANNED, **common)
                        else:
                            report.add(S.WRITTEN, **common)
                            slot = writes.setdefault((name, json.dumps(targets.jsonable(new), sort_keys=True)),
                                                     (new, []))
                            slot[1].append(pk)
                            ownership.append((pk, name, new))
                    else:
                        report.add(outcome, **common)
            for (name, _key), (value, write_pks) in writes.items():
                by_target[name][1].update(model.objects.filter(pk__in=write_pks), value)
                written_pks.update(write_pks)
            _own(ownership, object_type, mapping, run)
    if written_pks:
        _reindex(model, written_pks)


def _own(rows, object_type, mapping, run):
    """Records (pk, target, value) as written by the plugin."""
    if not rows:
        return
    WrittenValue.objects.bulk_create(
        [WrittenValue(object_type=object_type, object_id=pk, target=name, value=targets.jsonable(value),
                      mapping=mapping, run=run) for pk, name, value in rows],
        update_conflicts=True, unique_fields=('object_type', 'object_id', 'target'),
        update_fields=('value', 'mapping', 'run', 'updated'), batch_size=2000,
    )


def _reindex(model, pks):
    """Refreshes NetBox's global search for written objects (save() would have)."""
    try:
        from netbox.search.backends import search_backend

        pks = list(pks)
        for start in range(0, len(pks), 1000):
            search_backend.cache(model.objects.filter(pk__in=pks[start:start + 1000]))
    except Exception:  # noqa: BLE001 - search is a convenience; never fail a run on it
        logger.exception('Updating the search index failed')


#
# Applying a dry run
#

def apply(run, user):
    """Writes a dry run's planned values that NetBox still holds as the dry run saw them."""
    Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.APPLYING)
    if not _try_lock(run.source_id):
        Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.DRY_RUN)
        raise RunError('Another run of this source is going; try again when it ends.')
    try:
        items = list(run.items.filter(status=S.PLANNED).select_related('mapping', 'object_type'))
        counts = defaultdict(int)
        by_type = defaultdict(list)
        for item in items:
            by_type[item.object_type_id].append(item)
        for type_items in by_type.values():
            _apply_items(run, type_items, counts)
        RunItem.objects.bulk_update(items, ['status', 'message'], batch_size=2000)
        totals = dict(run.counts)
        totals[S.PLANNED] = 0
        for status, n in counts.items():
            totals[status] = totals.get(status, 0) + n
        run.counts = {k: v for k, v in totals.items() if v}
        run.status = RunStatusChoices.COMPLETED
        run.applied = timezone.now()
        run.applied_by = user
        run.log = list(run.log) + [{'level': 'info', 'message': (
            f'Applied by {user}: {counts[S.WRITTEN]} written, {counts[S.STALE]} changed since the dry run.')}]
        run.save()
        return counts
    except Exception:
        Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.DRY_RUN)
        raise
    finally:
        _unlock(run.source_id)


def _apply_items(run, items, counts):
    from django.core.exceptions import ValidationError

    object_type = items[0].object_type
    model = object_type.model_class()
    if model is None:
        for item in items:
            item.status, item.message = S.FAILED, 'The model is no longer installed.'
        return
    resolved = {}
    for start in range(0, len(items), CHUNK):
        chunk = items[start:start + CHUNK]
        pks = {item.object_id for item in chunk}
        for item in chunk:
            if item.target not in resolved:
                try:
                    resolved[item.target] = targets.resolve(model, item.target)
                except targets.TargetError as exc:
                    resolved[item.target] = exc
        columns = sorted({c for t in resolved.values() if not isinstance(t, Exception) for c in t.columns})
        with transaction.atomic():
            rows = {row['pk']: row for row in model.objects.filter(pk__in=pks).select_for_update(of=('self',))
                    .values('pk', *columns)}
            ownership, written = [], set()
            for item in chunk:
                target = resolved[item.target]
                row = rows.get(item.object_id)
                if isinstance(target, Exception) or row is None:
                    item.status = S.FAILED
                    item.message = str(target) if isinstance(target, Exception) else 'The object was deleted.'
                    counts[item.status] += 1
                    continue
                current = target.read(row)
                if targets.same(current, item.new):
                    item.status, item.message = S.WRITTEN, 'Already had this value.'
                elif targets.same(current, item.old):
                    try:
                        value = target.convert(item.new)
                    except ValidationError as exc:
                        item.status, item.message = S.INVALID, '; '.join(exc.messages)[:500]
                        counts[item.status] += 1
                        continue
                    target.update(model.objects.filter(pk=item.object_id), value)
                    written.add(item.object_id)
                    item.status = S.WRITTEN
                else:
                    item.status = S.STALE
                    item.message = 'Changed in NetBox after the dry run; left alone.'
                    counts[item.status] += 1
                    continue
                counts[item.status] += 1
                ownership.append((item.object_id, item.target, item.new))
            _own(ownership, object_type, items[0].mapping, run)
        if written:
            _reindex(model, written)


def forget(mapping):
    """Drops the plugin's claim on everything this mapping wrote."""
    return WrittenValue.objects.filter(Q(mapping=mapping)).delete()[0]
