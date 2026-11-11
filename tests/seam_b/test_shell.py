"""Seam B: the UI shell wears the ITdojo Design System."""

STYLE = """() => {
    const css = (element, property) => getComputedStyle(element).getPropertyValue(property).trim();
    const button = document.querySelector('#add-device button[type="submit"]');
    return {
        bodyFont: css(document.body, 'font-family'),
        bodyBackground: css(document.body, 'background-color'),
        headingColor: css(document.querySelector('h1'), 'color'),
        actionBackground: css(button, 'background-color'),
        actionText: css(button, 'color'),
    };
}"""


def test_the_shell_is_styled_with_itdojo_design_system_tokens(browser, instance):
    # Expected values are the design system's published light and dark hexes.
    light = browser.new_page(color_scheme="light")
    light.goto(instance.base_url)
    style = light.evaluate(STYLE)
    assert style["bodyFont"].startswith('"Fira Sans"')
    assert style["bodyBackground"] == "rgb(255, 255, 255)"   # --white
    assert style["headingColor"] == "rgb(62, 53, 102)"       # --violet
    assert style["actionBackground"] == "rgb(60, 184, 88)"   # --jade
    assert style["actionText"] == "rgb(20, 16, 43)"          # --text-on-action

    dark = browser.new_page(color_scheme="dark")
    dark.goto(instance.base_url)
    style = dark.evaluate(STYLE)
    assert style["bodyBackground"] == "rgb(15, 11, 30)"      # dark --white
    assert style["headingColor"] == "rgb(188, 176, 232)"     # dark --violet
    assert style["actionBackground"] == "rgb(78, 206, 106)"  # dark --jade
