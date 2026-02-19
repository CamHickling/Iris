# CaptureExpert Setup Guide

This guide walks through connecting all hardware devices and verifying they work before running an experiment.

---

## Hardware Required

- **2 USB Logitech webcams** (any Logitech USB webcam that supports 1080p/30fps)
- **2 GoPro Hero 7 Silver cameras** with charged batteries and SD cards inserted
- **2 USB WiFi adapters** (one per GoPro, for wireless control)
- **1 Polar H10 heart rate strap** with chest band
- **Mac computer** with available USB ports and Bluetooth

---

## 1. Install Dependencies

```bash
pip install -r requirements.txt
```

This installs: `opencv-python`, `numpy`, `goprocam`, `bleak`, and `pytest`.

---

## 2. Connect the USB Webcams

1. Plug both Logitech webcams into USB ports on your Mac.
2. The first webcam plugged in is typically assigned **device index 0**, the second **device index 1**. These indices are configured in `settings.json` under the `cameras` section.
3. To verify macOS recognises the cameras, open **System Settings > Privacy & Security > Camera** and confirm your terminal app (Terminal, iTerm, etc.) has camera access.

### Finding the correct device indices

If you're unsure which index maps to which webcam, you can test in Python:

```python
import cv2

# Try index 0
cap = cv2.VideoCapture(0)
if cap.isOpened():
    ret, frame = cap.read()
    print(f"Index 0: {frame.shape if ret else 'no frame'}")
    cap.release()

# Try index 1
cap = cv2.VideoCapture(1)
if cap.isOpened():
    ret, frame = cap.read()
    print(f"Index 1: {frame.shape if ret else 'no frame'}")
    cap.release()
```

If the indices don't match the expected front/side positions, swap the `device_index` values in `settings.json`:

```json
"cameras": [
  {"id": "usb_0", "name": "Logitech Webcam 1", "device_index": 0, ...},
  {"id": "usb_1", "name": "Logitech Webcam 2", "device_index": 1, ...}
]
```

---

## 3. Connect the GoPro Cameras

Each GoPro Hero 7 Silver is controlled over WiFi. Each camera needs its own dedicated USB WiFi adapter because each adapter connects to a different GoPro's WiFi network.

### Steps

1. **Turn on WiFi on each GoPro**: On the Hero 7 Silver, swipe down on the touchscreen and tap **Connections > Wireless Connections > On**. Note each camera's WiFi network name and password (shown on the camera screen).

2. **Plug in 2 USB WiFi adapters**. macOS will assign them network interface names (e.g. `en1`, `en2`).

3. **Connect each WiFi adapter to a different GoPro's WiFi network**:
   - Adapter on `en1` connects to GoPro #1's WiFi
   - Adapter on `en2` connects to GoPro #2's WiFi

   You can do this via **System Settings > Network**, or from the command line:
   ```bash
   networksetup -setairportnetwork en1 "GP-Hero7-XXXX" "password1"
   networksetup -setairportnetwork en2 "GP-Hero7-YYYY" "password2"
   ```

4. **Verify the settings match** in `settings.json`:
   ```json
   "gopros": [
     {"id": "gopro_hero7_1", "name": "GoPro Hero 7 Silver #1", "model": "hero7_silver",
      "wifi_interface": "en1", "ip_address": "10.5.5.9", "enabled": true},
     {"id": "gopro_hero7_2", "name": "GoPro Hero 7 Silver #2", "model": "hero7_silver",
      "wifi_interface": "en2", "ip_address": "10.5.5.9", "enabled": true}
   ]
   ```
   All GoPros use the same IP address (`10.5.5.9`) but are accessed through different WiFi interfaces.

### Finding your WiFi interface names

```bash
networksetup -listallhardwareports
```

Look for entries labelled "Wi-Fi" or "USB 10/100/1000 LAN" -- the `Device` field (e.g. `en1`) is what goes in the `wifi_interface` setting.

---

## 4. Connect the Polar H10 Heart Rate Strap

The Polar H10 connects over Bluetooth Low Energy (BLE). No pairing through macOS Bluetooth settings is needed -- the `bleak` library handles the connection directly.

### Steps

1. **Moisten the electrode pads** on the inside of the chest strap and put it on. The strap must detect skin contact to broadcast a signal.
2. **Ensure Bluetooth is enabled** on your Mac.
3. In `settings.json`, heart rate is already enabled:
   ```json
   "heart_rate": {
     "enabled": true,
     "device_address": null,
     "ecg_enabled": false
   }
   ```
   With `device_address` set to `null`, the software will automatically scan for any nearby Polar H10. If you have multiple Polar H10 straps nearby and need to target a specific one, set `device_address` to its BLE MAC address (e.g. `"A0:9E:1A:XX:XX:XX"`).

4. **Grant Bluetooth access**: macOS may prompt your terminal app for Bluetooth permission on first run. Accept this.

### Optional: Enable ECG

Set `"ecg_enabled": true` in `settings.json` to stream raw ECG data at 130 Hz alongside the standard heart rate. This produces an additional CSV file in the output. ECG requires the Polar H10 specifically (other Polar models do not support this feature).

---

## 5. Verify Everything with Calibration Mode

Before running an experiment, use the built-in calibration check to confirm all devices are connected and working:

```bash
python main.py --calibrate
```

This will:

1. **Test each USB webcam** -- opens the device, captures a test frame, and reports the resolution
2. **Test each GoPro** -- connects over WiFi, reads battery level and model info
3. **Test the Polar H10** -- scans via BLE, connects, reads battery, and records 3 seconds of heart rate data to confirm signal

You'll see output like:

```
============================================================
  CALIBRATION TOOL - Device Connectivity Check
============================================================

--- USB Cameras (2) ---
  Logitech Webcam 1: PASS - connected, frame captured (1920x1080)
  Logitech Webcam 2: PASS - connected, frame captured (1920x1080)

--- GoPro Cameras (2) ---
  GoPro Hero 7 Silver #1: PASS - connected, model=hero7_silver, battery=85%
  GoPro Hero 7 Silver #2: PASS - connected, model=hero7_silver, battery=72%

--- Polar H10 Heart Rate Monitor ---
  Polar H10: PASS - connected, battery=90%, HR signal detected

============================================================
  CALIBRATION SUMMARY
============================================================
  ...
  Results: 5/5 passed
  Overall: PASS - All devices ready
============================================================
```

If any device shows **FAIL**, check:

| Device | Common issues |
|---|---|
| USB webcam | Wrong device index, camera not plugged in, macOS camera permission denied |
| GoPro | WiFi adapter not connected to GoPro's network, GoPro powered off or asleep, wrong interface name in config |
| Polar H10 | Strap not worn / electrodes dry, Bluetooth off, macOS Bluetooth permission denied, another app connected to the strap |

---

## 6. Run an Experiment

Once calibration passes, run:

```bash
python main.py
```

Or with a custom config file:

```bash
python main.py --config my_settings.json
```

The experiment runs through three phases:

1. **Calibration** (10s) -- GoPros connect, webcams capture frames, HR recording begins
2. **Recording** (60s) -- GoPros record video to SD cards, webcams capture at ~30fps, HR continues
3. **Post-Recording** (indefinite) -- GoPro battery status displayed, press **Ctrl+C** to finish

On Ctrl+C or phase completion, the system saves all data and disconnects cleanly.

---

## 7. Post-Processing: GoPro Lens Correction

GoPro footage is saved to the cameras' SD cards. After downloading the video files, you can apply barrel distortion correction:

```bash
# Process all .mp4 files in a directory
python main.py --undistort /path/to/gopro/videos/

# Process a single file
python main.py --undistort /path/to/clip.mp4
```

Output files are saved alongside originals with an `_undistorted` suffix.

---

## Output Files

After an experiment, the `output/` directory contains:

```
output/
  frames/
    calibration/
      usb_0_000001.jpg
      usb_1_000001.jpg
      ...
    recording/
      usb_0_000001.jpg
      ...
  heart_rate_YYYYMMDD_HHMMSS.csv
  ecg_YYYYMMDD_HHMMSS.csv          (only if ecg_enabled was true)
```

- **Frame images**: captured from the USB webcams during each phase
- **heart_rate CSV**: timestamp, BPM, RR intervals, sensor contact status, and phase label
- **ECG CSV**: timestamp, phase, and raw ECG microvolts (if enabled)

GoPro video must be downloaded separately from the cameras' SD cards.

---

## Troubleshooting

**Webcam shows black frames or wrong resolution**: Some webcams need a moment to adjust exposure. The first few frames may be dark. If resolution doesn't match, check the webcam supports 1920x1080 -- lower the `resolution` in `settings.json` if needed (e.g. `[1280, 720]`).

**GoPro disconnects during experiment**: GoPros drop WiFi if they don't receive periodic keep-alive signals. The software sends these automatically every 2.5 seconds. If disconnects happen, check the WiFi adapter signal strength and move the adapters closer to the cameras.

**Polar H10 not found**: Make sure the strap is on your body with moistened electrodes. The H10 only broadcasts a BLE signal when it detects skin contact. Also ensure no other app (e.g. Polar Beat, Polar Flow) is currently connected to the strap, as BLE only allows one connection.

**macOS permission prompts**: The first time you run CaptureExpert, macOS may ask for Camera and Bluetooth access for your terminal. Grant both. If you accidentally denied, go to **System Settings > Privacy & Security** and add your terminal app manually.
