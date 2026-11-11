"""Map Extract: a pre-downloaded ``.pmtiles`` basemap for the deployment area (#61).

An instructor cuts one with ``scripts/cut-map-extract.sh`` while still online and drops it into
the ``maps`` folder of the instance's config folder. The instance picks it at startup and serves
it at ``EXTRACT_URL`` (``api/maps.py``), behind the session, with Range support, which is how
``pmtiles.js`` reads it in the browser. There is one extract per instance: with several files,
the most recently modified wins and the others are named in a warning.

A classroom with internet can name an online raster tile server as well, with ``CV_MAP_TILES``
(#62): a URL template the browser fills with ``{z}``, ``{x}`` and ``{y}``. It is operator input,
so ``tile_server`` refuses a template the map could never use, and the instance doesn't start.
Its attribution is ``CV_MAP_TILES_ATTRIBUTION``, plain text, and defaults to OpenStreetMap's,
since nearly every raster tile server draws OSM data, whose ODbL requires it. Tile requests go
from each browser straight to that server, so they reveal roughly where viewed Cameras are; a
Map Extract avoids that.

Satellite imagery is a second raster template, ``CV_MAP_IMAGERY`` (#83), checked by the same
rules with the same messages (``imagery_server``). There is no default provider, and so no
provider to credit by default: ``CV_MAP_IMAGERY_ATTRIBUTION`` is plain text, and without it the
map credits the imagery server by its host name, so imagery never shows with nothing saying
where it came from.

The browser learns which Basemaps the instance has from the ``settings`` event's ``map`` field
(``map_settings``): ``{"extract": EXTRACT_URL | None, "tiles": {"url", "attribution"} | None,
"imagery": {"url", "attribution"} | None}``. Each browser chooses one of them or Blank, and the
frontend's ``js/basemap.js`` defaults to the first of extract, online tiles, blank (ADR 0015);
Satellite is never the default.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger(__name__)

MAPS_DIR = "maps"
SUFFIX = ".pmtiles"
EXTRACT_URL = "/api/map/extract"
TILES_VARIABLE = "CV_MAP_TILES"
TILES_ATTRIBUTION_VARIABLE = "CV_MAP_TILES_ATTRIBUTION"
DEFAULT_TILES_ATTRIBUTION = "© OpenStreetMap contributors"
TILES_EXAMPLE = "https://tile.openstreetmap.org/{z}/{x}/{y}.png"
IMAGERY_VARIABLE = "CV_MAP_IMAGERY"
IMAGERY_ATTRIBUTION_VARIABLE = "CV_MAP_IMAGERY_ATTRIBUTION"
# NAIP through The National Map (README, "Map"): public domain and keyless, but US only.
IMAGERY_EXAMPLE = "https://basemap.nationalmap.gov/arcgis/rest/services/USGSImageryOnly/MapServer/tile/{z}/{y}/{x}"
# The placeholders MapLibre fills in a raster tile URL; the first three are required.
REQUIRED_PLACEHOLDERS = ("{z}", "{x}", "{y}")
FILLED_PLACEHOLDERS = frozenset({"z", "x", "y", "ratio", "prefix", "quadkey", "bbox-epsg-3857"})
PLACEHOLDER = re.compile(r"\{([^{}]*)\}")


@dataclass(frozen=True)
class TileServer:
    """An online raster tile server: its URL template and the attribution the map shows for it."""

    url: str
    attribution: str = DEFAULT_TILES_ATTRIBUTION


def tile_server(url: str, attribution: str = "") -> TileServer | None:
    """The tile server ``CV_MAP_TILES`` names, or None when it is unset or empty. An empty
    attribution means OpenStreetMap's. Raises ``ValueError`` naming the variable when the
    template is unusable (``raster_template``)."""
    url = raster_template(TILES_VARIABLE, url, TILES_EXAMPLE)
    if url is None:
        return None
    return TileServer(url, attribution.strip() or DEFAULT_TILES_ATTRIBUTION)


def imagery_server(url: str, attribution: str = "") -> TileServer | None:
    """The imagery server ``CV_MAP_IMAGERY`` names, or None when it is unset or empty. An empty
    attribution credits the server by its host name. Raises ``ValueError`` naming the variable
    when the template is unusable (``raster_template``)."""
    url = raster_template(IMAGERY_VARIABLE, url, IMAGERY_EXAMPLE)
    if url is None:
        return None
    return TileServer(url, attribution.strip() or f"Imagery: {urlsplit(url).hostname}")


def raster_template(variable: str, url: str, example: str) -> str | None:
    """The raster tile URL template ``variable`` holds, stripped, or None when it is empty.
    Raises ``ValueError`` naming the variable, with ``example`` as a usable one, when it is not an
    http(s) URL with a host, lacks ``{z}``, ``{x}`` or ``{y}``, or holds a placeholder MapLibre
    doesn't fill (Leaflet's ``{s}``), since the map could never draw it."""
    url = url.strip()
    if not url:
        return None
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise ValueError(f"{variable} must be an http:// or https:// URL with a host, "
                         f"got {url!r} (for example {example})")
    missing = [placeholder for placeholder in REQUIRED_PLACEHOLDERS if placeholder not in url]
    if missing:
        raise ValueError(f"{variable} must contain {{z}}, {{x}} and {{y}}, which the map fills "
                         f"for each tile; {url!r} has no {' or '.join(missing)} (for example {example})")
    unknown = sorted(set(PLACEHOLDER.findall(url)) - FILLED_PLACEHOLDERS)
    if unknown:
        hint = "; name one host instead of a {s} subdomain list" if "s" in unknown else ""
        raise ValueError(f"{variable} has {', '.join('{' + name + '}' for name in unknown)}, which the "
                         f"map can't fill, in {url!r}{hint} (for example {example})")
    return url


def find_extract(config_dir: Path) -> Path | None:
    """The Map Extract in ``config_dir/maps``: its one ``.pmtiles`` file, or with several the
    most recently modified one, with a warning naming the rest. None if there is none."""
    folder = config_dir / MAPS_DIR
    try:
        candidates = [path for path in folder.iterdir()
                      if path.suffix == SUFFIX and not path.name.startswith(".") and path.is_file()]
    except OSError:
        return None
    if not candidates:
        return None
    candidates.sort(key=lambda path: (path.stat().st_mtime, path.name), reverse=True)
    chosen, *ignored = candidates
    if ignored:
        log.warning("%d Map Extracts in %s: using %s, the most recently modified, and ignoring %s. "
                    "Remove the ones you don't want.", len(candidates), folder, chosen.name,
                    ", ".join(path.name for path in ignored))
    log.info("Map Extract: %s", chosen)
    return chosen


def map_settings(extract: Path | None, tiles: TileServer | None = None, imagery: TileServer | None = None) -> dict:
    """The ``settings`` event's ``map`` field: where the browser finds each basemap."""
    def server(found: TileServer | None) -> dict | None:
        return {"url": found.url, "attribution": found.attribution} if found is not None else None

    return {
        "extract": EXTRACT_URL if extract is not None else None,
        "tiles": server(tiles),
        "imagery": server(imagery),
    }
