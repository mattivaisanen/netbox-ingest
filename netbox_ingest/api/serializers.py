from django.contrib.contenttypes.models import ContentType
from rest_framework import serializers

from netbox.api.fields import ChoiceField, ContentTypeField
from netbox.api.serializers import ValidatedModelSerializer

from ..choices import ItemStatusChoices, RunStatusChoices, TriggerChoices
from ..models import Mapping, MappingField, Run, RunItem, Source
from ..secrets import encrypt


class SourceSerializer(ValidatedModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ingest-api:source-detail')
    #: Write only: the secret is never returned.
    secret = serializers.CharField(write_only=True, required=False, allow_blank=True, trim_whitespace=False)
    has_secret = serializers.BooleanField(read_only=True)
    # The model's "url" (the API it calls) is "endpoint" here, since "url"
    # is NetBox's link to this object.
    endpoint = serializers.CharField(source='url', max_length=2000)

    class Meta:
        model = Source
        fields = (
            'id', 'url', 'display', 'name', 'description', 'enabled', 'fetch_mode', 'endpoint', 'method', 'body',
            'headers', 'format', 'records_path', 'verify_tls', 'timeout', 'auth_type', 'auth_username',
            'auth_name', 'token_url', 'oauth_scope', 'secret', 'secret_env', 'has_secret', 'pagination',
            'page_size', 'next_path', 'page_param', 'size_param', 'lookup_url', 'lookup_records_path',
            'lookup_key_path', 'lookup_value_path', 'requests_per_second', 'concurrency', 'interval', 'created',
            'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'description')

    def validate(self, data):
        secret = data.pop('secret', None)
        data = super().validate(data)
        if secret:
            data['secret_ciphertext'] = encrypt(secret)
        elif secret == '':
            data['secret_ciphertext'] = ''
        return data


class MappingSerializer(ValidatedModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ingest-api:mapping-detail')
    source = serializers.PrimaryKeyRelatedField(queryset=Source.objects.all())
    object_type = ContentTypeField(queryset=ContentType.objects.all())

    class Meta:
        model = Mapping
        fields = (
            'id', 'url', 'display', 'source', 'name', 'description', 'enabled', 'weight', 'object_type',
            'object_filter', 'record_key_path', 'record_key_regex', 'object_key_field', 'object_key_regex',
            'ignore_case', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'name', 'description')


class MappingFieldSerializer(ValidatedModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ingest-api:mappingfield-detail')
    mapping = serializers.PrimaryKeyRelatedField(queryset=Mapping.objects.all())

    class Meta:
        model = MappingField
        fields = (
            'id', 'url', 'display', 'mapping', 'target', 'value_path', 'transform', 'expression', 'replacement',
            'table', 'overwrite_foreign', 'enabled', 'created', 'last_updated',
        )
        brief_fields = ('id', 'url', 'display', 'target')


class RunSerializer(ValidatedModelSerializer):
    url = serializers.HyperlinkedIdentityField(view_name='plugins-api:netbox_ingest-api:run-detail')
    source = serializers.PrimaryKeyRelatedField(read_only=True)
    status = ChoiceField(choices=RunStatusChoices, read_only=True)
    trigger = ChoiceField(choices=TriggerChoices, read_only=True)
    user = serializers.StringRelatedField(read_only=True)
    applied_by = serializers.StringRelatedField(read_only=True)

    class Meta:
        model = Run
        fields = (
            'id', 'url', 'display', 'source', 'dry_run', 'trigger', 'status', 'user', 'created', 'started',
            'finished', 'applied', 'applied_by', 'counts', 'stats', 'log', 'error', 'job_id',
        )
        read_only_fields = fields
        brief_fields = ('id', 'url', 'display', 'status')


class RunItemSerializer(serializers.ModelSerializer):
    status = ChoiceField(choices=ItemStatusChoices, read_only=True)
    object_type = ContentTypeField(read_only=True)
    mapping = serializers.PrimaryKeyRelatedField(read_only=True)

    class Meta:
        model = RunItem
        fields = ('id', 'mapping', 'object_type', 'object_id', 'object_repr', 'key', 'target', 'old', 'new',
                  'status', 'message')
        read_only_fields = fields
