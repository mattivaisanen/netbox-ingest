import django_filters
from django.db.models import Q
from django.utils.translation import gettext as _

from netbox.filtersets import BaseFilterSet

from .choices import FetchModeChoices, ItemStatusChoices, RunStatusChoices, TriggerChoices
from .models import Mapping, MappingField, Run, RunItem, Source


class _SearchMixin(django_filters.FilterSet):
    q = django_filters.CharFilter(method='search', label=_('Search'))
    search_fields = ()

    def search(self, queryset, name, value):
        value = value.strip()
        if not value:
            return queryset
        query = Q()
        for field in self.search_fields:
            query |= Q(**{f'{field}__icontains': value})
        return queryset.filter(query)


class SourceFilterSet(_SearchMixin, BaseFilterSet):
    search_fields = ('name', 'description', 'url')
    fetch_mode = django_filters.MultipleChoiceFilter(choices=FetchModeChoices)

    class Meta:
        model = Source
        fields = ('id', 'name', 'enabled', 'auth_type', 'pagination')


class MappingFilterSet(_SearchMixin, BaseFilterSet):
    search_fields = ('name', 'description', 'source__name')
    source_id = django_filters.ModelMultipleChoiceFilter(queryset=Source.objects.all(), label=_('Source (ID)'))

    class Meta:
        model = Mapping
        fields = ('id', 'name', 'enabled', 'object_type')


class MappingFieldFilterSet(_SearchMixin, BaseFilterSet):
    search_fields = ('target', 'value_path')
    mapping_id = django_filters.ModelMultipleChoiceFilter(queryset=Mapping.objects.all(), label=_('Mapping (ID)'))

    class Meta:
        model = MappingField
        fields = ('id', 'target', 'enabled', 'transform')


class RunFilterSet(_SearchMixin, BaseFilterSet):
    search_fields = ('source__name', 'error')
    source_id = django_filters.ModelMultipleChoiceFilter(queryset=Source.objects.all(), label=_('Source (ID)'))
    status = django_filters.MultipleChoiceFilter(choices=RunStatusChoices)
    trigger = django_filters.MultipleChoiceFilter(choices=TriggerChoices)

    class Meta:
        model = Run
        fields = ('id', 'dry_run')


class RunItemFilterSet(_SearchMixin, BaseFilterSet):
    search_fields = ('object_repr', 'key', 'target', 'message')
    run_id = django_filters.ModelMultipleChoiceFilter(queryset=Run.objects.all(), label=_('Run (ID)'))
    mapping_id = django_filters.ModelMultipleChoiceFilter(queryset=Mapping.objects.all(), label=_('Mapping (ID)'))
    status = django_filters.MultipleChoiceFilter(choices=ItemStatusChoices)

    class Meta:
        model = RunItem
        fields = ('id', 'target', 'object_id')
