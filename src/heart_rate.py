"""Polar H10 heart rate monitor via BLE.

Records heart rate (BPM), RR intervals, and optionally ECG data continuously
from calibration through post-recording, with data labeled by experiment phase.
"""

import asyncio
import csv
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from bleak import BleakClient, BleakScanner

# Polar H10 BLE UUIDs
HR_SERVICE_UUID = "0000180d-0000-1000-8000-00805f9b34fb"
HR_MEASUREMENT_UUID = "00002a37-0000-1000-8000-00805f9b34fb"
BATTERY_LEVEL_UUID = "00002a19-0000-1000-8000-00805f9b34fb"
PMD_SERVICE_UUID = "fb005c80-02e7-f387-1cad-8acd2d8df0c8"
PMD_CONTROL_UUID = "fb005c81-02e7-f387-1cad-8acd2d8df0c8"
PMD_DATA_UUID = "fb005c82-02e7-f387-1cad-8acd2d8df0c8"

# PMD start commands
ECG_START = bytearray([0x02, 0x00, 0x00, 0x01, 0x82, 0x00, 0x01, 0x01, 0x0E, 0x00])
ECG_STOP = bytearray([0x03, 0x00])

# Polar epoch offset (2000-01-01 in Unix time, in nanoseconds)
POLAR_EPOCH_OFFSET_NS = 946_684_800 * 1_000_000_000


@dataclass
class HeartRateSample:
    timestamp: float
    bpm: int
    rr_intervals_ms: list[float]
    sensor_contact: Optional[bool]
    phase: str


@dataclass
class ECGSample:
    timestamp: float
    values_uv: list[int]
    phase: str


class PolarH10:
    """Polar H10 BLE heart rate monitor.

    Records HR + RR intervals (and optionally ECG) continuously across
    experiment phases. Data is labeled by phase for later analysis.
    """

    def __init__(self, device_address: Optional[str] = None, ecg_enabled: bool = False):
        self.device_address = device_address
        self.ecg_enabled = ecg_enabled
        self._client: Optional[BleakClient] = None
        self._device = None
        self._hr_samples: list[HeartRateSample] = []
        self._ecg_samples: list[ECGSample] = []
        self._samples_lock = threading.Lock()
        self._current_phase: str = "unknown"
        self._recording = False
        self._battery_level: Optional[int] = None
        self._thread: Optional[threading.Thread] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._connected = threading.Event()
        self._stop_event = threading.Event()

    @staticmethod
    def _parse_hr_measurement(data: bytearray) -> dict:
        """Parse BLE Heart Rate Measurement characteristic (0x2A37)."""
        flags = data[0]
        hr_is_uint16 = (flags & 0x01) != 0
        contact_bits = (flags >> 1) & 0x03
        sensor_contact = None
        if contact_bits >= 2:
            sensor_contact = contact_bits == 3
        energy_present = (flags >> 3) & 0x01
        rr_present = (flags >> 4) & 0x01

        offset = 1

        if hr_is_uint16:
            bpm = int.from_bytes(data[offset:offset + 2], byteorder="little")
            offset += 2
        else:
            bpm = data[offset]
            offset += 1

        if energy_present:
            offset += 2

        rr_intervals_ms = []
        if rr_present:
            while offset + 1 < len(data):
                rr_raw = int.from_bytes(data[offset:offset + 2], byteorder="little")
                rr_ms = (rr_raw / 1024.0) * 1000.0
                rr_intervals_ms.append(round(rr_ms, 2))
                offset += 2

        return {
            "bpm": bpm,
            "rr_intervals_ms": rr_intervals_ms,
            "sensor_contact": sensor_contact,
        }

    @staticmethod
    def _parse_ecg_data(data: bytearray) -> Optional[dict]:
        """Parse PMD ECG notification data."""
        if data[0] != 0x00:
            return None

        timestamp_ns = int.from_bytes(data[1:9], byteorder="little", signed=False)
        unix_ns = timestamp_ns + POLAR_EPOCH_OFFSET_NS

        samples_uv = []
        for i in range(10, len(data) - 2, 3):
            ecg_uv = int.from_bytes(data[i:i + 3], byteorder="little", signed=True)
            samples_uv.append(ecg_uv)

        return {
            "timestamp_ns": unix_ns,
            "samples_uv": samples_uv,
        }

    def _hr_callback(self, sender, data: bytearray):
        """Callback for HR measurement notifications."""
        if not self._recording:
            return
        parsed = self._parse_hr_measurement(data)
        sample = HeartRateSample(
            timestamp=time.time(),
            bpm=parsed["bpm"],
            rr_intervals_ms=parsed["rr_intervals_ms"],
            sensor_contact=parsed["sensor_contact"],
            phase=self._current_phase,
        )
        with self._samples_lock:
            self._hr_samples.append(sample)

    def _ecg_callback(self, sender, data: bytearray):
        """Callback for PMD ECG data notifications."""
        if not self._recording:
            return
        parsed = self._parse_ecg_data(data)
        if parsed:
            sample = ECGSample(
                timestamp=time.time(),
                values_uv=parsed["samples_uv"],
                phase=self._current_phase,
            )
            with self._samples_lock:
                self._ecg_samples.append(sample)

    async def _find_device(self):
        """Scan for Polar H10."""
        print("Scanning for Polar H10...")

        if self.device_address:
            device = await BleakScanner.find_device_by_address(
                self.device_address, timeout=15.0
            )
        else:
            device = await BleakScanner.find_device_by_filter(
                lambda d, adv: d.name is not None and d.name.startswith("Polar H10"),
                timeout=15.0,
            )

        return device

    async def _connect_and_subscribe(self):
        """Connect to Polar H10 and subscribe to notifications."""
        device = await self._find_device()
        if device is None:
            print("ERROR: Polar H10 not found! Make sure the strap is worn (electrodes moistened).")
            return False

        self._device = device
        print(f"Found Polar H10: {device.name} [{device.address}]")

        def on_disconnect(client):
            print(f"WARNING: Polar H10 disconnected ({client.address})")

        self._client = BleakClient(device, disconnected_callback=on_disconnect)
        await self._client.connect()

        if not self._client.is_connected:
            print("ERROR: Failed to connect to Polar H10!")
            return False

        # Read battery level
        battery_data = await self._client.read_gatt_char(BATTERY_LEVEL_UUID)
        self._battery_level = battery_data[0]
        print(f"Polar H10 connected - Battery: {self._battery_level}%")

        # Subscribe to HR notifications
        await self._client.start_notify(HR_MEASUREMENT_UUID, self._hr_callback)
        print("Subscribed to heart rate notifications")

        # Optionally start ECG streaming
        if self.ecg_enabled:
            try:
                await self._client.start_notify(PMD_DATA_UUID, self._ecg_callback)
                await self._client.write_gatt_char(PMD_CONTROL_UUID, ECG_START, response=True)
                print("ECG streaming started (130 Hz, 14-bit)")
            except Exception as e:
                print(f"WARNING: ECG streaming failed: {e}")
                self.ecg_enabled = False

        return True

    async def _run_loop(self):
        """Main async loop keeping the BLE connection alive."""
        connected = await self._connect_and_subscribe()
        self._connected.set()
        if not connected:
            return

        while not self._stop_event.is_set():
            await asyncio.sleep(0.1)

        # Cleanup
        if self._client and self._client.is_connected:
            if self.ecg_enabled:
                try:
                    await self._client.write_gatt_char(
                        PMD_CONTROL_UUID, ECG_STOP, response=True
                    )
                    await self._client.stop_notify(PMD_DATA_UUID)
                except Exception:
                    pass
            try:
                await self._client.stop_notify(HR_MEASUREMENT_UUID)
            except Exception:
                pass
            await self._client.disconnect()

    def _thread_target(self):
        """Thread target running the async event loop."""
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._run_loop())
        self._loop.close()

    def connect(self) -> bool:
        """Connect to the Polar H10 in a background thread.

        Returns True if connection was successful.
        """
        self._stop_event.clear()
        self._connected.clear()
        self._thread = threading.Thread(target=self._thread_target, daemon=True)
        self._thread.start()

        self._connected.wait(timeout=25.0)
        time.sleep(0.5)
        return self._client is not None and self._client.is_connected

    def start_recording(self, phase: str):
        """Start recording data, labeling samples with the given phase."""
        self._current_phase = phase
        self._recording = True
        print(f"Polar H10 recording started (phase: {phase})")

    def set_phase(self, phase: str):
        """Update the current phase label for subsequent samples."""
        self._current_phase = phase
        print(f"Polar H10 phase updated to: {phase}")

    def stop_recording(self):
        """Stop recording data."""
        self._recording = False
        print("Polar H10 recording stopped")

    def disconnect(self):
        """Disconnect from the Polar H10 and stop the background thread."""
        self._recording = False
        self._stop_event.set()
        if self._thread:
            self._thread.join(timeout=5.0)
        print("Polar H10 disconnected")

    @property
    def battery_level(self) -> Optional[int]:
        return self._battery_level

    def get_samples(self, phase: Optional[str] = None) -> list[HeartRateSample]:
        """Get HR samples, optionally filtered by phase."""
        with self._samples_lock:
            if phase:
                return [s for s in self._hr_samples if s.phase == phase]
            return list(self._hr_samples)

    def save_to_csv(self, filepath: Path):
        """Save HR data (BPM + RR intervals) to CSV."""
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with self._samples_lock:
            samples = list(self._hr_samples)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "bpm", "rr_intervals_ms", "sensor_contact", "phase"])
            for s in samples:
                rr_str = ";".join(str(r) for r in s.rr_intervals_ms) if s.rr_intervals_ms else ""
                writer.writerow([s.timestamp, s.bpm, rr_str, s.sensor_contact, s.phase])
        print(f"Heart rate data saved to {filepath} ({len(samples)} samples)")

    def save_ecg_to_csv(self, filepath: Path):
        """Save ECG data to CSV (one row per notification batch)."""
        with self._samples_lock:
            samples = list(self._ecg_samples)
        if not samples:
            return
        filepath.parent.mkdir(parents=True, exist_ok=True)
        with open(filepath, "w", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(["timestamp", "phase", "ecg_values_uv"])
            for s in samples:
                values_str = ";".join(str(v) for v in s.values_uv)
                writer.writerow([s.timestamp, s.phase, values_str])
        total_samples = sum(len(s.values_uv) for s in samples)
        print(f"ECG data saved to {filepath} ({total_samples} samples across {len(samples)} packets)")

    def get_summary(self) -> dict:
        """Get a summary of heart rate data by phase."""
        with self._samples_lock:
            samples = list(self._hr_samples)
        summary = {}
        phases = set(s.phase for s in samples)
        for phase in phases:
            bpms = [s.bpm for s in samples if s.phase == phase]
            all_rr = []
            for s in samples:
                if s.phase == phase:
                    all_rr.extend(s.rr_intervals_ms)
            phase_stats = {
                "count": len(bpms),
                "min_bpm": min(bpms),
                "max_bpm": max(bpms),
                "avg_bpm": round(sum(bpms) / len(bpms), 1),
            }
            if all_rr:
                phase_stats["avg_rr_ms"] = round(sum(all_rr) / len(all_rr), 1)
                phase_stats["rr_count"] = len(all_rr)
            summary[phase] = phase_stats
        return summary
