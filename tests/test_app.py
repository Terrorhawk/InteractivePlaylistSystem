import os
from pathlib import Path
import sys
import tempfile
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app import create_app, RULES


class ManagementTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.config = dict(TESTING=True, SECRET_KEY='s'*32, ADMIN_URL='http://localhost/manage',
                           ADMIN_USERNAME='admin', ADMIN_PASSWORD='a-long-test-password',
                           SQLITE_TEST_PATH=str(Path(self.temp.name)/'test.sqlite3'))
        self.app = create_app(self.config)
        self.client = self.app.test_client()
        self.login(self.client)
        self.post(self.client, '/manage', action='site', name='Site A', domain='localhost')

    def tearDown(self):
        self.temp.cleanup()

    def post(self, client, path, host='localhost', **data):
        client.get('/', base_url='http://'+host)
        with client.session_transaction(base_url='http://'+host) as s:
            csrf = s['csrf']
        return client.post(path, base_url='http://'+host, data=dict(csrf=csrf, **data))

    def login(self, client, username='admin', password='a-long-test-password', host='localhost', path='/manage/login'):
        return self.post(client, path, host=host, username=username, password=password)

    def event(self, slug='party', **overrides):
        data = {k:v[1] for k,v in RULES.items()}
        data.update(name='Testevent', confirm='yes')
        data.update(overrides)
        return self.post(self.client, '/'+slug+'/admin', **data)

    def test_explicit_creation_and_defaults(self):
        self.assertEqual(self.client.get('/missing/').status_code, 404)
        self.assertEqual(self.client.get('/party/settings').status_code, 200)
        self.assertEqual(self.client.get('/party/').status_code, 404)
        self.assertEqual(self.event().status_code, 302)
        self.assertIn(b'Testevent', self.client.get('/party/').data)

    def test_csrf_required(self):
        self.assertEqual(self.client.post('/party/admin', data={}).status_code, 403)

    def test_unknown_host_rejected(self):
        self.assertEqual(self.client.get('/', base_url='http://unregistered.test').status_code, 404)

    def test_reserved_slug(self):
        self.assertEqual(self.event('api').status_code, 400)

    def test_ranges(self):
        for key in RULES:
            self.assertEqual(self.event(**{key:-1}).status_code, 400)

    def test_confirmation(self):
        self.assertEqual(self.event(confirm='no').status_code, 400)

    def test_revision_conflict(self):
        self.event()
        self.assertEqual(self.event(revision='1').status_code, 302)
        self.assertEqual(self.event(revision='1').status_code, 409)

    def test_unassigned_user_no_access(self):
        self.event()
        self.post(self.client, '/manage', action='user', username='visitor', password='visitor-password-long')
        visitor = self.app.test_client()
        self.login(visitor, 'visitor', 'visitor-password-long')
        self.assertEqual(visitor.get('/party/settings').status_code, 403)
        self.assertEqual(visitor.get('/manage').status_code, 403)

    def test_settings_and_bootstrap_survive_restart(self):
        self.event(request_limit=24)
        app = create_app({**self.config, 'ADMIN_PASSWORD':'changed-environment-password'})
        client = app.test_client()
        self.assertEqual(self.login(client).status_code, 302)
        self.assertIn(b'value="24"', client.get('/party/settings').data)

    def test_cross_event_permissions(self):
        import sqlite3
        self.event('first')
        self.event('second')
        self.post(self.client, '/manage', action='user', username='limited', password='limited-password-long')
        with sqlite3.connect(self.config['SQLITE_TEST_PATH']) as db:
            uid = db.execute("SELECT id FROM users WHERE username='limited'").fetchone()[0]
            eid = db.execute("SELECT id FROM events WHERE slug='first'").fetchone()[0]
        db.close()
        self.post(self.client, '/manage', action='grant', user=uid, scope=eid, role='event')
        client = self.app.test_client()
        self.login(client, 'limited', 'limited-password-long')
        self.assertEqual(client.get('/first/settings').status_code, 200)
        self.assertEqual(client.get('/second/admin').status_code, 403)
        self.assertEqual(client.get('/new/admin').status_code, 403)

    def test_same_slug_on_two_sites(self):
        self.event('party')
        self.post(self.client, '/manage', action='site', name='Site B', domain='second.test')
        self.assertEqual(self.client.get('/party/', base_url='http://second.test').status_code, 404)
        # Independent login cookie on the other hostname.
        self.login(self.client, host='second.test', path='/party/login')
        data = {k:v[1] for k,v in RULES.items()}
        self.assertEqual(self.post(self.client, '/party/admin', host='second.test', name='Andere site', confirm='yes', **data).status_code, 302)
        self.assertIn(b'Andere site', self.client.get('/party/', base_url='http://second.test').data)
        self.assertIn(b'Testevent', self.client.get('/party/').data)


if __name__ == '__main__':
    unittest.main()
