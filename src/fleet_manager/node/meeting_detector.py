"""Meeting detection — detects active camera/microphone usage on macOS.

When the camera or microphone is in use (Zoom, FaceTime, Meet, etc.),
this is a hard override signal: pause Ollama entirely on this node.
"""

from __future__ import annotations

import logging
import platform
import subprocess

logger = logging.getLogger(__name__)


class MeetingDetector:
    """Detects whether camera or microphone is currently in use on macOS."""

    def __init__(self):
        self._is_darwin = platform.system() == "Darwin"

    def is_camera_active(self) -> bool:
        """Check if any camera device is currently in use."""
        if not self._is_darwin:
            return False
        try:
            result = subprocess.run(
                [
                    "log",
                    "show",
                    "--predicate",
                    'subsystem == "com.apple.cmio" AND category == "CMIOExtensionProvider"',
                    "--last",
                    "5s",
                    "--style",
                    "compact",
                ],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout.lower()
            if "startstream" in output and "stopstream" not in output:
                return True
        except Exception:
            pass

        # Fallback: check for known camera-using processes via AppleScript
        try:
            result = subprocess.run(
                ["system_profiler", "SPCameraDataType"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # If a camera is connected and in use, check via lsof
            result = subprocess.run(
                ["lsof", "+D", "/Library/CoreMediaIO/"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if result.stdout.strip():
                return True
        except Exception:
            pass

        # Check if AppleCamera or VDC assistant is active
        try:
            result = subprocess.run(
                ["pgrep", "-f", "VDCAssistant|AppleCameraAssistant"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            return result.returncode == 0
        except Exception as e:
            logger.debug(f"Camera detection failed: {e}")
            return False

    def is_microphone_active(self) -> bool:
        """Check if any microphone is currently in use."""
        if not self._is_darwin:
            return False
        try:
            # Check coreaudiod for active input streams
            result = subprocess.run(
                ["pgrep", "-f", "coreaudiod"],
                capture_output=True,
                text=True,
                timeout=3,
            )
            if result.returncode != 0:
                return False

            # Use ioreg to check for active audio input
            result = subprocess.run(
                ["ioreg", "-l", "-w0"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            output = result.stdout
            # Look for IOAudioEngine entries with active input
            if '"IOAudioEngineState" = 1' in output:
                return True
        except Exception:
            pass

        # Fallback: check for microphone usage via process list
        try:
            result = subprocess.run(
                ["lsof", "+D", "/dev/"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            # Audio input devices on macOS
            for line in result.stdout.splitlines():
                if "audioinput" in line.lower():
                    return True
        except Exception as e:
            logger.debug(f"Microphone detection failed: {e}")
            return False

        return False

    def is_in_meeting(self) -> bool:
        """Check if user is likely in a meeting (camera OR mic active)."""
        camera = self.is_camera_active()
        mic = self.is_microphone_active()
        if camera or mic:
            logger.info(
                f"Meeting detected: camera={'active' if camera else 'off'}, "
                f"mic={'active' if mic else 'off'}"
            )
        return camera or mic
