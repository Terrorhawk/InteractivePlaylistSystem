"""IPS Multisite alpha 2: management and event-isolated player runtime."""
import json
import os
import re
import secrets
import sqlite3
import uuid
from urllib.parse import urlsplit

import pymysql
from flask import Flask, abort, g, jsonify, redirect, render_template, request, session
from i18n import translate
from management import mutate
from release_info import RELEASES_URL, VERSION, available_update
from werkzeug.security import check_password_hash, generate_password_hash

RULES = {
    'request_limit': ('Maximaal aanvragen per bezoeker', 10, 1, 100),
    'request_window_minutes': ('Aanvraagperiode (minuten)', 15, 1, 1440),
    'downvote_limit': ('Maximaal downvotes per bezoeker', 3, 0, 100),
    'downvote_window_minutes': ('Downvoteperiode (minuten)', 15, 1, 1440),
    'selection_delay_seconds': ('Wachttijd na aanvraag (seconden)', 10, 0, 600),
    'repeat_cooldown_minutes': ('Herhaalblokkade na playback (minuten)', 60, 0, 10080),
    'nonpositive_expiry_minutes': ('Verzoek met nul/minstemmen vervalt na (minuten)', 30, 1, 1440),
    'freeze_seconds': ('Opvolger vastzetten voor einde (seconden)', 10, 1, 60),
    'priority_interval_minutes': ('Iedere zoveel minuten een voorrangspunt', 3, 1, 60),
    'fallback_max_minutes': ('Maximale fallbacktrackduur (minuten)', 34, 1, 240),
    'fallback_preview_count': ('Aantal fallbacknummers in vooruitblik', 3, 1, 10),
}
RESERVED = {'api', 'login', 'logout', 'admin', 'health', 'static', 'ips-pair', 'site', 'release-update'}
SCHEMA = [
    'CREATE TABLE IF NOT EXISTS schema_version (version INTEGER PRIMARY KEY)',
    'CREATE TABLE IF NOT EXISTS users (id VARCHAR(36) PRIMARY KEY, username VARCHAR(80) NOT NULL UNIQUE, password_hash VARCHAR(255) NOT NULL, global_admin INTEGER NOT NULL DEFAULT 0)',
    'CREATE TABLE IF NOT EXISTS sites (id VARCHAR(36) PRIMARY KEY, name VARCHAR(120) NOT NULL)',
    'CREATE TABLE IF NOT EXISTS site_domains (domain VARCHAR(253) PRIMARY KEY, site_id VARCHAR(36) NOT NULL, FOREIGN KEY(site_id) REFERENCES sites(id))',
    'CREATE TABLE IF NOT EXISTS events (id VARCHAR(36) PRIMARY KEY, site_id VARCHAR(36) NOT NULL, slug VARCHAR(64) NOT NULL, name VARCHAR(120) NOT NULL, settings TEXT NOT NULL, revision INTEGER NOT NULL DEFAULT 1, UNIQUE(site_id,slug), FOREIGN KEY(site_id) REFERENCES sites(id))',
    'CREATE TABLE IF NOT EXISTS site_defaults (site_id VARCHAR(36) PRIMARY KEY, event_id VARCHAR(36) NOT NULL, FOREIGN KEY(site_id) REFERENCES sites(id) ON DELETE CASCADE, FOREIGN KEY(event_id) REFERENCES events(id) ON DELETE CASCADE)',
    'CREATE TABLE IF NOT EXISTS event_runtime (event_id VARCHAR(36) PRIMARY KEY, payload LONGTEXT NOT NULL, FOREIGN KEY(event_id) REFERENCES events(id))',
    'CREATE TABLE IF NOT EXISTS memberships (user_id VARCHAR(36) NOT NULL, scope_id VARCHAR(36) NOT NULL, role VARCHAR(10) NOT NULL, PRIMARY KEY(user_id,scope_id,role), FOREIGN KEY(user_id) REFERENCES users(id))',
]


def create_app(config=None):
    app = Flask(__name__)
    app.config.update(SECRET_KEY=os.environ.get('IPS_SECRET_KEY'),
                      ADMIN_URL=os.environ.get('IPS_ADMIN_URL'),
                      ADMIN_USERNAME=os.environ.get('IPS_ADMIN_USERNAME'),
                      ADMIN_PASSWORD=os.environ.get('IPS_ADMIN_PASSWORD'),
                      LANGUAGE=os.environ.get('IPS_LANGUAGE', 'nl'),
                      SQLITE_TEST_PATH=None, MAX_CONTENT_LENGTH=32768,
                      SESSION_COOKIE_NAME='ips_multisite_alpha',
                      SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE='Lax')
    if config:
        app.config.update(config)
    language = app.config['LANGUAGE'].strip().lower()
    if language not in ('nl', 'en'):
        raise ValueError('IPS_LANGUAGE must be nl or en')
    app.jinja_env.globals.update(t=lambda text: translate(text, language), ips_language=language,
                                 ips_version=VERSION, ips_releases=RELEASES_URL)
    if not app.config['SECRET_KEY'] or len(app.config['SECRET_KEY']) < 32:
        raise ValueError('IPS_SECRET_KEY vereist (minimaal 32 tekens)')
    admin = urlsplit(app.config['ADMIN_URL'] or '')
    if admin.scheme not in ('http', 'https') or not admin.hostname or admin.username or admin.password or admin.query or admin.fragment:
        raise ValueError('IPS_ADMIN_URL moet een volledige http(s)-URL zijn')
    admin_path = admin.path.rstrip('/') or '/manage'
    if not re.fullmatch(r'/[a-zA-Z0-9_-]+', admin_path) or admin_path[1:] in RESERVED:
        raise ValueError('Gebruik één uniek beheerpad, bijvoorbeeld /manage')
    app.config['SESSION_COOKIE_SECURE'] = admin.scheme == 'https'
    admin_host = admin.netloc.lower()
    def connect():
        if app.config['SQLITE_TEST_PATH']:
            db = sqlite3.connect(app.config['SQLITE_TEST_PATH'])
            db.row_factory = sqlite3.Row
            db.execute('PRAGMA foreign_keys=ON')
            return db
        return pymysql.connect(host=os.environ['IPS_DB_HOST'], port=int(os.environ.get('IPS_DB_PORT', '3306')),
                               database=os.environ['IPS_DB_NAME'], user=os.environ['IPS_DB_USER'],
                               password=os.environ['IPS_DB_PASSWORD'], charset='utf8mb4',
                               cursorclass=pymysql.cursors.DictCursor, autocommit=False, connect_timeout=10)
    def execute(db, sql, args=()):
        cursor = db.cursor()
        cursor.execute(sql if app.config['SQLITE_TEST_PATH'] else sql.replace('?', '%s'), args)
        return cursor
    def query(sql, args=(), one=False):
        cursor = execute(g.db, sql, args)
        return cursor.fetchone() if one else cursor.fetchall()
    with app.app_context():
        db = connect()
        try:
            for sql in SCHEMA:
                execute(db, sql)
            versions = execute(db, 'SELECT version FROM schema_version').fetchall()
            if versions and any(row['version'] not in (1, 2, 3) for row in versions):
                raise ValueError('Niet ondersteunde schemaversie')
            if not versions:
                execute(db, 'INSERT INTO schema_version VALUES (3)')
            else:
                execute(db, 'UPDATE schema_version SET version=3 WHERE version IN (1,2)')
            if not execute(db, 'SELECT id FROM users WHERE global_admin=1').fetchone():
                username, password = app.config['ADMIN_USERNAME'], app.config['ADMIN_PASSWORD']
                if not username or not password or len(password) < 8:
                    raise ValueError('Eerste start vereist IPS_ADMIN_USERNAME en IPS_ADMIN_PASSWORD (minimaal 8 tekens)')
                execute(db, 'INSERT INTO users VALUES (?,?,?,1)', (str(uuid.uuid4()), username, generate_password_hash(password)))
            db.commit()
        finally:
            db.close()

    def allowed(event=None, site=None):
        if not g.user:
            return False
        if g.user['global_admin']:
            return True
        scope = site or (event['site_id'] if event else None)
        return bool(query('SELECT user_id FROM memberships WHERE user_id=? AND ((scope_id=? AND role=?) OR (scope_id=? AND role=?))',
                          (g.user['id'], scope, 'site', event['id'] if event else '', 'event'), one=True))
    def require_global():
        if not g.user or not g.user['global_admin']:
            abort(403)
    def page(title, **data):
        if data.get('login'):
            return render_template('login.html',event_name=g.site['name'] if getattr(g,'site',None) else 'IPS',back_url='/',csrf=session['csrf'],error=translate(data.get('message'),language))
        return render_template('page.html', title=title if data.get('event') else translate(title,language), rules=RULES, admin_path=admin_path,
                               csrf=session['csrf'], **data)
    @app.before_request
    def context():
        g.db = connect()
        g.user = query('SELECT * FROM users WHERE id=?', (session.get('user_id', ''),), one=True)
        session.setdefault('csrf', secrets.token_urlsafe(32))
        if request.path == '/health':
            return None
        # Proxy headers are deliberately not trusted. Preserve Host in your proxy.
        host = request.host.lower()
        g.site = query('SELECT sites.* FROM sites JOIN site_domains ON sites.id=site_domains.site_id WHERE domain=?', (host.split(':')[0],), one=True)
        if host != admin_host and not g.site:
            abort(404)
        if request.path.startswith(admin_path) and host != admin_host:
            abort(404)
        if request.method == 'POST' and '/api/ips/v1/' not in request.path and not secrets.compare_digest(request.headers.get('X-CSRF-Token', '') or request.form.get('csrf', ''), session['csrf']):
            abort(403)
    @app.teardown_request
    def close(error):
        db = g.pop('db', None)
        if db:
            db.rollback()
            db.close()
    @app.after_request
    def headers(response):
        response.headers['Cache-Control'] = 'no-store'
        response.headers['X-Content-Type-Options'] = 'nosniff'
        response.headers['X-Frame-Options'] = 'DENY'
        response.headers['Referrer-Policy'] = 'same-origin'
        return response
    @app.errorhandler(400)
    @app.errorhandler(403)
    @app.errorhandler(404)
    @app.errorhandler(409)
    def error(exc):
        if getattr(g,'bridge',None) and (request.form.get('_return')=='page' or '/upvote/' in request.path or '/downvote/' in request.path):
            g.runtime_failed=True
            session['jukebox_error']=exc.description
            return redirect('/'+g.event['slug']+'/')
        if request.accept_mimetypes.best == 'application/json':
            return jsonify(error=translate(exc.description,language)), exc.code
        return page('Actie niet uitgevoerd', message=exc.description), exc.code
    @app.get('/health')
    def health():
        query('SELECT 1')
        return {'status': 'ok', 'version': VERSION, 'playerSupported': True}

    @app.get('/release-update')
    def release_update():
        return jsonify(version=available_update())
    def login():
        if request.method == 'POST':
            user = query('SELECT * FROM users WHERE username=?', (request.form.get('username', ''),), one=True)
            if not check_password_hash(user['password_hash'] if user else generate_password_hash('unused'), request.form.get('password', '')):
                return page('Aanmelden', login=True, message='Aanmelden mislukt.'), 401
            session.clear()
            session['user_id'] = user['id']
            return redirect(request.path)
        if g.user:
            if request.path=='/site/login':
                return redirect('/site/settings')
            if request.path.startswith(admin_path):
                return redirect(admin_path)
            return redirect(request.path.removesuffix('/login') + '/admin')
        return page('Aanmelden', login=True)
    app.add_url_rule(admin_path + '/login', 'global_login', login, methods=['GET', 'POST'])
    app.add_url_rule('/<slug>/login', 'event_login', lambda slug: login(), methods=['GET', 'POST'])
    app.add_url_rule('/site/login', 'site_login', login, methods=['GET', 'POST'])
    @app.post('/logout')
    def logout():
        session.clear()
        return redirect('/')
    @app.route(admin_path, methods=['GET', 'POST'])
    def manage():
        if not g.user:
            return redirect(admin_path + '/login')
        require_global()
        if request.method == 'POST':
            action = request.form.get('action')
            if mutate(action, execute, query):
                pass
            elif action == 'site':
                name = request.form.get('name', '').strip()
                domain = request.form.get('domain', '').strip().lower()
                if not name or len(name) > 120 or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', domain):
                    abort(400, 'Vul een naam en een hostnaam zonder schema, pad of poort in.')
                if query('SELECT domain FROM site_domains WHERE domain=?', (domain,), one=True):
                    abort(409, 'Dit domein is al gekoppeld.')
                site_id = str(uuid.uuid4())
                execute(g.db, 'INSERT INTO sites VALUES (?,?)', (site_id, name))
                execute(g.db, 'INSERT INTO site_domains VALUES (?,?)', (domain, site_id))
            elif action == 'user':
                name, password = request.form.get('username', '').strip(), request.form.get('password', '')
                if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', name) or len(password) < 8:
                    abort(400, 'Gebruikersnaam ongeldig of wachtwoord korter dan 8 tekens.')
                if query('SELECT id FROM users WHERE username=?', (name,), one=True):
                    abort(409, 'Gebruikersnaam bestaat al.')
                execute(g.db, 'INSERT INTO users VALUES (?,?,?,0)', (str(uuid.uuid4()), name, generate_password_hash(password)))
            elif action == 'grant':
                role, scope, user = (request.form.get(k, '') for k in ('role', 'scope', 'user'))
                table = {'site': 'sites', 'event': 'events'}.get(role)
                if not table or not query('SELECT id FROM users WHERE id=?', (user,), one=True) or not query('SELECT id FROM '+table+' WHERE id=?', (scope,), one=True):
                    abort(400, 'Ongeldige toewijzing.')
                if not query('SELECT user_id FROM memberships WHERE user_id=? AND scope_id=? AND role=?', (user, scope, role), one=True):
                    execute(g.db, 'INSERT INTO memberships VALUES (?,?,?)', (user, scope, role))
            else:
                abort(400)
            g.db.commit()
            tab='rights' if action in ('grant','revoke') else 'users' if action.startswith('user') else 'events' if action.startswith('event') else 'sites'
            return redirect(admin_path+'?tab='+tab)
        sites = [dict(x) for x in query('SELECT * FROM sites ORDER BY name')]
        for site in sites:
            site['domains'] = '\n'.join(x['domain'] for x in query('SELECT domain FROM site_domains WHERE site_id=? ORDER BY domain', (site['id'],)))
        return page('Globaal beheer', management=True, sites=sites,
                    users=query('SELECT id,username,global_admin FROM users ORDER BY username'),
                    events=query('SELECT events.*,sites.name AS site_name FROM events JOIN sites ON sites.id=events.site_id ORDER BY sites.name,events.name'),
                    memberships=query("SELECT m.*,u.username,COALESCE(s.name,e.name) AS scope_name,es.name AS event_site FROM memberships m JOIN users u ON u.id=m.user_id LEFT JOIN sites s ON m.role='site' AND s.id=m.scope_id LEFT JOIN events e ON m.role='event' AND e.id=m.scope_id LEFT JOIN sites es ON es.id=e.site_id"))
    def set_default(site_id, event_id):
        if event_id:
            event=query('SELECT id FROM events WHERE id=? AND site_id=?', (event_id,site_id),one=True)
            if not event:
                abort(400,'Kies een event dat bij deze site hoort.')
        execute(g.db,'DELETE FROM site_defaults WHERE site_id=?',(site_id,))
        if event_id:
            execute(g.db,'INSERT INTO site_defaults VALUES (?,?)',(site_id,event_id))

    @app.route('/site/settings',methods=['GET','POST'])
    def site_settings():
        if not g.site:
            abort(404)
        if not g.user:
            return redirect('/site/login')
        if not allowed(site=g.site['id']):
            abort(403)
        if request.method=='POST':
            set_default(g.site['id'],request.form.get('default_event',''))
            g.db.commit()
            return redirect('/site/settings')
        selected=query('SELECT event_id FROM site_defaults WHERE site_id=?',(g.site['id'],),one=True)
        return page(translate('Sitebeheer',language)+' · '+g.site['name'],site_settings=True,
                    site_events=query('SELECT id,name FROM events WHERE site_id=? ORDER BY name',(g.site['id'],)),
                    default_event=selected['event_id'] if selected else '')

    @app.get('/')
    def index():
        selected=query('SELECT e.slug FROM site_defaults d JOIN events e ON e.id=d.event_id AND e.site_id=d.site_id WHERE d.site_id=?',(g.site['id'],),one=True) if g.site else None
        if selected:
            return redirect('/'+selected['slug']+'/')
        if not g.user:
            return page('Welkom', message='Open de directe link van je event.')
        events=query('SELECT * FROM events WHERE site_id=?', (g.site['id'],)) if g.site else []
        return page('Events', events=[e for e in events if allowed(event=e)], overview=True)
    @app.route('/<slug>/settings', methods=['GET', 'POST'])
    @app.route('/<slug>/admin', methods=['GET', 'POST'])
    def event_admin(slug):
        if not g.site:
            abort(404)
        event = query('SELECT * FROM events WHERE site_id=? AND slug=?', (g.site['id'], slug), one=True)
        if not g.user:
            return redirect('/'+slug+'/login')
        if not allowed(event=event, site=g.site['id']):
            abort(403)
        if not re.fullmatch(r'[a-z0-9][a-z0-9-]{0,63}', slug) or slug in RESERVED or slug == admin_path[1:]:
            abort(400, 'Deze eventslug is gereserveerd of ongeldig.')
        if event and request.path.endswith('/admin') and request.method=='GET':
            return redirect('/'+slug+'/music')
        if request.method == 'POST':
            name = request.form.get('name', '').strip()
            if not name or len(name) > 120:
                abort(400, 'Eventnaam vereist, maximaal 120 tekens.')
            settings = {}
            previous=json.loads(event['settings']) if event else {}
            offline_text=request.form.get('offline_text',previous.get('offline_text','')).strip()
            if len(offline_text)>300:
                abort(400,'Offlinetekst mag maximaal 300 tekens bevatten.')
            settings['offline_text']=offline_text or 'Jukebox offline op dit moment'
            for key, (label, default, low, high) in RULES.items():
                try:
                    value = int(request.form.get(key, ''))
                except ValueError:
                    abort(400, label + ': vul een geheel getal in.')
                if not low <= value <= high:
                    abort(400, f'{label}: gebruik {low} t/m {high}.')
                settings[key] = value
            if not event:
                if request.form.get('confirm') != 'yes':
                    abort(400, 'Bevestig expliciet dat je dit event wilt aanmaken.')
                execute(g.db, 'INSERT INTO events VALUES (?,?,?,?,?,1)', (str(uuid.uuid4()), g.site['id'], slug, name, json.dumps(settings)))
            else:
                cursor = execute(g.db, 'UPDATE events SET name=?,settings=?,revision=revision+1 WHERE id=? AND revision=?',
                                 (name, json.dumps(settings), event['id'], request.form.get('revision', '')))
                if cursor.rowcount != 1:
                    abort(409, 'Instellingen zijn ondertussen gewijzigd. Herlaad de pagina.')
            g.db.commit()
            return redirect(request.path)
        return page(event['name'] if event else translate('Nieuw event',language)+': '+slug, event_form=True, event=event,
                    settings=json.loads(event['settings']) if event else {k:v[1] for k,v in RULES.items()})
    @app.get('/<slug>/')
    def public(slug):
        if not g.site:
            abort(404)
        event = query('SELECT * FROM events WHERE site_id=? AND slug=?', (g.site['id'], slug), one=True)
        if not event:
            abort(404)
        return app.view_functions['music'](slug)
    from runtime import install_runtime
    install_runtime(app, connect, execute, query, allowed, page)
    return app
