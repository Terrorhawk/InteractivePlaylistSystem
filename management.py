"""Global management mutations. Every call is already authenticated + CSRF checked."""
import re
import sqlite3
from flask import abort, g, request
from werkzeug.security import generate_password_hash


def mutate(action, execute, query):
    form = request.form
    target = form.get('target', '')
    def write(sql, args=()):
        return execute(g.db, sql, args)
    def confirmed():
        if form.get('confirm') != 'yes':
            abort(400, 'Bevestig het verwijderen expliciet.')
    if action in {'site_edit', 'site_delete'}:
        if not query('SELECT id FROM sites WHERE id=?', (target,), one=True):
            abort(404)
        if action == 'site_edit':
            name = form.get('name', '').strip()
            domains = sorted(set(x.strip().lower() for x in form.get('domains', '').splitlines() if x.strip()))
            if not name or len(name) > 120 or not domains or any(not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', d) for d in domains):
                abort(400, 'Naam en ten minste Ã©Ã©n geldige hostnaam vereist (Ã©Ã©n per regel).')
            for domain in domains:
                other = query('SELECT site_id FROM site_domains WHERE domain=?', (domain,), one=True)
                if other and other['site_id'] != target:
                    abort(409, 'Domein is aan een andere site gekoppeld.')
            write('UPDATE sites SET name=? WHERE id=?', (name, target))
            write('DELETE FROM site_domains WHERE site_id=?', (target,))
            for domain in domains:
                write('INSERT INTO site_domains VALUES (?,?)', (domain, target))
        else:
            confirmed()
            if query('SELECT id FROM events WHERE site_id=?', (target,), one=True):
                abort(409, 'Verwijder eerst de events van deze site.')
            write('DELETE FROM memberships WHERE scope_id=? AND role=?', (target, 'site'))
            write('DELETE FROM site_domains WHERE site_id=?', (target,))
            write('DELETE FROM sites WHERE id=?', (target,))
    elif action in {'user_edit', 'user_delete'}:
        user = query('SELECT * FROM users WHERE id=?', (target,), one=True)
        if not user:
            abort(404)
        if action == 'user_delete':
            confirmed()
            if user['global_admin'] or user['id'] == g.user['id']:
                abort(409, 'Een globale beheerder of je eigen account kan hier niet worden verwijderd.')
            write('DELETE FROM memberships WHERE user_id=?', (target,))
            write('DELETE FROM users WHERE id=?', (target,))
        else:
            name, password = form.get('username', '').strip(), form.get('password', '')
            if not re.fullmatch(r'[A-Za-z0-9_.-]{1,80}', name) or (password and len(password) < 8):
                abort(400, 'Ongeldige gebruikersnaam of wachtwoord korter dan 8 tekens.')
            other = query('SELECT id FROM users WHERE username=?', (name,), one=True)
            if other and other['id'] != target:
                abort(409, 'Gebruikersnaam bestaat al.')
            write('UPDATE users SET username=? WHERE id=?', (name, target))
            if password:
                write('UPDATE users SET password_hash=? WHERE id=?', (generate_password_hash(password), target))
    elif action == 'revoke':
        write('DELETE FROM memberships WHERE user_id=? AND scope_id=? AND role=?',
              (form.get('user'), form.get('scope'), form.get('role')))
    elif action == 'event_delete':
        confirmed()
        # Match the runtime lock order: event first, then its dependent state.
        query('SELECT id FROM events WHERE id=?' + ('' if isinstance(g.db, sqlite3.Connection) else ' FOR UPDATE'), (target,), one=True)
        write('DELETE FROM memberships WHERE scope_id=? AND role=?', (target, 'event'))
        write('DELETE FROM event_runtime WHERE event_id=?', (target,))
        write('DELETE FROM events WHERE id=?', (target,))
    else:
        return False
    return True
