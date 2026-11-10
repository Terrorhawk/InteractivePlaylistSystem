# Herkomst en dependencies

`protocol/ips_remote.py` en `protocol/player_v6.py` zijn afgeleid van de lokale IPS-server V0.5. De applicatie integreert dit bestaande protocol in een nieuwe eventgebonden runtime. De oude Flask-app, browserplayer, Premium-configuratie en QR-vendormap zijn niet meegeleverd.

Flask, Werkzeug, PyMySQL, Gunicorn, qrcode en ytmusicapi worden tijdens Docker-build via pip geïnstalleerd. De bijbehorende distributies bevatten hun eigen licenties/notices; die blijven in de image aanwezig. De alpha-ZIP bevat geen gekopieerde third-party bibliotheken.

De licentiekeuze voor IPS is nog niet vastgelegd.
