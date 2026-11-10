"""Fuzzes the layout guide page reader (HTML from the web): it must return a guide, never
raise, whatever the page holds.

Run: uv run --group fuzz python fuzz/fuzz_layouts.py -max_total_time=60
"""

import sys

import atheris

with atheris.instrument_imports():
    from poefire import layouts

URL = "https://www.definitivguide.com/docs/PoE1/Act%201/Test"
PARTS = ["<h1>", "</h1>", "<h2 id=\"layouts\">", "<h2 id=\"zone-read\">", "<table>", "<tr>",
         "<td>", "<th>", "<img src=\"/a.png\" alt=\"x\">", "<p>", "</p>",
         '<div class="theme-admonition">', '<div class="admonitionHeading_x">Simple Read</div>',
         "</div>", "<article>", "</article>", "&amp;", "&#8203;"]  # fmt: skip


def test_one_input(data: bytes) -> None:
    fdp = atheris.FuzzedDataProvider(data)
    html = []
    for _ in range(fdp.ConsumeIntInRange(0, 40)):
        if fdp.ConsumeBool():
            html.append(PARTS[fdp.ConsumeIntInRange(0, len(PARTS) - 1)])
        else:
            html.append(fdp.ConsumeUnicodeNoSurrogates(24))
    layouts.parse_page("".join(html), URL)
    layouts.parse_sitemap(fdp.ConsumeUnicodeNoSurrogates(256))


def main() -> None:
    atheris.Setup(sys.argv, test_one_input)
    atheris.Fuzz()


if __name__ == "__main__":
    main()
