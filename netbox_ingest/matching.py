"""
Matching API records to NetBox objects.

Both sides produce a key: the record from a JSON path, the object from a
field (any Django lookup path, e.g. "name" or "primary_ip4__address"). An
optional regex on either side narrows the key: its first group, or the whole
match when it has no group. Keys that don't match their regex are left out.
"""
import re
from functools import lru_cache

from django.core.exceptions import FieldError
from django.http import QueryDict

from netbox.registry import registry

from . import jsonpath


class FilterError(ValueError):
    pass


@lru_cache(maxsize=256)
def compile_regex(pattern):
    return re.compile(pattern)


def regex_error(pattern):
    if not pattern:
        return ''
    try:
        compile_regex(pattern)
    except re.error as exc:
        return f'Invalid regular expression: {exc}'
    return ''


def make_key(value, regex='', ignore_case=True):
    """A value as a matching key, or None when it has none."""
    if value is None or value is jsonpath.MISSING or isinstance(value, (dict, list)):
        return None
    text = str(value).strip()
    if regex:
        match = compile_regex(regex).search(text)
        if not match:
            return None
        text = match.group(1) if match.re.groups else match.group(0)
        if text is None:
            return None
    if not text:
        return None
    return text.casefold() if ignore_case else text


def object_key_error(model, field):
    """A message if field is not a usable lookup path on model."""
    try:
        model.objects.values_list(field)[:0].count()
    except (FieldError, ValueError) as exc:
        return f'Unknown field: {exc}'
    return ''


def filtered_queryset(model, params):
    """
    The model's objects narrowed by NetBox filter parameters, the same ones
    its list view and REST API accept (e.g. {"site": "hq", "status": ["active"]}).
    """
    queryset = model.objects.all()
    if not params:
        return queryset
    filterset_class = registry['filtersets'].get(f'{model._meta.app_label}.{model._meta.model_name}')
    if filterset_class is None:
        raise FilterError(f'{model._meta.verbose_name} has no NetBox filters; leave the filter empty.')
    data = QueryDict(mutable=True)
    for key, value in params.items():
        for item in value if isinstance(value, list) else [value]:
            data.appendlist(key, str(item))
    filterset = filterset_class(data, queryset=queryset)
    if not filterset.is_valid():
        errors = '; '.join(f'{k}: {" ".join(map(str, v))}' for k, v in filterset.errors.items())
        raise FilterError(f'Invalid filter: {errors}')
    unknown = set(params) - set(filterset.filters)
    if unknown:
        raise FilterError(f'Unknown filter: {", ".join(sorted(unknown))}')
    return filterset.qs


def filter_error(model, params):
    try:
        filtered_queryset(model, params).values('pk')[:0].count()
    except FilterError as exc:
        return str(exc)
    return ''


def object_keys(queryset, field, regex='', ignore_case=True):
    """{key: [pk, ...]} for the objects in queryset. Objects without a key are left out."""
    keys = {}
    for pk, value in queryset.values_list('pk', field).iterator(chunk_size=2000):
        key = make_key(value, regex, ignore_case)
        if key is not None:
            keys.setdefault(key, []).append(pk)
    return keys


def object_key_by_pk(queryset, field, regex='', ignore_case=True):
    """{pk: key or None} for every object in queryset."""
    return {
        pk: make_key(value, regex, ignore_case)
        for pk, value in queryset.values_list('pk', field).iterator(chunk_size=2000)
    }


def record_index(records, path, regex='', ignore_case=True):
    """
    ({key: record}, duplicate keys, records without a key). A key that more
    than one record has is ambiguous and is not matched at all.
    """
    index, duplicates, keyless = {}, set(), []
    for record in records:
        key = make_key(jsonpath.get(record, path), regex, ignore_case)
        if key is None:
            keyless.append(record)
        elif key in index or key in duplicates:
            duplicates.add(key)
            index.pop(key, None)
        else:
            index[key] = record
    return index, duplicates, keyless
