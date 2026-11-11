"""NMEA tracks for GPS Sidecars in replay mode, so a test knows the exact fixes a Device will report.

A test writes a track into a folder of its own with ``write_track`` and starts a Device with
``device_harness.add_device(..., gps=True, gps_tracks=folder, settings={"GPS_SOURCE": "replay:<name>"})``.
The Sidecar loops it at real-time speed, from the gaps between its epochs' UTC times.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

KNOT = 1852 / 3600  # metres per second


def nmea(body: str) -> str:
    """One sentence with its checksum: the XOR of every byte between $ and *."""
    checksum = 0
    for byte in body.encode():
        checksum ^= byte
    return f"${body}*{checksum:02X}"


def write_track(directory: Path, lines: list[str], name: str = "track.nmea") -> Path:
    """Write ``lines`` as an NMEA log in ``directory`` (made if missing) and return the folder."""
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("".join(f"{line}\r\n" for line in lines))
    return directory


@dataclass(frozen=True)
class Epoch:
    """One receiver epoch, as the fix the Sidecar builds from it. North and east are positive."""

    utc: str          # hhmmss.ss on 2026-10-15
    lat: float
    lon: float
    alt: float
    speed: float      # m/s
    track: float      # degrees true
    satellites: int = 9
    hdop: float = 0.8

    def sentences(self) -> list[str]:
        """RMC and GGA for this epoch, as a receiver sends them."""
        lat = f"{int(abs(self.lat)):02d}{abs(self.lat) % 1 * 60:07.4f},{'N' if self.lat >= 0 else 'S'}"
        lon = f"{int(abs(self.lon)):03d}{abs(self.lon) % 1 * 60:07.4f},{'E' if self.lon >= 0 else 'W'}"
        knots = f"{self.speed / KNOT:.3f}"
        return [
            nmea(f"GPRMC,{self.utc},A,{lat},{lon},{knots},{self.track:.1f},151026,,,A"),
            nmea(f"GPGGA,{self.utc},{lat},{lon},1,{self.satellites:02d},{self.hdop:.1f},{self.alt:.1f},M,47.0,M,,"),
        ]

    @property
    def gps_time(self) -> str:
        """The fix's GPS time as the Sidecar sends it."""
        hours, minutes, seconds = self.utc[:2], self.utc[2:4], float(self.utc[4:])
        return f"2026-10-15T{hours}:{minutes}:{seconds:06.3f}Z"


def track_lines(epochs: list[Epoch]) -> list[str]:
    return [line for epoch in epochs for line in epoch.sentences()]
