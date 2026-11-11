"""Maps: the instance's Map Extract (``camera_viewer.maps``).

    GET /api/map/extract    -> the .pmtiles file; Range requests answer 206 with those bytes

404 ``no_map_extract`` when the instance started without one, or the file has gone since.
Behind the session like every other route; ``pmtiles.js`` sends the browser's cookie.
"""

from __future__ import annotations

from fastapi import APIRouter, Depends
from fastapi.responses import FileResponse

from camera_viewer.api.errors import ApiError
from camera_viewer.maps import EXTRACT_URL
from camera_viewer.services import Services, services

router = APIRouter(tags=["maps"])


@router.get(EXTRACT_URL.removeprefix("/api"))
async def map_extract(svc: Services = Depends(services)) -> FileResponse:
    extract = svc.map_extract
    if extract is None or not extract.is_file():
        raise ApiError(404, "no_map_extract", "This instance has no Map Extract. Put a .pmtiles file "
                       "in the config folder's maps folder and restart the instance.")
    # Starlette's FileResponse answers Range (206, Content-Range) and If-Range itself.
    return FileResponse(extract, media_type="application/octet-stream")
