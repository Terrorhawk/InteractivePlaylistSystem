# Interactive Playlist System

Interactive Playlist System (IPS) is een zelf te hosten jukebox voor evenementen. Bezoekers zoeken nummers in YouTube Music, dienen aanvragen in en stemmen op de wachtrij. Eventbeheerders kiezen de regels en fallbackplaylists. Op een tv toont de monitor het huidige nummer, de resterende tijd en de wachtrij.

De server beheert sites, events, accounts, stemmen en de afspeelvolgorde. Een gekoppelde IPS-player speelt de audio af en bevestigt welke nummers daadwerkelijk spelen. Als geen aanvraag speelbaar is, gebruikt IPS de fallbackplaylists. De server gebruikt Flask en MariaDB en draait via Docker.

**Alpha 0.2.9.** Voor de volledige afspeeltest heb je een compatibele IPS-player nodig. Je kunt het beheer en de bezoekerspagina alvast zonder player testen.

## Snel starten

1. Download het installatiepakket bij de GitHub-release en pak het uit.
2. Kopieer `.env.example` naar `.env` en vervang de voorbeeldwaarden. Stel `IPS_ADMIN_URL` in op het adres waarop je de server opent, bijvoorbeeld `http://localhost:8044/manage`.
3. Start de server en meegeleverde MariaDB met `docker compose up -d --build`.
4. Open het beheeradres en meld je aan met de gegevens uit je eigen `.env`. Maak een site aan en koppel het domein of IP-adres waarmee je die site bezoekt.
5. Open `/<eventslug>/admin` op dat domein om een event aan te maken. Bezoekers gebruiken `/<eventslug>/`. Een groot scherm gebruikt `/<eventslug>/monitor`.

De stappen hieronder beschrijven ook installatie met een bestaande MariaDB, Portainer en HTTPS voor de player.

## Nieuw tegenover alpha1

- Sites hernoemen, meerdere domeinen koppelen en lege sites verwijderen.
- Gebruikers hernoemen, wachtwoorden aanpassen en gebruikers verwijderen. Globale beheerders en het eigen account kunnen niet worden verwijderd.
- Toewijzingen tonen gebruikersnamen en site-/eventnamen; scopes kies je uit een keuzelijst. Een recht wijzigen doe je door het oude in te trekken en het nieuwe toe te wijzen. Intrekken geldt bij de volgende request, ook voor reeds aangemelde gebruikers.
- Events verwijderen inclusief opgeslagen muziek- en playergegevens, met expliciete bevestiging. Een site met events moet eerst worden leeggemaakt.
- Muziekbediening via de link op de eventinstellingenpagina of `/<eventslug>/music`.
- Aanvraag- en downvotelimieten, wachttijd, herhaalblokkade, vervaltijd, voorrang, opvolgermoment en fallbackregels gebruiken de eventinstellingen.
- QR-koppeling, playervervanging, sync/acknowledgements, dubbele opdrachten, sequence/controlEpoch, crossfade en gewone terugval gebruiken de V0.5-protocollogica met een eventadapter.

## Alpha1 bijwerken (bestaande MariaDB, Portainer)

1. Maak een back-up van je **alpha-database** met je gebruikelijke MariaDB-beheer. Bewaar ook je oude bronpakket en environmentwaarden. Raak de V0.5-jukeboxdatabase en appbestanden niet aan.
2. Stop alleen de alpha-webcontainer via Portainer. Pak dit volledige pakket uit in de alpha-bronmap; behoud je eigen `.env` en overschrijf die niet met een voorbeeldbestand.
3. Bouw op de Docker-host de nieuwe image:

   ```sh
   cd /volume1/docker/ips-multisite-alpha
   docker build -t ips-multisite:0.2.9-alpha2 .
   ```

4. Bewerk dezelfde alpha-stack in Portainer. Vervang de inhoud door `portainer-external-db.yaml`. Behoud je bestaande environmentwaarden, waaronder secret key en databasehost/-naam/-gebruiker/-wachtwoord. De nieuwe image staat lokaal; laat geforceerd downloaden uit een registry uit.
5. Deploy de stack en controleer de webcontainerlogs. Bij eerste start wordt schema 1 automatisch naar schema 2 uitgebreid met eventruntimeopslag; je bestaande sites, events, gebruikers en instellingen blijven behouden. De init-login uit environment overschrijft geen bestaand wachtwoord.
6. Open je beheeradres en doe een volledige browser-refresh. Open een event en volg **Muziekbediening en player koppelen**.

Voor de zelfstandige variant gebruik je in stap 4 `portainer-stack.yaml` en behoud je het bestaande databasevolume en de bestaande stacknaam. Deploy niet als een nieuwe stack als je juist bestaande testgegevens wilt behouden.

Rollback: stop de alpha, herstel de databaseback-up en het oude pakket/image. Alpha1 accepteert schema 2 niet; alleen de oude image terugzetten is dus onvoldoende. Er worden geen V0.5-data geïmporteerd.

## Eerste installatie: bestaande MariaDB op Synology

Deze variant start alleen de webcontainer.

1. Maak een eigen lege `utf8mb4`-database en eigen gebruiker voor IPS aan, met rechten voor data én tabelaanmaak/wijziging binnen uitsluitend deze database. Gebruik geen rootaccount of een database van een andere applicatie.
2. Sta TCP-verbindingen vanaf het Docker-netwerk toe via MariaDB-gebruiker/hostrechten en je firewall. Gebruik het bereikbare NAS-IP/databaseadres, niet `localhost`. Controleer de daadwerkelijke MariaDB-poort.
3. Kopieer `.env.external-db.example` naar `.env`. Vul alle placeholders in, inclusief `IPS_DB_HOST`, `IPS_DB_PORT`, `IPS_DB_NAME`, `IPS_DB_USER` en `IPS_DB_PASSWORD`. Kies een beheerwachtwoord van minimaal 8 tekens en een willekeurige `IPS_SECRET_KEY` van minimaal 32 tekens. Houd de secret key bij updates gelijk.
4. `IPS_ADMIN_URL` is je volledige beheeradres, met één beheerpad, bijvoorbeeld `http://NAS-IP:8044/manage`. Het adres moet overeenkomen met de host/poort waarmee je de pagina opent. Voor een playerproef gebruik je HTTPS zoals hieronder beschreven.
5. Start met Compose:

   ```sh
   docker compose --project-name ips-multisite-alpha -f compose.external-db.yaml up -d --build
   docker compose --project-name ips-multisite-alpha -f compose.external-db.yaml logs --tail=80 web
   ```

   Of bouw de image met het eerder genoemde `docker build`-commando, maak een Portainer-stack met de volledige `portainer-external-db.yaml` en voeg de waarden uit `.env` als stack-environmentvariabelen toe. Deze instructies gelden voor een Docker-endpoint, niet Swarm.

De externe-databaseverbinding gebruikt in deze alpha geen TLS; gebruik daarvoor je vertrouwde LAN/Docker-netwerk. Er is geen database-rootwachtwoord nodig in deze variant. De applicatie initialiseert de tabellen en eerste globale beheerder bij starten.

## Eerste installatie: zelfstandig inclusief MariaDB

Kopieer `.env.example` naar `.env` en vul alle placeholders in. Deze variant maakt een eigen MariaDB 11.4-container en named volume aan. Databasepoort 3306 wordt niet naar buiten gepubliceerd.

```sh
docker compose --project-name ips-multisite-alpha up -d --build
```

Voor Portainer bouw je eerst de lokale image en gebruik je `portainer-stack.yaml`. Voeg alle waarden uit `.env` toe, inclusief het afzonderlijke database-rootwachtwoord. Kies één variant; draai de zelfstandige en externe stacks niet tegelijk. Wisselen van database kopieert geen gegevens automatisch.

## HTTPS voor de bestaande IPA

**De huidige IPA vereist HTTPS voor alle serververbindingen, ook bij handmatige koppeling.** Het HTTP-adres op poort 8044 is bruikbaar voor webtests en protocolsims, maar niet voor een echte playerproef.

1. Kies een apart testdomein dat jij beheert en naar je NAS wijst. Laat je bestaande productie-host ongemoeid.
2. Maak in Synology Reverse Proxy een HTTPS-bron voor dat testdomein op poort 443 en een bestemming `http://127.0.0.1:8044` (of het interne NAS-adres met die poort). Koppel een geldig, door je iOS-apparaat vertrouwd TLS-certificaat. Stel DNS/router/LAN-resolutie zo in dat je apparaat deze host bereikt.
3. Behoud de oorspronkelijke **Host-header**. De applicatie vertrouwt geen doorgestuurde hostheaders. Het publieke schema voor QR wordt gecontroleerd met de HTTPS-origin van de browser en de geregistreerde request-host; je hoeft geen onveilige algemene ProxyFix aan te zetten.
4. Zet `IPS_ADMIN_URL=https://JOUW-TESTDOMEIN/manage` in de stack en deploy opnieuw. Secure-cookies gaan dan aan: meld daarna aan via HTTPS, niet meer via het HTTP-IP-adres.
5. Voeg het testdomein in Globaal beheer aan je testsite toe. Het beheeradres blijft bereikbaar vóórdat je een site/domein hebt toegevoegd.
6. Open `https://JOUW-TESTDOMEIN/testavond/admin`, maak het event met expliciete bevestiging aan en open Muziekbediening.
7. Maak een eenmalige QR en scan die met IPS Player. De QR vervalt na twee minuten. Alternatief: vul de getoonde **event-URL inclusief slug**, vaste event-ID en koppelsleutel handmatig in de app in. Een bestaand event-ID uit de V0.5-server hoort niet bij dit nieuwe event.
8. Start de wachtrij bewust na koppelen. Koppelen van een andere player trekt alleen de oude toegang voor dit event in. Een volledig offline oud apparaat kan pas een stop ontvangen wanneer het weer verbinding maakt.

Een nieuwe koppeling op een apparaat verandert waar die app zijn opdrachten vandaan haalt. Gebruik daarom een testapparaat of noteer vooraf hoe je terugkoppelt naar je bestaande server. De serverrelease wijzigt geen IPA's en benadert de productieplayer niet automatisch.

## Volledige testronde

- Maak een site en twee events aan. Test verschillende instellingen per event, ook op hetzelfde domein. Dezelfde slug mag op een tweede site bestaan.
- Maak accounts en wijs via de keuzelijsten een site- of eventrol toe. Een eventbeheerder ziet alleen het toegewezen event; een sitebeheerder mag events binnen zijn site instellen/aanmaken. Globale beheerder beheert accounts en toewijzingen. Test intrekken van rechten zonder opnieuw aan te melden.
- Zoek nummers en vraag ze aan vanuit twee browsersessies. Test eigen aanvragen, upvotes, downvotes, quota en instelbare tijdvensters. Bij een eigen open aanvraag zijn downvotes geblokkeerd; eerdere downvotes worden teruggedraaid wanneer je daarna zelf aanvraagt. Dat verbruik blijft wel meetellen.
- Test +100/-100, verwijderen en verlopen verzoeken met niet-positieve score. Een aanvraag verdwijnt pas na bevestigde playback, niet na laden of een acknowledgement.
- Voeg een openbare YouTube Music-playlist toe via ID of URL. Controleer aantal nummers, totale duur en fallbackvooruitblik. Maximaal 500 fallbacktracks worden aan de app geleverd.
- Koppel de player via HTTPS. Test start, pauze, hervatten, next, natuurlijke overgang, vastzetten van de opvolger, crossfade en gewone terugval. Verifieer titels op het apparaat en luister daadwerkelijk naar de overgangen.
- Liveshow pauzeert muziek; bezoekers kunnen blijven aanvragen/stemmen. Liveshow beëindigen hervat de muziek. De afzonderlijke offlineknop maakt alleen de bezoekerspagina zwart en blokkeert aanvragen/stemmen; muziek en beheer blijven werken. Openzetten haalt bezoekers automatisch terug.
- Herstart de webcontainer en controleer behoud van wachtrij, pairing, instellingen en geschiedenis. Test tijdelijk netwerkverlies en herstel. Devicegegevens horen alleen zichtbaar te zijn voor eventbeheerders.
- Test wijzigen/verwijderen van sites en gebruikers en leesbare toewijzingen. Verwijderen vereist expliciete bevestiging; een site met events en een globale beheerder worden beschermd.

## Opslag en technische grenzen

MariaDB is de duurzame bron. Alpha2 bewaart de muziek-/playerstatus als één JSON-document per vaste event-ID in `event_runtime`. Een rijlock op het event serialiseert wijzigingen tussen workers; er zijn geen procesglobale eventwachtrijen of brede Python-locks over alle events. De bestaande protocolengine draait zijn SQLite-statements uitsluitend in request-lokaal werkgeheugen, waarna de complete staat in de MariaDB-transactie wordt opgeslagen. Er ontstaan geen SQLite-bestanden per event. Dit is een compatibiliteitsadapter, nog geen volledig genormaliseerde request-/vote-/historyopslag.

Instellingen gelden bij de volgende controle. Historie en verbruik worden niet gewist bij het aanpassen. Bestaande vastgezette keuzes en uitgegeven playeropdrachten blijven beschermd. De herhaalblokkade begint bij bevestigde playback; de minimumwachttijd begint bij aanvragen. Publieke zoekresultaten tonen de herhaalblokkade.

Nog buiten deze release: automatische migratie uit V0.5-SQLite, oude root-playeradressen, slugwijziging/redirects, account-lockout/login-rate-limiting, uitgebreide auditlog, sitebeheerders die zelf accounts/toewijzingen beheren en een globale beheerder toevoegen/verwijderen. Domeinen kunnen worden gewijzigd, maar een bestaande app-URL blijft niet vanzelf werken als je die host verwijdert. Gebruik voorlopig gecontroleerde testtoegang, niet een publiek productie-event.

Verzoek- en stemhistorie voor quota wordt maximaal één dag bewaard; playbackhistorie zeven dagen, passend bij de instelbare cooldown. Het beheer heeft nog geen historie-exportpagina. Catalogusverzoeken hebben een netwerk-timeout; fouten leveren een melding en horen geen bestaande wachtrij te verliezen. Elke eventtransactie houdt tijdens een catalogusactie de eventlock vast: andere requests van hetzelfde event kunnen dus kort wachten. De server speelt zelf geen audio en downloadt geen muziek.

## Validatie en beperkingen

De lokale tests draaien de volledige nieuwe Flask-routes met een SQLite-testdatabase. Ze testen ook twee gelijktijdige starts en twee gelijktijdige pairingclaims. Het protocol is gebaseerd op de bestaande V0.5-engine; de tests omvatten bevestiging, idempotentie, volgorde, crossfadeterugval, pauze/liveshow, offline en eventautorisatie. Dit vervangt geen MariaDB-concurrencytest of duurtest op de echte iPad/iPhone. Docker is op de bouwmachine niet beschikbaar; de nieuwe image en schema-upgrade moeten op je testinstallatie worden bevestigd. Er is niets live gedeployed vanuit deze chat.

Lokaal met de requirements geïnstalleerd:

```sh
python -m unittest discover -s tests -p 'test_*.py' -v
```

Voor de volgende test graag foutmeldingen, webcontainerlogs zonder credentials en de concrete stappen doorgeven. Geef geen koppelsleutels of databasewachtwoorden mee.

## Stoppen en back-up

Stop alleen de alpha-stack. Gebruik bij externe Compose-opdrachten steeds `-f compose.external-db.yaml`. Een normale `docker compose down` behoudt de standalone database; **`down -v` verwijdert het volume**. Voor je bestaande MariaDB gebruik je je bestaande back-upvoorziening. Bewaar `.env` privé en maak een databaseback-up vóór iedere volgende alpha-update. Open-sourcepublicatie en licentiecontrole blijven een apart besluit.
