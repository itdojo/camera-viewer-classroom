"""``scripts/cut-map-extract.sh`` cuts a Map Extract into the instance's maps folder (#61, #82).

No Docker and no network: a stand-in ``pmtiles`` on PATH records how it was called, keeps a copy
of each region file it is given, and writes a file where ``pmtiles extract`` or ``pmtiles merge``
would. The planet build list is a local file (``BUILDS_URL``). The real tool and the real build
are what ``harness.maps.FIXTURE`` was cut with.
"""

import json
import os
import re
import subprocess
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "cut-map-extract.sh"
PLANET = "https://planet.example"

STAND_IN = """#!/usr/bin/env bash
printf '%s\\n' "$*" >> "$PMTILES_CALLS"
case "$1" in
    extract)
        region="" maxzoom="" dry_run=""
        for arg in "$@"; do
            case "$arg" in
                --region=*)  region="${arg#--region=}" ;;
                --maxzoom=*) maxzoom="${arg#--maxzoom=}" ;;
                --dry-run)   dry_run=1 ;;
            esac
        done
        if [[ -n "$region" ]]; then
            printf '%s\\t%s\\n' "$region" "$(tr -d '\\n' < "$region")" >> "$PMTILES_REGIONS"
        fi
        if [[ -n "$dry_run" ]]; then
            size="2.7 MB"
            [[ -z "$region" && "$maxzoom" == 6 ]] && size="45 MB"   # the whole planet, zooms 0-6
            echo "extract.go:612: Extract transferred $size (overfetch 0.05) for an archive size of $size" >&2
            exit 0
        fi
        printf 'PMTiles\\003' > "$3" ;;
    merge)
        if [[ -n "${PMTILES_NO_MERGE:-}" ]]; then   # a release before 1.29.1
            echo "pmtiles: error: unexpected argument merge" >&2
            exit 1
        fi
        [[ "$2" == --help ]] && exit 0
        inputs=("${@:2:$#-2}")
        cat "${inputs[@]}" > "${@: -1}" ;;
esac
"""

# Two deployment areas far apart, as west,south,east,north, and the GeoJSON polygon of each.
WESTMINSTER = "-0.127,51.4985,-0.12,51.503"
HESSE = "10,50,10.1,50.1"
WESTMINSTER_POLYGON = [[[-0.127, 51.4985], [-0.12, 51.4985], [-0.12, 51.503], [-0.127, 51.503], [-0.127, 51.4985]]]
HESSE_POLYGON = [[[10, 50], [10.1, 50], [10.1, 50.1], [10, 50.1], [10, 50]]]


@pytest.fixture
def run(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    (bin_dir / "pmtiles").write_text(STAND_IN)
    (bin_dir / "pmtiles").chmod(0o755)
    builds = tmp_path / "builds.json"
    builds.write_text(json.dumps([
        {"key": "20261014.pmtiles", "version": "4.15.2"},
        {"key": "20261015.pmtiles", "version": "4.15.2"},
    ]))
    calls = tmp_path / "calls.txt"
    calls.touch()
    regions = tmp_path / "regions.txt"
    regions.touch()

    default_path = f"{bin_dir}{os.pathsep}{os.environ['PATH']}"
    default_builds = builds.as_uri()

    def run_script(*args, path=default_path, builds_url=default_builds, merge=True):
        env = {**os.environ, "PATH": path, "PMTILES_CALLS": str(calls), "PMTILES_REGIONS": str(regions),
               "BUILDS_URL": builds_url, "PLANET_URL": PLANET, "NO_COLOR": "1"}
        if not merge:
            env["PMTILES_NO_MERGE"] = "1"
        result = subprocess.run(["bash", str(SCRIPT), *args], capture_output=True, text=True, env=env,
                                stdin=subprocess.DEVNULL, timeout=30)
        result.calls = calls.read_text().splitlines()
        # Every pmtiles call that cuts (not a dry run), with the region file's path left out.
        result.cuts = [re.sub(r"--region=\S+", "--region=REGION", call) for call in result.calls
                       if call.startswith("extract") and "--dry-run" not in call]
        # (path, GeoJSON) of each region file pmtiles was given, dry runs included.
        result.regions = [(Path(line.split("\t")[0]), json.loads(line.split("\t")[1]))
                          for line in regions.read_text().splitlines()]
        return result

    return run_script


def test_a_bounding_box_is_cut_from_the_newest_planet_build_into_the_maps_folder(run, tmp_path):
    maps = tmp_path / "maps"

    result = run("--bbox", WESTMINSTER, "--name", "westminster", "--maps-dir", str(maps))

    assert result.returncode == 0, result.stdout + result.stderr
    assert (maps / "westminster.pmtiles").read_bytes() == b"PMTiles\x03"
    assert list(maps.iterdir()) == [maps / "westminster.pmtiles"]  # no partial file left behind
    assert result.cuts == [f"extract {PLANET}/20261015.pmtiles {maps}/westminster.pmtiles.partial "
                           "--region=REGION --maxzoom=15"]
    assert {"type": "MultiPolygon", "coordinates": [WESTMINSTER_POLYGON]} == result.regions[-1][1]
    assert "2.7 MB" in result.stdout
    assert "restart" in result.stdout


def test_several_areas_are_cut_as_one_extract_from_one_multipolygon(run, tmp_path):
    maps = tmp_path / "maps"

    result = run("--bbox", WESTMINSTER, "--bbox", HESSE, "--name", "two-sites", "--maps-dir", str(maps))

    assert result.returncode == 0, result.stdout + result.stderr
    assert list(maps.iterdir()) == [maps / "two-sites.pmtiles"]
    assert (maps / "two-sites.pmtiles").read_bytes() == b"PMTiles\x03"
    assert result.cuts == [f"extract {PLANET}/20261015.pmtiles {maps}/two-sites.pmtiles.partial "
                           "--region=REGION --maxzoom=15"]
    both = {"type": "MultiPolygon", "coordinates": [WESTMINSTER_POLYGON, HESSE_POLYGON]}
    assert [region for _, region in result.regions] == [both, both]  # measured, then cut, from the same areas
    assert not any(path.exists() for path, _ in result.regions)  # the region file is cleaned up
    assert "2.7 MB" in result.stdout


def test_the_world_base_is_merged_under_the_areas_from_zoom_7(run, tmp_path):
    maps = tmp_path / "maps"

    result = run("--bbox", WESTMINSTER, "--bbox", HESSE, "--world", "--name", "sites", "--maps-dir", str(maps))

    assert result.returncode == 0, result.stdout + result.stderr
    world, areas, partial = (f"{maps}/sites.pmtiles.{part}" for part in ("world.partial", "areas.partial", "partial"))
    assert result.cuts == [
        f"extract {PLANET}/20261015.pmtiles {world} --maxzoom=6",
        f"extract {PLANET}/20261015.pmtiles {areas} --region=REGION --minzoom=7 --maxzoom=15",
    ]
    assert f"merge {areas} {world} {partial}" in result.calls
    both = {"type": "MultiPolygon", "coordinates": [WESTMINSTER_POLYGON, HESSE_POLYGON]}
    assert [region for _, region in result.regions] == [both, both]
    # Both parts went into the extract, and only the extract is left.
    assert (maps / "sites.pmtiles").read_bytes() == b"PMTiles\x03PMTiles\x03"
    assert list(maps.iterdir()) == [maps / "sites.pmtiles"]
    # The whole cut is measured, world base included, before anything is cut.
    first_cut = next(i for i, call in enumerate(result.calls) if call.startswith("extract") and "--dry-run" not in call)
    assert len([call for call in result.calls[:first_cut] if "--dry-run" in call]) == 2
    assert "45 MB" in result.stdout and "2.7 MB" in result.stdout
    assert "48 MB" in result.stdout  # 45 MB + 2.7 MB


def test_the_world_base_needs_the_areas_to_go_deeper_than_it(run, tmp_path):
    result = run("--bbox", HESSE, "--world", "--maxzoom", "6", "--maps-dir", str(tmp_path / "maps"))

    assert result.returncode == 2
    assert "--world" in result.stderr and "--maxzoom" in result.stderr
    assert result.calls == []


def test_the_world_base_needs_a_pmtiles_with_merge_and_says_so_before_any_download(run, tmp_path):
    maps = tmp_path / "maps"

    result = run("--bbox", HESSE, "--world", "--maps-dir", str(maps), merge=False)

    assert result.returncode == 1
    assert "merge" in result.stderr and "1.29.1" in result.stderr
    assert "github.com/protomaps/go-pmtiles/releases" in result.stderr
    assert [call for call in result.calls if call.startswith("extract")] == []
    assert not maps.exists()

    without_world = run("--bbox", HESSE, "--name", "here", "--maps-dir", str(maps), merge=False)

    assert without_world.returncode == 0, without_world.stdout + without_world.stderr
    assert (maps / "here.pmtiles").exists()


def test_an_existing_extract_is_kept_unless_forced(run, tmp_path):
    maps = tmp_path / "maps"
    maps.mkdir()
    (maps / "westminster.pmtiles").write_bytes(b"mine")
    args = ["--bbox", "-0.127,51.4985,-0.12,51.503", "--name", "westminster", "--maps-dir", str(maps)]

    kept = run(*args)

    assert kept.returncode == 0, kept.stdout + kept.stderr
    assert (maps / "westminster.pmtiles").read_bytes() == b"mine"
    assert [call for call in kept.calls if call.startswith("extract")] == []
    assert "already" in kept.stdout

    forced = run(*args, "--force")

    assert forced.returncode == 0, forced.stdout + forced.stderr
    assert (maps / "westminster.pmtiles").read_bytes() == b"PMTiles\x03"


def test_a_pinned_build_and_zoom_are_used(run, tmp_path):
    maps = tmp_path / "maps"

    result = run("--bbox", HESSE, "--build", "20261014", "--maxzoom", "13",
                 "--name", "here", "--maps-dir", str(maps))

    assert result.returncode == 0, result.stdout + result.stderr
    assert result.cuts == [f"extract {PLANET}/20261014.pmtiles {maps}/here.pmtiles.partial "
                           "--region=REGION --maxzoom=13"]


@pytest.mark.parametrize("bbox", [
    "-0.12,51.4985,-0.127,51.503",   # west east of east
    "-0.127,51.503,-0.12,51.4985",   # south north of north
    "-0.127,51.4985,-0.12",          # three numbers
    "west,51.4985,-0.12,51.503",     # not a number
    "-0.127,91,-0.12,92",            # beyond the pole
])
def test_an_impossible_bounding_box_is_refused_before_anything_is_fetched(run, tmp_path, bbox):
    result = run("--bbox", bbox, "--name", "bad", "--maps-dir", str(tmp_path / "maps"))

    assert result.returncode == 2
    assert "--bbox" in result.stderr
    assert result.calls == []
    assert not (tmp_path / "maps" / "bad.pmtiles").exists()


def test_an_area_is_required(run, tmp_path):
    result = run("--maps-dir", str(tmp_path / "maps"))

    assert result.returncode == 2
    assert "--bbox" in result.stderr and "--place" in result.stderr


def test_a_build_of_another_tiles_schema_is_refused(run, tmp_path):
    builds = tmp_path / "builds-v5.json"
    builds.write_text(json.dumps([{"key": "20270101.pmtiles", "version": "5.0.0"}]))

    result = run("--bbox", "10,50,10.1,50.1", "--name", "here", "--maps-dir", str(tmp_path / "maps"),
                 builds_url=builds.as_uri())

    assert result.returncode == 1
    assert "5.0.0" in result.stderr
    assert [call for call in result.calls if call.startswith("extract")] == []


def test_without_the_pmtiles_tool_it_says_where_to_get_it(run, tmp_path):
    result = run("--bbox", "10,50,10.1,50.1", "--maps-dir", str(tmp_path / "maps"), path="/usr/bin:/bin")

    assert result.returncode == 1
    assert "github.com/protomaps/go-pmtiles/releases" in result.stderr
