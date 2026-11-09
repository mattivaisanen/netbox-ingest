"""
NetBox fields the plugin can write: reading, converting, validating and
writing values without Model.save().

Writes are bulk UPDATE statements, grouped by value, so thousands of objects
cost a handful of queries. They bypass save() on purpose: no change log
rows, no signals. Because save() and the form validation are skipped, every
value is converted and validated here first, the same way the model field
or custom field would.

Writable: a model's own editable fields (text, numbers, choices such as
status, booleans, dates, IP addresses, ...), foreign keys (by ID, or by a
related object's name or slug), and custom fields as "cf_<name>". Not
writable: the primary key, timestamps, many-to-many fields, generic
relations and NetBox internals.
"""
import datetime
import decimal
import json

from django.core.exceptions import FieldDoesNotExist, ValidationError
from django.contrib.contenttypes.fields import GenericForeignKey
from django.db import models
from django.db.models import F, Func, Value
from django.utils.translation import gettext as _

#: Never written, whatever the model.
BLOCKED = {
    'id', 'created', 'last_updated', 'custom_field_data', 'lft', 'rght', 'tree_id', 'level',
}


class TargetError(ValueError):
    pass


def jsonable(value):
    """A value as it is stored in WrittenValue and shown in reports."""
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        return [jsonable(v) for v in value]
    if isinstance(value, dict):
        return {str(k): jsonable(v) for k, v in value.items()}
    if isinstance(value, (datetime.date, datetime.datetime, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, models.Model):
        return value.pk
    return str(value)


def is_empty(value):
    return value in (None, '', [], {})


def same(a, b):
    """Equal as stored values (1 == '1' is not equal; 1.0 == 1 is)."""
    return json.dumps(jsonable(a), sort_keys=True) == json.dumps(jsonable(b), sort_keys=True) or \
        (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool)
         and not isinstance(b, bool) and a == b)


def _parse_bool(value):
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on', 'y', 't'):
        return True
    if text in ('0', 'false', 'no', 'off', 'n', 'f', ''):
        return False
    raise ValidationError(_('"{value}" is not true or false.').format(value=value))


class Target:
    """One writable field of a model."""
    #: 'field', 'fk' or 'cf'
    kind = None

    def __init__(self, model, name):
        self.model = model
        self.name = name

    #: Columns to read with values() for this target.
    columns = ()

    def read(self, row):
        raise NotImplementedError

    def convert(self, value):
        """The value to store, validated. Raises ValidationError."""
        raise NotImplementedError

    def update(self, queryset, value):
        """Write value into every object of queryset."""
        raise NotImplementedError


class ModelFieldTarget(Target):
    kind = 'field'

    def __init__(self, model, name, field):
        super().__init__(model, name)
        self.field = field
        self.columns = (field.attname,)
        self.label = str(field.verbose_name)

    def read(self, row):
        return row.get(self.field.attname)

    def convert(self, value):
        field = self.field
        if isinstance(field, models.BooleanField):
            value = None if value is None and field.null else _parse_bool(value)
        elif value == '' and field.null and not isinstance(field, (models.CharField, models.TextField)):
            value = None
        elif value is None and not field.null and isinstance(field, (models.CharField, models.TextField)):
            value = ''
        elif isinstance(field, (models.CharField, models.TextField)) and not isinstance(value, str) \
                and value is not None:
            value = str(value) if not isinstance(value, (dict, list)) else json.dumps(value)
        value = field.to_python(value)
        field.validate(value, None)
        field.run_validators(value)
        return value

    def update(self, queryset, value):
        return queryset.update(**{self.field.attname: value})


class ForeignKeyTarget(Target):
    """A foreign key, set by the related object's ID, name or slug."""
    kind = 'fk'

    def __init__(self, model, name, field):
        super().__init__(model, name)
        self.field = field
        self.related = field.related_model
        self.columns = (field.attname,)
        self.label = str(field.verbose_name)

    def read(self, row):
        return row.get(self.field.attname)

    def convert(self, value):
        if is_empty(value):
            if not self.field.null:
                raise ValidationError(_('{field} cannot be empty.').format(field=self.name))
            return None
        if isinstance(value, dict):
            value = value.get('id', value.get('name'))
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit()):
            if self.related.objects.filter(pk=int(value)).exists():
                return int(value)
        for lookup in ('name', 'slug'):
            try:
                self.related._meta.get_field(lookup)
            except FieldDoesNotExist:
                continue
            pks = list(self.related.objects.filter(**{f'{lookup}__iexact': str(value)}).values_list('pk', flat=True)[:2])
            if len(pks) == 1:
                return pks[0]
            if len(pks) > 1:
                raise ValidationError(_('More than one {model} is called "{value}".').format(
                    model=self.related._meta.verbose_name, value=value))
        raise ValidationError(_('No {model} "{value}".').format(model=self.related._meta.verbose_name, value=value))

    def update(self, queryset, value):
        return queryset.update(**{self.field.attname: value})


class JSONBSet(Func):
    """jsonb_set(custom_field_data, '{name}', value): changes one key only."""
    function = 'jsonb_set'
    output_field = models.JSONField()


class CustomFieldTarget(Target):
    kind = 'cf'
    columns = ('custom_field_data',)

    def __init__(self, model, name, custom_field):
        super().__init__(model, name)
        self.custom_field = custom_field
        self.label = custom_field.label or custom_field.name

    def read(self, row):
        return (row.get('custom_field_data') or {}).get(self.custom_field.name)

    def convert(self, value):
        from extras.choices import CustomFieldTypeChoices as T

        cf = self.custom_field
        if is_empty(value):
            value = None
        elif cf.type in (T.TYPE_TEXT, T.TYPE_LONGTEXT, T.TYPE_URL, T.TYPE_SELECT):
            value = value if isinstance(value, str) else (
                json.dumps(value) if isinstance(value, (dict, list)) else
                ('true' if value is True else 'false' if value is False else str(value)))
        elif cf.type == T.TYPE_INTEGER:
            try:
                value = int(decimal.Decimal(str(value)))
            except (decimal.InvalidOperation, ValueError):
                raise ValidationError(_('"{value}" is not a whole number.').format(value=value))
        elif cf.type == T.TYPE_DECIMAL:
            try:
                value = float(decimal.Decimal(str(value)))
            except (decimal.InvalidOperation, ValueError):
                raise ValidationError(_('"{value}" is not a number.').format(value=value))
        elif cf.type == T.TYPE_BOOLEAN:
            value = _parse_bool(value)
        elif cf.type in (T.TYPE_DATE, T.TYPE_DATETIME):
            value = str(value)
        elif cf.type == T.TYPE_MULTISELECT:
            value = value if isinstance(value, list) else [v.strip() for v in str(value).split(',') if v.strip()]
            value = [str(v) for v in value]
        elif cf.type == T.TYPE_OBJECT:
            try:
                value = int(value)
            except (TypeError, ValueError):
                raise ValidationError(_('Object custom fields take an object ID.'))
        elif cf.type == T.TYPE_MULTIOBJECT:
            items = value if isinstance(value, list) else str(value).split(',')
            try:
                value = [int(v) for v in items]
            except (TypeError, ValueError):
                raise ValidationError(_('Multiple object custom fields take a list of object IDs.'))
        cf.validate(value)
        if value is None and cf.required:
            raise ValidationError(_('{field} is required.').format(field=cf.name))
        return value

    def update(self, queryset, value):
        from django.contrib.postgres.fields import ArrayField

        return queryset.update(custom_field_data=JSONBSet(
            F('custom_field_data'),
            Value([self.custom_field.name], output_field=ArrayField(models.TextField())),
            # Sent as JSON text and cast, so None becomes JSON null (not SQL
            # NULL, which would make jsonb_set empty the whole column).
            Func(Value(json.dumps(value)), template='%(expressions)s::jsonb', output_field=models.JSONField()),
            Value(True),
        ))


def _gfk_parts(model):
    parts = set()
    for field in model._meta.private_fields:
        if isinstance(field, GenericForeignKey):
            parts.update((field.ct_field, field.fk_field))
    return parts


def _writable(model, field):
    return (
        field.concrete and field.editable and not field.primary_key and not field.auto_created
        and not field.many_to_many and field.name not in BLOCKED and not field.name.startswith('_')
        and field.name not in _gfk_parts(model)
    )


def resolve(model, name):
    """The Target for name on model. Raises TargetError with a readable message."""
    name = (name or '').strip()
    if name.startswith('cf_'):
        from extras.models import CustomField

        cf_name = name[3:]
        for custom_field in CustomField.objects.get_for_model(model):
            if custom_field.name == cf_name:
                return CustomFieldTarget(model, name, custom_field)
        raise TargetError(_('{model} has no custom field "{name}".').format(
            model=model._meta.verbose_name, name=cf_name))
    try:
        field = model._meta.get_field(name)
    except FieldDoesNotExist:
        raise TargetError(_('{model} has no field "{name}". Custom fields are written as cf_<name>.').format(
            model=model._meta.verbose_name, name=name))
    if not _writable(model, field):
        raise TargetError(_('The field "{name}" cannot be written by the plugin.').format(name=name))
    if isinstance(field, models.ForeignKey):
        return ForeignKeyTarget(model, name, field)
    return ModelFieldTarget(model, name, field)


def available(model):
    """[(name, label, kind)] of everything writable on model, for help texts."""
    from extras.models import CustomField

    out = []
    for field in model._meta.get_fields():
        if getattr(field, 'concrete', False) and _writable(model, field):
            kind = 'fk' if isinstance(field, models.ForeignKey) else 'field'
            out.append((field.name, str(field.verbose_name), kind))
    for custom_field in CustomField.objects.get_for_model(model):
        out.append((f'cf_{custom_field.name}', custom_field.label or custom_field.name, 'cf'))
    return out
