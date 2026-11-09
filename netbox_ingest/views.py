from django.contrib import messages
from django.core.exceptions import PermissionDenied
from django.db.models import Count
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.views.generic import View

from netbox.object_actions import AddObject, BulkDelete, DeleteObject, EditObject
from netbox.views import generic
from utilities.views import ConditionalLoginRequiredMixin, register_model_view

from . import engine, filtersets, forms, jobs, livetest, tables
from .choices import ItemStatusChoices, RunStatusChoices, TriggerChoices
from .models import Mapping, MappingField, Run, Source, WrittenValue

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
    # Adds the live test panel below the form.
    template_name = 'netbox_ingest/source_edit.html'


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
    Calls the saved source's API once (first page only, nothing written) and
    shows the response, so paths can be written against a real record. The
    edit form has the same test for unsaved values (LiveTestView).
    """
    template_name = 'netbox_ingest/source_test.html'

    def get(self, request, pk):
        source = get_object_or_404(Source.objects.restrict(request.user, 'view'), pk=pk)
        if not request.user.has_perm(f'{APP}.run_source'):
            raise PermissionDenied
        key = request.GET.get('key', '').strip()
        context = {'object': source, 'key': key, 'tab': None}
        if not (livetest.needs_key(source) and not key):
            context['result'] = livetest.endpoint(source, key)
        return render(request, self.template_name, context)


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
    template_name = 'netbox_ingest/mapping_edit.html'


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
    template_name = 'netbox_ingest/mappingfield_edit.html'

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


#
# Live tests in the edit forms
#

class LiveTestView(ConditionalLoginRequiredMixin, View):
    """
    Runs a live test on the values in an edit form, saved or not, and
    returns the result as an HTML fragment for the form's test panel.

    The form's own POST data is bound to the form class, so the test sees
    exactly what Save would store. Only the fields a test needs must be
    valid: an error elsewhere (a duplicate name, say) does not stop it.
    Nothing is saved.
    """
    model = None
    form_class = None
    #: Form fields whose errors stop the test.
    needed = ()
    template_name = 'netbox_ingest/livetest/result.html'

    def dispatch(self, request, *args, **kwargs):
        # The answer goes into the form's panel, so a refusal is shown there too.
        try:
            return super().dispatch(request, *args, **kwargs)
        except PermissionDenied as exc:
            return self.respond(request, errors=[(_('Permission'), str(exc) or _('You may not run this test.'))])

    def bind(self, request):
        """(instance, form) from the posted form values; checks permissions."""
        meta = self.model._meta
        if not request.user.has_perm(f'{APP}.run_source'):
            raise PermissionDenied(_('Live tests call the API, which needs permission to run sources.'))
        pk = request.POST.get('_test_pk')
        if pk:
            instance = get_object_or_404(self.model.objects.restrict(request.user, 'change'), pk=pk)
        elif request.user.has_perm(f'{APP}.add_{meta.model_name}'):
            instance = self.model()
        else:
            raise PermissionDenied
        form = self.get_form(request, instance)
        form.is_valid()
        return instance, form

    def get_form(self, request, instance):
        return self.form_class(data=request.POST, instance=instance)

    def form_errors(self, form):
        """[(label, message)] for the errors that stop the test."""
        errors = []
        for name, messages_ in form.errors.items():
            if name in self.needed:
                label = form.fields[name].label if name in form.fields else name
                errors.extend((label, message) for message in messages_)
        return errors

    def respond(self, request, result=None, errors=None):
        return render(request, self.template_name, {'result': result or {}, 'errors': errors or []})

    def object_test(self, request, mapping, fields):
        obj, error = livetest.find_object(mapping, request.POST.get('_test_object', ''), request.user)
        if obj is None:
            return self.respond(request, {'test': 'object', 'error': error})
        return self.respond(request, livetest.object_preview(mapping, fields, obj))


class SourceLiveTestView(LiveTestView):
    model = Source
    form_class = forms.SourceForm
    needed = (
        'fetch_mode', 'method', 'url', 'body', 'headers', 'format', 'records_path', 'verify_tls', 'timeout',
        'auth_type', 'auth_username', 'auth_name', 'secret', 'secret_env', 'token_url', 'oauth_scope',
        'pagination', 'page_size', 'next_path', 'page_param', 'size_param', 'lookup_url', 'lookup_records_path',
        'lookup_key_path', 'lookup_value_path',
    )

    def post(self, request):
        source, form = self.bind(request)
        errors = self.form_errors(form)
        if errors:
            return self.respond(request, errors=errors)
        # A secret typed into the form is tested before it is stored. An
        # environment variable wins over it, as it does when the source runs.
        secret = None
        if not source.secret_env:
            if form.cleaned_data.get('secret'):
                secret = form.cleaned_data['secret']
            elif form.cleaned_data.get('clear_secret'):
                secret = ''
        key = request.POST.get('_test_key', '').strip()
        if request.POST.get('_test') == 'auth':
            return self.respond(request, livetest.authentication(source, key, secret))
        return self.respond(request, livetest.endpoint(source, key, secret))


class MappingLiveTestView(LiveTestView):
    model = Mapping
    form_class = forms.MappingForm
    needed = ('source', 'object_type', 'object_filter', 'object_key_field', 'object_key_regex', 'record_key_path',
              'record_key_regex', 'ignore_case')

    def get_form(self, request, instance):
        return self.form_class(data=request.POST, instance=instance, user=request.user)

    def post(self, request):
        mapping, form = self.bind(request)
        errors = self.form_errors(form)
        if errors:
            return self.respond(request, errors=errors)
        if not request.user.has_perm(f'{APP}.view_source') or \
                not Source.objects.restrict(request.user, 'view').filter(pk=mapping.source_id).exists():
            raise PermissionDenied
        # The mapping's saved fields; a new mapping has none yet.
        fields = list(mapping.fields.filter(enabled=True)) if mapping.pk else []
        return self.object_test(request, mapping, fields)


class MappingFieldLiveTestView(LiveTestView):
    model = MappingField
    form_class = forms.MappingFieldForm
    needed = ('mapping', 'target', 'value_path', 'transform', 'expression', 'replacement', 'table')

    def post(self, request):
        field, form = self.bind(request)
        errors = self.form_errors(form)
        if errors:
            return self.respond(request, errors=errors)
        mapping = field.mapping
        if not Mapping.objects.restrict(request.user, 'view').filter(pk=mapping.pk).exists():
            raise PermissionDenied
        return self.object_test(request, mapping, [field])
