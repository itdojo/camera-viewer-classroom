# Camera Viewer

Camera Viewer shows, records and snapshots live video from Devices: small Linux hosts running MediaMTX, usually a Raspberry Pi with a camera, rebuilt every week or so. Each team runs one instance. A student adds a Device by address, label and credentials, and the instance finds that Device's Cameras and plays their Feeds in the browser.

One Docker image holds the whole instance. Inside it, a MediaMTX 1.21.2 Relay pulls every Camera from its Device, and a Python backend serves the UI, signs browsers in, proxies WebRTC signalling and owns the Relay's configuration. Browsers get video only from the Relay, over WebRTC. The words used here (Device, Camera, Feed, Relay, Capture) are the project's defined terms, and "ADR" numbers in comments name its recorded design decisions.

## Deploy an instance

You need a checkout of this repository on the machine that will run the instance, and Docker with Compose v2.24 or later (`docker compose version`). Compose reads its settings from a file named `.env` next to `compose.yaml`. Git ignores that file, so the password never gets committed.

The only setting you must provide is the team's password. Everything else has a default.

### On Linux

Linux runs the instance with host networking, so the Relay's WebRTC media leaves from the host's real address and needs no extra setup.

```sh
cd camera-viewer
cat > .env <<'EOF'
CV_PASSWORD=pick-a-team-password
TZ=America/Chicago
EOF
docker compose up -d --build
```

Open `https://<this host>:8080`, accept the certificate warning once (see "HTTPS and the UI certificate" below), and log in with the password. If the host runs a firewall, open 8080/tcp for the UI and 8189/udp for WebRTC media. Nothing else in the instance listens beyond localhost.

### On macOS (Docker Desktop)

Docker Desktop runs containers inside a VM, so host networking can't put WebRTC media on the Mac's own address. The macOS overlay, `compose.macos.yaml`, publishes the UI and the WebRTC port instead and tells the Relay which address to give browsers. That address is the one variable you set, `CV_HOST_IP`. Use the address other laptops reach this Mac on, which is usually the Wi-Fi or Ethernet address:

```sh
ipconfig getifaddr en0        # Wi-Fi on most Macs
ifconfig | grep 'inet '       # every address, if en0 prints nothing (USB Ethernet is often en5 or higher)
```

System Settings, under Network, shows the same address.

Then:

```sh
cd camera-viewer
cat > .env <<'EOF'
CV_PASSWORD=pick-a-team-password
TZ=America/Chicago
CV_HOST_IP=192.168.1.20
COMPOSE_FILE=compose.yaml:compose.macos.yaml
EOF
docker compose up -d --build
```

`COMPOSE_FILE` makes every `docker compose` command in this folder (`up`, `logs`, `down`) use both files, so you never have to type `-f` twice. Open `https://192.168.1.20:8080`, or `https://localhost:8080` on the Mac itself, and accept the certificate warning once. macOS may ask whether Docker may accept incoming connections. Allow it, or other laptops won't reach the instance.

If the Mac's address changes (a new Wi-Fi network, a DHCP lease that moved), update `CV_HOST_IP` and run `docker compose up -d` again. Video will fail to start in other browsers until you do, while the Mac's own browser keeps working, which makes this easy to miss.

### HTTPS and the UI certificate

The UI is served over HTTPS, so the team's password, the session cookie and the WebRTC signalling never cross the network in the clear. WebRTC media is encrypted either way (DTLS-SRTP). HTTPS also lets Full Screen keep a phone's screen awake while a Feed plays, since browsers grant that wake lock only to secure pages.

With no certificate of your own, the instance makes one the first time it starts: a self-signed certificate in `config/tls/generated-cert.pem`, with its key beside it, valid for 825 days. It covers `localhost`, the loopback addresses, the host's name, `CV_HOST_IP` and anything listed in `CV_TLS_SANS`, so add the names and addresses people type there:

```sh
echo 'CV_TLS_SANS=camera-viewer.local,192.0.2.5' >> .env
docker compose up -d
```

The instance keeps that certificate across restarts and rebuilds, and makes a new one only when the names it should cover change or it is a month from expiring. A new certificate means every browser warns once more.

Because no authority vouches for it, every browser warns the first time it opens the UI. Before you accept, check the fingerprint. The instance prints it at every start:

```sh
docker compose logs camera-viewer | grep fingerprint
# [supervisor] serving the UI at https://192.168.1.20:8080; its certificate's SHA-256 fingerprint is 3A:91:…
```

Compare it with the SHA-256 fingerprint the browser shows for the certificate (in Chrome, click "Not secure" in the address bar, then the certificate). If they match, accept it. Students make the same check for a Device's fingerprint. To stop the warning on one laptop for good, import `config/tls/generated-cert.pem` into its trust store (Keychain Access on a Mac, set to Always Trust).

Typing `http://` still works. The instance answers plain HTTP on the same port with a redirect to the `https://` address, so old bookmarks keep working.

**Your own certificate.** If you have one from a CA (a lab CA, or a real name from Let's Encrypt), put it, with any intermediates after it, and its private key in the config folder, and name them by their paths inside the container. The instance then serves yours and makes none of its own:

```sh
mkdir -p config/tls
cp fullchain.pem config/tls/cert.pem
cp privkey.pem   config/tls/key.pem
printf 'CV_TLS_CERT=/config/tls/cert.pem\nCV_TLS_KEY=/config/tls/key.pem\n' >> .env
docker compose up -d
```

Set both variables or neither. If only one is set, or a path doesn't point at a file, the instance refuses to start and says why in `docker compose logs`.

**Plain HTTP.** `CV_TLS=off` serves the UI over plain HTTP. Use it only behind a reverse proxy that terminates TLS for the instance, or on a test rig: with it off, the password crosses the network in the clear and Full Screen can't keep the screen awake. `CV_TLS=off` together with `CV_TLS_CERT` or `CV_TLS_KEY` refuses to start, because it is unclear which you meant.

**Upgrading from before HTTPS was the default.** After `git pull && docker compose up -d --build`, the UI moves to `https://` on the same port. The redirect carries old bookmarks across, and each browser shows the certificate warning once.

**What stays unencrypted.** A Manual Camera added with a plain `rtsp://` URL reaches the Relay in the clear, and its tile says "Unencrypted"; use `rtsps://` when the camera offers it. Inside the instance, the backend talks to the Relay over the host's loopback without TLS. Only the Relay's generated internal user may use those ports (ADR 0006), and reading loopback traffic takes root on the host.

### Where your data lives

The instance keeps everything in two folders next to `compose.yaml`, bind-mounted rather than kept in Docker volumes, so you can see, copy and back them up like any other folder.

- `captures/` holds Recordings and Snapshots. The UI's Captures list shows the same files, newest first, and previews, downloads or deletes them from any browser. Its dot-folders (`.recording/`, `.incoming/`) are the instance's working space; leave them alone. A file you add or delete here by hand shows in the list the next time a Capture is added or deleted, or when the page is reloaded.
- `config/` holds `devices.json` (every Device and Camera, with credentials), `recordings.json` (the Recordings a restart must not forget), `session-secret` (signs login sessions) and, if you added them, `tls/` and `maps/` (a Map Extract, below).

A Recording running when the instance stops is not lost. Its file in `captures/` is valid up to the moment the instance stopped, and the Recording is flagged Interrupted. It does not start again by itself when the instance comes back; start a new one if you still need it.

To update an instance, pull the new code and rebuild: `git pull && docker compose up -d --build`. To stop it, `docker compose down`. Neither touches the two folders.

### Device credentials are stored in plain text

`config/devices.json` holds every Device's username and password, and any password embedded in a manual Camera's RTSP URL, in plain text. The instance creates the file with mode 0600 (readable by its owner only) and keeps it that way on every rewrite. It does not encrypt it.

That is deliberate. The instance has to present those credentials by itself, after every restart, each time the Relay pulls a Camera or the backend calls a Device's API, with no person around to type a passphrase. Any key that could decrypt the file would have to live on the same host, next to it, so encryption would add steps without keeping out anyone who can already read the folder. Mode 0600 keeps out other ordinary users of the host. It does not keep out root, anyone in the `docker` group (which amounts to root), or anyone holding a copy of `config/`.

So treat `config/` as a secret. Don't commit it (`.gitignore` already excludes it), and think before copying it to a shared drive. The Device Kit limits the damage a leak can do. The credentials it generates belong to a dedicated instance user that may only read Camera streams and query the API, never publish or reconfigure. To rotate them, regenerate that Device's `.env` (delete it and run `gen-cert.sh` again), restart the Device, then remove and re-add the Device in the UI.

### Streets with no internet: a Map Extract

Two maps show where Devices are. Single View draws one below the Feed of any Device with a Position. Map View, opened from the Multi View | Map View switch in the masthead, fills the screen with one marker per Device. Choosing a marker, by click or with Enter or Space, opens the Map Preview, a panel on the map's right edge that plays that Device's first Camera at once, with its Feed Status, the Stall overlay, and the Unencrypted and recording badges. Below the video it shows the Device's Position as the Single View header does, with a Copy button for the coordinates (over plain HTTP, where browsers withhold the clipboard, Copy selects them for you to copy with the keyboard); a Device that loses its Position keeps playing and reads "No Position". Devices with no Position are listed over the map, and choosing a name there opens the Map Preview too. A Device with more than one Camera lists them all in the panel with their Feed Status, and choosing one switches the panel to it. Pointing at a marker, or reaching it with Tab, shows a card beside it with the Device's label, address and coordinates, and the Details button beside the zoom buttons shows every marker's card at once (on a phone, where there is no pointing, it is the way to see them); the browser remembers it, and the cards never open anything. Each marker is a button Tab reaches, and opening the panel moves focus to its close button; when the panel closes, focus goes back to the marker that opened it. Choosing another marker swaps the panel to that Device, and only one Map Preview plays at a time, so browsing the map never pulls more than one Feed. The close button, Esc, the browser's Back and leaving Map View close it, as does removing its Device; clicking the map does not. Its corner button puts the same Feed Full Screen with no reconnect, and Esc leaves Full Screen before it closes the panel. On a phone the panel is a sheet fixed along the bottom of the screen with the map above it, and turning the phone to landscape puts its Feed Full Screen, as in Single View. Its Open in Single View button opens the playing Camera in Single View, and every way back to Map View from there (the back button, Esc or the masthead switch) reopens the Map Preview on the Camera Single View was showing. A reload comes back to Map View with no Map Preview open. Map View fits every marker in view, clear of the Map Preview, when it opens, when a Device joins and when a marker leaves the view, and otherwise keeps the zoom you chose; dragging the map stops it following, and Recentre fits every marker again. With nothing else installed both maps draw a blank background with a coordinate grid and a scale bar, which shows movement and distance but no streets. A Map Extract adds the streets, and needs no internet once it is in place: it is one `.pmtiles` file covering your deployment area, cut from the [Protomaps](https://protomaps.com) planet build of OpenStreetMap data, and the instance serves it to every browser itself. The map's style, labels and icons ship inside the image, so nothing is fetched from anywhere else.

Cut it while you still have internet, on any machine with `curl`, `python3` and the `pmtiles` tool. Download `pmtiles` 1.31.2 or later for your system from [go-pmtiles' releases](https://github.com/protomaps/go-pmtiles/releases) and put it on your `PATH`; releases before 1.29.1 lack the `merge` command that `--world` needs. Then, from this folder:

```sh
scripts/cut-map-extract.sh --place "Monterey, California"            # a town or area, looked up by name
scripts/cut-map-extract.sh --bbox -121.95,36.55,-121.85,36.63 --name monterey   # or west,south,east,north
docker compose restart                                                 # the instance picks the extract at startup
```

The script measures the extract and asks before downloading it; a town at street level is typically tens of megabytes, and `--maxzoom 13` (or less) makes a wide area much smaller at the cost of street detail. It writes `config/maps/<name>.pmtiles` and never overwrites an existing extract without `--force`. A place name takes the bounding box OpenStreetMap's Nominatim gives its best match, so name a town or region rather than a single building, and check the match it prints. You can also copy a `.pmtiles` file into `config/maps/` by hand, from another machine or an earlier course. Run `scripts/cut-map-extract.sh --help` for every option.

If your Devices work in more than one place, such as a campus and a test range 200 km away, name every area in the same run. Repeat `--place` and `--bbox` as often as you need, in any mix, and the script cuts them all into one extract. Add `--world` to include the whole planet at zooms 0 to 6, about 45 MB more, so a map zoomed out between your areas shows coastlines and countries instead of a blank background. The world base is left out unless you ask for it, so a single-town extract stays small. With it, your areas are cut from zoom 7 up, and `--maxzoom` must be 7 or more. The size the script measures and asks you about covers the whole extract, world base included.

```sh
scripts/cut-map-extract.sh --place "Monterey, California" --place "Fort Hunter Liggett, California" --name field-sites --world
```

The instance uses one extract. With several `.pmtiles` files in `config/maps/`, it takes the most recently modified and logs a warning naming the others. Put several areas into one extract rather than dropping in one file per area. Every extract carries the low zooms that cover its area, so two extracts drawn together would show every label twice and stack their shading wherever they overlap. Cutting them together gives one copy of each tile. The extract is served only to signed-in browsers. Its data is © OpenStreetMap contributors under the [ODbL](https://opendatacommons.org/licenses/odbl/), and the map always shows that attribution. With internet, `CV_MAP_TILES` and `CV_MAP_IMAGERY` (under Configuration, "Map") add online streets and satellite imagery as well, and each browser chooses which to draw.

## Configuration

Every setting is an environment variable. Put it in `.env` next to `compose.yaml` and run `docker compose up -d` to apply it. A variable left empty (`FEED_DOWN_AFTER_SECONDS=`) keeps its default; one the instance can't read stops it from starting, with a "refusing to start" line in `docker compose logs` naming the variable. Every line of `.env` reaches the instance, so a setting needs no edit to the compose files. Keep settings in `.env` rather than exporting them in your shell. Compose reads both, but only `.env` reliably reaches the instance itself.

Each table below covers one concern.

### Access

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_PASSWORD` | none, required | The team's shared password. The instance refuses to start without it. Changing it and restarting ends every session. |
| `CV_SESSION_MAX_AGE_SECONDS` | `1209600` (14 days) | How long a login lasts. |
| `CV_LOGIN_MAX_FAILURES` | `10` | Wrong passwords in a row from one address before it is locked out. While locked out the login page says how long to wait, and even the right password is refused. A right password clears the count. |
| `CV_LOGIN_LOCKOUT_SECONDS` | `30` | The first lockout. Each further lockout without a successful login in between doubles it, up to 15 minutes. A restart forgives everyone. Behind one NAT, or Docker Desktop's published ports, a whole class shares one address, so one student's typos can lock everyone out for a while; keep these lenient there. |
| `CV_TLS` | `on` | `on` serves the UI over HTTPS, with your certificate if you supply one and otherwise with one the instance makes for itself. `off` serves plain HTTP, for a test rig or behind a proxy that terminates TLS. See "HTTPS and the UI certificate". |
| `CV_TLS_CERT` | unset | Path inside the container to your own PEM certificate for the UI, with any intermediates after it. Set with `CV_TLS_KEY`. |
| `CV_TLS_KEY` | unset | Path inside the container to that certificate's private key. |
| `CV_TLS_SANS` | none | Extra names the instance's own certificate covers, separated by commas: the DNS names and addresses people type to reach it, beyond `localhost`, the host's name and `CV_HOST_IP`. Changing it makes a new certificate, so browsers warn once more. |

### Network

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_HOST_IP` | none, required on macOS | The address the Relay offers browsers for WebRTC media, set through MediaMTX's `MTX_WEBRTCADDITIONALHOSTS`. Required on macOS; Linux host networking needs none for media. A DNS name works too. On either, the instance's own UI certificate covers it, so set it on Linux too if people reach the instance by that address. |
| `CV_HTTP_PORT` | `8080` | The port the UI listens on. On macOS it's also the published port. |
| `CV_WEBRTC_UDP_PORT` | `8189` | The UDP port for WebRTC media, set through `MTX_WEBRTCLOCALUDPADDRESS`. The Relay advertises its own port to browsers, so on macOS it's published under the same number. |
| `CV_HTTP_HOST` | `0.0.0.0` | The address the UI listens on inside the container. |

`COMPOSE_FILE` is read by Docker Compose, not the instance. Set it on macOS as shown above.

### Devices

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_CERTIFICATE_CHECK_SECONDS` | `5` | How often the instance reads each Device's certificate, so a rebuilt Device shows "certificate changed" within that time. |
| `CV_QUALITY_CHECK_SECONDS` | `10` | How often the instance reads each reachable Device's start time, so a restarted Device gets its Quality back within that time. |
| `CV_QUALITY_CONFIG_CHECK_SECONDS` | `60` | How often the instance also reads each reachable Device's Camera settings, so a Quality change made by another instance shows within that time. They are read after a restart, when a Device is added and before each Quality change as well. |
| `CV_DEVICE_PROBE_SECONDS` | `2` | How often the instance checks that each Device's API port still accepts a connection. A Device that keeps failing for `FEED_DOWN_AFTER_SECONDS` shows Offline. |

### Time

| Variable | Default | What it does |
| --- | --- | --- |
| `TZ` | `UTC` | The instance's local time zone, as an IANA name such as `Europe/London`. Capture file names use it, so set it to the classroom's wall clock. |

### Feed Status thresholds

Feed Status turns Relay counters into Live, Degraded, Down, Idle or Offline. Lossy LTE links and wired labs need different limits. Pulls run over TCP, but a Device whose uplink can't keep up drops packets from its send queue, and the Relay counts the gaps as RTP loss, so Degraded comes from loss and errored frames. Falling bitrate is not a signal, because a still scene encodes well under its target.

| Variable | Default | What it does |
| --- | --- | --- |
| `FEED_DOWN_AFTER_SECONDS` | `5` | A source not ready or in error for this long is Down. Shorter than this it's Degraded, or still Idle if the Feed is only just being opened. A Device's probes must also fail this long before it shows Offline. |
| `FEED_DEGRADED_WINDOW_SECONDS` | `10` | How far back the Degraded rules look. |
| `FEED_RTP_LOSS_MAX_PERCENT` | `1` | Degraded when RTP loss in the window goes above this. |
| `FEED_ERRORED_FRAMES_MAX` | `0` | Degraded when more errored frames than this arrive in the window. The default means any errored frame. |
| `CV_FEED_SAMPLE_SECONDS` | `1` | How often the instance reads the Relay's counters for every Feed. |
| `FEED_STALL_SECONDS` | `2` | A browser shows a Stall overlay on a Feed that has decoded no new frame for this long while it is Live. Each browser judges this for itself, so a Stall never changes the Feed Status anyone else sees. |

### Auto Quality

On Auto, the instance lowers a Camera's Quality when its Feed keeps losing packets and tries a higher one once the Feed has been clean for a while (ADR 0009). It judges loss in windows, and a window is lossy when its loss would make the Feed Degraded (`FEED_RTP_LOSS_MAX_PERCENT`). Encoders send less than their target on a still scene, so the only way to learn that a link carries more is to try: each step up is on trial, and a failed one doubles the wait before the next. A lossy LTE lab may want longer waits; a wired lab can shorten them.

| Variable | Default | What it does |
| --- | --- | --- |
| `QUALITY_WINDOW_SECONDS` | `10` | The length of one window. Windows start afresh at every step. |
| `QUALITY_DOWN_LOSSY_WINDOWS` | `2` | Auto steps down one level when this many of the last `QUALITY_DOWN_LOOKBACK_WINDOWS` windows are lossy. A Feed that goes Down while its Device is still reachable steps down at once. |
| `QUALITY_DOWN_LOOKBACK_WINDOWS` | `3` | How many recent windows the step-down rule looks at. It can't be less than `QUALITY_DOWN_LOSSY_WINDOWS`. |
| `QUALITY_UP_CLEAN_SECONDS` | `60` | Auto tries one level up after the Feed has been this long without a lossy window. After a step up holds, the wait goes back to this. |
| `QUALITY_UP_TRIAL_SECONDS` | `30` | A step up is on trial this long. A lossy window in that time sends it back down and doubles the wait. |
| `QUALITY_UP_WAIT_MAX_SECONDS` | `960` (16 minutes) | The longest the doubling wait gets, so a link that stays bad costs only a few tries an hour. It can't be less than `QUALITY_UP_CLEAN_SECONDS`. |
| `QUALITY_UP_DISRUPTIVE_CLEAN_SECONDS` | `300` | The least clean time before a step up that drops viewers for a few seconds: CSI's 720p to 1080p step, and every USB and test-pattern step. While a Camera is Recording, Auto never takes such a step, because it would split the Recording. |

### Positions

A Device running the GPS Sidecar (see "GPS Sidecar" under Device Kit) streams its fixes to the instance, which shows the Device's Position in the Single View header. The instance looks for a Sidecar on port 9998 of every Device it has an API for, over the same pinned certificate and credentials as the Device API, so a Sidecar started after its Device was added is found without removing and re-adding the Device. Every line a Sidecar sends is checked before it is shown, and an impossible one is dropped. A Device sends one fix every 30 seconds, and one a second while any browser has one of its Cameras in Single View or shows Map View.

A Device with no GPS receiver, such as a Camera mounted on a pole, can be given a Fixed Position from the Position line of its entry in the Devices Panel, which reads GPS, Fixed or None and the coordinates: a latitude and longitude in decimal degrees, refused if either is out of range. A fresh fix always wins over it, and it shows again once the fix goes stale. Fixed Positions are kept in `config/positions.json` (mode 0600) and survive a restart; removing the Device deletes its Fixed Position.

| Variable | Default | What it does |
| --- | --- | --- |
| `POSITION_STALE_SECONDS` | `10` | A Position whose last fix is this old is stale: it stays on screen greyed out, with its age. That holds while the Device streams one fix a second; at the idle rate the threshold is `POSITION_IDLE_SECONDS` plus this, 40 seconds by default (ADR 0012). |
| `POSITION_TRAIL_SECONDS` | `600` (10 minutes) | How far back each Device's Trail on the Single View and Map View maps reaches. The Trail is kept in the instance's memory only, so a restart starts it afresh, and removing the Device discards it. |
| `POSITION_PROBE_SECONDS` | `30` | How often the instance tries each Device's Sidecar port again while it has found no Sidecar there, and the longest it waits before reconnecting a Sidecar whose stream dropped. |
| `POSITION_IDLE_SECONDS` | `30` | How often a Device streams a fix while no browser has one of its Cameras in Single View or shows Map View, from 1 to 3600. While it streams this slowly, a Position goes stale only after this long plus `POSITION_STALE_SECONDS`. |
| `POSITION_DEMAND_LINGER_SECONDS` | `25` | How long a Device keeps streaming one fix a second after the last browser with one of its Cameras in Single View, or showing Map View, stopped asking (ADR 0012). Browsers ask every 10 seconds, so keep it above 10. |

### Map

A map draws the Basemap its browser chooses from those the instance has, with a Basemap switcher in the map's top-left corner: Streets from the Map Extract (see "Streets with no internet: a Map Extract"), Streets online from the tile server below, Satellite from the imagery server below, and Blank, a background with a coordinate grid. The choice is kept in that browser and shared by every map in it, Single View's and Map View's. On Streets from a Map Extract, a 3D button beside the switcher raises the buildings and tilts the map, and pressing it again lays the map flat, north up; that is kept in the browser too, and 3D comes back with Streets after another Basemap. A browser that hasn't chosen, or chose one the instance no longer has, gets the Map Extract, then the online tiles, then Blank, so Satellite is only ever drawn by choice. With only Blank there is no switcher. The instance checks both templates at startup and refuses to start if either isn't an `http://` or `https://` URL containing `{z}`, `{x}` and `{y}`.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_MAP_TILES` | unset | An online raster tile server for a classroom with internet, as a URL template such as `https://tile.example.org/{z}/{x}/{y}.png`. It must be `https://` while the UI is HTTPS, because browsers block plain-HTTP tiles on a secure page; the instance refuses to start otherwise. Each browser fetches the tiles from that server itself, so tile requests reveal roughly where viewed Cameras are to whoever runs it; a Map Extract avoids that. The template reaches every signed-in browser, API key included. With a Map Extract as well, each browser chooses between them, and the extract is the default. Check the server's usage policy before pointing a class at it; OpenStreetMap's own tile servers permit only light use. |
| `CV_MAP_TILES_ATTRIBUTION` | `© OpenStreetMap contributors` | The attribution the map shows, always visible, for those tiles, as plain text. The default fits most raster tile servers, which draw OpenStreetMap data; name the tile provider too if its terms ask for it. |
| `CV_MAP_IMAGERY` | unset | A satellite imagery server for the Satellite Basemap, as a raster URL template such as `https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}`. There is no default provider; "Choosing an imagery server" below names some. The rules of `CV_MAP_TILES` apply: it must be `https://` while the UI is HTTPS, and the instance refuses to start otherwise. Each browser fetches the imagery from that server itself, so imagery requests reveal roughly where viewed Cameras are to whoever runs it, and the template reaches every signed-in browser, API key included. A browser draws Satellite only when its viewer chooses it. |
| `CV_MAP_IMAGERY_ATTRIBUTION` | `Imagery: <host>` | The attribution the map shows, always visible, for the imagery, as plain text, such as `Source: Esri, Vantor, Earthstar Geographics, and the GIS User Community`. Unset, the map credits the server by its host name, which names no provider, so set the credit your provider's terms ask for. |

Choosing an imagery server: NAIP, the US Department of Agriculture's aerial imagery, is public domain and needs no key, and the USGS National Map serves it as tiles at the example template above (USGS asks for the credit `U.S. Geological Survey, National Geospatial Program`). It is detailed only in the United States. Its tiles stop at level 16. The map asks for deeper levels, up to 19, because other servers have them; NAIP answers those with a 404, which the browser's console lists, and the map draws its level-16 imagery enlarged, so the closest zooms turn blurry but never blank. Outside the US it shows only coarse world imagery at low zoom. Detailed imagery elsewhere needs a key: Esri World Imagery (`https://ibasemaps-api.arcgis.com/arcgis/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}?token=<key>`), MapTiler Satellite (`https://api.maptiler.com/maps/satellite/{z}/{x}/{y}.jpg?key=<key>`) or Mapbox Satellite (`https://api.mapbox.com/v4/mapbox.satellite/{z}/{x}/{y}.jpg90?access_token=<token>`). Read the plan's terms before pointing a class at one, because a paid course is probably commercial use. MapTiler's free plan forbids commercial use, and so does EOX's keyless Sentinel-2 cloudless imagery, while Esri's and Mapbox's free tiers allow it within a monthly quota. Mapbox requires a separate licence for any application related to vehicles, drones included. Each of the three requires its credit in `CV_MAP_IMAGERY_ATTRIBUTION`, and Mapbox, like MapTiler on its free plan, also requires a logo, which a plain-text attribution can't show. Esri's older `server.arcgisonline.com` address answers without a key, but its licence covers only use with Esri software.

### Disk thresholds

The instance watches free space on the filesystem holding `captures/`. It warns below 10% or 5 GB, refuses new Recordings and Snapshots below 1 GB, and finalizes every active Recording below 500 MB, so a full disk never leaves a corrupt file.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_DISK_WARN_PERCENT` | `10` | The UI warns when free space falls below this percentage of the disk. |
| `CV_DISK_WARN_BYTES` | `5G` | The UI also warns when free space falls below this size, whatever the percentage. |
| `CV_DISK_REFUSE_BYTES` | `1G` | Below this, new Recordings and Snapshots are refused with a message saying why. Recordings already running carry on. |
| `CV_DISK_FINALIZE_BYTES` | `500M` | The low-disk cutoff: below this, every active Recording is stopped and its file finalized, so it stays playable. |
| `CV_DISK_CHECK_SECONDS` | `5` | How often free space is measured and pushed to every browser, and so how often the cutoff can act. |

Sizes are bytes, or a number with `K`, `M`, `G` or `T` (decimal: `1G` is 1,000,000,000 bytes, as disks are sold). Each threshold acts on its own, so keep the cutoff below the refuse size, as the defaults do; a small SD card might use `CV_DISK_REFUSE_BYTES=500M` and `CV_DISK_FINALIZE_BYTES=200M`. On macOS, Docker Desktop reports a bind mount's free space rounded to a few GB, so the UI's figure moves in steps there; on Linux it is exact.

### Recording presets

Timed Recordings stop on their own after a preset duration.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_RECORDING_PRESETS` | `30,60,300` | The durations offered for a timed Recording, in whole seconds, separated by commas. The timer counts wall-clock time from the click, so a Feed drop eats into it. The instance refuses to start if any value is not a positive whole number, or if a value appears twice. |

### Snapshots

A Snapshot is the frame on screen, uploaded by the browser as a PNG at the Feed's full resolution. The instance stores only a well-formed PNG and names it itself.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_SNAPSHOT_MAX_BYTES` | `67108864` (64 MiB) | The largest Snapshot upload the instance accepts. A 1080p frame is well under 10 MB, so the default only matters for Feeds above 4K. |

### Set by the image

The image sets these to match its own layout. They exist for development and tests, and changing them in a deployment breaks the instance.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_CONFIG_DIR` | `/config` | The config folder inside the container. |
| `CV_CAPTURES_DIR` | `/captures` | The captures folder inside the container. |
| `CV_FRONTEND_DIR` | `/app/frontend` | The UI's static files. |
| `CV_RELAY_CONFIG` | `/etc/camera-viewer/relay.yml` | The Relay's MediaMTX config. |
| `CV_RELAY_API_URL` | `http://127.0.0.1:19997` | The Relay's API, on loopback only. |
| `CV_RELAY_WHEP_URL` | `http://127.0.0.1:18889` | The Relay's WHEP signalling, on loopback only. |
| `CV_RELAY_RECONCILE_SECONDS` | `2` | How often the backend checks the Relay's paths against what it should have. |

### Set by the supervisor

At every start the image's supervisor makes up the Relay's one internal user and hands it to the Relay and the backend, and to nobody else. The Relay's API, WebRTC signalling and RTSP server listen on loopback, which under Linux host networking is the whole host's, so without it any local user or host-networked container could reconfigure the Relay or pull Feeds without logging in. Setting these yourself does nothing: the supervisor overwrites them.

| Variable | Default | What it does |
| --- | --- | --- |
| `CV_RELAY_USER` | generated | The internal user's name, as the backend reads it. |
| `CV_RELAY_PASS` | generated | Its password, new at every start. |
| `MTX_AUTHINTERNALUSERS_0_USER` | generated | The same name, as the Relay reads it. `relay/relay.yml` holds an unusable placeholder in its place. |
| `MTX_AUTHINTERNALUSERS_0_PASS` | generated | The same password, as the Relay reads it. |

Variables starting with `MTX_` reach the Relay and override its MediaMTX config. The compose files set the two WebRTC ones above. Overriding any other is unsupported, because the backend assumes the Relay listens where `relay/relay.yml` says.

### Set by the Relay

MediaMTX sets these when it runs the Recorder's segment hooks, and the hook reads them. Nobody sets them by hand, and setting them on the container does nothing.

| Variable | Default | What it does |
| --- | --- | --- |
| `MTX_PATH` | set per hook run | The Relay path whose recording segment was created or completed. |
| `MTX_SEGMENT_PATH` | set per hook run | That segment's file, which the backend places as a Capture. |

## Device Kit

`device-kit/` has what you need to build a Device: one folder per variant with its compose file and MediaMTX config, and `gen-cert.sh`, which makes the Device's self-signed certificate and its instance-user credentials. A Device built from the kit has authentication on, RTSPS only, and its API over HTTPS, and the instance pins its certificate by fingerprint, so you don't need a certificate authority.

Every instance serves the kit it was built with at `/device-kit.tar.gz`, so a Device needs no copy of this repo. Logged in to the instance, download it in the browser (the UI's Device setup guide links it), then copy it to the Device and unpack it there:

```sh
scp ~/Downloads/device-kit.tar.gz <user>@<device>:~
ssh <user>@<device>
tar xzf device-kit.tar.gz && cd device-kit/<variant>
```

If the browser unpacked the download into a `device-kit` folder, copy the folder instead, with `scp -r ~/Downloads/device-kit <user>@<device>:~`. The kit's own `README.md` carries the guide's steps, for reading on the Device. The archive never holds a Device's `.env`, certificate or key, even from a build machine that has run the kit, so every Device generates its own.

A Device needs:

- a 64-bit OS, such as Raspberry Pi OS (64-bit) on a Pi 4 or Pi 5, or any arm64 or amd64 Linux;
- Docker Engine with the Compose plugin (`docker compose version` should answer);
- the camera attached before you start: a CSI module on its ribbon, or a USB webcam. The test-pattern variant needs no camera at all;
- `bash` and `openssl`, for `gen-cert.sh`;
- 8322/tcp (RTSPS) and 9997/tcp (the API) reachable from the instance, and 9998/tcp too if it runs the GPS Sidecar. The instance learns only the API's port, from the address you add the Device by, so 8322 and 9998 must be published under those numbers.

To bring one up, run `gen-cert.sh` once in the variant's folder, then start it. The test pattern needs no hardware and makes a known-good Feed to compare against:

```sh
cd device-kit/test-pattern
../gen-cert.sh          # prints the fingerprint, username and password
docker compose up -d
```

A Pi camera module Device is the same two steps in `device-kit/csi`, on a Pi 4 or Pi 5 running 64-bit Raspberry Pi OS (Bookworm or Trixie). Connect the module with the Pi powered off and check that `rpicam-hello --list-cameras` lists it before you start. It publishes the Camera `csi` at 1920×1080 and 15 fps, H.264 at 1.5 Mbit/s with no B-frames (Quality Medium), straight from MediaMTX's own camera support (its `-rpi` image). The instance changes the Quality from the Feed's gear menu, up to 1080p30 at 4 Mbit/s, through the Device API, and puts it back after the Device restarts. The container runs unprivileged: run `gen-cert.sh` on the Pi itself, because besides the certificate it lists that Pi's camera device nodes (`/dev/media*`, `/dev/video*`, `/dev/v4l-subdev*`, `/dev/dma_heap`) in `compose.override.yaml`, which `docker compose` picks up by itself. Those nodes differ between a Pi 4 and a Pi 5, so the list is never copied between Pis; if `docker compose up` later complains that a `/dev` path does not exist, after a kernel update for instance, re-run `gen-cert.sh` and start it again. Because libcamera finds the module itself, this variant needs no `--camera` and no `/dev/v4l/by-id/` path; a Pi with only a camera module has no `by-id` folder at all, just `by-path`, and that is expected. The Pi 4 encodes in hardware; the Pi 5 has no H.264 encoder and encodes in software, so watch `docker stats` the first time a Feed plays. If the camera is missing or badly seated, the Device still starts and adds with its Camera `csi`, which shows Down, and `docker compose logs` repeats a `[RPI Camera source] camera_create(): ...` error every few seconds. Power off, reseat the ribbon, and start the Device again. The Camera plays once its camera works, without removing and re-adding the Device.

A camera module mounted upside down or sideways is flipped on the Device, in the kit's `.env`, where `gen-cert.sh` writes both switches commented out: `CAMERA_HFLIP=true` mirrors the picture left to right, `CAMERA_VFLIP=true` turns it top to bottom, and the two together rotate it 180°. Run `docker compose up -d` again to apply a change. Leave `mediamtx.yml` alone, so a `git pull` of the kit never conflicts with a Device's own settings. Each switch takes only `true` or `false`; anything else stops MediaMTX from starting, with an `invalid value` line in `docker compose logs`.

A USB webcam Device is the same two steps in `device-kit/usb`, with the webcam plugged in first. It publishes the Camera `usb` at 1280×720 and 15 fps (Quality Medium): ffmpeg captures the webcam's MJPG mode and encodes H.264 at 1 Mbit/s with no B-frames, which browsers need. The Feed's gear menu changes the Quality, from 640×360 at 10 fps up to 1280×720 at 30 fps and 2 Mbit/s, and each change restarts ffmpeg, so viewers lose the picture for a few seconds. `gen-cert.sh` finds the webcam and saves its stable `/dev/v4l/by-id/` path in `.env` as `USB_CAMERA`, so the kit never depends on `/dev/videoN`, whose numbers shift between boots. With two webcams attached it won't guess; name one with `--camera`, either by its full path or by the bare name `ls /dev/v4l/by-id` prints:

```sh
cd device-kit/usb
ls /dev/v4l/by-id/                                   # webcams, if there are several
../gen-cert.sh --camera /dev/v4l/by-id/usb-046d_C922_Pro_Stream_Webcam_2E4A0F8F-video-index0
docker compose up -d
```

The encode is in software, and on a Pi 5 it costs about half of one core per webcam. To flip a webcam's picture, uncomment the `USB_CAMERA_FILTER` line `gen-cert.sh` left in `.env` and set it to `hflip` (left to right), `vflip` (top to bottom) or `hflip,vflip` (180°), then `docker compose up -d`; ffmpeg applies it as a video filter before encoding. A second webcam publishes as `usb-2`; the comments in the variant's `compose.yaml` say which lines to uncomment.

The Device's clock has to be within an hour of the instance's. RTSPS keys its encryption with a timestamped message, so a Device whose clock has drifted further (a Pi with no internet, so no NTP) refuses the instance's pull, and its Feeds never start even though the Device adds fine. The instance reads the Device's clock from its API's answers. Adding such a Device warns how far off its clock is, its entry under Devices keeps saying so, and its Feeds show Down "Device clock wrong" until the clock is fixed. Check with `timedatectl` on the Device, then enable NTP (`sudo timedatectl set-ntp true`, which needs a time server the Pi can reach) or, with no time server in reach, set the clock by hand (`sudo timedatectl set-ntp false`, then `sudo timedatectl set-time "2026-10-13 14:05:00"`, since `set-time` refuses while NTP is on). If every Device shows it, the instance's clock is the wrong one. The Feeds recover within about 10 seconds.

Keep that output. In the UI, under Devices, enter the Device's address, a label (lowercase, digits and hyphens, such as `cam-2`), the username and the password, and choose Check fingerprint. Confirm only if the fingerprint shown matches the one `gen-cert.sh` printed. A Camera whose source is not working when you add the Device is still listed, and shows Down until its source comes up. A weekly rebuild gives the Device a new certificate. The instance refuses it and shows "certificate changed" until you check the new fingerprint against what `gen-cert.sh` printed and trust it, which re-pins the Device. If the rebuild also changed the Device's Cameras, remove the Device and add it again instead.

### GPS Sidecar

A Device with a GNSS receiver can report where it is. Every variant carries an optional GPS Sidecar (`device-kit/gps/`), which reads the receiver's NMEA (RMC, GGA and VTG) and serves its fixes over HTTPS on 9998/tcp (fixed, since that is the only port the instance looks for it on), with the Device's certificate and the same username and password as its API, so there is nothing new to generate or confirm. It runs on the stock `python:3.13-slim` image with no image build and no `gpsd`, so a Device with no internet needs only that image pulled once. The instance connects to it; it never connects to the instance. It reads any receiver that sends NMEA over a serial port, such as a USB puck (a u-blox, say) or an LTE HAT's NMEA port. The Sidecar only reads, so an LTE HAT's GNSS has to be switched on through its AT port first (on a Quectel modem, `AT+QGPS=1`, or `AT+QGPSCFG="autogps",1` to have it start at boot).

Plug the receiver in, run `gen-cert.sh` (it finds the receiver under `/dev/serial/by-id/` and saves its stable path in `.env` as `GPS_TTY`, as it does for a webcam), then start the Device with the `gps` profile. Without the profile the Device runs exactly as it did before. Stop it with the profile too, because a plain `docker compose down` leaves the Sidecar running:

```sh
cd device-kit/usb                       # or csi, or test-pattern
../gen-cert.sh                          # "GNSS receiver: /dev/serial/by-id/..."
docker compose --profile gps up -d
docker compose --profile gps down       # when you are done
```

`gen-cert.sh` takes a name that says GPS, GNSS or u-blox, and an LTE HAT's interface 01 (Quectel EC25/EG25, SIMCom SIM7600); with two receivers it won't guess and lists both. Anything else, such as a GPS on the Pi's own serial pins, you set by hand in `.env`, with its baud rate in `GPS_BAUD`. If the receiver is missing or can't be read, the Sidecar exits with a line in `docker compose --profile gps logs gps` saying what to fix, and Docker restarts it. `Device or resource busy` there means something else on the Device holds the receiver, usually a gpsd, which opens it exclusively: stop it with `sudo systemctl disable --now gpsd.socket gpsd.service`, or stop the container running it, since `systemctl` won't show one started by Docker. A `GPS_TTY` path that doesn't exist at all stops `docker compose up` with that path in the error, so plug the receiver in or re-run `gen-cert.sh`.

No receiver? `GPS_SOURCE=replay:sample.nmea` in `.env` loops a recorded track at real-time speed instead: one lap of a 200 m circle at 8 m/s. With the test-pattern variant that makes a fully simulated moving Camera on any laptop. To replay your own NMEA log, put it in `device-kit/gps/tracks/` and name it the same way.

| Variable | Default | What it does |
| --- | --- | --- |
| `GPS_SOURCE` | `tty` | Where fixes come from: `tty` reads the receiver at `GPS_TTY`, and `replay:<file>` loops a track from `device-kit/gps/tracks/` instead. |
| `GPS_TTY` | set by `gen-cert.sh` | The receiver's stable `/dev/serial/by-id/` path (or `/dev/ttyAMA0` for the Pi's serial pins). Never `/dev/ttyACM0`, whose number can change between boots. |
| `GPS_BAUD` | unset | A serial receiver's baud rate, such as `9600` or `115200`. USB receivers ignore it. |

## Development

Python 3.12+ with `uv`, and Docker for the tests:

```sh
uv run playwright install chromium   # once, for the browser tests
uv run pytest                        # everything; needs Docker running
```

The tests run every instance through `compose.yaml` plus `compose.macos.yaml`, the same files a maintainer uses, on ephemeral loopback ports.

Install the git hooks once per clone:

```sh
uv run pre-commit install --hook-type pre-commit --hook-type commit-msg
uv run pre-commit run --all-files    # what CI runs
```

Before each commit they lint the Python with ruff, check with deptry that every imported package is declared in `pyproject.toml` (and every declared one used), and run `node --check` on `frontend/js/*.js`. The commit-msg hook refuses a message that would keep git's comment lines, such as the `# Conflicts:` block `git commit --no-edit` leaves after a conflicted merge. CI (`.github/workflows/ci.yml`) runs the same hooks and the Docker-free tests on every pull request, and the full suite in a second job.

The hardware tests in `tests/device_kit/test_*_hardware.py` take a real Device from `CV_HW_*` variables; their docstrings say how. Without `CV_HW_CSI_ADDRESSES`, the CSI one runs against a test stand-in instead: the CSI kit with an ffmpeg test pattern encoded to the Pi's 1080p30 profile in place of the camera (`tests/harness/csi-stand-in.yaml`, never for students). That proves the instance handles a CSI-shaped Feed, not that a camera module works on a Pi, so the real-hardware run still has to happen.
