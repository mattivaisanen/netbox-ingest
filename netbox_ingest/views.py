import json
import time

from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.generic import View

from netbox.object_actions import AddObject, BulkDelete, DeleteObject, EditObject
from netbox.views import generic
from utilities.views import ConditionalLoginRequiredMixin, register_model_view

from . import engine, filtersets, forms, http, jobs, jsonpath, matching, tables
from .choices import FetchModeChoices, ItemStatusChoices, RunStatusChoices, TriggerChoices
from .models import Mapping, MappingField, Run, Source, WrittenValue
from .secrets import SecretError

APP = 'netbox_ingest'


def _check_run_permission(user, source):
    """A person running a source needs run_source and change on every model it writes."""
    if not user.has_perm(f'{APP}.run_source'):
        raise PermissionDenied(_('You may not run sources.'))
    for mapping in source.mappings.filter(enabled=True).select_related('object_type'):
        model = mapping.model
        if model is not None and not user.has_perm(f'{model._meta.app_label}.change_{model._meta.model_name}'):
            raise PermissionDenied(_('Running this source writes {model} objects, which you may not change.').format(
                model=model._meta.verbose_name))


#
# Sources
#

@register_model_view(Source, 'list', path='', detail=False)
class SourceListView(generic.ObjectListView):
    queryset = Source.objects.annotate(mapping_count=Count('mappings'))
    table = tables.SourceTable
    filterset = filtersets.SourceFilterSet
    filterset_form = forms.SourceFilterForm
    actions = (AddObject, BulkDelete)


@register_model_view(Source)
class SourceView(generic.ObjectView):
    queryset = Source.objects.all()
    template_name = 'netbox_ingest/source.html'
    actions = (EditObject, DeleteObject)

    def get_extra_context(self, request, instance):
        mappings = tables.MappingTable(
            instance.mappings.select_related('object_type').annotate(field_count=Count('fields')),
            exclude=('source',), orderable=False)
        mappings.configure(request)
        runs = tables.RunTable(instance.runs.select_related('user')[:10], exclude=('source',), orderable=False)
        runs.configure(request)
        try:
            next_run = jobs.next_run(instance) if instance.interval else None
        except Exception:  # noqa: BLE001 - the page works without it
            next_run = None
        return {
            'mappings_table': mappings,
            'runs_table': runs,
            'next_run': next_run,
            'can_run': request.user.has_perm(f'{APP}.run_source'),
        }


@register_model_view(Source, 'add', detail=False)
@register_model_view(Source, 'edit')
class SourceEditView(generic.ObjectEditView):
    queryset = Source.objects.all()
    form = forms.SourceForm


@register_model_view(Source, 'delete')
class SourceDeleteView(generic.ObjectDeleteView):
    queryset = Source.objects.all()


@register_model_view(Source, 'bulk_delete', path='delete', detail=False)
class SourceBulkDeleteView(generic.BulkDeleteView):
    queryset = Source.objects.all()
    filterset = filtersets.SourceFilterSet
    table = tables.SourceTable


@register_model_view(Source, 'run')
class SourceRunView(ConditionalLoginRequiredMixin, View):
    """Queues a run (dry by default) and opens it."""

    def post(self, request, pk):
        source = get_object_or_404(Source.objects.restrict(request.user, 'view'), pk=pk)
        _check_run_permission(request.user, source)
        dry_run = request.POST.get('dry_run', 'true') != 'false'
        run = Run.objects.create(source=source, dry_run=dry_run, trigger=TriggerChoices.MANUAL, user=request.user)
        try:
            jobs.enqueue_run(run, request.user)
        except jobs.QueueError as exc:
            run.delete()
            messages.error(request, str(exc))
            return redirect(source.get_absolute_url())
        messages.info(request, _('Dry run queued.') if dry_run else _('Run queued.'))
        return redirect(run.get_absolute_url())


@register_model_view(Source, 'test')
class SourceTestView(ConditionalLoginRequiredMixin, View):
    """
    Calls the API once (first page only, nothing written) and shows the
    response, so paths can be written against a real record.
    """
    template_name = 'netbox_ingest/source_test.html'

    def get(self, request, pk):
        source = get_object_or_404(Source.objects.restrict(request.user, 'view'), pk=pk)
        if not request.user.has_perm(f'{APP}.run_source'):
            raise PermissionDenied
        key = request.GET.get('key', '').strip()
        context = {'object': source, 'key': key, 'tab': None}
        per_object = source.fetch_mode == FetchModeChoices.PER_OBJECT
        if per_object and not key and http.uses_placeholder(source.url + source.body, 'key'):
            return render(request, self.template_name, context)
        context.update(self.call(source, key))
        return render(request, self.template_name, context)

    @staticmethod
    def call(source, key):
        out = {}
        client = None
        try:
            client = http.Client(source)
            values = {'key': key, 'pk': ''}
            if source.lookup_url:
                pairs = engine.fetch_lookup(source, client, engine.Report(Run(pk=0)))
                out['lookup_count'] = len(pairs)
                mapping = source.mappings.filter(enabled=True).first() or Mapping(ignore_case=True)
                found = engine.lookup_index(pairs, mapping).get(
                    matching.make_key(key, ignore_case=mapping.ignore_case))
                values['lookup'] = found
                out['lookup_value'] = found
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
            records = client.records_of(data, source.records_path)
            out['record_count'] = len(records)
            sample = records[0] if records else data
            out['sample'] = json.dumps(sample, indent=2, default=str)[:20000]
            out['paths'] = jsonpath.paths(sample)
            if source.pagination != 'none':
                out['paging_note'] = _('Only the first page was read.')
        except (http.FetchError, SecretError) as exc:
            out['error'] = str(exc)
        finally:
            if client is not None:
                client.close()
        return out


#
# Mappings
#

@register_model_view(Mapping, 'list', path='', detail=False)
class MappingListView(generic.ObjectListView):
    queryset = Mapping.objects.select_related('source', 'object_type').annotate(field_count=Count('fields'))
    table = tables.MappingTable
    filterset = filtersets.MappingFilterSet
    filterset_form = forms.MappingFilterForm
    actions = (AddObject, BulkDelete)


@register_model_view(Mapping)
class MappingView(generic.ObjectView):
    queryset = Mapping.objects.select_related('source', 'object_type')
    template_name = 'netbox_ingest/mapping.html'
    actions = (EditObject, DeleteObject)

    def get_extra_context(self, request, instance):
        fields = tables.MappingFieldTable(instance.fields.all(), exclude=('mapping',), orderable=False)
        fields.configure(request)
        model = instance.model
        return {
            'fields_table': fields,
            'owned_count': WrittenValue.objects.filter(mapping=instance).count(),
            'object_count': _object_count(instance),
            'list_url': _list_url(model, instance.object_filter),
        }


def _object_count(mapping):
    from .matching import FilterError, filtered_queryset

    if mapping.model is None:
        return None
    try:
        return filtered_queryset(mapping.model, mapping.object_filter).count()
    except FilterError:
        return None


def _list_url(model, params):
    """The model's NetBox list, filtered like the mapping, for a quick look."""
    from urllib.parse import urlencode

    from django.urls import NoReverseMatch, reverse
    from utilities.views import get_viewname

    if model is None:
        return None
    try:
        url = reverse(get_viewname(model, 'list'))
    except NoReverseMatch:
        return None
    return f'{url}?{urlencode(params or {}, doseq=True)}' if params else url


@register_model_view(Mapping, 'add', detail=False)
@register_model_view(Mapping, 'edit')
class MappingEditView(generic.ObjectEditView):
    queryset = Mapping.objects.all()
    form = forms.MappingForm


@register_model_view(Mapping, 'delete')
class MappingDeleteView(generic.ObjectDeleteView):
    queryset = Mapping.objects.all()


@register_model_view(Mapping, 'bulk_delete', path='delete', detail=False)
class MappingBulkDeleteView(generic.BulkDeleteView):
    queryset = Mapping.objects.all()
    filterset = filtersets.MappingFilterSet
    table = tables.MappingTable


@register_model_view(Mapping, 'forget')
class MappingForgetView(ConditionalLoginRequiredMixin, View):
    """Drops the plugin's claim on the values this mapping wrote."""

    def post(self, request, pk):
        mapping = get_object_or_404(Mapping.objects.restrict(request.user, 'change'), pk=pk)
        count = engine.forget(mapping)
        messages.success(request, _(
            'Forgot {n} written values. The plugin treats them as set by someone else from now on.'
        ).format(n=count))
        return redirect(mapping.get_absolute_url())


#
# Fields
#

@register_model_view(MappingField, 'list', path='', detail=False)
class MappingFieldListView(generic.ObjectListView):
    queryset = MappingField.objects.select_related('mapping')
    table = tables.MappingFieldTable
    filterset = filtersets.MappingFieldFilterSet
    actions = (BulkDelete,)


@register_model_view(MappingField)
class MappingFieldView(generic.ObjectView):
    queryset = MappingField.objects.select_related('mapping', 'mapping__source')
    template_name = 'netbox_ingest/mappingfield.html'
    actions = (EditObject, DeleteObject)

    def get_extra_context(self, request, instance):
        return {'owned_count': WrittenValue.objects.filter(
            mapping=instance.mapping, target=instance.target).count()}


@register_model_view(MappingField, 'add', detail=False)
@register_model_view(MappingField, 'edit')
class MappingFieldEditView(generic.ObjectEditView):
    queryset = MappingField.objects.all()
    form = forms.MappingFieldForm

    def get_return_url(self, request, obj=None):
        if obj is not None and obj.pk and obj.mapping_id:
            return obj.mapping.get_absolute_url()
        return super().get_return_url(request, obj)


@register_model_view(MappingField, 'delete')
class MappingFieldDeleteView(generic.ObjectDeleteView):
    queryset = MappingField.objects.all()

    def get_return_url(self, request, obj=None):
        if obj is not None and obj.mapping_id:
            return obj.mapping.get_absolute_url()
        return super().get_return_url(request, obj)


@register_model_view(MappingField, 'bulk_delete', path='delete', detail=False)
class MappingFieldBulkDeleteView(generic.BulkDeleteView):
    queryset = MappingField.objects.all()
    filterset = filtersets.MappingFieldFilterSet
    table = tables.MappingFieldTable


#
# Runs
#

@register_model_view(Run, 'list', path='', detail=False)
class RunListView(generic.ObjectListView):
    queryset = Run.objects.select_related('source', 'user', 'applied_by')
    table = tables.RunTable
    filterset = filtersets.RunFilterSet
    filterset_form = forms.RunFilterForm
    actions = (BulkDelete,)


#: Order and labels of the result chips on a run's page.
SUMMARY = (
    ItemStatusChoices.PLANNED, ItemStatusChoices.WRITTEN, ItemStatusChoices.EDITED, ItemStatusChoices.FOREIGN,
    ItemStatusChoices.STALE, ItemStatusChoices.INVALID, ItemStatusChoices.FAILED, ItemStatusChoices.AMBIGUOUS,
    ItemStatusChoices.NO_VALUE, ItemStatusChoices.NO_DATA, ItemStatusChoices.UNMATCHED,
)


@register_model_view(Run)
class RunView(generic.ObjectView):
    queryset = Run.objects.select_related('source', 'user', 'applied_by')
    template_name = 'netbox_ingest/run.html'
    actions = (DeleteObject,)

    def get_extra_context(self, request, instance):
        items = instance.items.select_related('mapping', 'object_type')
        filterset = filtersets.RunItemFilterSet(request.GET, queryset=items)
        queryset = filterset.qs if filterset.is_valid() else items
        table = tables.RunItemTable(queryset)
        table.configure(request)
        labels = dict(ItemStatusChoices.CHOICES[i][:2] for i in range(len(ItemStatusChoices.CHOICES)))
        colors = ItemStatusChoices.colors
        counts = instance.counts or {}
        return {
            'items_table': table,
            'chips': [(s, labels[s], colors.get(s), counts[s]) for s in SUMMARY if counts.get(s)],
            'unchanged': counts.get('unchanged', 0),
            'matched': counts.get('matched', 0),
            'current_status': request.GET.getlist('status'),
            'stored': instance.items.count(),
            'can_apply': instance.can_apply and request.user.has_perm(f'{APP}.run_source'),
            'active': instance.status in RunStatusChoices.ACTIVE,
        }


@register_model_view(Run, 'delete')
class RunDeleteView(generic.ObjectDeleteView):
    queryset = Run.objects.all()


@register_model_view(Run, 'bulk_delete', path='delete', detail=False)
class RunBulkDeleteView(generic.BulkDeleteView):
    queryset = Run.objects.all()
    filterset = filtersets.RunFilterSet
    table = tables.RunTable


@register_model_view(Run, 'apply')
class RunApplyView(ConditionalLoginRequiredMixin, View):
    """Writes a dry run's planned values (in the background)."""

    def post(self, request, pk):
        run = get_object_or_404(Run.objects.restrict(request.user, 'view').select_related('source'), pk=pk)
        _check_run_permission(request.user, run.source)
        if not run.can_apply:
            messages.error(request, _('Only a finished dry run that was not applied yet can be applied.'))
            return redirect(run.get_absolute_url())
        # Marked first, so a quick job can't finish before the mark.
        Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.APPLYING)
        try:
            jobs.enqueue_apply(run, request.user)
        except jobs.QueueError as exc:
            Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.DRY_RUN)
            messages.error(request, str(exc))
        else:
            messages.info(request, _('Applying in the background; reload to see the result.'))
        return redirect(run.get_absolute_url())
