"""Microphone recording via sounddevice + soundfile."""

import threading
from dataclasses import dataclass, field
from typing import Optional

import numpy as np
import sounddevice as sd
import soundfile as sf


@dataclass
class AudioConfig:
    device_name: str = "Tonor"
    device_index: Optional[int] = None
    sample_rate: int = 44100
    channels: int = 1
    enabled: bool = True


def find_audio_device(name_substring: str) -> Optional[int]:
    """Find an audio input device by name substring (case-insensitive)."""
    devices = sd.query_devices()
    needle = name_substring.lower()
    for i, dev in enumerate(devices):
        if needle in dev["name"].lower() and dev["max_input_channels"] > 0:
            return i
    return None


class AudioRecorder:
    """Records audio from a USB microphone to a WAV file."""

    def __init__(self, config: AudioConfig):
        self.config = config
        self._stream: Optional[sd.InputStream] = None
        self._sndfile: Optional[sf.SoundFile] = None
        self._lock = threading.Lock()
        self._recording = False

    def _audio_callback(self, indata: np.ndarray, frames: int, time_info, status):
        if status:
            print(f"Audio warning: {status}")
        with self._lock:
            if self._sndfile is not None and self._recording:
                self._sndfile.write(indata.copy())

    def open(self, output_path: str) -> bool:
        """Open the audio device and prepare WAV file for writing."""
        device_index = self.config.device_index
        if device_index is None:
            device_index = find_audio_device(self.config.device_name)
            if device_index is None:
                print(f"Audio device '{self.config.device_name}' not found.")
                return False
            print(f"Auto-detected audio device: index {device_index}")

        try:
            self._sndfile = sf.SoundFile(
                output_path,
                mode="w",
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                subtype="PCM_16",
            )
            self._stream = sd.InputStream(
                device=device_index,
                samplerate=self.config.sample_rate,
                channels=self.config.channels,
                callback=self._audio_callback,
                blocksize=1024,
            )
            print(f"Audio device opened: index {device_index}")
            return True
        except Exception as e:
            print(f"Failed to open audio device: {e}")
            self.close()
            return False

    def start_recording(self):
        """Start capturing audio data."""
        if self._stream is None:
            return
        self._recording = True
        self._stream.start()
        print("Audio recording started")

    def stop_recording(self):
        """Stop capturing audio data."""
        self._recording = False
        if self._stream is not None and self._stream.active:
            self._stream.stop()
        print("Audio recording stopped")

    def close(self):
        """Release all audio resources."""
        self._recording = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None
        with self._lock:
            if self._sndfile is not None:
                try:
                    self._sndfile.close()
                except Exception:
                    pass
                self._sndfile = None
        print("Audio device closed")
