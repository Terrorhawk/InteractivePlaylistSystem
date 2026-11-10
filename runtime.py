"""Event transactions and compatibility adapter for the existing v6 player.

MariaDB is authoritative. The old protocol's SQLite statements execute only in
request-local memory; its entire state is committed under the event row lock.
No SQLite file or process-global event state exists in this server.
"""
from functools import wraps
import hashlib
import json
import math
import random
import secrets
import time
import uuid
from urllib.parse import urlsplit

from flask import Blueprint, abort, g, jsonify, redirect, render_template, request, session
from werkzeug.local import LocalProxy
from protocol.ips_remote import Bridge, Conflict, VIDEO, same_secret
from protocol.player_v6 import install as install_v6


def track_artwork(track):
    for thumbnail in reversed(track.get('thumbnails') or []):
        if not isinstance(thumbnail, dict):
            continue
        url = thumbnail.get('url')
        if not isinstance(url, str):
            continue
        try:
            parts = urlsplit(url)
            if (parts.scheme == 'https' and not parts.username and not parts.password
                    and parts.hostname and parts.hostname.endswith(('.googleusercontent.com', '.ytimg.com'))):
                return url
        except ValueError:
            continue
    return None


class EventBridge(Bridge):
    def artwork_for(self, video):
        tracks = [self.ns.get('current_track') or {}, *self.ns['queue'], *self.ns['fallback_tracks']]
        return next((track['artwork'] for track in tracks if track.get('id') == video and track.get('artwork')),
                    self.ns['catalogue'].get(video, {}).get('artwork'))

    def confirm(self, status):
        reservation=self.get('reservation') or {}
        if reservation.get('origin')=='fallback' and not reservation.get('confirmed'):
            for index,track in enumerate(self.ns['fallback_tracks']):
                if track['id']==reservation['id']:
                    self.put('reservation',{**reservation,'index':index})
                    break
        artwork = None
        if reservation and not reservation.get('confirmed') and status.get('videoId') == reservation['id']:
            artwork = self.artwork_for(reservation['id'])
        result = super().confirm(status)
        if artwork and status.get('playbackState') == 'playing':
            self.ns['current_track']['artwork'] = artwork
        return result

    def ranked(self):
        now = self.clock()
        return sorted((x for x in self.ns['queue'] if x['votes'] > 0 and now-x['timestamp'] >= self.ns['settings']['selection_delay_seconds']),
                      key=lambda x: (-(x['votes']+int((now-x['timestamp'])/(60*self.ns['settings']['priority_interval_minutes']))), x['timestamp'], x['id']))

    def locked(self, item):
        reservation = self.get('reservation') or {}
        preview = self.get('next_pick') or {}
        return (reservation.get('id') == item['id'] and not reservation.get('confirmed')) or preview.get('id') == item['id']

    def cleanup(self):
        now = self.clock()
        self.ns['queue'] = [x for x in self.ns['queue'] if self.locked(x) or x['votes'] > 0 or now-x.get('suppressed_at', x['timestamp']) < 60*self.ns['settings']['nonpositive_expiry_minutes']]
        # Quota records are bounded by the largest configurable window (one day).
        self.ns['requests'] = [x for x in self.ns['requests'] if x['at'] > now-86400]
        self.ns['votes'] = [x for x in self.ns['votes'] if x['at'] > now-86400 or any(q['key'] == x['track'] for q in self.ns['queue'])]
        self.ns['stats_data']['plays'] = [x for x in self.ns['stats_data']['plays'] if x['time'] > now-604800]

    def pick(self):
        frozen = self.get('next_pick')
        if frozen and (frozen['origin'] == 'fallback' or any(x['id'] == frozen['id'] and x['timestamp'] == frozen.get('timestamp') for x in self.ns['queue'])):
            if frozen['origin']=='fallback':
                for index,track in enumerate(self.ns['fallback_tracks']):
                    if track['id']==frozen['id']:
                        frozen={**frozen,'index':index}
                        break
            return frozen
        ranked = self.ranked()
        if ranked:
            x = ranked[0]
            return dict(id=x['id'], title=x['title'], timestamp=x['timestamp'], origin='request')
        tracks = self.ns['fallback_tracks']
        if tracks:
            index = self.ns['fallback_index'] % len(tracks)
            return dict(id=tracks[index]['id'], title=tracks[index]['title'], index=index, origin='fallback')

    def status(self, payload):
        status = self.validate_status(payload)
        before = self.get('reservation') or {}
        if (status['playbackState'] == 'playing' and status['videoId'] == before.get('id') and before.get('confirmed')
                and (status.get('duration') or 0) > 0 and 0 <= status['duration']-(status.get('position') or 0) <= self.ns['settings']['freeze_seconds'] and not self.get('next_pick')):
            choice = self.pick()
            if choice:
                self.put('next_pick', choice)
        result = super().status(payload)
        after = self.get('reservation') or {}
        if after.get('confirmed') and (after.get('command') != before.get('command') or not before.get('confirmed')):
            self.put('next_pick', None)
        return result

    def blocked_until(self, video):
        played = [p['time'] for p in self.ns['stats_data']['plays'] if p['id'] == video]
        return max(played, default=0) + self.ns['settings']['repeat_cooldown_minutes']*60 if played else 0


def sync_fallback(b, shuffle=False):
    """Reconcile metadata without recreating the durable order on every poll."""
    ns=b.ns
    catalogue={t['id']:t for p in ns['playlists'] for t in p['tracks'] if 0<t.get('duration',0)<=ns['settings']['fallback_max_minutes']*60}
    catalogue=dict(list(catalogue.items())[:500])
    old=ns['fallback_tracks']
    next_id=old[ns['fallback_index']%len(old)]['id'] if old else None
    order=list(dict.fromkeys(t['id'] for t in old if t['id'] in catalogue))
    additions=[video for video in catalogue if video not in order]
    random.shuffle(additions)
    order+=additions
    if shuffle or not ns.get('fallback_order_version'):
        random.shuffle(order)
        # An issued or frozen successor retains priority across a shuffle.
        reservation=b.get('reservation') or {}
        preview=b.get('next_pick') or {}
        pinned=reservation if reservation.get('origin')=='fallback' and not reservation.get('confirmed') else preview
        pinned_id=pinned.get('id') if pinned.get('origin')=='fallback' else None
        if pinned_id in order:
            order.remove(pinned_id);order.insert(0,pinned_id)
        elif len(order)>1 and order[0]==(ns.get('current_track') or {}).get('id'):
            order[0],order[1]=order[1],order[0]
        ns['fallback_index']=0
    else:
        ns['fallback_index']=order.index(next_id) if next_id in order else 0
    ns['fallback_tracks']=[catalogue[video] for video in order]
    ns['fallback_order_version']=1


def install_runtime(app, connect, execute, query, allowed, page):
    api = Blueprint('event_player', __name__, url_prefix='/<slug>')
    bridge = LocalProxy(lambda: g.bridge)
    paths = {'music', 'monitor', 'search', 'add', 'vote', 'control', 'fallback', 'adjust', 'remove', 'state', 'ips-pair', 'admin', 'upvote', 'downvote'}

    @api.url_value_preprocessor
    def consume_slug(endpoint, values):
        if values:
            values.pop('slug', None)

    @app.before_request
    def load_event():
        parts = request.path.strip('/').split('/')
        if not (parts[0] and len(parts) == 1 and request.path.endswith('/') or len(parts) > 1 and (parts[1] in paths or parts[1] == 'api')):
            return
        slug = parts[0]
        if len(parts)>1 and parts[1]=='admin' and len(parts)==2:
            return  # Settings/setup handler, or redirect to music for an existing event.
        if not g.site:
            abort(404)
        # Discard the initial read snapshot before acquiring a current row lock.
        g.db.rollback()
        if app.config['SQLITE_TEST_PATH']:
            execute(g.db, 'BEGIN IMMEDIATE')
        sql = 'SELECT * FROM events WHERE site_id=? AND slug=?'
        event = query(sql + ('' if app.config['SQLITE_TEST_PATH'] else ' FOR UPDATE'), (g.site['id'], slug), one=True)
        if not event:
            abort(404)
        g.event = event
        stored = query('SELECT payload FROM event_runtime WHERE event_id=?' + ('' if app.config['SQLITE_TEST_PATH'] else ' FOR UPDATE'), (event['id'],), one=True)
        saved = json.loads(stored['payload']) if stored else {}
        ns = saved.get('namespace') or dict(queue=[], fallback_tracks=[], fallback_index=0, current_track=None, IS_ONLINE=True,
                                          visitor_offline=False, stats_data={'plays': []}, votes=[], requests=[], catalogue={}, adjustments={}, playlists=[])
        ns['settings'] = json.loads(event['settings'])
        b = EventBridge(ns, ':memory:', saved.get('token') or secrets.token_urlsafe(32), event['id'], clock=app.config.get('CLOCK', time.time))
        for key, value in saved.get('meta', {}).items():
            b.put(key, value)
        for row in saved.get('commands', []):
            b.db.execute('INSERT INTO commands VALUES (?,?,?,?,?)', row)
        b.token = b.get('active_player_token') or b.token
        sync_fallback(b)
        b.cleanup()
        g.bridge = b
        g.runtime_exists = bool(stored)
        session.setdefault('visitor', secrets.token_urlsafe(24))

    @app.after_request
    def persist_event(response):
        b = getattr(g, 'bridge', None)
        if b and response.status_code < 400 and not getattr(g,'runtime_failed',False):
            state = dict(namespace=b.ns, token=b.token,
                         meta={r['key']: json.loads(r['value']) for r in b.db.execute('SELECT * FROM meta')},
                         commands=[list(r) for r in b.db.execute('SELECT * FROM commands')])
            data = json.dumps(state, ensure_ascii=False)
            if g.runtime_exists:
                execute(g.db, 'UPDATE event_runtime SET payload=? WHERE event_id=?', (data, g.event['id']))
            else:
                execute(g.db, 'INSERT INTO event_runtime VALUES (?,?)', (g.event['id'], data))
            g.db.commit()
        return response

    @app.teardown_request
    def close_bridge(error):
        b = g.pop('bridge', None)
        if b:
            b.db.close()

    def admin_required():
        if not allowed(event=g.event):
            abort(403)

    def run(function):
        try:
            return jsonify(function())
        except Conflict as exc:
            return jsonify(error=str(exc)), 409
        except (ValueError, TypeError, KeyError) as exc:
            return jsonify(error=str(exc)), 400

    @api.before_request
    def player_auth():
        if request.endpoint in {'event_player.manage', 'event_player.manage_command', 'event_player.make_pair'}:
            admin_required()
            if request.method == 'POST' and not same_secret(request.headers.get('X-CSRF-Token', ''), session.get('csrf', '!')):
                abort(403)
            return
        if request.endpoint == 'event_player.claim_pair':
            return
        supplied = request.headers.get('Authorization', '')
        if not same_secret(supplied, 'Bearer '+bridge.token):
            digest = hashlib.sha256(supplied[7:].encode()).hexdigest() if supplied.startswith('Bearer ') else ''
            revoked = bridge.get('revoked_player_tokens', {})
            if digest in revoked and request.path.endswith(('/sync', '/command')):
                return jsonify(bridge.response(command=dict(id=revoked[digest], action='stop', expiresAt=bridge.now()+15000),
                    config=dict(version='revoked', tracks=[], online=False, automatic=False, nextIndex=0), revoked=True, activePlayer=False, reconnectAccepted=True))
            return jsonify(error='unauthorized'), 401
        if request.headers.get('X-IPS-Event') != bridge.event:
            return jsonify(error='event-mismatch'), 403
        try:
            bridge.bind(request.headers.get('X-IPS-Player-ID', ''))
        except ValueError as exc:
            return jsonify(error=str(exc)), 400
        except Conflict as exc:
            return jsonify(error=str(exc), activePlayer=False), 409

    @api.get('/api/ips/v1/manage')
    def manage():
        scheme = 'https' if app.config['SESSION_COOKIE_SECURE'] else request.scheme
        return jsonify({**bridge.snapshot(), 'pairingToken': bridge.token, 'serverUrl': scheme+'://'+request.host+'/'+g.event['slug']})

    @api.post('/api/ips/v1/manage/command')
    def manage_command():
        return run(lambda: bridge.command(request.get_json(silent=True)))

    @api.post('/api/ips/v1/status')
    def status():
        return run(lambda: bridge.status(request.get_json(silent=True)))

    @api.get('/api/ips/v1/command')
    def command():
        return run(lambda: bridge.response(command=bridge.pending()))

    @api.post('/api/ips/v1/ack')
    def ack():
        return run(lambda: bridge.ack(request.get_json(silent=True)))

    install_v6(api, bridge, run)
    app.register_blueprint(api)

    def music(slug):
        is_admin = allowed(event=g.event)
        return render_template('music.html', site_admin=allowed(site=g.event['site_id']), event_name=g.event['name'], event=g.event, admin=is_admin, settings=bridge.ns['settings'],
                               csrf=session['csrf'], offline=bridge.ns['visitor_offline'], results=[], message=session.pop('jukebox_error',None))
    app.add_url_rule('/<slug>/music', 'music', music)
    app.add_url_rule('/<slug>/ips-pair', 'pair_landing', lambda slug: page('Player koppelen', message='Scan deze URL met de IPS-app. De eenmalige code staat in de URL-fragment.'))

    @app.get('/<slug>/monitor')
    def monitor_page(slug):
        return render_template('monitor.html', event_name=g.event['name'], slug=slug)

    @app.get('/<slug>/monitor/state', defaults={'monitor': True})
    @app.get('/<slug>/state')
    def state(slug, monitor=False):
        admin = not monitor and allowed(event=g.event)
        if bridge.ns['visitor_offline'] and not admin:
            return jsonify(offline=True,offlineText=bridge.ns['settings'].get('offline_text') or 'Jukebox offline op dit moment')
        snapshot = bridge.snapshot()
        player = dict(snapshot['status'] or {})
        if not admin:
            for key in ('deviceName', 'deviceType', 'appVersion', 'adapterVersion'):
                player.pop(key, None)
        ordered_queue = sorted(bridge.ns['queue'], key=lambda item: (-item['votes'], item['timestamp']))
        tracks = bridge.ns['fallback_tracks']
        offset = bridge.ns['fallback_index']
        preview = [tracks[(offset+i) % len(tracks)] for i in range(min(len(tracks), bridge.ns['settings']['fallback_preview_count']))] if tracks else []
        if monitor:
            current = bridge.ns['current_track'] or {}
            known = snapshot['connected'] and bool(current) and current.get('id') == player.get('videoId')
            playing = known and snapshot['online'] and player.get('playbackState') == 'playing'
            position = player.get('position') or 0
            if playing:
                position += max(0, (bridge.now() - player['receivedAt']) / 1000)
            return jsonify(online=snapshot['online'], connected=snapshot['connected'],
                           title=current.get('title') if known else None,
                           artwork=bridge.artwork_for(current['id']) if known else None,
                           position=position if known else 0,
                           duration=player.get('duration') if known else None, playing=playing,
                           queue=[dict(title=item['title'], votes=item['votes'], duration=item.get('duration'))
                                  for item in ordered_queue],
                           fallback=[dict(title=track['title'], duration=track.get('duration')) for track in preview])
        queue = []
        for item in ordered_queue:
            queue.append({k:item[k] for k in ('id', 'title', 'votes')} | dict(mine=item['owner'] == session['visitor'], locked=bridge.locked(item), voted=any(v['track']==item['key'] and v['visitor']==session['visitor'] for v in bridge.ns['votes']),
                expiresAt=(item.get('suppressed_at', item['timestamp'])+60*bridge.ns['settings']['nonpositive_expiry_minutes'])*1000 if item['votes'] <= 0 and not bridge.locked(item) else None))
        return jsonify(canDownvote=not any(x['owner']==session['visitor'] for x in bridge.ns['queue']), offline=bridge.ns['visitor_offline'], online=bridge.ns['IS_ONLINE'], queue=queue, player=player,
                       connected=snapshot['connected'], current=bridge.ns['current_track'], next=bridge.get('next_pick'), fallback=preview,
                       fallbackActive=not bool(bridge.ranked()), automatic=snapshot['auto'], error=snapshot['error'] if admin else None,
                       playlists=bridge.ns['playlists'] if admin else [], fallbackCount=len(tracks), fallbackDuration=sum(x.get('duration',0) for x in tracks))

    def catalogue():
        factory = app.config.get('MUSIC_FACTORY')
        if factory:
            return factory()
        from ytmusicapi import YTMusic
        import requests
        class TimedSession(requests.Session):
            def request(self, *args, **kwargs):
                kwargs.setdefault('timeout', 12)
                return super().request(*args, **kwargs)
        return YTMusic(requests_session=TimedSession(), language='nl')

    @app.get('/<slug>/search')
    def search(slug):
        if bridge.ns['visitor_offline'] and not allowed(event=g.event):
            abort(409, 'Bezoekerspagina is offline.')
        query_text = request.args.get('q', request.args.get('query','')).strip()[:120]
        try:
            found = catalogue().search(query_text, filter='songs', limit=15) if query_text else []
            results = []
            for track in found:
                video = track.get('videoId') or ''
                if not VIDEO.fullmatch(video):
                    continue
                artist = ', '.join(a.get('name', '') for a in track.get('artists', []) if a.get('id'))
                title = (str(track.get('title', 'Onbekend'))+' — '+artist).strip(' —')[:240]
                duration = track.get('duration_seconds')
                if not isinstance(duration, int) or duration <= 0:
                    parts = str(track.get('duration') or '').split(':')
                    duration = (sum(int(part) * 60**i for i, part in enumerate(reversed(parts)))
                                if 2 <= len(parts) <= 3 and all(p.isascii() and p.isdigit() and len(p) <= 3 for p in parts)
                                else None)
                bridge.ns['catalogue'][video] = dict(title=title, at=bridge.clock(), duration=duration,
                                                     artwork=track_artwork(track))
                blocked = bridge.blocked_until(video)
                results.append(dict(id=video, title=str(track.get('title','Onbekend'))[:240], artist=artist, duration=track.get('duration',''), blockedUntil=blocked*1000,
                                    blocked_until=blocked*1000, cooldown=max(0, math.ceil((blocked-bridge.clock())/60))))
            bridge.ns['catalogue'] = {k:v for k,v in bridge.ns['catalogue'].items() if v['at'] > bridge.clock()-86400}
            if 'query' in request.args:
                return render_template('music.html',event_name=g.event['name'],event=g.event,admin=allowed(event=g.event),settings=bridge.ns['settings'],csrf=session['csrf'],offline=bridge.ns['visitor_offline'],results=results,query=query_text)
            return jsonify(results=results)
        except Exception:
            app.logger.exception('Catalogus zoeken mislukt')
            return jsonify(error='Zoeken is tijdelijk niet beschikbaar.'), 502

    def visitor_mutation():
        if bridge.ns['visitor_offline']:
            abort(409, 'Bezoekerspagina is offline.')

    def cast_vote(video, direction):
        item = next((x for x in bridge.ns['queue'] if x['id'] == video), None)
        if not item or bridge.locked(item):
            abort(409, 'Verzoek ontbreekt of staat al vast.')
        visitor, now = session['visitor'], bridge.clock()
        records = bridge.ns['votes']
        if any(x['visitor'] == visitor and x['track'] == item['key'] for x in records):
            abort(409, 'Je hebt al gestemd op dit verzoek.')
        if direction < 0:
            if any(x['owner'] == visitor for x in bridge.ns['queue']):
                abort(409, 'Met een eigen open verzoek kun je niet downvoten.')
            rule = bridge.ns['settings']
            used = sum(x['visitor'] == visitor and x['direction'] == -1 and x['at'] > now-rule['downvote_window_minutes']*60 for x in records)
            if used >= rule['downvote_limit']:
                abort(409, f"Maximaal {rule['downvote_limit']} downvotes per {rule['downvote_window_minutes']} minuten.")
        records.append(dict(visitor=visitor, track=item['key'], direction=direction, at=now, active=True))
        item['votes'] += direction
        if item['votes'] <= 0:
            item.setdefault('suppressed_at', now)
        else:
            item.pop('suppressed_at', None)

    @app.post('/<slug>/add')
    def add(slug):
        visitor_mutation()
        video = request.form.get('video', request.form.get('video_id', ''))
        if any(x['id'] == video for x in bridge.ns['queue']):
            cast_vote(video, 1)
        else:
            now, visitor, rule = bridge.clock(), session['visitor'], bridge.ns['settings']
            if sum(x['visitor'] == visitor and x['at'] > now-rule['request_window_minutes']*60 for x in bridge.ns['requests']) >= rule['request_limit']:
                abort(409, f"Maximaal {rule['request_limit']} aanvragen per {rule['request_window_minutes']} minuten.")
            record = bridge.ns['catalogue'].get(video)
            if not record or bridge.blocked_until(video) > now:
                abort(409, 'Zoek dit nummer eerst of wacht tot de herhaalblokkade voorbij is.')
            for item in bridge.ns['queue']:
                for vote in bridge.ns['votes']:
                    if vote['visitor'] == visitor and vote['track'] == item['key'] and vote['direction'] == -1 and vote['active']:
                        # A frozen successor is not re-ranked, but its score can reflect the reversal.
                        item['votes'] += 1
                        vote['active'] = False
                        if item['votes'] > 0:
                            item.pop('suppressed_at', None)
            key = str(uuid.uuid4())
            bridge.ns['queue'].append(dict(id=video, key=key, title=record['title'], votes=1, owner=visitor, timestamp=now,
                                           duration=record.get('duration'), artwork=record.get('artwork')))
            bridge.ns['votes'].append(dict(visitor=visitor, track=key, direction=1, at=now, active=True))
            bridge.ns['requests'].append(dict(visitor=visitor, at=now))
        if request.form.get('_return') == 'page':
            return redirect('/'+slug+'/')
        return jsonify(ok=True)

    # V0.5 UI routes. They share the same event transaction and authorization;
    # only the URL/payload shape is adapted, not the business rules.
    from werkzeug.datastructures import MultiDict

    def adapt(view, mapping):
        def handler(slug, **kwargs):
            data=request.get_json(silent=True) or {}
            request.form=MultiDict({key:str(value) for key,value in mapping(data,kwargs).items()})
            return view(slug)
        return handler

    def old_vote(slug, video, direction):
        visitor_mutation()
        cast_vote(video,direction)
        return redirect('/'+slug+'/')

    def old_playlists(slug):
        admin_required()
        return jsonify(playlists=[dict(code=p['id'],name=p['name']) for p in bridge.ns['playlists']],
                       total_songs=len(bridge.ns['fallback_tracks']),total_duration=sum(t.get('duration',0) for t in bridge.ns['fallback_tracks']))

    # Registration is deferred until all target view functions are defined.
    def register_legacy_ui():
        app.add_url_rule('/<slug>/api/modern','legacy_state',state)
        app.add_url_rule('/<slug>/upvote/<video>','legacy_upvote',lambda slug,video:old_vote(slug,video,1),methods=['POST'])
        app.add_url_rule('/<slug>/downvote/<video>','legacy_downvote',lambda slug,video:old_vote(slug,video,-1),methods=['POST'])
        app.add_url_rule('/<slug>/admin/get-playlists','legacy_playlists',old_playlists)
        routes = [
            ('toggle-offline',control,lambda d,k:dict(action='offline')),
            ('toggle-status',control,lambda d,k:dict(action='liveshow')),
            ('add-playlist',fallback,lambda d,k:dict(action='add',playlist=d.get('code',''))),
            ('remove-playlist',fallback,lambda d,k:dict(action='remove',playlist=d.get('code',''))),
            ('shuffle-playlists',fallback,lambda d,k:dict(action='shuffle')),
            ('refresh-playlist-artwork',fallback,lambda d,k:dict(action='refresh-artwork',playlist=d.get('code',''))),
            ('adjust-votes/<video>',adjust,lambda d,k:dict(video=k['video'],delta=d.get('delta'),operation=d.get('operationId',''))),
            ('remove-queue-item/<video>',remove,lambda d,k:dict(video=k['video'])),
        ]
        for index,(path,view,mapping) in enumerate(routes):
            app.add_url_rule('/<slug>/admin/'+path,'legacy_mutation_'+str(index),adapt(view,mapping),methods=['POST'])

    @app.post('/<slug>/vote')
    def vote(slug):
        visitor_mutation()
        if request.form.get('direction') not in ('1', '-1'):
            abort(400)
        cast_vote(request.form.get('video'), int(request.form['direction']))
        return jsonify(ok=True)

    @app.post('/<slug>/adjust')
    def adjust(slug):
        admin_required()
        operation = request.form.get('operation', '')
        video, delta = request.form.get('video'), request.form.get('delta')
        if delta not in ('100', '-100') or not operation or len(operation) > 96:
            abort(400)
        old = bridge.ns['adjustments'].get(operation)
        if old:
            if old != [video, delta]:
                abort(409)
            return jsonify(ok=True, duplicate=True)
        item = next((x for x in bridge.ns['queue'] if x['id'] == video), None)
        if not item:
            abort(404)
        reservation = bridge.get('reservation') or {}
        if reservation.get('id') == video and not reservation.get('confirmed'):
            abort(409, 'Nummer is al naar de player gestuurd.')
        bridge.put('next_pick', None)
        item['votes'] += int(delta)
        if item['votes'] <= 0:
            item['suppressed_at'] = bridge.clock()
        else:
            item.pop('suppressed_at', None)
        bridge.ns['adjustments'][operation] = [video, delta]
        return jsonify(ok=True)

    @app.post('/<slug>/remove')
    def remove(slug):
        admin_required()
        video = request.form.get('video')
        reservation = bridge.get('reservation') or {}
        if reservation.get('id') == video and not reservation.get('confirmed'):
            abort(409, 'Nummer is al naar de player gestuurd.')
        bridge.ns['queue'] = [x for x in bridge.ns['queue'] if x['id'] != video]
        if (bridge.get('next_pick') or {}).get('id') == video:
            bridge.put('next_pick', None)
        return jsonify(ok=True)

    @app.post('/<slug>/control')
    def control(slug):
        admin_required()
        action = request.form.get('action')
        if action == 'offline':
            bridge.ns['visitor_offline'] = not bridge.ns['visitor_offline']
        elif action == 'liveshow':
            bridge.ns['IS_ONLINE'] = not bridge.ns['IS_ONLINE']
            bridge.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL")
            bridge.put('next_pick', None)
            if not bridge.ns['IS_ONLINE']:
                bridge.put('auto', False)
                if bridge.get('player'):
                    bridge.issue('pause')
                bridge.put('offline_paused', True)
            elif bridge.get('player'):
                observed = bridge.get('status') or {}
                reservation = bridge.get('reservation') or {}
                if reservation.get('confirmed') and observed.get('playbackState') != 'ended':
                    bridge.issue('resume')
                    bridge.put('auto', True)
                else:
                    bridge.put('reservation', None)
                    try:
                        bridge.reserve()
                        bridge.put('auto', True)
                    except Conflict as exc:
                        bridge.put('error', str(exc))
        else:
            abort(400)
        return jsonify(ok=True)

    @app.post('/<slug>/fallback')
    def fallback(slug):
        admin_required()
        action = request.form.get('action')
        if action == 'shuffle':
            sync_fallback(bridge,shuffle=True)
            return jsonify(ok=True)
        code = request.form.get('playlist', '').strip()[:200]
        if action == 'refresh-artwork':
            playlist = next((p for p in bridge.ns['playlists'] if p['id'] == code), None)
            if playlist is None:
                abort(404, 'Afspeellijst niet gevonden.')
            try:
                fetched = catalogue().get_playlist(code, limit=500)
            except Exception:
                app.logger.exception('Ophalen van hoesgegevens mislukt')
                return jsonify(error='Hoesgegevens ophalen lukt nu niet.'), 502
            artwork = {track.get('videoId'): track_artwork(track) for track in fetched.get('tracks', [])}
            for track in playlist['tracks']:
                if artwork.get(track['id']):
                    track['artwork'] = artwork[track['id']]
            sync_fallback(bridge)
            return jsonify(updated=sum(bool(artwork.get(track['id'])) for track in playlist['tracks']))
        if action == 'remove':
            bridge.ns['playlists'] = [x for x in bridge.ns['playlists'] if x['id'] != code]
        else:
            try:
                # Accept either the catalogue playlist ID or its public URL.
                from urllib.parse import urlsplit, parse_qs
                if code.startswith(('https://', 'http://')):
                    parameters = parse_qs(urlsplit(code).query)
                    code = parameters.get('list', [''])[0]
                    if not code and parameters.get('v'):
                        abort(400, 'Dit is een link naar één nummer, niet naar een playlist. Open de playlist zelf en kopieer de playlistlink met list= in het adres.')
                if not code or any(not (c.isalnum() or c in '_-') for c in code):
                    abort(400, 'Ongeldige playlist-ID.')
                playlist = catalogue().get_playlist(code, limit=500)
                tracks = []
                for x in playlist.get('tracks', []):
                    duration = x.get('duration_seconds')
                    if duration is None:
                        duration = 0
                        for part in str(x.get('duration', '0')).split(':'):
                            duration = duration*60+int(part)
                    if VIDEO.fullmatch(x.get('videoId') or '') and 0 < duration <= bridge.ns['settings']['fallback_max_minutes']*60:
                        tracks.append(dict(id=x['videoId'], title=str(x.get('title', x['videoId']))[:240],
                                           duration=duration, artwork=track_artwork(x)))
                bridge.ns['playlists'] = [x for x in bridge.ns['playlists'] if x['id'] != code] + [dict(id=code, name=playlist.get('title', code), tracks=tracks)]
            except (ValueError, TypeError):
                return jsonify(error='Playlist bevat ongeldige metadata.'), 502
        sync_fallback(bridge,shuffle=True)
        if (bridge.get('next_pick') or {}).get('origin') == 'fallback' and not any(t['id']==bridge.get('next_pick')['id'] for t in bridge.ns['fallback_tracks']):
            bridge.put('next_pick', None)
        return jsonify(ok=True)

    register_legacy_ui()
