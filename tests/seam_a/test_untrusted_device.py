"""Seam A: a Device is untrusted input.

Camera names come from whatever a Device's API answers, and they become
Relay path names, URLs and Capture file names. A Device that answers with
names that are not safe gets those Cameras left out, and says which.
"""

HOSTILE_NAMES = [
    "..",
    ".",
    "../../config",
    "cam/../../etc",
    "/absolute",
    "cam?x=1",
    "cam#frag",
    "cam%2f..",
    "cam name",
    "cam\n2",
    "",
    "a" * 200,
]


def path_item(name):
    return {"name": name, "available": True, "ready": True,
            "tracks2": [{"codec": "H264", "codecProps": {"width": 640, "height": 480}}]}


def test_cameras_with_unsafe_names_are_left_out_and_reported(
        instance, instance_image, device_harness, unique_label):
    label = unique_label("hostile")
    device = device_harness.add_scripted_device(
        label, instance_image,
        paths=[path_item("ok-cam"), *map(path_item, HOSTILE_NAMES), {"name": 7, "available": True},
               path_item("ok-cam")])  # twice

    created = instance.add_device(device, label=label)

    assert created["cameras"] == [f"{label}/ok-cam"]
    assert sorted(skipped["name"] for skipped in created["skipped_cameras"]) == sorted([*HOSTILE_NAMES, "7", "ok-cam"])
    with instance.client() as api:
        listed = [c["name"] for c in api.get("/api/cameras").json() if c["device"] == label]
        assert listed == ["ok-cam"]


def test_a_device_reporting_a_nonsense_resolution_is_shown_without_one(
        instance, instance_image, device_harness, unique_label):
    label = unique_label("odd")
    device = device_harness.add_scripted_device(label, instance_image, paths=[
        {"name": "cam", "available": True,
         "tracks2": [{"codecProps": {"width": "<script>", "height": -1}}]},
    ])

    instance.add_device(device, label=label)

    with instance.client() as api:
        listed = [c for c in api.get("/api/cameras").json() if c["device"] == label]
        assert [c["resolution"] for c in listed] == [None]
