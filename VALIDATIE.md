# Validatie alpha2

Uitgevoerd op 4 november 2026:

- 53 lokale tests tegen de volledige nieuwe Flask-routes, met een SQLite-testdatabase en de echte qrcode-package. Inclusief wachtwoorden van 8 tekens accepteren, 7 tekens weigeren en een nummerlink zonder playlist-ID herkenbaar afwijzen.
- Gelijktijdige startrequests geven één opdracht en één conflict, zonder een aanvraag te verbruiken.
- Gelijktijdige inwisseling van één QR geeft één gekoppelde player en één conflict.
- Getest: CRUD voor sites/accounts/events, leesbare rechten, intrekken tijdens een bestaande sessie, eventtokenisolatie, instelbare quota, wachttijd, stemgedrag, bevestigde playback, herhaalblokkade, idempotente beheeraanpassingen, verzoekverval, deviceprivacy, fallback, liveshow, offline en herladen via een nieuwe appinstantie.
- Crossfadevoorbereiding houdt het verzoek vast; mislukte overlap geeft gewone opvolging; bevestigde playback verbruikt het verzoek.
- Playertransport en command sequence/controlEpoch blijven idempotent over meerdere HTTP-requests.
- Python-syntax gecontroleerd voor 3.12, Jinja-muziektemplate gerenderd en het resulterende JavaScript met `node --check` gecontroleerd.
- Een echte openbare YouTube Music-zoekopdracht met ytmusicapi 1.12.1 slaagt zonder browsercookies of account.

Niet uitgevoerd: Docker-build/containerstart, schema-upgrade op jouw MariaDB, MariaDB-concurrencytest, browser-layoutcontrole, echte iPad/iPhone-playback en duurtest. Die horen bij de nu mogelijk gemaakte gebruikerstest. De alpha1-livecontroles uit de vorige ronde zijn geen livevalidatie van deze nieuwe release.

Er is tijdens de ontwikkeling niets naar de NAS gedeployed en geen bestaande serverdatabase gemigreerd. De oorspronkelijke IPA's en V0.5-serverbronnen zijn niet gewijzigd.

Update 0.2.2: geen publieke eventlijst; muziektemplate en primaire stylesheet overgenomen uit V0.5. Getest: oude fallback-API en zoekpagina, bezoeker/adminweergave en HTTPS-browser-origin achter HTTP-backend met afwijzing van een andere host. JavaScript-syntax gecontroleerd. Visuele vergelijking in een echte browser is nog niet uitgevoerd.

0.2.3: tests voor standaardevent kiezen/wissen, gastredirect zonder eventlijst, weigeren van een event uit een andere site, sitebeheerder versus eventbeheerder en automatisch opruimen bij eventverwijdering. Schema 3 gebruikt foreign keys met ON DELETE CASCADE. MariaDB-upgrade moet nog op de testinstallatie worden bevestigd.

0.2.5: aangepaste offlinetekst, HTML-escaping en terugvallen op standaardtekst getest.

0.2.6: tests voor behouden shuffle over requests en nieuwe appinstantie, shuffle-autorisatie en behoud van vastgezette/verstuurde opvolger met correcte cursor na bevestiging.

0.2.10: Nederlandse en Engelse paginaweergave, versiefooter, vergelijking van prereleases en caching lokaal getest.
