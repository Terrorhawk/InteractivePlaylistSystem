"""Optional v6 protocol. v5 routes and pairing secrets remain compatible."""
import hashlib
import json
import secrets
import io
from pathlib import Path
import sys
from urllib.parse import urlsplit

import qrcode
from qrcode.image.svg import SvgPathImage

from flask import request, g, current_app
from .ips_remote import Conflict, IDENTIFIER, VIDEO, same_secret


def install(api, bridge, run):
    def configuration(version=None):
        # Preserve the server's shuffled order; no catalogue or browser credentials.
        tracks = [{"id": str(t.get("id", "")), "title": str(t.get("title", ""))[:180]}
                  for t in bridge.ns.get("fallback_tracks", [])
                  if VIDEO.fullmatch(str(t.get("id", "")))][:500]
        digest = hashlib.sha256(json.dumps(tracks, sort_keys=True).encode()).hexdigest()
        result = dict(version=digest, eventId=bridge.event,
                      online=bool(bridge.ns.get("IS_ONLINE", True)),
                      automatic=bool(bridge.get("auto", False)),
                      nextIndex=bridge.ns.get("fallback_index", 0) % max(1, len(tracks)))
        if version != digest:
            result["tracks"] = tracks
        return result

    def reconcile(payload):
        # A local fallback is never evidence that a visitor request was played.
        if not payload.get("offlineFallback"):
            return
        video = payload.get("videoId", "")
        if not VIDEO.fullmatch(video or ""):
            raise ValueError("invalid-fallback-track")
        # Discard stale loads, retaining safety pauses and every unconfirmed request.
        bridge.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL AND json_extract(payload,'$.action')='load'")
        old = bridge.get("reservation")
        if old and old.get("id") == video and not old.get("confirmed"):
            # Do not consume even a same-ID request: it was selected locally.
            bridge.put("reservation", None)
        bridge.put("reservation", dict(id=video, title=payload.get("fallbackTitle", video)[:180],
                   origin="local-fallback", confirmed=True, created=bridge.now(), command="local"))
        bridge.put('next_pick', None)
        bridge.ns["current_track"] = dict(id=video, title=payload.get("fallbackTitle", video)[:180])
        # Move the shared fallback cursor past this item when still in the list.
        for index, track in enumerate(bridge.ns.get("fallback_tracks", [])):
            if track.get("id") == video:
                bridge.ns["fallback_index"] = (index+1) % len(bridge.ns["fallback_tracks"])
                break

    def recover(payload):
        fault = payload.get("fault")
        identifier = payload.get("faultId")
        if fault not in {"stalled", "not-started"} or not isinstance(identifier, str) or not IDENTIFIER.fullmatch(identifier):
            return
        reservation = bridge.get("reservation")
        if not reservation or payload.get("desiredVideoId") != reservation["id"]:
            return
        if not bridge.ns.get("IS_ONLINE", True) or not bridge.get("auto", False):
            return
        # The same observation delivered twice must not issue two commands.
        history = bridge.get("recovery", {})
        if history.get("command") != reservation["command"]:
            history = dict(command=reservation["command"], attempts=0, lastAt=0)
        if history.get("faultId") == identifier or bridge.now()-history["lastAt"] < 35000:
            return
        if bridge.pending():
            return
        if history["attempts"] >= 2:
            bridge.put("auto", False)
            bridge.put("error", "Afspelen blijft vastlopen; automatisch herstel gestopt. Controleer de iPad en start opnieuw via beheer.")
        else:
            # First resume a stuck playing track, then reload the reserved ID once.
            action = "resume" if fault == "stalled" and history["attempts"] == 0 else "load"
            bridge.issue(action, reservation["id"] if action == "load" else None)
            # Give an unconfirmed startup a bounded extra confirmation window.
            if not reservation.get("confirmed"):
                reservation["created"] = bridge.now()
                bridge.put("reservation", reservation)
            history["attempts"] += 1
        history.update(faultId=identifier, lastAt=bridge.now())
        bridge.put("recovery", history)

    @api.post('/api/ips/v1/sync')
    def sync():
        def operation():
            payload = request.get_json(silent=True)
            if not isinstance(payload, dict):
                raise ValueError("invalid-sync")
            bridge.validate_status(payload)
            for field in ('offlineFallback', 'localEmergency', 'fallbackPending', 'crossfadeCapable', 'crossfadeActive'):
                if field in payload:
                    value = payload[field]
                    # Some Objective-C NSNumber/BOOL expressions serialize as
                    # integer 0/1. Accept exactly those, never arbitrary truthy data.
                    if type(value) in (int, float) and value in (0, 1):
                        payload[field] = bool(value)
                    elif not isinstance(value, bool):
                        raise ValueError('invalid-'+field+' (type='+type(value).__name__+', value='+repr(value)[:40]+', protocol=6.2)')
            for field in ('fault', 'faultId', 'desiredVideoId', 'configVersion'):
                if field in payload and not isinstance(payload[field], str):
                    raise ValueError('invalid-'+field)
            # Validate all ordinary fields before allowing reconciliation to mutate.
            video = payload.get("videoId", "")
            if not isinstance(video, str) or (video and not VIDEO.fullmatch(video)):
                raise ValueError("invalid-video")
            if payload.get("offlineFallback") and not isinstance(payload.get("fallbackTitle", ""), str):
                raise ValueError("invalid-title")
            # status validates and runs in the same transaction; hide local identity
            # from confirm/end logic until reconciliation has completed.
            if payload.get("offlineFallback"):
                shadow = dict(payload, videoId="", endedEvent="", endedVideoId="")
                bridge.status(shadow)
                reconcile(payload)
                bridge.status(payload)
            else:
                bridge.status(payload)
            if (payload.get('localEmergency') or payload.get('fallbackPending')) and bridge.ns.get('IS_ONLINE', True) and bridge.get('auto', False) and not bridge.pending():
                bridge.put('reservation', None)
                try:
                    bridge.reserve()
                except Conflict as error:
                    bridge.put('error', str(error))
            recover(payload)
            # V0.3 clients can keep A audible in a separate context while the
            # normal app resolves B. Freeze/reserve B before A ends, never for
            # old clients or while the current load is unconfirmed.
            reservation=bridge.get('reservation') or {}
            remaining=(payload.get('duration') or 0)-(payload.get('position') or 0)
            if (payload.get('crossfadeCapable') is True and not payload.get('crossfadeActive')
                    and bridge.ns.get('IS_ONLINE',True) and bridge.get('auto',False)
                    and reservation.get('confirmed') and payload.get('videoId')==reservation.get('id')
                    and payload.get('playbackState')=='playing' and 5<remaining<=bridge.ns.get('settings',{}).get('freeze_seconds',10)
                    and not bridge.pending() and not bridge.get('crossfade_deferred')):
                try:
                    command=bridge.reserve()
                    bridge.put('crossfade_context',dict(command=command['id'],previous=reservation,immediate=False))
                    command['crossfade']=True
                    bridge.db.execute('UPDATE commands SET payload=? WHERE id=?',(json.dumps(command),command['id']))
                except Conflict:
                    pass  # No suitable successor: keep normal end-of-track flow.
            return bridge.response(command=bridge.pending(), config=configuration(payload.get("configVersion")),
                                   reconnectAccepted=True, activePlayer=True, protocol=6, pollAfter=4)
        return run(operation)

    @api.post('/api/ips/v1/crossfade-failed')
    def crossfade_failed():
        def operation():
            payload=request.get_json(silent=True) or {}
            if not isinstance(payload,dict):
                raise ValueError('invalid-crossfade-failure')
            ended=payload.get('outgoingEnded',False)
            if type(ended) in (int,float) and ended in (0,1):ended=bool(ended)
            if not isinstance(ended,bool):raise ValueError('invalid-outgoingEnded')
            if not bridge.recover_crossfade(payload.get('id'),outgoing_ended=ended):
                raise Conflict('no-unconfirmed-crossfade')
            return bridge.response(accepted=True,command=bridge.pending())
        return run(operation)

    @api.post('/api/ips/v1/transport')
    def player_transport():
        def operation():
            body=request.get_json(silent=True)
            if not isinstance(body,dict) or body.get('action') not in {'pause','resume','next'}:
                raise ValueError('invalid-player-transport')
            action=body['action'];identifier=body.get('id');video=body.get('videoId','')
            if not isinstance(identifier,str) or not IDENTIFIER.fullmatch(identifier):raise ValueError('invalid-transport-id')
            if not isinstance(video,str) or (video and not VIDEO.fullmatch(video)):raise ValueError('invalid-video')
            proof=body.get('playback');load_id=body.get('loadCommandId','')
            if not isinstance(load_id,str) or (load_id and not IDENTIFIER.fullmatch(load_id)):raise ValueError('invalid-load-command')
            if proof is not None:
                bridge.validate_status(proof)
                if proof.get('videoId','')!=video:raise ValueError('transport-video-mismatch')
            existing=bridge.db.execute('SELECT payload FROM commands WHERE id=?',(identifier,)).fetchone()
            if existing:
                command=json.loads(existing[0])
                if command.get('transportAction')!=action:raise Conflict('transport-id-conflict')
                return bridge.response(command=command,duplicate=True)
            if action in {'resume','next'} and not bridge.ns.get('IS_ONLINE',True):raise Conflict('live-show-mode-active')
            reservation=bridge.get('reservation') or {}
            if action=='pause':
                bridge.put('auto',False)
                # A freshly observed playing B can be confirmed before pausing,
                # but never with an older load's snapshot for the same video ID.
                if proof is not None and load_id==reservation.get('command') and video==reservation.get('id') and proof.get('playbackState')=='playing' and (proof.get('position') or 0)>0.03:
                    bridge.status(proof)
            context=bridge.get('crossfade_context') or {}
            previous=context.get('previous') or {}
            reservation=bridge.get('reservation') or {}
            if not reservation.get('confirmed') and previous.get('confirmed') and previous.get('id')==video and (reservation.get('id')!=video or load_id==previous.get('command')):
                bridge.put('auto',False)
                bridge.recover_crossfade(context.get('command'))
                reservation=bridge.get('reservation') or {}
            if action in {'pause','next'}:
                command=bridge.command(dict(id=identifier,action=action))['command']
            else:
                # A new physical play intent supersedes older pending controls.
                bridge.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL")
                if not reservation:
                    command=bridge.command(dict(id=identifier,action='start'))['command']
                elif video!=reservation['id'] or (not reservation.get('confirmed') and load_id!=reservation.get('command')):
                    command=bridge.issue('load',reservation['id'],identifier)
                    bridge.put('reservation',{**reservation,'command':identifier,'created':bridge.now(),'confirmed':False})
                    bridge.put('crossfade_context',None);bridge.put('crossfade_deferred',None);bridge.put('auto',True)
                else:
                    if not reservation.get('confirmed'):
                        reservation['created']=bridge.now();bridge.put('reservation',reservation)
                    command=bridge.command(dict(id=identifier,action='resume'))['command']
            bridge.put('error',None)
            command['transportAction']=action
            bridge.db.execute('UPDATE commands SET payload=? WHERE id=?',(json.dumps(command),command['id']))
            return bridge.response(command=command)
        return run(operation)

    @api.post('/api/ips/v1/manage/pair')
    def make_pair():
        def operation():
            code = secrets.token_urlsafe(32)
            bridge.put("pair_code", dict(digest=hashlib.sha256(code.encode()).hexdigest(), expires=bridge.now()+120000, replace=True))
            # Fragment keeps the code out of HTTP access logs and Referer headers.
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                raise ValueError('invalid-pair-request')
            scheme = 'https' if current_app.config['SESSION_COOKIE_SECURE'] else request.scheme
            origin = scheme + '://' + request.host + '/' + g.event['slug']
            # Browser knows the external scheme even when TLS terminates at a
            # proxy. Accept only HTTPS and exactly the already-resolved host;
            # never use arbitrary forwarded headers or a different QR host.
            public = payload.get('serverUrl')
            if public:
                browser = urlsplit(public)
                if (browser.scheme != 'https' or browser.netloc.lower() != request.host.lower()
                        or browser.username or browser.password or browser.query or browser.fragment or browser.path not in ('', '/')):
                    raise ValueError('Gebruik de HTTPS-URL van ditzelfde eventdomein.')
                origin = 'https://' + request.host + '/' + g.event['slug']
            parts = urlsplit(origin)
            if parts.scheme != 'https' or not parts.hostname or parts.username or parts.password or parts.query or parts.fragment or parts.path != '/' + g.event['slug']:
                raise ValueError('Gebruik de publieke HTTPS-URL om een QR te maken.')
            url = origin.rstrip('/')+'/ips-pair#'+code
            image = qrcode.make(url, image_factory=SvgPathImage, box_size=6, border=4)
            output = io.BytesIO()
            image.save(output)
            return bridge.response(pairUrl=url, qrSvg=output.getvalue().decode(),
                                   expiresAt=bridge.now()+120000, eventId=bridge.event)
        return run(operation)

    @api.post('/api/ips/v1/pair/claim')
    def claim_pair():
        def operation():
            payload = request.get_json(silent=True) or {}
            if not isinstance(payload, dict):
                raise ValueError('invalid-pair-request')
            code = payload.get("code")
            if not isinstance(code, str) or not 32 <= len(code) <= 96:
                raise ValueError("invalid-pair-code")
            saved = bridge.get("pair_code")
            if not saved or saved["expires"] <= bridge.now() or not same_secret(saved["digest"], hashlib.sha256(code.encode()).hexdigest()):
                raise Conflict("pair-code-expired-or-used")
            player = payload.get('playerId', '')
            if not isinstance(player, str) or not IDENTIFIER.fullmatch(player):
                raise ValueError('invalid-player-id')
            current = bridge.get('player')
            if current and current != player and not saved.get('replace'):
                raise Conflict('another-player-is-paired')
            if current and current != player:
                revoked = bridge.get('revoked_player_tokens', {})
                revoked[hashlib.sha256(bridge.token.encode()).hexdigest()] = 'revoked-'+secrets.token_hex(16)
                bridge.put('revoked_player_tokens', revoked)
                bridge.put('active_player_token', secrets.token_urlsafe(32))
                bridge.db.execute("UPDATE commands SET result='expired' WHERE result IS NULL")
                bridge.put('reservation', None)
                bridge.put('next_pick', None)
                bridge.put('status', None)
                bridge.put('auto', False)
                bridge.put('offline_paused', False)
                bridge.put('error', 'Nieuwe player gekoppeld. Start de wachtrij via beheer.')
                bridge.put('player', player)
            else:
                bridge.bind(player)
            bridge.put("pair_code", None)
            return bridge.response(token=bridge.get('active_player_token') or bridge.token, eventId=bridge.event)
        response = run(operation)
        if not isinstance(response, tuple) and response.status_code == 200:
            bridge.token = bridge.get('active_player_token') or bridge.token
        return response
