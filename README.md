# Interactive Playlist System

IPS is a self-hosted jukebox for events. Guests request and vote for tracks from YouTube Music. Event managers configure request limits and fallback playlists. A connected IPS player plays the queue, and a TV monitor shows the current track, countdown, artwork and upcoming requests.

## Quick start

1. Download and extract the installation ZIP from [Releases](https://github.com/Terrorhawk/InteractivePlaylistSystem/releases), or clone this repository.
2. Copy `.env.example` to `.env`. Replace the example credentials and set `IPS_ADMIN_URL` to your full administration URL, such as `http://localhost:8044/manage`. Set `IPS_LANGUAGE` to `en` or `nl`.
3. Start IPS with its included MariaDB instance:

   ```sh
   docker compose up -d --build
   ```

4. Sign in at your administration URL. Create a site and assign the hostname visitors will use, such as `localhost` for a local test.
5. Open `/<event-slug>/admin` on that hostname to create an event. The visitor page is `/<event-slug>/` and the TV display is `/<event-slug>/monitor`.

## Connect a player

Playback requires a compatible IPS player. Serve IPS over HTTPS with a trusted certificate, preserve the original `Host` header in your reverse proxy, and set `IPS_ADMIN_URL` to the public HTTPS administration URL. Open the event's music controls and scan its pairing QR code in the player. Add a fallback playlist and start the queue from the controls.

## Other installation options

- **Existing MariaDB:** Create a dedicated database and user. Copy `.env.external-db.example` to `.env`, set the database connection values, and run `docker compose -f compose.external-db.yaml up -d --build`.
- **Portainer:** Build the local image with `docker build -t ips-multisite:0.2.10-alpha2 .`. Use `portainer-stack.yaml` for bundled MariaDB or `portainer-external-db.yaml` for an existing database. Set the corresponding environment variables in the stack.

This is an alpha release intended for testing.
