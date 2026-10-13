# Unifi Camera Bridge

Use your ONVIF and RTSP cameras with **UniFi Protect**. The bridge connects to
cameras on your network and presents them to Protect as ONVIF cameras, without
modifying Protect.

Manage multiple cameras through a simple web interface. Supported features include
video, audio, native HQ/LQ streams, pan/tilt control and motion events, depending
on what each camera provides. Multi-lens cameras can appear as separate cameras
in Protect. SONOFF CAM-S3W, CAM-C44HE and CAM-PT2 presets are included, alongside
automatic ONVIF setup and an option for standard RTSP cameras.

## Install on Debian

For **Debian 12 or 13**, on **x64 or ARM64**. Run the installer on your Debian
machine; it automatically installs Docker and runs the bridge in a container.

1. Download the [Debian installer](https://github.com/smarthome-au/unifi-camera-bridge/releases/download/v1.6.7/unifi-camera-bridge-1.6.7-debian-installer.tar.gz).
2. In the download folder, run:

   ```sh
   tar -xzf unifi-camera-bridge-1.6.7-debian-installer.tar.gz
   cd unifi-camera-bridge-1.6.7-debian
   sudo ./install.sh
   ```

3. Open the web address printed by the installer, normally `http://SERVER-IP:18080`.

The installer downloads the correct image for your machine. No image import or
GitHub login is needed. Files are installed in `/opt/unifi-camera-bridge`.
Settings are stored in `/opt/unifi-camera-bridge/data/config.json` and survive
container updates. Back up the `data` folder securely; it contains camera logins.
This installer uses a fresh installation and does not migrate older Docker volumes.

## Install with Docker

On a machine with Docker and Docker Compose installed:

```sh
mkdir unifi-camera-bridge
cd unifi-camera-bridge
curl -fsSL https://github.com/smarthome-au/unifi-camera-bridge/releases/latest/download/compose.yaml -o compose.yaml
docker compose up -d
```

Open `http://localhost:18080` on that machine. To access it from another device,
create a `.env` file beside `compose.yaml` containing `ADMIN_BIND_IP=SERVER-IP`,
using your Docker host's LAN IP, then run `docker compose up -d` again.
Open `http://SERVER-IP:18080`.

### QNAP Container Station

1. Download [compose.qnap.yaml](https://github.com/smarthome-au/unifi-camera-bridge/releases/latest/download/compose.qnap.yaml).
2. In Container Station, create an **Application** and paste the file's contents
   into its Compose editor, then create/start the application.
3. Open `http://NAS-IP:18080`.

Docker automatically selects x64 or ARM64; the QNAP TS-464 uses x64.
Keep the bridge on your private network, with access to your cameras and Protect.
Keep its settings volume when updating or recreating the container.

## Set up your cameras

1. Create an admin password and check the bridge's LAN address. This is filled in
   automatically when you open the manager using its LAN IP.
2. Enable ONVIF or RTSP in your camera's own app.
3. Choose **Add camera → Automatic**, enter the camera address and login, then
   **Inspect camera** and save the detected camera. For an RTSP-only camera,
   choose **RTSP camera · any brand** and enter its stream details instead.

## Add to UniFi Protect

1. Copy the **ONVIF address** from the camera tile in the bridge.
2. In Protect, use **Advanced Adoption**, paste the copied `IP:port`, and enter
   the camera login used in the bridge. The manager's admin password is separate.
3. Enable **Improved Stream Compatibility** and **Audio** where available.
4. For a supported pan/tilt camera, enable **PTZ Control**, leave **Auto Return
   Home** off, and use the arrow keys in the expanded live view.

Add each lens separately for a multi-lens camera. HQ/LQ choices depend on the
camera's available streams. If a connection fails, use **Test connection** or
**Logs** in the bridge.

## Motion events

Enable motion detection in the camera's app and leave **Forward camera motion
events** enabled in the bridge. In Protect, enable motion recording and ensure
recording storage is available.

Motion events require camera ONVIF support; RTSP-only cameras provide video and
audio but no motion events or PTZ control. The bridge forwards camera detections
and does not add person, vehicle or animal recognition. On the SONOFF DUO, both
lenses receive the camera's shared motion events.

The tile's last-event time confirms the bridge received motion; check Protect's
timeline to confirm recording.

## Event thumbnails and snapshots

The bridge supplies snapshots from each camera's HQ stream so Protect can show
thumbnails for new events. This works automatically when video is available and
does not change live or recorded video quality.

Protect may take a little time to pick up snapshots after an update. Older events
without thumbnails are not updated.
