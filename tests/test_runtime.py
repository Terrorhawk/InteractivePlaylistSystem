import json
import sqlite3
import unittest
import test_app as fixtures

A, B = 'abcdefghijk', 'lmnopqrstuv'
ARTWORK = 'https://lh3.googleusercontent.com/test-cover'


class Music:
    def search(self, *args, **kwargs):
        return [dict(videoId=A, title='First', artists=[], duration='3:00', thumbnails=[{'url': ARTWORK}]),
                dict(videoId=B, title='Second', artists=[], duration='3:00')]

    def get_playlist(self, *args, **kwargs):
        return dict(title='Test fallback', tracks=[dict(videoId=B, title='Fallback', duration_seconds=180)])


class RuntimeTests(unittest.TestCase):
    setUpBase = fixtures.ManagementTests.setUp
    tearDown = fixtures.ManagementTests.tearDown
    post = fixtures.ManagementTests.post
    login = fixtures.ManagementTests.login
    event = fixtures.ManagementTests.event

    def setUp(self):
        self.setUpBase()
        self.now = 100000
        self.app.config.update(CLOCK=lambda:self.now, MUSIC_FACTORY=Music)
        self.event(selection_delay_seconds=0)
        self.client.get('/party/search?q=test')
        self.client.get('/party/')
        with self.client.session_transaction() as s:
            self.csrf = s['csrf']
        manage = self.client.get('/party/api/ips/v1/manage').json
        self.headers = {'Authorization':'Bearer '+manage['pairingToken'], 'X-IPS-Player-ID':'ipad-test', 'X-IPS-Event':manage['eventId']}

    def command(self, action='start', identifier='cmd-1'):
        return self.client.post('/party/api/ips/v1/manage/command', headers={'X-CSRF-Token':self.csrf}, json=dict(action=action,id=identifier))

    def sync(self, video=A, state='playing', **data):
        return self.client.post('/party/api/ips/v1/sync', headers=self.headers,
                                json=dict(videoId=video,playbackState=state,position=20,duration=180, **data))

    def ack(self, command):
        return self.client.post('/party/api/ips/v1/ack', headers=self.headers, json=dict(id=command['id'], result='submitted'))

    def add(self, video=A, client=None):
        return self.post(client or self.client, '/party/add', video=video)

    def test_monitor_shows_event_queue_playback_and_offline(self):
        guest = self.app.test_client()
        self.assertIn(b'/party/monitor/state', guest.get('/party/monitor').data)
        self.assertEqual(guest.get('/unknown/monitor').status_code, 404)
        self.post(self.client, '/party/fallback', playlist='PLtest', action='add')
        self.assertEqual(guest.get('/party/monitor/state').json['fallback'],
                         [dict(title='Fallback', duration=180)])
        self.add()
        self.assertEqual(guest.get('/party/monitor/state').json['queue'],
                         [dict(title='First', votes=1, duration=180)])
        self.sync(video='', state='unknown')
        command = self.command().json['command']
        self.ack(command)
        self.sync(deviceName='Private phone', deviceType='phone')
        self.now += 2
        state = guest.get('/party/monitor/state').json
        self.assertEqual((state['title'], state['position'], state['duration']), ('First', 22, 180))
        self.assertTrue(state['playing'])
        self.assertEqual(state['artwork'], ARTWORK)
        self.assertNotIn('Private phone', str(state))
        self.post(self.client, '/party/control', action='offline')
        self.assertTrue(self.client.get('/party/monitor/state').json['offline'])

    def test_confirmation_not_ack_consumes_request(self):
        self.add()
        self.sync(video='',state='unknown')
        command = self.command().json['command']
        self.assertEqual(command['videoId'], A)
        self.ack(command)
        self.assertEqual(len(self.client.get('/party/state').json['queue']), 1)
        self.sync()
        self.assertEqual(self.client.get('/party/state').json['queue'], [])
        self.assertGreater(self.client.get('/party/search?q=test').json['results'][0]['blockedUntil'], self.now*1000)

    def test_event_token_cannot_cross_events(self):
        self.event('other')
        r = self.client.post('/other/api/ips/v1/sync', headers=self.headers, json=dict(videoId=A,playbackState='playing'))
        self.assertEqual(r.status_code, 401)

    def test_offline_does_not_pause_music(self):
        self.add(); self.sync(video='',state='unknown'); c=self.command().json['command']; self.ack(c); self.sync()
        self.post(self.client, '/party/control', action='offline')
        self.assertTrue(self.client.get('/party/api/ips/v1/manage').json['auto'])
        guest=self.app.test_client()
        self.assertEqual(guest.get('/party/state').json, {'offline':True,'offlineText':'Jukebox offline op dit moment'})
        self.assertEqual(self.add(B, guest).status_code,409)
        self.post(self.client, '/party/control', action='offline')
        self.assertFalse(guest.get('/party/state').json['offline'])

    def test_custom_offline_text_and_default(self):
        self.event(revision=1,offline_text='Even pauze <test>')
        self.post(self.client,'/party/control',action='offline')
        guest=self.app.test_client()
        self.assertEqual(guest.get('/party/state').json['offlineText'],'Even pauze <test>')
        self.assertIn('Even pauze &lt;test&gt;',guest.get('/party/').get_data(as_text=True))
        self.event(revision=2,offline_text='')
        self.assertEqual(guest.get('/party/state').json['offlineText'],'Jukebox offline op dit moment')

    def test_liveshow_pause_resume(self):
        self.add(); self.sync(video='',state='unknown'); c=self.command().json['command']; self.ack(c); self.sync()
        self.post(self.client, '/party/control', action='liveshow')
        self.assertEqual(self.sync().json['command']['action'],'pause')
        self.assertEqual(self.command('next','during-live').status_code,409)
        self.post(self.client, '/party/control', action='liveshow')
        self.assertEqual(self.sync().json['command']['action'],'resume')

    def test_quotas_are_configurable(self):
        self.event(revision=1, request_limit=1)
        self.assertEqual(self.add().status_code,200)
        self.assertEqual(self.add(B).status_code,409)

    def test_own_request_blocks_downvote(self):
        self.add()
        other=self.app.test_client(); other.get('/party/'); self.add(B,other)
        self.assertEqual(self.post(self.client,'/party/vote',video=B,direction=-1).status_code,409)

    def test_downvote_reversed_on_new_own_request(self):
        self.add()
        visitor=self.app.test_client(); visitor.get('/party/')
        self.assertEqual(self.post(visitor,'/party/vote',video=A,direction=-1).status_code,200)
        self.add(B,visitor)
        scores={x['id']:x['votes'] for x in self.client.get('/party/state').json['queue']}
        self.assertEqual(scores[A],1)

    def test_adjustment_idempotency(self):
        self.add()
        for i in range(2):
            self.assertEqual(self.post(self.client,'/party/adjust',video=A,delta=100,operation='same').status_code,200)
        self.assertEqual(self.client.get('/party/state').json['queue'][0]['votes'],101)
        self.assertEqual(self.post(self.client,'/party/adjust',video=A,delta=-100,operation='same').status_code,409)

    def test_nonpositive_expiry(self):
        self.add(); self.post(self.client,'/party/adjust',video=A,delta=-100,operation='neg')
        self.now+=1801
        self.assertEqual(self.client.get('/party/state').json['queue'],[])

    def test_pair_replacement_revokes_old_player(self):
        self.sync()
        pair=self.client.post('/party/api/ips/v1/manage/pair',base_url='https://localhost',headers={'X-CSRF-Token':self.csrf},json={})
        self.assertEqual(pair.status_code,200)
        self.assertIn('/party/ips-pair#',pair.json['pairUrl'])
        code=pair.json['pairUrl'].split('#')[1]
        claim=self.client.post('/party/api/ips/v1/pair/claim',json=dict(code=code,playerId='new-ipad'))
        self.assertEqual(claim.status_code,200)
        self.assertNotEqual(claim.json['token'],self.headers['Authorization'][7:])
        self.assertTrue(self.sync().json['revoked'])
        self.assertEqual(self.client.post('/party/api/ips/v1/pair/claim',json=dict(code=code,playerId='third')).status_code,409)

    def test_device_details_admin_only(self):
        self.sync(deviceName='Private phone',deviceType='phone')
        visitor=self.app.test_client()
        self.assertNotIn('deviceName',visitor.get('/party/state').json['player'])
        self.assertEqual(self.client.get('/party/state').json['player']['deviceName'],'Private phone')

    def test_fallback_load_and_duration(self):
        self.assertEqual(self.post(self.client,'/party/fallback',playlist='PLtest',action='add').status_code,200)
        state=self.client.get('/party/state').json
        self.assertEqual(state['fallbackCount'],1)
        self.assertEqual(state['fallbackDuration'],180)
        self.sync(video='',state='unknown')
        self.assertEqual(self.command().json['command']['videoId'],B)

    def test_refresh_artwork_for_existing_fallback_now_playing(self):
        self.post(self.client, '/party/fallback', playlist='PLtest', action='add')
        self.sync(video='', state='unknown')
        command = self.command().json['command']
        self.ack(command)
        self.sync(video=B)
        self.assertIsNone(self.client.get('/party/monitor/state').json['artwork'])

        class MusicWithArtwork(Music):
            def get_playlist(self, *args, **kwargs):
                playlist = super().get_playlist(*args, **kwargs)
                playlist['tracks'][0]['thumbnails'] = [{'url': ARTWORK}]
                return playlist

        self.app.config['MUSIC_FACTORY'] = MusicWithArtwork
        response = self.client.post('/party/admin/refresh-playlist-artwork',
                                    headers={'X-CSRF-Token': self.csrf}, json={'code': 'PLtest'})
        self.assertEqual(response.json['updated'], 1)
        self.assertEqual(self.client.get('/party/monitor/state').json['artwork'], ARTWORK)

    def load_many_fallback(self):
        class Many(Music):
            def get_playlist(self,*args,**kwargs):
                return dict(title='Mixed',tracks=[dict(videoId=v,title=v,duration_seconds=180) for v in [A,B,'zzzzzzzzzzz','yyyyyyyyyyy']])
        self.app.config['MUSIC_FACTORY']=Many
        self.post(self.client,'/party/fallback',playlist='PLmany',action='add')

    def test_shuffle_persists_on_poll_and_restart(self):
        from unittest.mock import patch
        from app import create_app
        self.load_many_fallback()
        def stored():
            db=sqlite3.connect(self.config['SQLITE_TEST_PATH'])
            try:return json.loads(db.execute('SELECT payload FROM event_runtime').fetchone()[0])['namespace']
            finally:db.close()
        before=[x['id'] for x in stored()['fallback_tracks']]
        with patch('runtime.random.shuffle',side_effect=lambda x:x.reverse()):
            self.assertEqual(self.post(self.client,'/party/admin/shuffle-playlists').status_code,200)
        expected=list(reversed(before))
        for i in range(3):self.client.get('/party/state')
        self.assertEqual([x['id'] for x in stored()['fallback_tracks']],expected)
        app=create_app({**self.config,'CLOCK':lambda:self.now})
        app.test_client().get('/party/state')
        self.assertEqual([x['id'] for x in stored()['fallback_tracks']],expected)
        self.assertEqual(self.post(self.app.test_client(),'/party/admin/shuffle-playlists').status_code,403)

    def test_shuffle_preserves_frozen_successor(self):
        self.load_many_fallback();self.sync(video='',state='unknown')
        c=self.command().json['command'];self.ack(c);self.sync(video=c['videoId'])
        self.client.post('/party/api/ips/v1/sync',headers=self.headers,json=dict(videoId=c['videoId'],playbackState='playing',position=172,duration=180))
        frozen=self.client.get('/party/state').json['next']['id']
        self.post(self.client,'/party/admin/shuffle-playlists')
        self.assertEqual(self.client.get('/party/state').json['next']['id'],frozen)
        next_command=self.command('next','next-shuffled').json['command']
        self.assertEqual(next_command['videoId'],frozen)

    def test_shuffle_preserves_issued_track_and_cursor(self):
        self.load_many_fallback();self.sync(video='',state='unknown')
        c=self.command().json['command']
        self.post(self.client,'/party/admin/shuffle-playlists')
        self.assertEqual(self.client.get('/party/api/ips/v1/command',headers=self.headers).json['command'],c)
        self.ack(c);self.sync(video=c['videoId'])
        self.assertNotEqual(self.client.get('/party/state').json['fallback'][0]['id'],c['videoId'])

    def test_song_link_explains_missing_playlist(self):
        r=self.post(self.client,'/party/fallback',playlist='https://music.youtube.com/watch?v=PekVe2nZ2xU&si=test',action='add')
        self.assertEqual(r.status_code,400)
        self.assertIn('niet naar een playlist',r.get_data(as_text=True))
        self.assertEqual(self.client.get('/party/state').json['fallbackCount'],0)

    def test_failed_mutation_rolls_back(self):
        self.add()
        self.assertEqual(self.post(self.client,'/party/adjust',video=A,delta=-200,operation='bad').status_code,400)
        self.assertEqual(self.client.get('/party/state').json['queue'][0]['votes'],1)

    def test_order_and_idempotent_commands_survive_requests(self):
        self.add();self.sync(video='',state='unknown')
        first=self.command().json['command']
        again=self.command().json
        self.assertTrue(again['duplicate'])
        self.assertEqual(first,again['command'])
        pause=self.command('pause','cmd-2').json['command']
        self.assertGreater(pause['sequence'],first['sequence'])
        self.assertEqual(pause['controlEpoch'],first['controlEpoch'])

    def test_playback_state_survives_new_app_instance(self):
        from app import create_app
        self.add(); self.sync(video='',state='unknown'); cmd=self.command().json['command']; self.ack(cmd); self.sync()
        app=create_app(self.config)
        with app.test_client() as client:
            # No authenticated admin needed for public confirmed playback.
            self.assertEqual(client.get('/party/state').json['current']['id'], A)

    def test_selection_delay_then_eligible(self):
        self.event(revision=1, selection_delay_seconds=10)
        self.add();self.sync(video='',state='unknown')
        self.assertEqual(self.command().status_code,409)
        self.now+=10
        self.assertEqual(self.command(identifier='after-delay').json['command']['videoId'],A)

    def test_frozen_successor_and_crossfade_failure(self):
        self.post(self.client,'/party/fallback',playlist='PLtest',action='add')
        self.add();self.sync(video='',state='unknown')
        c=self.command().json['command'];self.ack(c);self.sync()
        self.add(B)
        sync=self.client.post('/party/api/ips/v1/sync',headers=self.headers,
            json=dict(videoId=A,playbackState='playing',position=172,duration=180,crossfadeCapable=True))
        self.assertEqual(sync.status_code,200)
        command=sync.json['command']
        self.assertTrue(command['crossfade'])
        self.assertEqual(command['videoId'],B)
        self.assertEqual(len(self.client.get('/party/state').json['queue']),1)
        failed=self.client.post('/party/api/ips/v1/crossfade-failed',headers=self.headers,json=dict(id=command['id'],outgoingEnded=True))
        self.assertEqual(failed.status_code,200)
        ordinary=failed.json['command']
        self.assertEqual(ordinary['videoId'],B)
        self.assertFalse(ordinary.get('crossfade',False))
        self.ack(ordinary);self.sync(video=B)
        self.assertEqual(self.client.get('/party/state').json['queue'],[])

    def test_player_transport_idempotency(self):
        self.add();self.sync(video='',state='unknown')
        c=self.command().json['command'];self.ack(c);self.sync()
        for i in range(2):
            r=self.client.post('/party/api/ips/v1/transport',headers=self.headers,json=dict(action='pause',id='physical-pause',videoId=A))
            self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['duplicate'])
        self.assertFalse(self.client.get('/party/api/ips/v1/manage').json['auto'])

    def test_parallel_start_only_one_command(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        self.add();self.sync(video='',state='unknown')
        cookie=self.client.get_cookie('ips_multisite_alpha').value
        barrier=Barrier(2)
        def start(number):
            c=self.app.test_client();c.set_cookie('ips_multisite_alpha',cookie)
            barrier.wait(timeout=5)
            return c.post('/party/api/ips/v1/manage/command',headers={'X-CSRF-Token':self.csrf},json=dict(action='start',id='parallel-'+str(number))).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(start,range(2)))
        self.assertEqual(sorted(statuses),[200,409])
        self.assertEqual(len(self.client.get('/party/state').json['queue']),1)

    def test_parallel_claim_only_one_player(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        pair=self.client.post('/party/api/ips/v1/manage/pair',base_url='https://localhost',headers={'X-CSRF-Token':self.csrf},json={}).json
        code=pair['pairUrl'].split('#')[1]
        barrier=Barrier(2)
        def claim(number):
            c=self.app.test_client();barrier.wait(timeout=5)
            return c.post('/party/api/ips/v1/pair/claim',json=dict(code=code,playerId='race-'+str(number))).status_code
        with ThreadPoolExecutor(max_workers=2) as pool:
            statuses=list(pool.map(claim,range(2)))
        self.assertEqual(sorted(statuses),[200,409])

    def test_http_pair_requires_https(self):
        self.assertEqual(self.client.post('/party/api/ips/v1/manage/pair',headers={'X-CSRF-Token':self.csrf},json={}).status_code,400)

    def test_https_proxy_pair_uses_configured_public_scheme(self):
        self.app.config['SESSION_COOKIE_SECURE']=True
        # Reverse proxy reaches the app over HTTP, but the public URL is HTTPS.
        r=self.client.post('/party/api/ips/v1/manage/pair',headers={'X-CSRF-Token':self.csrf},json={})
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['pairUrl'].startswith('https://localhost/party/'))

    def test_https_browser_origin_with_http_backend(self):
        r=self.client.post('/party/api/ips/v1/manage/pair',headers={'X-CSRF-Token':self.csrf},json={'serverUrl':'https://localhost'})
        self.assertEqual(r.status_code,200)
        self.assertTrue(r.json['pairUrl'].startswith('https://localhost/party/'))
        bad=self.client.post('/party/api/ips/v1/manage/pair',headers={'X-CSRF-Token':self.csrf},json={'serverUrl':'https://other.example'})
        self.assertEqual(bad.status_code,400)

    def test_guest_root_has_no_event_names_or_links(self):
        guest=self.app.test_client()
        body=guest.get('/').get_data(as_text=True)
        self.assertNotIn('Testevent',body)
        self.assertNotIn('/party/',body)
        self.assertNotIn('Eventoverzicht',body)

    def test_v05_ui_and_fallback_adapter(self):
        body=self.client.get('/party/music').get_data(as_text=True)
        self.assertIn('Jouw avond. Onder controle.',body)
        self.assertIn('Fallback-afspeellijsten',body)
        guest=self.app.test_client()
        self.assertIn('Wat wil jij horen?',guest.get('/party/').get_data(as_text=True))
        self.assertNotIn('Fallback-afspeellijsten',guest.get('/party/').get_data(as_text=True))
        r=self.client.post('/party/admin/add-playlist',headers={'X-CSRF-Token':self.csrf},json={'code':'PLtest'})
        self.assertEqual(r.status_code,200)
        lists=self.client.get('/party/admin/get-playlists').json
        self.assertEqual(lists['total_songs'],1)
        self.assertEqual(lists['playlists'][0]['name'],'Test fallback')
        self.assertEqual(self.client.get('/party/api/modern').status_code,200)
        self.assertIn('First',self.client.get('/party/search?query=Song').get_data(as_text=True))


class CrudTests(unittest.TestCase):
    setUp = fixtures.ManagementTests.setUp
    tearDown = fixtures.ManagementTests.tearDown
    post = fixtures.ManagementTests.post
    login = fixtures.ManagementTests.login
    event = fixtures.ManagementTests.event

    def row(self,table,where='1=1'):
        db=sqlite3.connect(self.config['SQLITE_TEST_PATH']); db.row_factory=sqlite3.Row
        try:return dict(db.execute('SELECT * FROM '+table+' WHERE '+where).fetchone())
        finally:db.close()

    def test_edit_and_delete_site(self):
        site=self.row('sites')
        self.assertEqual(self.post(self.client,'/manage',action='site_edit',target=site['id'],name='New site',domains='localhost\nsecond.test').status_code,302)
        self.assertEqual(self.client.get('/',base_url='http://second.test').status_code,200)
        self.assertEqual(self.post(self.client,'/manage',action='site_delete',target=site['id'],confirm='yes').status_code,302)

    def test_site_with_event_cannot_delete(self):
        self.event()
        self.assertEqual(self.post(self.client,'/manage',action='site_delete',target=self.row('sites')['id'],confirm='yes').status_code,409)

    def test_edit_user_password_and_delete(self):
        self.post(self.client,'/manage',action='user',username='test-user',password='long-test-password')
        user=self.row('users',"username='test-user'")
        self.assertEqual(self.post(self.client,'/manage',action='user_edit',target=user['id'],username='renamed',password='a-new-password-long').status_code,302)
        client=self.app.test_client()
        self.assertEqual(self.login(client,'renamed','a-new-password-long').status_code,302)
        self.assertEqual(self.post(self.client,'/manage',action='user_delete',target=user['id'],confirm='yes').status_code,302)
        self.assertEqual(client.get('/party/settings').status_code,302)

    def test_password_minimum_eight(self):
        self.assertEqual(self.post(self.client,'/manage',action='user',username='eight',password='12345678').status_code,302)
        self.assertEqual(self.post(self.client,'/manage',action='user',username='seven',password='1234567').status_code,400)
        uid=self.row('users',"username='eight'")['id']
        self.assertEqual(self.post(self.client,'/manage',action='user_edit',target=uid,username='eight',password='abcdefgh').status_code,302)
        client=self.app.test_client()
        self.assertEqual(self.login(client,'eight','abcdefgh').status_code,302)

    def test_revoke_and_named_membership(self):
        self.event()
        self.post(self.client,'/manage',action='user',username='named-user',password='long-test-password')
        uid=self.row('users',"username='named-user'")['id'];eid=self.row('events')['id']
        self.post(self.client,'/manage',action='grant',user=uid,scope=eid,role='event')
        body=self.client.get('/manage').get_data(as_text=True)
        self.assertIn('named-user</strong>',body)
        self.assertIn('Eventbeheerder: Site A / Testevent',body)
        client=self.app.test_client();self.login(client,'named-user','long-test-password')
        self.assertEqual(client.get('/party/settings').status_code,200)
        self.post(self.client,'/manage',action='revoke',user=uid,scope=eid,role='event')
        self.assertEqual(client.get('/party/settings').status_code,403)

    def test_global_admin_delete_prohibited(self):
        self.assertEqual(self.post(self.client,'/manage',action='user_delete',target=self.row('users')['id'],confirm='yes').status_code,409)

    def test_delete_event_removes_runtime(self):
        self.event();self.client.get('/party/state')
        eid=self.row('events')['id']
        self.assertEqual(self.post(self.client,'/manage',action='event_delete',target=eid,confirm='yes').status_code,302)
        self.assertEqual(self.client.get('/party/').status_code,404)

    def test_alpha1_schema_upgrade_retains_data(self):
        from app import create_app
        self.event()
        db=sqlite3.connect(self.config['SQLITE_TEST_PATH'])
        try:
            db.execute('DROP TABLE event_runtime')
            db.execute('UPDATE schema_version SET version=1')
            db.commit()
        finally:db.close()
        app=create_app(self.config)
        self.assertEqual(self.row('schema_version')['version'],3)
        client=app.test_client();self.login(client)
        self.assertIn(b'Testevent',client.get('/party/settings').data)

    def test_site_default_redirect_and_clear(self):
        self.event()
        eid=self.row('events')['id']
        self.assertEqual(self.post(self.client,'/site/settings',default_event=eid).status_code,302)
        guest=self.app.test_client()
        self.assertEqual(guest.get('/').location,'/party/')
        self.post(self.client,'/site/settings',default_event='')
        self.assertEqual(guest.get('/').status_code,200)
        self.assertNotIn('Testevent',guest.get('/').get_data(as_text=True))

    def test_site_default_rejects_foreign_event(self):
        self.event()
        self.post(self.client,'/manage',action='site',name='Other',domain='other.test')
        self.login(self.client,host='other.test',path='/site/login')
        self.assertEqual(self.post(self.client,'/site/settings',host='other.test',default_event=self.row('events')['id']).status_code,400)

    def test_site_manager_can_choose_default_event_manager_cannot(self):
        self.event()
        self.post(self.client,'/manage',action='user',username='manager',password='12345678')
        uid=self.row('users',"username='manager'")['id'];site=self.row('sites')['id'];eid=self.row('events')['id']
        self.post(self.client,'/manage',action='grant',user=uid,scope=eid,role='event')
        client=self.app.test_client();self.login(client,'manager','12345678',path='/site/login')
        self.assertEqual(client.get('/site/settings').status_code,403)
        self.post(self.client,'/manage',action='grant',user=uid,scope=site,role='site')
        self.assertEqual(self.post(client,'/site/settings',default_event=eid).status_code,302)
        self.assertEqual(self.app.test_client().get('/').location,'/party/')

    def test_deleting_default_event_clears_redirect(self):
        self.event();eid=self.row('events')['id']
        self.post(self.client,'/site/settings',default_event=eid)
        self.post(self.client,'/manage',action='event_delete',target=eid,confirm='yes')
        self.assertEqual(self.app.test_client().get('/').status_code,200)
