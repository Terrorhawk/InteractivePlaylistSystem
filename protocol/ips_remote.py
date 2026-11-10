"""V0.5-compatible protocol engine. Runtime storage is supplied by runtime.py.

The SQLite connection is request-local memory, never an on-disk event database.
"""
import hmac
import hashlib
import json
import math
import os
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time
import uuid

from flask import Blueprint, jsonify, redirect, render_template_string, request, session, url_for

VIDEO = re.compile(r'^[A-Za-z0-9_-]{11}$')
IDENTIFIER = re.compile(r'^[A-Za-z0-9_-]{1,96}$')
EVENT = re.compile(r'^[A-Za-z0-9_-]{1,64}$')
RESULTS = {'submitted', 'unsupported', 'expired', 'uncertain', 'history_full'}
STATES = {'unknown', 'playing', 'paused', 'ended', 'unexpected_track'}


def same_secret(actual, expected):
    return hmac.compare_digest(actual.encode('utf-8'), expected.encode('utf-8'))


class Conflict(Exception):
    pass


class Bridge:
    def __init__(self, namespace, database, token, event, clock=time.time):
        if not isinstance(token, str) or not 24 <= len(token) <= 256 or '\n' in token or '\r' in token:
            raise ValueError('IPS_PLAYER_TOKEN must contain 24 to 256 characters')
        if not EVENT.fullmatch(event):
            raise ValueError('Invalid IPS_EVENT_ID')
        self.ns, self.token, self.event, self.clock = namespace, token, event, clock
        self.lock = threading.RLock()
        Path(database).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(str(database), check_same_thread=False, timeout=10)
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS commands (
                id TEXT PRIMARY KEY, payload TEXT NOT NULL, expires REAL NOT NULL,
                result TEXT, player TEXT NOT NULL);
        ''')
        saved_event = self.get('event')
        if saved_event and saved_event != event:
            raise ValueError('Database belongs to another event; use a separate file')
        self.put('event', event)
        self.token = self.get('active_player_token') or self.token
        self.db.commit()

    def now(self):
        return self.clock() * 1000

    def get(self, key, default=None):
        row = self.db.execute('SELECT value FROM meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def put(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO meta VALUES (?,?)', (key, json.dumps(value)))

    def response(self, **values):
        return {'serverTime': self.now(), **values}

    def bind(self, player):
        if not IDENTIFIER.fullmatch(player):
            raise ValueError('Invalid player ID')
        current = self.get('player')
        if current and current != player:
            raise Conflict('another-player-is-paired')
        if not current:
            self.put('player', player)

    def expire(self):
        self.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL AND expires<=?", (self.now(),))
        self.db.execute('DELETE FROM commands WHERE result IS NOT NULL AND expires<?', (self.now()-86400000,))
        reservation = self.get('reservation')
        if reservation and not reservation.get('confirmed') and self.now()-reservation['created'] > 45000:
            self.put('auto', False)
            self.put('error', 'Het nummer is niet als spelend bevestigd; opnieuw starten via beheer.')
            self.put('reservation', None)

    def pending(self):
        self.expire()
        row = self.db.execute('SELECT payload FROM commands WHERE result IS NULL ORDER BY expires LIMIT 1').fetchone()
        return json.loads(row[0]) if row else None

    def issue(self, action, video_id=None, identifier=None):
        identifier = identifier or str(uuid.uuid4())
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('Invalid command ID')
        if action not in {'load', 'pause', 'resume', 'stop'} or (action == 'load' and not VIDEO.fullmatch(video_id or '')):
            raise ValueError('Invalid command')
        old = self.db.execute('SELECT payload FROM commands WHERE id=?', (identifier,)).fetchone()
        if old:
            payload = json.loads(old[0])
            if payload['action'] != action or payload.get('videoId') != video_id:
                raise Conflict('command-id-conflict')
            return payload
        player = self.get('player')
        if not player:
            raise Conflict('connect-the-ipad-first')
        if self.pending():
            raise Conflict('command-pending')
        sequence=self.get('control_sequence',0)+1
        epoch=self.get('control_epoch') or str(uuid.uuid4())
        self.put('control_sequence',sequence);self.put('control_epoch',epoch)
        payload = {'id': identifier, 'action': action, 'expiresAt': self.now()+15000,'sequence':sequence,'controlEpoch':epoch}
        if video_id:
            payload['videoId'] = video_id
            # Matched by video ID: never label outgoing A with reserved B's title.
            candidates=[self.get('reservation') or {},self.ns.get('current_track') or {}]+self.ns.get('queue',[])+self.ns.get('fallback_tracks',[])
            track=next((item for item in candidates if item.get('id')==video_id and item.get('title')),None)
            if track:payload['metadata']={'videoId':video_id,'title':str(track['title'])[:512]}
        self.db.execute('INSERT INTO commands VALUES (?,?,?,?,?)',
                        (identifier, json.dumps(payload), payload['expiresAt'], None, player))
        return payload

    def pick(self):
        candidates = [x for x in self.ns.get('queue', []) if x.get('votes', 0) > 0 and VIDEO.fullmatch(str(x.get('id', '')))]
        if candidates:
            # Freeze this candidate until playback is confirmed. Arrival during a
            # later polling request cannot replace the already reserved choice.
            item = max(candidates, key=lambda x: x.get('votes', 0))
            return {'id': item['id'], 'title': item.get('title', item['id']),
                    'origin': 'request', 'timestamp': item.get('timestamp')}
        tracks = self.ns.get('fallback_tracks') or []
        if tracks:
            index = self.ns.get('fallback_index', 0) % len(tracks)
            item = tracks[index]
            if VIDEO.fullmatch(str(item.get('id', ''))):
                return {'id': item['id'], 'title': item.get('title', item['id']), 'origin': 'fallback', 'index': index}
        return None

    def reserve(self, identifier=None, manual_id=None):
        if self.pending():
            raise Conflict('command-pending')
        old = self.get('reservation')
        if old and not old.get('confirmed'):
            raise Conflict('waiting-for-playback-confirmation')
        track = {'id': manual_id, 'title': manual_id, 'origin': 'manual'} if manual_id else self.pick()
        if not track:
            self.put('auto', False)
            raise Conflict('no-tracks-available')
        command = self.issue('load', track['id'], identifier)
        self.put('crossfade_context',None);self.put('crossfade_deferred',None)
        self.put('reservation', {**track, 'command': command['id'], 'created': self.now(), 'confirmed': False})
        self.put('error', None)
        return command

    def recover_crossfade(self, identifier, outgoing_ended=False):
        if not isinstance(identifier,str) or not IDENTIFIER.fullmatch(identifier):
            return False
        row=self.db.execute('SELECT payload FROM commands WHERE id=?',(identifier,)).fetchone()
        original=json.loads(row[0]) if row else {}
        if original.get('ordinaryFallbackHandled'):
            return True
        context=self.get('crossfade_context') or {}
        reservation=self.get('reservation') or {}
        if context.get('command')!=identifier or reservation.get('command')!=identifier or reservation.get('confirmed'):
            return False
        previous=context.get('previous') or {}
        if not previous.get('confirmed'):
            return False
        original["ordinaryFallbackHandled"]=True
        self.db.execute("UPDATE commands SET result=COALESCE(result,'expired'),payload=? WHERE id=?",(json.dumps(original),identifier))
        self.put('reservation',previous)
        self.put('crossfade_context',None)
        self.put('crossfade_deferred',dict(after=previous['id'],track=reservation))
        self.put('next_pick',reservation)
        self.put('error','Crossfade niet beschikbaar; gewone overgang wordt gebruikt.')
        observed=self.get('status') or {}
        ended=outgoing_ended or (observed.get('videoId')==previous['id'] and observed.get('playbackState')=='ended')
        if (context.get('immediate') or ended) and self.get('auto',False) and self.ns.get('IS_ONLINE',True) and not self.pending():
            self.load_deferred(previous['id'])
        return True

    def load_deferred(self, after):
        deferred=self.get('crossfade_deferred') or {}
        if deferred.get('after')!=after:
            return False
        track=deferred['track']
        command=self.issue('load',track['id'])
        self.put('reservation',{**track,'command':command['id'],'created':self.now(),'confirmed':False})
        self.put('crossfade_deferred',None);self.put('error',None)
        return True

    def confirm(self, status):
        reservation = self.get('reservation')
        if not reservation or reservation.get('confirmed') or status['videoId'] != reservation['id'] or status['playbackState'] != 'playing':
            return
        if reservation['origin'] == 'request':
            queue = self.ns.get('queue', [])
            for index, item in enumerate(queue):
                if item.get('id') == reservation['id'] and item.get('timestamp') == reservation.get('timestamp'):
                    queue.pop(index)
                    break
        elif reservation['origin'] == 'fallback':
            tracks = self.ns.get('fallback_tracks') or []
            if tracks:
                self.ns['fallback_index'] = (reservation['index']+1) % len(tracks)
        self.ns['current_track'] = {'id': reservation['id'], 'title': reservation['title']}
        stats = self.ns.get('stats_data')
        if isinstance(stats, dict):
            stats.setdefault('plays', []).append({'id': reservation['id'], 'title': reservation['title'], 'time': self.clock()})
            save = self.ns.get('save_stats')
            if callable(save):
                save()
        reservation['confirmed'] = True
        self.put('reservation', reservation)

    def validate_status(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid status')
        video = payload.get('videoId', '')
        state = payload.get('playbackState', 'unknown')
        if not isinstance(video, str) or (video and not VIDEO.fullmatch(video)) or state not in STATES:
            raise ValueError('Invalid status identity/state')
        for name in ['position', 'duration']:
            value = payload.get(name)
            if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
                raise ValueError('Invalid media time')
        ended = payload.get('endedEvent', '')
        ended_video = payload.get('endedVideoId', '')
        if not isinstance(ended, str) or (ended and not IDENTIFIER.fullmatch(ended)) or not isinstance(ended_video, str) or (ended_video and not VIDEO.fullmatch(ended_video)):
            raise ValueError('Invalid end event')
        status = {k: payload.get(k) for k in ['videoId', 'position', 'duration', 'playbackState', 'endedEvent', 'endedVideoId', 'hasPlayer']}
        status.update(appVersion=str(payload.get('appVersion', ''))[:32], adapterVersion=str(payload.get('adapterVersion', ''))[:32], receivedAt=self.now())
        name=payload.get('deviceName', '')
        kind=payload.get('deviceType', 'unknown')
        if not isinstance(name,str) or kind not in {'phone','tablet','unknown'}:
            raise ValueError('Invalid device information')
        status.update(crossfadeCapable=payload.get('crossfadeCapable') is True,crossfadeActive=payload.get('crossfadeActive') is True)
        status.update(deviceName=''.join(c for c in name if c.isprintable())[:80], deviceType=kind)
        return status

    def status(self, payload):
        status = self.validate_status(payload)
        ended, ended_video = status['endedEvent'], status['endedVideoId']
        self.put('status', status)
        self.expire()
        self.confirm(status)
        reservation = self.get('reservation')
        if self.ns.get('IS_ONLINE', True):
            self.put('offline_paused', False)
        if not self.ns.get('IS_ONLINE', True) and not self.get('offline_paused', False):
            self.put('auto', False)
            self.put('offline_paused', True)
            # Cancel undelivered loads before sending a live-show pause.
            self.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL")
            self.issue('pause')
        elif ended and ended != self.get('last_end') and reservation and reservation.get('confirmed') and ended_video == reservation['id']:
            if self.get('auto', False) and not self.pending():
                self.put('last_end', ended)
                self.put('reservation', None)
                try:
                    if not self.load_deferred(ended_video):
                        self.reserve()
                except Conflict as error:
                    self.put('error', str(error))
        # A failed overlap can leave an older client paused at the last audio
        # sample without an end event. Require two fresh stable tail reports;
        # only recover the already-reserved ordinary fallback, never a pause.
        deferred=self.get('crossfade_deferred') or {}
        reservation=self.get('reservation') or {}
        tail=(self.ns.get('IS_ONLINE',True) and self.get('auto',False) and not self.pending()
              and reservation.get('confirmed') and status.get('videoId')==reservation.get('id')
              and deferred.get('after')==reservation.get('id') and status.get('playbackState')=='paused'
              and (status.get('duration') or 0)>0
              and 0<=(status.get('duration') or 0)-(status.get('position') or 0)<=0.25)
        candidate=self.get('deferred_end_candidate') or {}
        if tail:
            if candidate.get('id')!=reservation['id'] or abs(candidate.get('position',-10)-(status.get('position') or 0))>0.05:
                self.put('deferred_end_candidate',dict(id=reservation['id'],position=status.get('position') or 0,at=self.now()))
            elif self.now()-candidate.get('at',self.now())>=2000:
                self.put('last_recovery',dict(kind='missing-crossfade-end',id=reservation['id'],at=self.now()))
                self.load_deferred(reservation['id'])
                self.put('deferred_end_candidate',None)
        else:
            self.put('deferred_end_candidate',None)
        return self.response(accepted=True)

    def ack(self, payload):
        self.expire()
        if not isinstance(payload, dict) or payload.get('result') not in RESULTS:
            raise ValueError('Invalid acknowledgement')
        row = self.db.execute('SELECT * FROM commands WHERE id=?', (payload.get('id'),)).fetchone()
        if not row:
            raise ValueError('Unknown command')
        result = row['result'] or payload['result']
        self.db.execute('UPDATE commands SET result=? WHERE id=? AND result IS NULL', (result, payload['id']))
        command=json.loads(row['payload'])
        if command.get('ordinaryFallbackHandled'):
            return self.response(result=result,ordinaryFallback=True)
        if result=='unsupported' and command.get('crossfade') and self.recover_crossfade(payload['id']):
            return self.response(result=result,ordinaryFallback=True)
        newest=self.db.execute('SELECT MAX(expires) FROM commands').fetchone()[0]
        if result!='submitted' and ((command.get('sequence',0)>0 and command['sequence']<self.get('control_sequence',0)) or (not command.get('sequence') and newest is not None and row['expires']<newest)):
            return self.response(result=result,superseded=True)
        if result != 'submitted':
            self.put('auto', False)
            self.put('error', 'Opdracht niet uitgevoerd: '+result)
            reservation = self.get('reservation')
            if reservation and reservation['command'] == payload['id'] and not reservation.get('confirmed'):
                self.put('reservation', None)
        return self.response(result=result)

    def command(self, payload):
        if not isinstance(payload, dict):
            raise ValueError('Invalid request')
        action, identifier = payload.get('action'), payload.get('id')
        if not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            raise ValueError('Missing command ID')
        if action == 'load' and not VIDEO.fullmatch(payload.get('videoId') or ''):
            raise ValueError('Invalid video ID')
        old = self.db.execute('SELECT payload FROM commands WHERE id=?', (identifier,)).fetchone()
        if old:
            previous = json.loads(old[0])
            expected = 'load' if action in {'start', 'next', 'load'} else action
            if previous['action'] != expected or (action == 'load' and previous.get('videoId') != payload.get('videoId')):
                raise Conflict('command-id-conflict')
            return self.response(command=previous, duplicate=True)
        if action in {'start', 'next', 'load'}:
            if not self.ns.get('IS_ONLINE', True):
                raise Conflict('live-show-mode-active')
            observed=self.get('status') or {}
            if action=='next' and observed.get('crossfadeActive') and self.now()-observed.get('receivedAt',0)<12000:
                raise Conflict('crossfade-already-active')
            cross=(action=='next' and observed.get('crossfadeCapable') and not observed.get('crossfadeActive')
                   and observed.get('playbackState')=='playing' and self.now()-observed.get('receivedAt',0)<12000)
            previous=self.get('reservation')
            command = self.reserve(identifier, payload.get('videoId') if action == 'load' else None)
            if cross:
                command.update(crossfade=True,crossfadeImmediate=True)
                self.put('crossfade_context',dict(command=command['id'],previous=previous,immediate=True))
                self.db.execute('UPDATE commands SET payload=? WHERE id=?',(json.dumps(command),command['id']))
            self.put('auto', action != 'load')
        elif action in {'pause', 'resume', 'stop'}:
            if action == 'resume' and not self.ns.get('IS_ONLINE', True):
                raise Conflict('live-show-mode-active')
            if action in {'pause', 'stop'}:
                # Safety commands supersede an undelivered command.
                self.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL")
            command = self.issue(action, identifier=identifier)
            self.put('auto', action == 'resume' and bool(self.get('reservation')))
            if action == 'stop':
                self.put('reservation', None)
        else:
            raise ValueError('Unsupported action')
        return self.response(command=command)

    def snapshot(self):
        self.expire()
        status = self.get('status')
        return self.response(eventId=self.event, status=status, paired=bool(self.get('player')),
            connected=bool(status and self.now()-status['receivedAt']<12000),
            auto=self.get('auto', False), pending=self.pending(), reservation=self.get('reservation'),
            error=self.get('error'), online=self.ns.get('IS_ONLINE', True))

