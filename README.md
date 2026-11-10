# Interactive Playlist System

IPS is a self-hosted jukebox for events. Guests request and vote for tracks from YouTube Music. Event managers configure request limits and fallback playlists. A connected IPS player plays the queue, and a TV monitor shows the current track, countdown, artwork and upcoming requests.

## Quick start

1. Download and extract the installation ZIP from [Releases](https://github.com/Terrorhawk/InteractivePlaylistSystem/releases), or clone this repository.
2. Copy `.env.example` to `.env`. Replace the example credentials and set `IPS_ADMIN_URL` to your full administration URL, such as `http://localhost:8044/manage`. Set `IPS_LANGUAGE` to `en` or `nl`.
3. Start IPS with its included MariaDB instance:

   ```sh
   docker compose up -d --build
   ```

## Set up your first event

1. **Create a site.** Sign in at `IPS_ADMIN_URL` with the global administrator account from your `.env`. In **Sites**, choose a site name and add the hostname people will use to reach it. For a local test, enter `localhost`. For a hosted setup, enter a hostname such as `jukebox.example.com` without a scheme, port or path. A site can host multiple events.
2. **Choose an event URL.** On the site's hostname, open `/<event-slug>/admin`. For example, open `http://localhost:8044/summer-party/admin` to create an event with the slug `summer-party`. Sign in there if prompted. Use lowercase letters, numbers and hyphens for the slug.
3. **Create the event.** Enter the display name, review the request and voting rules, tick **Create this new event**, then click **Create event**. Repeat with a different slug to create another event on the same site.

A global or site administrator can choose a default event at `/site/settings` on the site's hostname. Visitors who open the hostname directly then go straight to that event. In global administration, use **Users** and **Access rights** to give someone access to a specific site or event.

## Run the jukebox

- **Manage playback.** Open `/<event-slug>/admin`. Use **Event settings** to change request rules or the offline message.
- **Connect a player.** Playback requires a compatible IPS player. Serve IPS over HTTPS with a trusted certificate, preserve the original `Host` header in your reverse proxy, and set `IPS_ADMIN_URL` to the public HTTPS administration URL. In the event's player controls, open **Pair or replace player** and scan its QR code in the player.
- **Add fallback music.** In **Fallback playlists**, add a YouTube Music playlist by URL or ID. IPS uses these tracks when no request is eligible to play. Click **Start queue** when you're ready.
- **Share the event.** Guests open `/<event-slug>/` to search for tracks, request songs and vote on the queue. Open `/<event-slug>/monitor` on a TV to show the current track, countdown and upcoming music.

## Other installation options

- **Existing MariaDB:** Create a dedicated database and user. Copy `.env.external-db.example` to `.env`, set the database connection values, and run `docker compose -f compose.external-db.yaml up -d --build`.
- **Portainer:** Build the local image with `docker build -t ips-multisite:0.2.10-alpha2 .`. Use `portainer-stack.yaml` for bundled MariaDB or `portainer-external-db.yaml` for an existing database. Set the corresponding environment variables in the stack.

This is an alpha release intended for testing.
