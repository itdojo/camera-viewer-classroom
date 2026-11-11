"""Seam B: the login wall. The harness logs every browser in; these tests
start logged out by clearing the context's cookies."""

from playwright.sync_api import expect


def cameras_heading(page):
    return page.get_by_role("heading", name="Cameras", exact=True)


def test_the_login_wall_blocks_the_ui_until_the_correct_password_is_entered(page, instance):
    page.context.clear_cookies()
    page.goto(instance.base_url)

    login = page.get_by_role("form", name="Log in")
    expect(login).to_be_visible()
    expect(cameras_heading(page)).to_have_count(0)

    login.get_by_label("Password").fill(instance.password + "-wrong")
    login.get_by_role("button", name="Log in").click()
    expect(login.get_by_role("alert")).to_contain_text("Wrong password")
    expect(cameras_heading(page)).to_have_count(0)

    login.get_by_label("Password").fill(instance.password)
    login.get_by_role("button", name="Log in").click()
    expect(cameras_heading(page)).to_be_visible()

    page.reload()
    expect(cameras_heading(page)).to_be_visible()
    expect(page.get_by_role("form", name="Log in")).to_have_count(0)


def test_logging_out_returns_to_the_login_wall(page, instance):
    page.goto(instance.base_url)
    expect(cameras_heading(page)).to_be_visible()

    page.get_by_role("button", name="Log out").click()

    expect(page.get_by_role("form", name="Log in")).to_be_visible()
    page.goto(instance.base_url)
    expect(page.get_by_role("form", name="Log in")).to_be_visible()


def test_a_page_whose_session_has_ended_goes_back_to_the_login_wall(page, instance, test_pattern_device):
    page.goto(instance.base_url)
    expect(cameras_heading(page)).to_be_visible()

    page.context.clear_cookies()  # the session ends while the page is open
    form = page.get_by_role("form", name="Add Device")
    form.get_by_label("Address").fill(test_pattern_device.address)
    form.get_by_label("Label", exact=True).fill("never-added")
    form.get_by_label("Username").fill(test_pattern_device.username)
    form.get_by_label("Password").fill(test_pattern_device.password)
    form.get_by_role("button", name="Check fingerprint").click()

    expect(page.get_by_role("form", name="Log in")).to_be_visible()
