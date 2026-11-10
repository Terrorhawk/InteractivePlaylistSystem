"""Installed version and cached GitHub release lookup."""
import json
import re
from threading import Lock
from time import monotonic
from urllib.request import Request, urlopen

VERSION = '0.2.10-alpha2'
REPOSITORY = 'Terrorhawk/InteractivePlaylistSystem'
RELEASES_URL = f'https://github.com/{REPOSITORY}/releases'
_lock = Lock()
_next_check = 0
_available = None


def version_key(tag):
    match = re.fullmatch(r'v?(\d+)\.(\d+)\.(\d+)(?:-(alpha|beta|rc)[.-]?(\d+))?', tag)
    if not match:
        return None
    major, minor, patch, stage, sequence = match.groups()
    return int(major), int(minor), int(patch), {'alpha': 0, 'beta': 1, 'rc': 2, None: 3}[stage], int(sequence or 0)


def available_update():
    global _next_check, _available
    with _lock:
        if monotonic() < _next_check:
            return _available
        request = Request(f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=100',
                          headers={'Accept': 'application/vnd.github+json', 'User-Agent': 'IPS-release-check'})
        try:
            with urlopen(request, timeout=3) as response:
                releases = json.load(response)
            if not isinstance(releases, list):
                raise ValueError('Invalid GitHub release response')
            tags = [r['tag_name'] for r in releases if isinstance(r, dict) and not r.get('draft')
                    and isinstance(r.get('tag_name'), str) and version_key(r['tag_name'])]
            newest = max(tags, key=version_key, default=None)
            _available = newest if newest and version_key(newest) > version_key(VERSION) else None
            _next_check = monotonic() + 3600
        except (OSError, ValueError, TypeError, KeyError):
            _next_check = monotonic() + 300
        return _available
