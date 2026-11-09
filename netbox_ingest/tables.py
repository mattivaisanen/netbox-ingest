import json

import django_tables2 as tables
from django.utils.html import format_html
from django.utils.safestring import mark_safe
from django.utils.translation import gettext_lazy as _

from netbox.tables import NetBoxTable, columns

from .models import Mapping, MappingField, Run, RunItem, Source


class SourceTable(NetBoxTable):
    name = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    fetch_mode = columns.ChoiceFieldColumn()
    auth_type = columns.ChoiceFieldColumn()
    url = tables.Column(verbose_name=_('URL'))
    mappings = tables.Column(accessor='mapping_count', verbose_name=_('Mappings'), orderable=False)
    interval = tables.Column(verbose_name=_('Every (min)'))
    actions = columns.ActionsColumn(actions=('edit', 'delete'))

    class Meta(NetBoxTable.Meta):
        model = Source
        fields = ('pk', 'id', 'name', 'enabled', 'fetch_mode', 'url', 'auth_type', 'pagination', 'mappings',
                  'interval', 'description', 'created', 'last_updated')
        default_columns = ('pk', 'name', 'enabled', 'fetch_mode', 'url', 'mappings', 'interval')


class MappingTable(NetBoxTable):
    name = tables.Column(linkify=True)
    source = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    object_type = columns.ContentTypeColumn(verbose_name=_('Object type'))
    fields = tables.Column(accessor='field_count', verbose_name=_('Fields'), orderable=False)
    actions = columns.ActionsColumn(actions=('edit', 'delete'))

    class Meta(NetBoxTable.Meta):
        model = Mapping
        fields = ('pk', 'id', 'name', 'source', 'enabled', 'weight', 'object_type', 'object_key_field',
                  'record_key_path', 'fields', 'description', 'created', 'last_updated')
        default_columns = ('pk', 'name', 'source', 'enabled', 'object_type', 'object_key_field', 'record_key_path',
                           'fields')


class MappingFieldTable(NetBoxTable):
    target = tables.Column(linkify=True, verbose_name=_('NetBox field'))
    mapping = tables.Column(linkify=True)
    enabled = columns.BooleanColumn()
    transform = columns.ChoiceFieldColumn()
    overwrite_foreign = columns.BooleanColumn(verbose_name=_('Overwrites'))
    value_path = tables.TemplateColumn('<code>{{ value }}</code>')
    actions = columns.ActionsColumn(actions=('edit', 'delete'))

    class Meta(NetBoxTable.Meta):
        model = MappingField
        fields = ('pk', 'id', 'target', 'mapping', 'value_path', 'transform', 'expression', 'enabled',
                  'overwrite_foreign')
        default_columns = ('pk', 'target', 'value_path', 'transform', 'enabled', 'overwrite_foreign')


def _counts(record, statuses):
    return ', '.join(f'{record.counts.get(s, 0)} {label}' for s, label in statuses if record.counts.get(s))


class RunTable(NetBoxTable):
    id = tables.Column(linkify=True, verbose_name=_('Run'))
    source = tables.Column(linkify=True)
    status = columns.ChoiceFieldColumn()
    dry_run = columns.BooleanColumn()
    trigger = columns.ChoiceFieldColumn()
    user = tables.Column()
    summary = tables.Column(empty_values=(), orderable=False, verbose_name=_('Result'))
    actions = columns.ActionsColumn(actions=('delete',))

    class Meta(NetBoxTable.Meta):
        model = Run
        fields = ('pk', 'id', 'source', 'status', 'dry_run', 'trigger', 'user', 'created', 'started', 'finished',
                  'applied', 'applied_by', 'summary', 'error')
        default_columns = ('pk', 'id', 'source', 'status', 'dry_run', 'trigger', 'user', 'created', 'summary')

    def render_summary(self, record):
        text = _counts(record, (
            ('written', _('written')), ('planned', _('to write')), ('unchanged', _('unchanged')),
            ('edited', _('edited')), ('foreign', _('not ours')), ('invalid', _('invalid')),
            ('failed', _('failed')),
        ))
        return text or (record.error[:80] if record.error else '—')


EMPTY = mark_safe('<span class="text-muted">—</span>')


def _json(value):
    if value is None:
        return EMPTY
    text = value if isinstance(value, str) else json.dumps(value)
    return format_html('<code>{}</code>', text if len(text) <= 120 else text[:117] + '…')


class RunItemTable(NetBoxTable):
    status = columns.ChoiceFieldColumn(verbose_name=_('Result'))
    object = tables.Column(empty_values=(), orderable=False, verbose_name=_('Object'))
    mapping = tables.Column(linkify=True)
    key = tables.Column(verbose_name=_('Key'))
    target = tables.Column(verbose_name=_('Field'))
    old = tables.Column(verbose_name=_('Current'), orderable=False)
    new = tables.Column(verbose_name=_('From API'), orderable=False)
    message = tables.Column(verbose_name=_('Note'))
    actions = columns.ActionsColumn(actions=())

    class Meta(NetBoxTable.Meta):
        model = RunItem
        fields = ('pk', 'id', 'status', 'object', 'mapping', 'key', 'target', 'old', 'new', 'message')
        default_columns = ('status', 'object', 'key', 'target', 'old', 'new', 'message')

    def render_object(self, record):
        if record.object_id is None:
            return EMPTY
        model = record.object_type.model_class() if record.object_type_id else None
        name = record.object_repr or f'#{record.object_id}'
        if model is not None and hasattr(model, 'get_absolute_url'):
            try:
                from django.urls import reverse
                from utilities.views import get_viewname

                url = reverse(get_viewname(model), args=[record.object_id])
                return format_html('<a href="{}">{}</a>', url, name)
            except Exception:  # noqa: BLE001 - a model without a detail view
                pass
        return name

    def render_old(self, value):
        return _json(value)

    def render_new(self, value):
        return _json(value)
