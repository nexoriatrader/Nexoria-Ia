from html.parser import HTMLParser
from pathlib import Path


INDEX_HTML = Path(__file__).resolve().parents[1] / "app" / "index.html"


class MobileNavigationParser(HTMLParser):
    VOID_ELEMENTS = {
        "area", "base", "br", "col", "embed", "hr", "img", "input",
        "link", "meta", "param", "source", "track", "wbr",
    }

    def __init__(self):
        super().__init__()
        self.stack = []
        self.ancestors = None
        self.in_mobile_nav = False
        self.button_count = 0

    def handle_starttag(self, tag, attrs):
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if tag == "nav" and "nx-neo-mobile-nav" in classes:
            self.ancestors = list(self.stack)
            self.in_mobile_nav = True
        elif self.in_mobile_nav and tag == "button":
            self.button_count += 1

        if tag not in self.VOID_ELEMENTS:
            self.stack.append((tag, attributes))

    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
        if tag == "nav" and self.in_mobile_nav:
            self.in_mobile_nav = False


def test_dashboard_mobile_nav_is_visible_outside_collapsible_details():
    parser = MobileNavigationParser()
    parser.feed(INDEX_HTML.read_text())

    assert parser.ancestors is not None
    assert any(tag == "section" and attrs.get("id") == "dashboard"
               for tag, attrs in parser.ancestors)
    assert not any(tag == "details" for tag, _ in parser.ancestors)
    assert parser.button_count == 4


def test_dashboard_mobile_nav_uses_a_fixed_four_column_layout():
    html = INDEX_HTML.read_text()

    assert (
        "#dashboard .nx-neo-mobile-nav{display:grid;"
        "grid-template-columns:repeat(4,minmax(0,1fr));position:fixed"
    ) in html
