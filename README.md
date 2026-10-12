# Unifi Camera Bridge 1.6.1

One Docker container, a local web interface, and a persistent settings volume.
Add, edit, disable, test, restart and remove cameras without editing per-camera
Compose files. No Docker socket, cloud account, or Protect patch is required.

## Automatic camera setup

The default **Automatic · inspect camera** option reads ONVIF identity, services,
profiles and source configurations, retrieves RTSP addresses, and measures the
actual video and audio with ffprobe before offering an adoption plan. It groups
profiles by video source, then selects native HQ and lower-resolution LQ from the
same source. HQ is preserved. When native HQ/LQ codecs differ, only the native
LQ stream is converted to match HQ for Protect. Missing LQ is never generated
from HQ or advertised as available.

Automatic setup supports verified H.264/H.265 video with PCMA, PCMU or AAC audio.
It is not a promise of compatibility with every ONVIF implementation. Ambiguous
source identities, unreadable streams and unsupported codecs require review.
Profiles that cannot be verified are excluded. The review shows resolutions,
frame rates, audio and any fallback or codec conversion. Automatic mode also discovers and exposes continuous pan/tilt when the profile,
PTZ configuration and velocity space can be verified. Movements are limited to
two seconds with an independent Stop watchdog. Optical zoom, home, presets are not exposed in automatic mode. Existing verified presets
remain available for CAM-S3W, CAM-C44HE and CAM-PT2.

The **CAM-PT2 · verified pan/tilt preset** uses the same tested generic bridge
as Automatic setup, checking the camera model, native H.264 1920×1080 feed,
audio and PTZ configuration before saving. Verified on firmware 1.1.0 with
Protect 7.2.105, it exposes one native HQ stream, preserves supported audio,
and offers pan/tilt where verified. It does not advertise the broken SD profile
or synthesize LQ. Use Automatic to investigate different firmware capabilities.
Existing automatically configured cameras keep their settings and identity.

A known CAM-C44HE quirk supplies its missing lens paths, but measures the actual
media rather than assuming its resolution. A SONOFF SD-address fallback is only
accepted if the native stream's measured resolution matches its advertised
profile. Camera credentials and full source URIs stay on the server. Inspection
results expire after 15 minutes if not saved.

The default port pool supports 16 views. Each source gets its own persistent
ONVIF/RTSP port pair and identity. Conversion load depends on the selected native
LQ codecs; native streams are relayed without video re-encoding.

## Standard RTSP cameras (including other brands)

Choose **Add camera → RTSP camera · any brand** for a camera that provides local
RTSP video but no usable ONVIF service. Enable RTSP in its app first, then enter
its LAN address, RTSP login, main stream path and optional native lower-quality
stream path. Full `rtsp://` URLs for that same camera are also accepted; remove
embedded credentials and enter those in the login fields. Paths use the RTSP
port in More settings; full URLs use their own port (554 when omitted).

The bridge measures every supplied stream and creates one ONVIF view with the
correct codec, resolution, frame rate and supported audio. Video must be H.264
or H.265 over RTSP/TCP; audio may be AAC, PCMA or PCMU. HQ stays native. A genuine
lower-resolution LQ stream is optional; if its codec differs, only LQ is
converted to match HQ. Invalid LQ fails inspection rather than silently dropping
it. Add separate entries with different main paths for different lenses.

For an anonymous camera stream, select **Camera stream requires no login** and
choose a username/password in the login fields to protect the bridge. Otherwise
use the camera's RTSP credentials. Adopt in Protect with those same credentials
and the copied bridge IP:port. Video, audio and authenticated snapshots use the
existing generic bridge. RTSP alone supplies neither camera motion events nor
PTZ controls, so these services are not advertised. Aqara model compatibility
requires a working local RTSP feed; no proprietary Aqara integration is assumed.

Source URLs, including query tokens, remain private on the server. As with
Automatic mode, connection settings are locked after inspection; remove and add
a new entry to change them. **Test connection** verifies all configured streams
and flags changed codec/resolution/audio settings. This release was tested in
Docker with authenticated and anonymous RTSP sources; real Aqara testing is
pending a camera and its stream paths.

## Install and update from GitHub Container Registry

Published releases include a small Debian installer and ready-to-use Docker /
QNAP Compose downloads. Release builds publish one multi-platform GHCR image for
x64 (amd64) and ARM64. `stable` follows verified releases; numbered version tags
are retained for pinning and rollback. Public images need no GitHub login.

For Debian 12/13, download the Debian installer from the repository's Releases
page, extract it, enter its directory and run `sudo ./install.sh`. It installs
Docker/Compose if needed, selects the host CPU, pulls the image and preserves an
existing installer-managed deployment's settings and network bindings. Internet
access is required. Complete first-admin setup at the printed address.

To update an installer-managed Debian deployment:

```sh
cd /opt/sonoff-protect-bridge
sudo docker compose -p sonoff-protect-bridge pull manager
sudo docker compose -p sonoff-protect-bridge up -d --no-build manager
```

The image downloads while the old container runs. Recreating the container briefly
interrupts video; settings stay in the same volume. Keep the existing project,
volume and port mappings so Protect can reconnect to the same camera identities.
The internal `sonoff-protect-bridge` names are retained for upgrade compatibility.
Plain Compose updates do not automatically roll back or back up settings.

For QNAP, use the release's `compose.qnap.yaml` in Container Station. It publishes
admin port 18080 on all NAS IPv4 interfaces. Keep it on a trusted network and
finish initial account setup promptly. Camera ports are 8100–8115 and 8600–8615.
The same GHCR image automatically selects the CPU architecture.

See [release publishing](deploy/REGISTRY.md) for GitHub Actions configuration and
[registry installer details](deploy/debian/REGISTRY.md) for update behavior.
Offline image bundles remain available through the packaging scripts below.

## Portable packages (QNAP, x64 and ARM64)

Use the architecture-specific Docker kit: **amd64** for QNAP TS-464 and other
Intel/AMD 64-bit hosts, or **arm64** for Apple Silicon/64-bit ARM hosts. Each kit
contains an image archive, ready-to-use Compose files and installation/migration
instructions. See [deployment guide](deploy/INSTALL.md). The QNAP template needs
no IP edits and publishes admin port 18080 on all NAS IPv4 interfaces; the existing development Compose installation below uses 8080.

Build and package both architectures with Python 3.11+, Docker Buildx and
Docker Engine API 1.48+ (for platform-specific image export). The packager uses a
pinned Skopeo container to create classic Docker archives for NAS importers:


```sh
python3 scripts/package-release.py /path/to/release-output
```

Use `--skip-build` only when both version-and-architecture-tagged images already
exist. No registry push or runtime camera data is included.

## Install on this Mac

Use the ARM64 Docker kit and its installation guide for a fresh portable install.
For an existing development/source installation (including this Mac), run from
this directory:

```sh
docker compose up -d --build
```

Open http://127.0.0.1:8080 and choose an admin password (12 characters minimum).
No setup code or container terminal is needed. Setup is available only until the
first admin account is created. The password is stored as a salted scrypt hash;
sign-in sessions expire after 12 hours and on container restart.

When opened through a private LAN IPv4 address, the setup page fills the bridge
address from that URL. Check it and change it if Protect uses another network.
Opening through localhost, a hostname or an IPv6 address requires entering the
Docker host's LAN IPv4 address (or a LAN hostname Protect can resolve) manually.
The container IP, Docker gateway and forwarded proxy headers are not guessed.
Existing saved addresses are never replaced on login. Network port bindings
are separate; the development/default Compose binding remains loopback-only.

Use **Add camera**, leave **Automatic** selected, and enter its address and ONVIF
login. Click **Inspect camera**, review the detected streams, then **Add detected
camera**. Verified model presets are available in the same selector.
Select **Test connection** to check basic ONVIF reachability and authentication. The interface shows
the host/port and copies just `IP:port`, ready to paste into Protect.

The manager uses saved camera credentials for ONVIF inspection and upstream RTSP
access. Automatic endpoints authenticate ONVIF requests locally with WS-Security
and RTSP through their private MediaMTX relay. Adopt with the camera's own login,
not the manager admin password. Credentials never enter process arguments.

## Add to UniFi Protect

Use Advanced Adoption with each endpoint's address and port. Set Improved Stream
Compatibility and Audio. For the PTZ view enable PTZ Control, leave Auto Return
Home off, and use arrow keys in expanded live view. No optical zoom or presets
are advertised. Automatic tracking can move the camera independently.

If PTZ was enabled after adoption, or a bridge restart leaves video stalled,
open Recording Manager, select only that endpoint, temporarily select its LQ
profile as HQ and apply, then restore HQ and apply. Verify LQ is also correctly
selected. Protect 7.2.105 caches ONVIF connections; this refreshes them without
removing the camera. This option requires two verified native stream profiles.
For a single-profile camera such as CAM-PT2, re-adopting the endpoint refreshed
PTZ successfully in Protect 7.2.105. Before removal, select the option to archive
all recordings on the Playback and Find Anything pages, and note the camera
settings so they can be restored. Re-adoption interrupts video and resets Protect
settings; obtain the owner's approval first. Do not invent an LQ profile to force
a refresh. A restart can briefly interrupt streams.

## Motion events

Motion forwarding is enabled by default for Automatic setup and all verified
presets (CAM-S3W, CAM-C44HE and CAM-PT2). It uses the camera's ONVIF PullPoint
service and real motion state; it does not analyse video or generate detections.
Enable motion detection in eWeLink/the camera's own settings. Each camera's
**Forward camera motion events** switch controls forwarding. The tile shows
connection status and the last motion received by the bridge, which does not
by itself confirm Protect recorded the event.

The bridge exposes a standard MotionAlarm topic with authenticated subscriptions,
bounded queues, renewal, unsubscribe and synchronization. Topic filtering and
ItemFilter boolean State equality predicates are supported; unsupported filter
expressions are rejected. Person, vehicle and animal topics are not presented as
separate Protect smart detections. Cameras that do not expose recognised ONVIF
motion topics are shown as unsupported; temporarily unreachable cameras retry.

CAM-C44HE's incomplete source model provides camera-wide events. Both lens
endpoints receive those events; they are not claimed to be independent lens
detections. Its firmware rejects Renew, so the bridge creates a replacement
upstream subscription before the previous one expires. Other cameras use native
renewal with reconnect on transport failure. Unknown multi-source cameras have
notifications filtered against the discovered source/configuration tokens;
unattributable events are not assigned to a lens.

Protect caches ONVIF services. An endpoint adopted before events were added may
need its connection refreshed or re-adoption before Protect subscribes. Restarting
only Protect 7.2.105 refreshed existing endpoints in live testing: subscriptions
resumed and CAM-PT2 events appeared without re-adoption. A restart briefly
interrupts Protect. Obtain
approval before removing a camera and preserve recordings with Protect's archive
option. Protect also requires recognised recording storage for recording modes;
event forwarding cannot fix missing storage. The CAM-S3W preset supports this
same relay, but live CAM-S3W event validation is still required with its login.

## Event thumbnails and snapshots

The bridge provides an authenticated JPEG snapshot URL through ONVIF Media1/Media2
for each camera view. Protect uses this to create thumbnails for new events.
Snapshots come from that view's HQ video, resized only for the JPEG (up to 1280px
wide); live and recorded video streams are unchanged. HTTP Digest authentication
uses the camera login. Passwords are never included in snapshot URLs or encoder
arguments. Requests are serialized per view, briefly cached for one second, and
return an error when current video is unavailable rather than an old image.

Existing event thumbnails are not backfilled. Protect may need time to refresh
the snapshot URL after updating the bridge. No camera re-adoption is needed if
its regular ONVIF refresh picks up the new URL. Legacy standalone deployments
need EVENT_CAMERA_FILE; snapshots are otherwise disabled.

## Logs

Open **Logs** in the header for all activity, or on a camera card to filter that
camera. Filter warnings/errors, pause or resume automatic updates, refresh on
demand, and download retained matching entries as JSON.

The view records worker starts and failures, camera connection tests, ONVIF
request errors, event-feed connection changes, snapshot failures and video relay
interruptions. Normal polling and motion messages are omitted. Consecutive
identical entries show a repeat count. A warning can describe a recovered issue;
look for a later connection-restored entry.

Logs require manager sign-in. Only known diagnostic fields are converted into
fixed messages: raw worker output, camera credentials, RTSP URLs and request
bodies are never copied into the log view or download. This is a diagnostic
summary, not a complete protocol trace. It does not collect Protect's own logs.

Up to 1,000 entries persist in `/data/diagnostics.json` with restricted file
permissions; the view shows 250 matches and download includes all retained
matches. The file is updated at the supervisor's five-second interval, so an
abrupt host failure can lose the most recent few seconds. Logs from before this
feature was installed are unavailable.

## Addresses, identity and saved data

- The bridge host, camera names and enable switches are editable. Verified presets
  also allow connection edits. Automatic camera connection fields are locked after
  inspection to avoid applying stale discovered URIs; rediscovery editing is not yet
  exposed. No real camera or network address is embedded in the image.
- IPv4 private LAN addresses and hostnames resolving to them are supported.
  Docker must be able to resolve camera hostnames; multicast `.local` names may
  require a LAN DNS record. Preset workers re-resolve hostnames periodically. Automatic discovery currently
  stores pinned IPv4 stream addresses, so use a DHCP reservation for those cameras.
- Each endpoint receives a persistent random local MAC and port slot. Editing
  camera names, credentials or addresses preserves those identities. Removing
  and adding a camera creates new identities.
- `/data/config.json` contains the camera settings and camera passwords. It is
  mode 0600 and is never served by the web interface. Use a protected Docker
  volume; backups of this volume contain secrets. Passwords are not returned by
  the API, passed in process arguments, or written to logs. Active converter
  configuration is in tmpfs and removed when disabled/deleted.
- The original standalone bridge containers are separate. For migration, disable
  their duplicate feeds after adopting the new manager endpoints and deciding
  how to retain the old Protect recordings. This release does not silently
  replace or remove existing Protect cameras.
- Keep the named volume when updating/recreating the container. Do not use
  `docker compose down -v` unless you intend to erase settings.

## QNAP and other hosts

Prebuilt Linux amd64 and arm64 Docker kits are available; use the matching image
for your NAS CPU. The TS-464 needs amd64. Startup and media tools are tested on
Docker Desktop (amd64 under emulation); physical QNAP validation is still pending.
For remote administration, set `ADMIN_BIND_IP` to the Docker host's trusted LAN
address before starting Compose, or use an HTTPS reverse proxy. The default UI
is loopback-only. Do not publish the admin UI to the internet. Enable
`SECURE_COOKIES=true` behind HTTPS. Camera/ONVIF traffic remains local HTTP/RTSP.

Publish ONVIF/RTSP ports one-to-one: defaults are 8100–8115 and 8600–8615.
Changing port mappings without changing the corresponding base variables will
produce incorrect advertised addresses. The bridge host is configured in the UI.
WS-Discovery multicast is not implemented in this release; use manual adoption.

To increase capacity, update `ENDPOINT_CAPACITY` (maximum 100) and extend both
published port ranges. Existing slots stay unchanged. Do not lower capacity
below the highest allocated slot. The private converter ports 19000–19099 and
20000–20099 are loopback-only inside the container and must not be published.

## Health, backup and recovery

The dashboard's **Bridge running** means the worker processes are running; it is
not a guarantee that a camera is reachable or that Protect is recording. Use
**Test connection** to verify ONVIF login, and verify video/recordings in Protect.
Failed workers restart automatically. Editing one camera restarts only workers
whose settings changed; other camera endpoints continue serving.

Back up the `/data` volume securely. To recover a forgotten admin password, stop
the container, securely back up the volume, set only `admin` to null in
`config.json`, then restart and create the replacement admin password in the setup page. This requires host
access and invalidates prior sessions. Do not alter camera identities/slots.

Run the included tests from this directory:

```sh
python3 -m unittest discover -s tests -v
```

Docker references: [port publishing and Compose networking](https://docs.docker.com/compose/how-tos/networking/)
and [multi-platform images](https://docs.docker.com/build/building/multi-platform/).
