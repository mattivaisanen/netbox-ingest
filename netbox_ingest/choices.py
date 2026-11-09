from django.utils.translation import gettext_lazy as _

from utilities.choices import ChoiceSet


class AuthTypeChoices(ChoiceSet):
    NONE = 'none'
    BASIC = 'basic'
    BEARER = 'bearer'
    HEADER = 'header'
    QUERY = 'query'
    OAUTH2 = 'oauth2'

    CHOICES = (
        (NONE, _('None')),
        (BASIC, _('HTTP Basic')),
        (BEARER, _('Bearer token')),
        (HEADER, _('API key in a header')),
        (QUERY, _('API key in the query string')),
        (OAUTH2, _('OAuth2 client credentials')),
    )


class FetchModeChoices(ChoiceSet):
    BULK = 'bulk'
    PER_OBJECT = 'per-object'

    CHOICES = (
        (BULK, _('Bulk: fetch all records once, then match')),
        (PER_OBJECT, _('Per object: one request per NetBox object')),
    )


class PaginationChoices(ChoiceSet):
    NONE = 'none'
    NEXT_URL = 'next-url'
    LINK_HEADER = 'link-header'
    PAGE = 'page'
    OFFSET = 'offset'

    CHOICES = (
        (NONE, _('None')),
        (NEXT_URL, _('Next URL in the response body')),
        (LINK_HEADER, _('Link header (rel="next")')),
        (PAGE, _('Page number')),
        (OFFSET, _('Offset and limit')),
    )


class HttpMethodChoices(ChoiceSet):
    GET = 'GET'
    POST = 'POST'

    CHOICES = ((GET, 'GET'), (POST, 'POST'))


class TransformChoices(ChoiceSet):
    NONE = 'none'
    REGEX = 'regex'
    LOOKUP = 'lookup'
    JINJA = 'jinja'

    CHOICES = (
        (NONE, _('None')),
        (REGEX, _('Regular expression')),
        (LOOKUP, _('Lookup table')),
        (JINJA, _('Jinja2 template')),
    )


class TriggerChoices(ChoiceSet):
    MANUAL = 'manual'
    SCHEDULE = 'schedule'
    API = 'api'

    CHOICES = (
        (MANUAL, _('Manual')),
        (SCHEDULE, _('Schedule')),
        (API, _('REST API')),
    )


class RunStatusChoices(ChoiceSet):
    QUEUED = 'queued'
    RUNNING = 'running'
    DRY_RUN = 'dry-run'
    APPLYING = 'applying'
    COMPLETED = 'completed'
    FAILED = 'failed'
    SKIPPED = 'skipped'

    CHOICES = (
        (QUEUED, _('Queued'), 'gray'),
        (RUNNING, _('Running'), 'blue'),
        (DRY_RUN, _('Dry run'), 'cyan'),
        (APPLYING, _('Applying'), 'blue'),
        (COMPLETED, _('Completed'), 'green'),
        (FAILED, _('Failed'), 'red'),
        (SKIPPED, _('Skipped'), 'orange'),
    )

    #: Nothing more happens to a run in one of these.
    FINISHED = (COMPLETED, FAILED, SKIPPED)
    #: A job is working on the run.
    ACTIVE = (QUEUED, RUNNING, APPLYING)


class ItemStatusChoices(ChoiceSet):
    """What happened to one field of one object (or to one record) in a run."""
    PLANNED = 'planned'
    WRITTEN = 'written'
    EDITED = 'edited'
    FOREIGN = 'foreign'
    STALE = 'stale'
    INVALID = 'invalid'
    NO_VALUE = 'no-value'
    NO_DATA = 'no-data'
    UNMATCHED = 'unmatched'
    AMBIGUOUS = 'ambiguous'
    FAILED = 'failed'

    CHOICES = (
        (PLANNED, _('Would write'), 'cyan'),
        (WRITTEN, _('Written'), 'green'),
        (EDITED, _('Skipped: edited in NetBox'), 'orange'),
        (FOREIGN, _('Skipped: not written by the plugin'), 'orange'),
        (STALE, _('Skipped: changed since dry run'), 'orange'),
        (INVALID, _('Invalid value'), 'red'),
        (NO_VALUE, _('No value in record'), 'gray'),
        (NO_DATA, _('No record for object'), 'gray'),
        (UNMATCHED, _('Record matches no object'), 'gray'),
        (AMBIGUOUS, _('Ambiguous match'), 'purple'),
        (FAILED, _('Request failed'), 'red'),
    )

    #: Statuses that left a field alone for a reason the user may want to fix.
    SKIPPED = (EDITED, FOREIGN, STALE, INVALID, AMBIGUOUS, FAILED)
