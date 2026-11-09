"""
The plugin's own records.

None of these models use NetBox's change logging, tags, journal, bookmarks
or jobs-by-object features, on purpose: each of those stores rows in NetBox's
own tables that point at the plugin's models, and those rows would break
NetBox pages once the plugin is removed. Everything the plugin needs to
remember lives in its own tables, so uninstalling never needs a cleanup.

- Source: one API endpoint, its authentication, paging and schedule.
- Mapping: which NetBox objects (one model) a source's records belong to.
- MappingField: which value goes into which field of those objects.
- WrittenValue: the last value the plugin wrote into a field of an object.
  The plugin only ever overwrites values it can find here unchanged.
- Run / RunItem: one execution of a source and its report.
"""
from django.conf import settings
from django.contrib.contenttypes.fields import GenericForeignKey
from django.core.exceptions import ValidationError
from django.core.validators import MinValueValidator
from django.db import models
from django.urls import reverse
from django.utils.translation import gettext_lazy as _

from netbox.models import BaseModel
from utilities.querysets import RestrictedQuerySet

from .choices import (
    AuthTypeChoices, FetchModeChoices, HttpMethodChoices, ItemStatusChoices, PaginationChoices,
    RunStatusChoices, TransformChoices, TriggerChoices,
)


class TimestampedModel(BaseModel):
    created = models.DateTimeField(verbose_name=_('created'), auto_now_add=True)
    last_updated = models.DateTimeField(verbose_name=_('last updated'), auto_now=True)

    class Meta:
        abstract = True


class Source(TimestampedModel):
    """One API endpoint and how to call it."""
    name = models.CharField(verbose_name=_('name'), max_length=100, unique=True)
    description = models.CharField(verbose_name=_('description'), max_length=200, blank=True)
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True)

    # Request
    fetch_mode = models.CharField(
        verbose_name=_('fetch mode'), max_length=20, choices=FetchModeChoices, default=FetchModeChoices.BULK,
    )
    url = models.CharField(
        verbose_name=_('URL'), max_length=2000,
        help_text=_('Per object: use {key}, {lookup} and {pk} as placeholders, e.g. https://api/items/{key}.'),
    )
    method = models.CharField(
        verbose_name=_('method'), max_length=10, choices=HttpMethodChoices, default=HttpMethodChoices.GET,
    )
    body = models.TextField(
        verbose_name=_('request body'), blank=True,
        help_text=_('POST only. Sent as JSON; the same placeholders as the URL work here.'),
    )
    headers = models.JSONField(
        verbose_name=_('headers'), default=dict, blank=True,
        help_text=_('Extra request headers as a JSON object. Put secrets in the authentication fields instead.'),
    )
    #: Response parser, see parsers.py. Only "json" for now.
    format = models.CharField(verbose_name=_('format'), max_length=20, default='json')
    records_path = models.CharField(
        verbose_name=_('records path'), max_length=500, blank=True,
        help_text=_('Where the records are in the response, e.g. data.items. Empty: the response itself.'),
    )
    verify_tls = models.BooleanField(verbose_name=_('verify TLS certificate'), default=True)
    timeout = models.PositiveIntegerField(
        verbose_name=_('timeout'), null=True, blank=True, validators=[MinValueValidator(1)],
        help_text=_('Seconds per request. Empty: the administrator\'s limit.'),
    )

    # Authentication. The secret is stored encrypted, or read from an
    # environment variable of the NetBox worker; it is never shown again.
    auth_type = models.CharField(
        verbose_name=_('authentication'), max_length=20, choices=AuthTypeChoices, default=AuthTypeChoices.NONE,
    )
    auth_username = models.CharField(
        verbose_name=_('username / client ID'), max_length=200, blank=True,
    )
    auth_name = models.CharField(
        verbose_name=_('header or parameter name'), max_length=100, blank=True,
        help_text=_('For API keys, e.g. X-API-Key or api_key.'),
    )
    token_url = models.CharField(verbose_name=_('token URL'), max_length=2000, blank=True)
    oauth_scope = models.CharField(verbose_name=_('scope'), max_length=500, blank=True)
    secret_ciphertext = models.TextField(blank=True, editable=False)
    secret_env = models.CharField(
        verbose_name=_('secret from environment variable'), max_length=100, blank=True,
        help_text=_('Name of an environment variable holding the secret. Used instead of a stored secret.'),
    )

    # Pagination
    pagination = models.CharField(
        verbose_name=_('pagination'), max_length=20, choices=PaginationChoices, default=PaginationChoices.NONE,
    )
    page_size = models.PositiveIntegerField(verbose_name=_('page size'), null=True, blank=True)
    next_path = models.CharField(
        verbose_name=_('next URL path'), max_length=500, blank=True,
        help_text=_('Where the next page\'s URL is in the response, e.g. links.next.'),
    )
    page_param = models.CharField(
        verbose_name=_('page / offset parameter'), max_length=100, blank=True,
        help_text=_('Query parameter for the page number or offset.'),
    )
    size_param = models.CharField(
        verbose_name=_('page size / limit parameter'), max_length=100, blank=True,
    )

    # Chained lookup: fetch a list first to translate an object's key into the
    # API's own ID, then use it as {lookup} in a per-object URL.
    lookup_url = models.CharField(verbose_name=_('lookup URL'), max_length=2000, blank=True)
    lookup_records_path = models.CharField(verbose_name=_('lookup records path'), max_length=500, blank=True)
    lookup_key_path = models.CharField(
        verbose_name=_('lookup key path'), max_length=500, blank=True,
        help_text=_('Value in each lookup record that matches an object\'s key, e.g. name.'),
    )
    lookup_value_path = models.CharField(
        verbose_name=_('lookup value path'), max_length=500, blank=True,
        help_text=_('Value in each lookup record that becomes {lookup}, e.g. id.'),
    )

    # Limits, capped by the administrator's settings
    requests_per_second = models.FloatField(
        verbose_name=_('requests per second'), null=True, blank=True, validators=[MinValueValidator(0.01)],
    )
    concurrency = models.PositiveSmallIntegerField(
        verbose_name=_('parallel requests'), null=True, blank=True, validators=[MinValueValidator(1)],
    )
    interval = models.PositiveIntegerField(
        verbose_name=_('run every (minutes)'), null=True, blank=True,
        help_text=_('Empty: manual runs only. Scheduled runs write directly, without a dry run.'),
    )

    class Meta:
        ordering = ('name',)
        verbose_name = _('source')
        verbose_name_plural = _('sources')
        permissions = (
            ('run_source', 'Can run sources'),
        )

    def __str__(self):
        return self.name

    def get_absolute_url(self):
        return reverse('plugins:netbox_ingest:source', args=[self.pk])

    @property
    def has_secret(self):
        return bool(self.secret_ciphertext or self.secret_env)

    @property
    def last_run(self):
        return self.runs.order_by('-created').first()

    def clean(self):
        super().clean()
        from .settings import get_setting

        errors = {}
        if self.interval is not None and self.interval < (get_setting('min_interval') or 1):
            errors['interval'] = _('The administrator allows no less than {n} minutes.').format(
                n=get_setting('min_interval'))
        if self.auth_type in (AuthTypeChoices.HEADER, AuthTypeChoices.QUERY) and not self.auth_name:
            errors['auth_name'] = _('Name the header or query parameter for the API key.')
        if self.auth_type == AuthTypeChoices.OAUTH2 and not (self.token_url and self.auth_username):
            errors['token_url'] = _('OAuth2 needs the token URL and the client ID.')
        if self.pagination == PaginationChoices.NEXT_URL and not self.next_path:
            errors['next_path'] = _('Say where the next page\'s URL is.')
        if self.pagination in (PaginationChoices.PAGE, PaginationChoices.OFFSET) and not self.page_param:
            errors['page_param'] = _('Name the page or offset parameter.')
        if self.pagination == PaginationChoices.OFFSET and not self.page_size:
            errors['page_size'] = _('Offset paging needs a page size.')
        if self.lookup_url and not (self.lookup_key_path and self.lookup_value_path):
            errors['lookup_key_path'] = _('A lookup needs both the key path and the value path.')
        if not isinstance(self.headers, dict):
            errors['headers'] = _('Headers must be a JSON object.')
        if errors:
            raise ValidationError(errors)


class Mapping(TimestampedModel):
    """Which NetBox objects (all of one model) a source's records belong to."""
    source = models.ForeignKey(to=Source, on_delete=models.CASCADE, related_name='mappings',
                               verbose_name=_('source'))
    name = models.CharField(verbose_name=_('name'), max_length=100)
    description = models.CharField(verbose_name=_('description'), max_length=200, blank=True)
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True)
    weight = models.PositiveSmallIntegerField(verbose_name=_('weight'), default=100,
                                              help_text=_('Mappings run in this order (lowest first).'))
    object_type = models.ForeignKey(
        to='contenttypes.ContentType', on_delete=models.PROTECT, related_name='+',
        verbose_name=_('object type'),
    )
    object_filter = models.JSONField(
        verbose_name=_('object filter'), default=dict, blank=True,
        help_text=_('Only these objects, as NetBox filter parameters in JSON, e.g. {"status": "active"}.'),
    )
    # Matching: the record's key and the object's key must be equal.
    record_key_path = models.CharField(
        verbose_name=_('record key path'), max_length=500, blank=True,
        help_text=_('Where the record\'s key is, e.g. host.name. Per object: only needed to pick one record '
                    'from a response that has several.'),
    )
    record_key_regex = models.CharField(
        verbose_name=_('record key regex'), max_length=500, blank=True,
        help_text=_('Optional. The first group (or the whole match) becomes the key. Also applied to '
                    'the keys of a lookup.'),
    )
    object_key_field = models.CharField(
        verbose_name=_('object key field'), max_length=200, default='name',
        help_text=_('NetBox field holding the object\'s key, e.g. name, serial, '
                    'custom_field_data__asset_id or primary_ip4__address.'),
    )
    object_key_regex = models.CharField(
        verbose_name=_('object key regex'), max_length=500, blank=True,
        help_text=_('Optional. The first group (or the whole match) becomes the key.'),
    )
    ignore_case = models.BooleanField(verbose_name=_('ignore case'), default=True)

    class Meta:
        ordering = ('source', 'weight', 'name')
        verbose_name = _('mapping')
        verbose_name_plural = _('mappings')
        constraints = (
            models.UniqueConstraint(fields=('source', 'name'), name='netbox_ingest_unique_mapping_name'),
        )

    def __str__(self):
        return f'{self.source} / {self.name}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_ingest:mapping', args=[self.pk])

    @property
    def model(self):
        return self.object_type.model_class()

    def clean(self):
        super().clean()
        from . import matching

        errors = {}
        if self.object_type_id and self.model is None:
            errors['object_type'] = _('This model is not installed.')
        for field in ('record_key_regex', 'object_key_regex'):
            message = matching.regex_error(getattr(self, field))
            if message:
                errors[field] = message
        if not isinstance(self.object_filter, dict):
            errors['object_filter'] = _('The filter must be a JSON object.')
        if self.source_id and self.source.fetch_mode == FetchModeChoices.BULK and not self.record_key_path:
            errors['record_key_path'] = _('Bulk sources match records by this key.')
        if not errors and self.object_type_id:
            message = matching.object_key_error(self.model, self.object_key_field) \
                or matching.filter_error(self.model, self.object_filter)
            if message:
                errors['object_key_field' if 'field' in message.lower() else 'object_filter'] = message
        if errors:
            raise ValidationError(errors)


class MappingField(TimestampedModel):
    """One value of a record going into one field of the mapped objects."""
    mapping = models.ForeignKey(to=Mapping, on_delete=models.CASCADE, related_name='fields',
                                verbose_name=_('mapping'))
    target = models.CharField(
        verbose_name=_('NetBox field'), max_length=100,
        help_text=_('A field of the model (e.g. description, status) or a custom field as cf_<name>.'),
    )
    value_path = models.CharField(
        verbose_name=_('value path'), max_length=500,
        help_text=_('Where the value is in the record, e.g. state or checks[0].result.'),
    )
    transform = models.CharField(
        verbose_name=_('transform'), max_length=20, choices=TransformChoices, default=TransformChoices.NONE,
    )
    expression = models.TextField(
        verbose_name=_('expression'), blank=True,
        help_text=_('Regex: the pattern. Jinja2: the template, with value, record and key available.'),
    )
    replacement = models.CharField(
        verbose_name=_('replacement'), max_length=500, blank=True,
        help_text=_('Regex only, e.g. \\1. Empty: the first group, or the whole match.'),
    )
    table = models.JSONField(
        verbose_name=_('lookup table'), default=dict, blank=True,
        help_text=_('Lookup only: {"api value": "NetBox value"}. A value not in the table is left alone, '
                    'unless the table has a "*" entry.'),
    )
    overwrite_foreign = models.BooleanField(
        verbose_name=_('overwrite values not written by the plugin'), default=False,
        help_text=_('Off: the plugin only fills empty fields and updates values it wrote itself. On: it also '
                    'replaces values set by people or other tools. Check a dry run first.'),
    )
    enabled = models.BooleanField(verbose_name=_('enabled'), default=True)

    class Meta:
        ordering = ('mapping', 'target')
        verbose_name = _('field')
        verbose_name_plural = _('fields')
        constraints = (
            models.UniqueConstraint(fields=('mapping', 'target'), name='netbox_ingest_unique_field_target'),
        )

    def __str__(self):
        return f'{self.mapping.name}: {self.target}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_ingest:mappingfield', args=[self.pk])

    def clean(self):
        super().clean()
        from . import targets, transforms

        errors = {}
        if self.mapping_id and self.mapping.model is not None:
            try:
                targets.resolve(self.mapping.model, self.target)
            except targets.TargetError as exc:
                errors['target'] = str(exc)
        message = transforms.config_error(self)
        if message:
            errors['expression' if self.transform != TransformChoices.LOOKUP else 'table'] = message
        if errors:
            raise ValidationError(errors)

    @property
    def is_custom_field(self):
        return self.target.startswith('cf_')


class WrittenValue(models.Model):
    """
    The value the plugin last wrote into one field of one object. A value
    only counts as the plugin's while the field still holds exactly this.
    """
    object_type = models.ForeignKey(to='contenttypes.ContentType', on_delete=models.CASCADE, related_name='+')
    object_id = models.PositiveBigIntegerField()
    object = GenericForeignKey('object_type', 'object_id')
    target = models.CharField(max_length=100)
    value = models.JSONField(null=True, blank=True)
    mapping = models.ForeignKey(to=Mapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    run = models.ForeignKey(to='Run', on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    updated = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = _('written value')
        verbose_name_plural = _('written values')
        constraints = (
            models.UniqueConstraint(fields=('object_type', 'object_id', 'target'),
                                    name='netbox_ingest_unique_written_value'),
        )

    def __str__(self):
        return f'{self.object_type_id}:{self.object_id}.{self.target}'


class Run(BaseModel):
    """One execution of a source: what was fetched, compared and written."""
    source = models.ForeignKey(to=Source, on_delete=models.CASCADE, related_name='runs', verbose_name=_('source'))
    dry_run = models.BooleanField(verbose_name=_('dry run'), default=False)
    trigger = models.CharField(verbose_name=_('trigger'), max_length=20, choices=TriggerChoices,
                               default=TriggerChoices.MANUAL)
    status = models.CharField(verbose_name=_('status'), max_length=20, choices=RunStatusChoices,
                              default=RunStatusChoices.QUEUED)
    user = models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                             related_name='+', verbose_name=_('user'))
    created = models.DateTimeField(verbose_name=_('created'), auto_now_add=True)
    started = models.DateTimeField(verbose_name=_('started'), null=True, blank=True)
    finished = models.DateTimeField(verbose_name=_('finished'), null=True, blank=True)
    applied = models.DateTimeField(verbose_name=_('applied'), null=True, blank=True)
    applied_by = models.ForeignKey(to=settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                   related_name='+', verbose_name=_('applied by'))
    #: Item counts by status, plus "unchanged".
    counts = models.JSONField(verbose_name=_('counts'), default=dict, blank=True)
    #: requests, bytes, pages, records, seconds.
    stats = models.JSONField(verbose_name=_('statistics'), default=dict, blank=True)
    #: [{"level": "info|warning|error", "message": "..."}]
    log = models.JSONField(verbose_name=_('log'), default=list, blank=True)
    error = models.TextField(verbose_name=_('error'), blank=True)
    #: Approximate storage used by the run and its report, for retention.
    size = models.PositiveBigIntegerField(verbose_name=_('size'), default=0)
    #: NetBox job running this, for the link to its log.
    job_id = models.UUIDField(null=True, blank=True)

    class Meta:
        ordering = ('-created',)
        verbose_name = _('run')
        verbose_name_plural = _('runs')
        indexes = (
            models.Index(fields=('source', 'status')),
        )

    def __str__(self):
        return f'{self.source.name} #{self.pk}'

    def get_absolute_url(self):
        return reverse('plugins:netbox_ingest:run', args=[self.pk])

    def get_status_color(self):
        return RunStatusChoices.colors.get(self.status)

    @property
    def can_apply(self):
        return self.status == RunStatusChoices.DRY_RUN and self.applied is None

    @property
    def duration(self):
        if self.started and self.finished:
            return self.finished - self.started
        return None


class RunItem(models.Model):
    """One line of a run's report: a field of an object, or a record."""
    run = models.ForeignKey(to=Run, on_delete=models.CASCADE, related_name='items')
    mapping = models.ForeignKey(to=Mapping, on_delete=models.SET_NULL, null=True, blank=True, related_name='+')
    object_type = models.ForeignKey(to='contenttypes.ContentType', on_delete=models.CASCADE, null=True,
                                    blank=True, related_name='+')
    object_id = models.PositiveBigIntegerField(null=True, blank=True)
    object = GenericForeignKey('object_type', 'object_id')
    #: str(object) when the run happened; also for objects deleted since.
    object_repr = models.CharField(max_length=200, blank=True)
    #: The matching key (object's or record's).
    key = models.CharField(max_length=300, blank=True)
    target = models.CharField(max_length=100, blank=True)
    old = models.JSONField(null=True, blank=True)
    new = models.JSONField(null=True, blank=True)
    status = models.CharField(max_length=20, choices=ItemStatusChoices)
    message = models.CharField(max_length=500, blank=True)

    objects = RestrictedQuerySet.as_manager()

    class Meta:
        ordering = ('run', 'pk')
        verbose_name = _('report row')
        verbose_name_plural = _('report rows')
        indexes = (
            models.Index(fields=('run', 'status')),
        )

    def __str__(self):
        return f'{self.object_repr or self.key} {self.target}'.strip()

    def get_status_color(self):
        return ItemStatusChoices.colors.get(self.status)
