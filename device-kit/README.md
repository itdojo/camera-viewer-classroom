# Device Kit

This kit turns a Raspberry Pi, or any 64-bit Linux machine, into a Device that Camera Viewer can add. These are the same steps as the Device setup guide in Camera Viewer itself (open the Management Bar with ☰ and choose **Device setup guide** at its foot), which also explains what to do when something goes wrong.

The kit has one folder per variant. Pick the one for your camera and run every command below from inside it:

| Variant | Camera |
| --- | --- |
| `usb` | a USB webcam |
| `csi` | a Raspberry Pi camera module on its ribbon |
| `test-pattern` | none: it draws a test picture, so you can try the steps before any hardware is involved |

```sh
cd device-kit/<variant>
```

## 1. Check Docker

Docker Engine with its Compose plugin must be installed, and your user must be able to run it without `sudo` (Docker's post-install steps for Linux). This should answer with a version:

```sh
docker compose version
```

## 2. Attach the camera and GPS receiver

Plug the GPS receiver into a USB port now, so step 4 finds it. A Device without one still works; it just shows no Position on the map.

For `usb`, plug the webcam in. With more than one webcam attached, list them, since step 4 needs you to name one. Each webcam is the name ending in `-video-index0`.

```sh
ls /dev/v4l/by-id/
```

For `csi`, connect the module's ribbon with the Pi powered off, then boot it and check that it lists the camera:

```sh
rpicam-hello --list-cameras
```

## 3. Check the clock

A Device whose clock is more than an hour off adds without complaint, but its Feeds never start. Look for `System clock synchronized: yes`:

```sh
timedatectl
```

If it says `no`, turn NTP on, which needs a time server the Pi can reach:

```sh
sudo timedatectl set-ntp true
```

With no time server in reach, run `sudo timedatectl set-ntp false`, then set the clock by hand to your computer's time with `sudo timedatectl set-time "YYYY-MM-DD HH:MM:SS"`.

## 4. Make the Device's certificate

```sh
../gen-cert.sh
```

It makes the Device's certificate and password, finds the camera and GPS receiver, and writes them to `.env` in this folder. With two webcams attached it won't guess, so run it again with `--camera` and one of the `-video-index0` names from step 2. For `csi`, run it on the Pi itself, because it also lists this Pi's camera devices for the container.

**Copy the lines under the green DEVICE CERTIFICATE READY banner before you go on.** They hold the fingerprint and password, which steps 6 and 7 need, and look like this with your own values:

```text
   ⛩ DEVICE CERTIFICATE READY ⛩
▌ INFO   Fingerprint:    <64 characters, 0-9 and a-f>
▌ INFO   Instance user:  viewer
▌ INFO   Password:       <32 characters, 0-9 and a-f>
▌ INFO   GNSS receiver:  /dev/serial/by-id/<your receiver>
```

No `GNSS receiver` line means it found none: check the receiver is plugged in and run `../gen-cert.sh` again. If the output has scrolled away, running it again prints the same values and changes nothing.

## 5. Start the Device

If `gen-cert.sh` printed a `GNSS receiver` line, start the Device with the `gps` profile, which runs the container that reads the receiver:

```sh
docker compose --profile gps up -d
```

With no receiver, leave the profile out:

```sh
docker compose up -d
```

The first start downloads the kit's images, which takes a few minutes. After that the Device starts by itself whenever the Pi boots. Stop a GPS Device with the profile too, because a plain `down` leaves the receiver's container running:

```sh
docker compose --profile gps down
```

## 6. Add it in Camera Viewer

In the Add Device Panel, enter the Pi's address (its hostname with `.local`, or its IP address), a label (lowercase letters, digits and hyphens, such as `cam-2`), the username `viewer` and the password from step 4. Choose **Check fingerprint**.

## 7. Compare the fingerprint

Compare the fingerprint the panel shows with the one from step 4, every character; the panel only adds a colon between pairs. If they match, confirm it. **Never confirm a fingerprint that doesn't match.** A mismatch means the address reached some other machine, so stop and find out why.

The Device's Camera then appears under Cameras. Tick **Connect** to put it on screen.

## Flip the picture

A camera mounted upside down or sideways is flipped in `.env`, where `gen-cert.sh` left the setting commented out. For `csi`, uncomment `CAMERA_HFLIP=true` (left to right), `CAMERA_VFLIP=true` (top to bottom), or both for 180°. For `usb`, uncomment `USB_CAMERA_FILTER` and set it to `hflip`, `vflip` or `hflip,vflip`. Then start the Device again as in step 5.
