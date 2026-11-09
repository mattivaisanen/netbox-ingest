from django import forms
from django.contrib.contenttypes.models import ContentType
from django.utils.translation import gettext_lazy as _

from utilities.forms import FilterForm
from utilities.forms.fields import DynamicModelChoiceField, JSONField
from utilities.forms.rendering import FieldSet

from . import parsers
from .choices import ItemStatusChoices, RunStatusChoices, TriggerChoices
from .models import Mapping, MappingField, Source
from .secrets import encrypt


class SourceForm(forms.ModelForm):
    secret = forms.CharField(
        label=_('Secret'), required=False, strip=False,
        widget=forms.PasswordInput(attrs={'autocomplete': 'new-password'}, render_value=False),
        help_text=_('Password, token, API key or client secret. Stored encrypted and never shown again; '
                    'leave empty to keep the current one.'),
    )
    clear_secret = forms.BooleanField(label=_('Remove the stored secret'), required=False)
    format = forms.ChoiceField(label=_('Format'), choices=parsers.choices, initial='json')
    headers = JSONField(label=_('Headers'), required=False)

    fieldsets = (
        FieldSet('name', 'description', 'enabled', name=_('Source')),
        FieldSet('fetch_mode', 'method', 'url', 'body', 'headers', 'format', 'records_path', 'verify_tls',
                 'timeout', name=_('Request')),
        FieldSet('auth_type', 'auth_username', 'auth_name', 'secret', 'secret_env', 'clear_secret', 'token_url',
                 'oauth_scope', name=_('Authentication')),
        FieldSet('pagination', 'page_size', 'next_path', 'page_param', 'size_param', name=_('Pagination')),
        FieldSet('lookup_url', 'lookup_records_path', 'lookup_key_path', 'lookup_value_path',
                 name=_('Lookup (per object, optional)')),
        FieldSet('interval', 'requests_per_second', 'concurrency', name=_('Schedule and limits')),
    )

    class Meta:
        model = Source
        fields = (
            'name', 'description', 'enabled', 'fetch_mode', 'method', 'url', 'body', 'headers', 'format',
            'records_path', 'verify_tls', 'timeout', 'auth_type', 'auth_username', 'auth_name', 'secret_env',
            'token_url', 'oauth_scope', 'pagination', 'page_size', 'next_path', 'page_param', 'size_param',
            'lookup_url', 'lookup_records_path', 'lookup_key_path', 'lookup_value_path', 'interval',
            'requests_per_second', 'concurrency',
        )
        widgets = {
            'body': forms.Textarea(attrs={'rows': 4, 'class': 'font-monospace'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        if self.instance.pk and self.instance.secret_ciphertext:
            self.fields['secret'].help_text = _('A secret is stored. Leave empty to keep it.')
        else:
            del self.fields['clear_secret']
        # Show what applies when a limit is left empty.
        from .settings import get_setting

        self.fields['requests_per_second'].help_text = _('Empty: {n} (the administrator\'s limit).').format(
            n=get_setting('max_requests_per_second'))
        self.fields['concurrency'].help_text = _('Per object only. Empty: {n} (the administrator\'s limit).').format(
            n=get_setting('max_concurrency'))

    def clean_headers(self):
        return self.cleaned_data.get('headers') or {}

    def save(self, commit=True):
        source = super().save(commit=False)
        if self.cleaned_data.get('secret'):
            source.secret_ciphertext = encrypt(self.cleaned_data['secret'])
        elif self.cleaned_data.get('clear_secret'):
            source.secret_ciphertext = ''
        if commit:
            source.save()
        return source


def _object_type_queryset():
    """Models users can map to: NetBox's and plugins' models with a list view."""
    from core.models import ObjectType

    return ObjectType.objects.public().order_by('app_label', 'model')


class MappingForm(forms.ModelForm):
    source = DynamicModelChoiceField(label=_('Source'), queryset=Source.objects.all())
    object_type = forms.ModelChoiceField(
        label=_('Object type'), queryset=ContentType.objects.none(),
        help_text=_('Every object this mapping writes is of this type.'),
    )
    object_filter = JSONField(label=_('Object filter'), required=False)

    fieldsets = (
        FieldSet('source', 'name', 'description', 'enabled', 'weight', name=_('Mapping')),
        FieldSet('object_type', 'object_filter', name=_('Objects')),
        FieldSet('object_key_field', 'object_key_regex', 'record_key_path', 'record_key_regex', 'ignore_case',
                 name=_('Matching')),
    )

    class Meta:
        model = Mapping
        fields = (
            'source', 'name', 'description', 'enabled', 'weight', 'object_type', 'object_filter',
            'object_key_field', 'object_key_regex', 'record_key_path', 'record_key_regex', 'ignore_case',
        )

    def __init__(self, *args, user=None, **kwargs):
        super().__init__(*args, **kwargs)
        self.user = user
        self.fields['object_type'].queryset = _object_type_queryset()
        self.fields['object_type'].label_from_instance = \
            lambda ot: f'{ot.app_label} | {ot.model_class()._meta.verbose_name if ot.model_class() else ot.model}'

    def clean_object_filter(self):
        return self.cleaned_data.get('object_filter') or {}


class MappingFieldForm(forms.ModelForm):
    mapping = DynamicModelChoiceField(label=_('Mapping'), queryset=Mapping.objects.all())
    table = JSONField(label=_('Lookup table'), required=False)

    fieldsets = (
        FieldSet('mapping', 'target', 'value_path', 'enabled', name=_('Field')),
        FieldSet('transform', 'expression', 'replacement', 'table', name=_('Transform')),
        FieldSet('overwrite_foreign', name=_('Ownership')),
    )

    class Meta:
        model = MappingField
        fields = ('mapping', 'target', 'value_path', 'enabled', 'transform', 'expression', 'replacement', 'table',
                  'overwrite_foreign')
        widgets = {
            'expression': forms.Textarea(attrs={'rows': 3, 'class': 'font-monospace'}),
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        mapping = self.instance.mapping if self.instance.mapping_id else None
        if mapping is None and self.initial.get('mapping'):
            mapping = Mapping.objects.filter(pk=self.initial['mapping']).first()
        if mapping is not None and mapping.model is not None:
            from . import targets

            names = ', '.join(name for name, _label, _kind in targets.available(mapping.model))
            self.fields['target'].help_text = _('Writable on {model}: {names}.').format(
                model=mapping.model._meta.verbose_name, names=names)

    def clean_table(self):
        return self.cleaned_data.get('table') or {}


class SourceFilterForm(FilterForm):
    enabled = forms.NullBooleanField(required=False, label=_('Enabled'), widget=forms.Select(
        choices=((None, '---------'), (True, _('Yes')), (False, _('No')))))
    fieldsets = (FieldSet('q', 'enabled'),)


class MappingFilterForm(FilterForm):
    source_id = DynamicModelChoiceField(queryset=Source.objects.all(), required=False, label=_('Source'))
    fieldsets = (FieldSet('q', 'source_id'),)


class RunFilterForm(FilterForm):
    source_id = DynamicModelChoiceField(queryset=Source.objects.all(), required=False, label=_('Source'))
    status = forms.MultipleChoiceField(choices=RunStatusChoices, required=False, label=_('Status'))
    trigger = forms.MultipleChoiceField(choices=TriggerChoices, required=False, label=_('Trigger'))
    dry_run = forms.NullBooleanField(required=False, label=_('Dry run'), widget=forms.Select(
        choices=((None, '---------'), (True, _('Yes')), (False, _('No')))))
    fieldsets = (FieldSet('q', 'source_id', 'status', 'trigger', 'dry_run'),)


class RunItemFilterForm(FilterForm):
    status = forms.MultipleChoiceField(choices=ItemStatusChoices, required=False, label=_('Result'))
    mapping_id = forms.ModelMultipleChoiceField(queryset=Mapping.objects.all(), required=False, label=_('Mapping'))
    target = forms.CharField(required=False, label=_('Field'))
    fieldsets = (FieldSet('q', 'status', 'mapping_id', 'target'),)
