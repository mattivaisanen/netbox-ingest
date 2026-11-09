from django.shortcuts import get_object_or_404
from rest_framework import status
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.routers import APIRootView

from netbox.api.authentication import TokenPermissions
from netbox.api.viewsets import NetBoxModelViewSet, NetBoxReadOnlyModelViewSet

from .. import filtersets, jobs
from ..choices import RunStatusChoices, TriggerChoices
from ..models import Mapping, MappingField, Run, Source
from . import serializers


class IngestRootView(APIRootView):
    def get_view_name(self):
        return 'Ingest'


class ActionPermissions(TokenPermissions):
    """POST to an action of an existing object needs view permission here; the action checks the rest."""
    perms_map = {**TokenPermissions.perms_map, 'POST': ['%(app_label)s.view_%(model_name)s']}


def _check(request, source):
    from ..views import _check_run_permission
    from django.core.exceptions import PermissionDenied as DjangoDenied

    try:
        _check_run_permission(request.user, source)
    except DjangoDenied as exc:
        raise PermissionDenied(str(exc))


class SourceViewSet(NetBoxModelViewSet):
    """Sources. POST /sources/<id>/run/ with {"dry_run": true|false} queues a run."""
    queryset = Source.objects.all()
    serializer_class = serializers.SourceSerializer
    filterset_class = filtersets.SourceFilterSet

    def get_permissions(self):
        if self.action == 'run':
            return [ActionPermissions()]
        return super().get_permissions()

    @action(detail=True, methods=['post'])
    def run(self, request, pk):
        source = get_object_or_404(Source.objects.restrict(request.user, 'view'), pk=pk)
        _check(request, source)
        dry_run = request.data.get('dry_run', True)
        if not isinstance(dry_run, bool):
            raise ValidationError({'dry_run': 'true or false'})
        run = Run.objects.create(source=source, dry_run=dry_run, trigger=TriggerChoices.API, user=request.user)
        try:
            jobs.enqueue_run(run, request.user)
        except jobs.QueueError as exc:
            run.delete()
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        data = serializers.RunSerializer(run, context={'request': request}).data
        return Response(data, status=status.HTTP_202_ACCEPTED)


class MappingViewSet(NetBoxModelViewSet):
    queryset = Mapping.objects.select_related('source', 'object_type')
    serializer_class = serializers.MappingSerializer
    filterset_class = filtersets.MappingFilterSet


class MappingFieldViewSet(NetBoxModelViewSet):
    queryset = MappingField.objects.select_related('mapping')
    serializer_class = serializers.MappingFieldSerializer
    filterset_class = filtersets.MappingFieldFilterSet


class RunViewSet(NetBoxReadOnlyModelViewSet):
    """Runs. GET /runs/<id>/items/ lists the report; POST /runs/<id>/apply/ applies a dry run."""
    queryset = Run.objects.select_related('source', 'user', 'applied_by')
    serializer_class = serializers.RunSerializer
    filterset_class = filtersets.RunFilterSet

    def get_permissions(self):
        if self.action == 'apply':
            return [ActionPermissions()]
        return super().get_permissions()

    @action(detail=True, methods=['get'])
    def items(self, request, pk):
        run = self.get_object()
        queryset = filtersets.RunItemFilterSet(request.GET, queryset=run.items.select_related('object_type')).qs
        page = self.paginate_queryset(queryset)
        data = serializers.RunItemSerializer(page, many=True).data
        return self.get_paginated_response(data)

    @action(detail=True, methods=['post'])
    def apply(self, request, pk):
        run = get_object_or_404(Run.objects.restrict(request.user, 'view').select_related('source'), pk=pk)
        _check(request, run.source)
        if not run.can_apply:
            raise ValidationError({'detail': 'Only a finished dry run that was not applied yet can be applied.'})
        Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.APPLYING)
        try:
            jobs.enqueue_apply(run, request.user)
        except jobs.QueueError as exc:
            Run.objects.filter(pk=run.pk).update(status=RunStatusChoices.DRY_RUN)
            return Response({'detail': str(exc)}, status=status.HTTP_503_SERVICE_UNAVAILABLE)
        run.refresh_from_db()
        return Response(serializers.RunSerializer(run, context={'request': request}).data,
                        status=status.HTTP_202_ACCEPTED)
