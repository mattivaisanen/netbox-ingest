"""
The plugin's core rules, end to end against a test database. HTTP is
replaced by canned responses; nothing leaves the machine.
"""
from unittest import mock

from django.contrib.contenttypes.models import ContentType
from django.test import TestCase

from dcim.models import Site

from netbox_ingest import engine, http, jsonpath, livetest, secrets
from netbox_ingest.choices import AuthTypeChoices, ItemStatusChoices as S, RunStatusChoices, TransformChoices
from netbox_ingest.models import Mapping, MappingField, Run, Source, WrittenValue


class FakeResponse:
    def __init__(self, url):
        self.url = url
        self.links = {}
        self.status_code = 200


def serve(records):
    """Patches the HTTP client to answer every request with {"items": records}."""
    def request(client, url, params=None, body=None):
        client.stats['requests'] += 1
        return {'items': records}, FakeResponse(url)

    return mock.patch('netbox_ingest.http.Client.request', request)


class JsonPathTests(TestCase):
    def test_paths(self):
        data = {'a': {'b': [{'c': 1}, {'c': 2}]}, 'x.y': 3}
        self.assertEqual(jsonpath.get(data, 'a.b[1].c'), 2)
        self.assertEqual(jsonpath.get(data, '$.a.b[-1].c'), 2)
        self.assertEqual(jsonpath.get(data, '["x.y"]'), 3)
        self.assertIs(jsonpath.get(data, 'a.missing'), jsonpath.MISSING)
        self.assertEqual(jsonpath.get(data, ''), data)


class SecretTests(TestCase):
    def test_round_trip(self):
        token = secrets.encrypt('s3cret')
        self.assertNotIn('s3cret', token)
        self.assertEqual(secrets.decrypt(token), 's3cret')


class OwnershipRuleTests(TestCase):
    """decide(): the plugin only overwrites values it wrote itself."""

    def written(self, value):
        return WrittenValue(value=value)

    def test_rule(self):
        self.assertEqual(engine.decide('', 'up', None)[0], engine.WRITE)          # empty: fill
        self.assertEqual(engine.decide('up', 'up', None)[0], engine.UNCHANGED)
        self.assertEqual(engine.decide('manual', 'up', None)[0], S.FOREIGN)       # someone else's
        self.assertEqual(engine.decide('manual', 'up', None, True)[0], engine.WRITE)
        self.assertEqual(engine.decide('down', 'up', self.written('down'))[0], engine.WRITE)  # ours
        self.assertEqual(engine.decide('edited', 'up', self.written('down'))[0], S.EDITED)


class RunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.alpha = Site.objects.create(name='Alpha', slug='alpha')
        cls.beta = Site.objects.create(name='Beta', slug='beta', description='set by hand')
        Site.objects.create(name='Gamma', slug='gamma')
        cls.source = Source.objects.create(name='test', url='https://api.invalid/sites', records_path='items')
        cls.mapping = Mapping.objects.create(
            source=cls.source, name='sites', object_type=ContentType.objects.get_for_model(Site),
            record_key_path='site', object_key_field='name',
        )
        MappingField.objects.create(mapping=cls.mapping, target='description', value_path='state',
                                    transform=TransformChoices.LOOKUP, table={'1': 'online', '0': 'offline'})

    def run_source(self, records, dry_run=False):
        run = Run.objects.create(source=self.source, dry_run=dry_run)
        with serve(records):
            engine.execute(run)
        run.refresh_from_db()
        return run

    def statuses(self, run):
        return {(item.object_repr or item.key, item.status) for item in run.items.all()}

    def test_dry_run_apply_and_ownership(self):
        records = [{'site': 'ALPHA', 'state': 1}, {'site': 'beta', 'state': 0}, {'site': 'delta', 'state': 1}]

        # Dry run: plans Alpha, protects Beta's hand-written value, reports the rest.
        run = self.run_source(records, dry_run=True)
        self.assertEqual(run.status, RunStatusChoices.DRY_RUN)
        self.assertEqual(self.statuses(run), {
            ('Alpha', S.PLANNED), ('Beta', S.FOREIGN), ('Gamma', S.NO_DATA), ('delta', S.UNMATCHED),
        })
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, '')

        # Apply writes it, without a change log entry, and records ownership.
        engine.apply(run, None)
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, 'online')
        self.assertEqual(WrittenValue.objects.get(object_id=self.alpha.pk).value, 'online')

        # The API changes: the plugin updates its own value.
        records[0]['state'] = 0
        run = self.run_source(records)
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, 'offline')

        # A person edits it: the plugin leaves it alone and says so.
        Site.objects.filter(pk=self.alpha.pk).update(description='maintenance')
        records[0]['state'] = 1
        run = self.run_source(records)
        self.assertIn(('Alpha', S.EDITED), self.statuses(run))
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, 'maintenance')

    def test_apply_skips_values_changed_after_dry_run(self):
        run = self.run_source([{'site': 'alpha', 'state': 1}], dry_run=True)
        Site.objects.filter(pk=self.alpha.pk).update(description='changed meanwhile')
        engine.apply(run, None)
        self.assertEqual(run.items.get(object_id=self.alpha.pk).status, S.STALE)
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, 'changed meanwhile')


class LiveTestTests(TestCase):
    """Live tests from the forms: clear verdicts, and never a write."""

    @classmethod
    def setUpTestData(cls):
        cls.alpha = Site.objects.create(name='Alpha', slug='alpha')
        cls.source = Source.objects.create(name='live', url='https://api.invalid/sites', records_path='items',
                                           auth_type=AuthTypeChoices.BEARER)
        cls.mapping = Mapping.objects.create(
            source=cls.source, name='sites', object_type=ContentType.objects.get_for_model(Site),
            record_key_path='site', object_key_field='name',
        )

    def answer(self, with_token, without_token):
        """Patches requests: the HTTP status with and without credentials (None = 200)."""
        def request(client, url, params=None, body=None):
            status = with_token if client.source.auth_type != AuthTypeChoices.NONE else without_token
            if status:
                raise http.FetchError(f'HTTP {status}', status=status)
            return {'items': []}, FakeResponse(url)

        return mock.patch('netbox_ingest.http.Client.request', request)

    def test_authentication_verdicts(self):
        with self.answer(401, 401):
            result = livetest.authentication(self.source, secret='wrong')
        self.assertIs(result['ok'], False)
        self.assertIn('401', result['message'])
        with self.answer(None, 401):
            self.assertIs(livetest.authentication(self.source, secret='right')['ok'], True)
        # Accepted, but the API answers anyone: no verdict on the credentials.
        with self.answer(None, None):
            self.assertIsNone(livetest.authentication(self.source, secret='any')['ok'])

    def test_object_preview_writes_nothing(self):
        field = MappingField(mapping=self.mapping, target='description', value_path='state')
        obj, error = livetest.find_object(self.mapping, 'ALPHA')
        self.assertEqual((obj, error), (self.alpha, ''))
        with serve([{'site': 'alpha', 'state': 'online'}]):
            result = livetest.object_preview(self.mapping, [field], obj, secret='x')
        self.assertEqual(result['fields'][0]['status'], S.PLANNED)
        self.assertEqual(result['fields'][0]['new'], '"online"')
        self.alpha.refresh_from_db()
        self.assertEqual(self.alpha.description, '')
        self.assertFalse(WrittenValue.objects.exists())
        self.assertFalse(Run.objects.exists())
