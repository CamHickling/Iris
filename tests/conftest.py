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
