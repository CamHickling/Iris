"""Mock hardware dependencies that aren't installed in test environments."""

import sys
from unittest.mock import MagicMock

# Mock bleak before any src imports
bleak_mock = MagicMock()
sys.modules["bleak"] = bleak_mock

# Mock goprocam before any src imports
goprocam_mock = MagicMock()
sys.modules["goprocam"] = goprocam_mock
sys.modules["goprocam.GoProCamera"] = goprocam_mock.GoProCamera
sys.modules["goprocam.constants"] = goprocam_mock.constants

# Mock sounddevice and soundfile
sounddevice_mock = MagicMock()
sys.modules["sounddevice"] = sounddevice_mock

soundfile_mock = MagicMock()
sys.modules["soundfile"] = soundfile_mock

# Mock ffmpeg-python
ffmpeg_mock = MagicMock()
sys.modules["ffmpeg"] = ffmpeg_mock

# Mock PIL
pil_mock = MagicMock()
sys.modules["PIL"] = pil_mock
sys.modules["PIL.Image"] = pil_mock.Image
