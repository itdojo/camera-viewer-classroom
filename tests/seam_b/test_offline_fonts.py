"""Seam B: with nothing outside the instance reachable, the UI and the login
page load without reaching out, in the fonts the instance bundles (#63)."""

from playwright.sync_api import expect

# Every face the UI's stylesheets ask for: Fira Sans 400/500/700 and italic
# 400/700, Fira Code 400/500/700.
FACES = [
    '400 16px "Fira Sans"',
    '500 16px "Fira Sans"',
    '700 16px "Fira Sans"',
    'italic 400 16px "Fira Sans"',
    'italic 700 16px "Fira Sans"',
    '400 16px "Fira Code"',
    '500 16px "Fira Code"',
    '700 16px "Fira Code"',
]

# For each face, the status of every font face that document.fonts.load
# fetched for it (an empty list means no @font-face declares it), and the
# URL of every font file the page loaded.
LOAD_FACES = """async (faces) => {
    await document.fonts.ready;
    const loaded = {};
    for (const face of faces) {
        try {
            loaded[face] = (await document.fonts.load(face)).map((font) => font.status);
        } catch (error) {
            loaded[face] = [`error: ${error}`];
        }
    }
    const files = performance.getEntriesByType('resource')
        .map((entry) => entry.name)
        .filter((name) => new URL(name).pathname.endsWith('.woff2'));
    return { loaded, files, bodyFont: getComputedStyle(document.body).fontFamily };
}"""


def assert_bundled_fonts_in_use(page, instance):
    fonts = page.evaluate(LOAD_FACES, FACES)
    assert fonts["bodyFont"].startswith('"Fira Sans"')
    assert {face: statuses for face, statuses in fonts["loaded"].items()
            if not statuses or set(statuses) != {"loaded"}} == {}
    assert fonts["files"], "no font file was loaded"
    assert [url for url in fonts["files"] if not url.startswith(instance.base_url + "/")] == []


def test_the_ui_loads_offline_in_its_bundled_fonts(offline, instance):
    page = offline.new_page()
    page.goto(instance.base_url)
    expect(page.get_by_role("heading", name="Cameras", exact=True)).to_be_visible()

    assert_bundled_fonts_in_use(page, instance)
    assert offline.blocked == []


def test_the_login_page_loads_offline_in_its_bundled_fonts(offline, instance):
    offline.context.clear_cookies()
    page = offline.new_page()
    page.goto(instance.base_url)
    expect(page.get_by_role("form", name="Log in")).to_be_visible()

    assert_bundled_fonts_in_use(page, instance)
    assert offline.blocked == []
