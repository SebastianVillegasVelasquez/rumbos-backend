"""The CSS rules of `html` steps: what passes, and a hostile corpus in which
every entry must be refused. No database."""

import base64

import pytest

from app.core.paths import ASSETS_PATH
from app.welcome_flow.css import check_css

ASSET = "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
PNG_URI = "data:image/png;base64," + base64.b64encode(PNG).decode()
FONT_URI = "data:font/woff2;base64,AAAA"

ALLOWED = [
    "",
    "p { color: #123; margin: 0 auto }",
    ".a > .b + .c ~ .d, h1::before { content: '\\201C'; }",
    "@media (max-width: 600px) { .a { display: none } }",
    "@keyframes spin { from { transform: rotate(0) } to { transform: rotate(1turn) } }",
    "a:not(.x):hover { text-decoration: underline }",
    ":root { --accent: #0a7; } p { color: var(--accent) }",
    "p { background: url(" + PNG_URI + ") }",
    "p { background: url('" + PNG_URI + "') no-repeat }",
    f"p {{ background-image: url({ASSETS_PATH}/{ASSET}) }}",
    f"p {{ background-image: url('{ASSETS_PATH}/{ASSET}/thumb') }}",
    f"@font-face {{ font-family: F; src: url({FONT_URI}) format('woff2'); }}",
    f"@font-face {{ font-family: F; src: local('Arial'), url('{FONT_URI}'); }}",
    "p { font-family: 'Open Sans', sans-serif; scroll-behavior: smooth }",
    "/* a comment */ p { color: red } /* another */",
    "p::after { content: 'a > b and (c)' }",
    "@supports (display: grid) { .g { display: grid } }",
    "[data-x='1'] { color: red }",
    "@charset 'utf-8'; p { color: red }",
]


@pytest.mark.parametrize("css", ALLOWED)
def test_allowed_css_passes(css: str) -> None:
    assert check_css(css) is None


HOSTILE = {
    # @import in every spelling
    "import url": "@import url(https://evil.example/x.css);",
    "import string": "@import 'https://evil.example/x.css';",
    "import no space": '@import"https://evil.example/x.css";',
    "import upper case": "@IMPORT url(x.css);",
    "import mixed case": "@ImPoRt url(x.css);",
    "import hex escape": r"@\69mport url(x.css);",
    "import hex escape with space": "@\\69 mport url(x.css);",
    "import hex escape inside": r"@imp\6frt url(x.css);",
    "import plain escape": r"@i\mport url(x.css);",
    "import after another rule": "p{color:red} @import url(x.css);",
    "import inside media": "@media screen { @import url(x.css); }",
    "import with layer": "@import url(x.css) layer(base);",
    "import with comment": "@/**/import url(x.css);",
    # url() to anywhere else
    "remote url": "p { background: url(https://evil.example/t.png) }",
    "remote url quoted": "p { background: url('https://evil.example/t.png') }",
    "remote url double quoted": 'p { background: url("https://evil.example/t.png") }',
    "remote url with spaces": "p { background: url(  https://evil.example/t.png  ) }",
    "remote url upper function": "p { background: URL(https://evil.example/t.png) }",
    "remote url escaped function": r"p { background: u\72l(https://evil.example/t.png) }",
    "remote url escaped hex": r"p { background: \75 rl(https://evil.example/t.png) }",
    "protocol relative url": "p { background: url(//evil.example/t.png) }",
    "relative url": "p { background: url(images/a.png) }",
    "http url": "p { background: url(http://evil.example/a.png) }",
    "empty url": "p { background: url() }",
    "svg data url": "p { background: url(data:image/svg+xml;base64,PHN2Zz48L3N2Zz4=) }",
    "html data url": "p { background: url(data:text/html;base64,PHNjcmlwdD4=) }",
    "data url wrong magic": "p { background: url(data:image/png;base64,PHNjcmlwdD4=) }",
    "image-set remote": "p { background: image-set('https://evil.example/a.png' 1x) }",
    "webkit image-set remote": "p { background: -webkit-image-set('https://evil.example/a.png' 1x) }",
    "image-set escaped": r"p { background: ima\67 e-set('https://evil.example/a.png' 1x) }",
    "src function": "p { background: src('https://evil.example/a.png') }",
    "cross-fade remote": "p { background: cross-fade('https://evil.example/a.png', red) }",
    "list-style remote": "li { list-style: url(https://evil.example/a.png) }",
    "cursor remote": "p { cursor: url(https://evil.example/a.cur), auto }",
    "content remote": "p::before { content: url(https://evil.example/a.png) }",
    "border-image remote": "p { border-image: url(https://evil.example/a.png) 30 }",
    "mask remote": "p { mask-image: url(https://evil.example/a.svg#m) }",
    "filter remote": "p { filter: url(https://evil.example/a.svg#f) }",
    "var hiding nothing": "p { background: url(https://evil.example/a.png) , var(--x) }",
    "url inside media": "@media print { p { background: url(https://evil.example/a.png) } }",
    "url inside supports": "@supports (a:b) { p { background: url(x) } }",
    "url inside keyframes": "@keyframes k { to { background: url(https://evil.example/a) } }",
    "url asset with query": f"p {{ background: url({ASSETS_PATH}/{ASSET}?x=1) }}",
    "url asset upper case": f"p {{ background: url({ASSETS_PATH}/{ASSET.upper()}) }}",
    # fonts
    "remote font": "@font-face { font-family: F; src: url(https://evil.example/f.woff2) }",
    "remote font quoted": "@font-face { font-family: F; src: url('https://evil.example/f.woff2') }",
    "remote font second source": f"@font-face {{ src: url({FONT_URI}), url(https://evil.example/f.woff) }}",
    "remote font inside media": "@media print { @font-face { src: url(https://evil.example/f) } }",
    "remote font escaped at": r"@\66ont-face { src: url(https://evil.example/f) }",
    "remote font upper": "@FONT-FACE { src: url(https://evil.example/f) }",
    "font face with relative url": "@font-face { src: url(f.woff2) }",
    # legacy script hooks
    "expression": "p { width: expression(alert(1)) }",
    "expression upper": "p { width: EXPRESSION(alert(1)) }",
    "expression escaped": r"p { width: e\78 pression(alert(1)) }",
    "expression escaped letter": r"p { width: expr\65 ssion(alert(1)) }",
    "expression with comment": "p { width: expr/**/ession(alert(1)) }",
    "expression with space": "p { width: expression (alert(1)) }",
    "behavior": "p { behavior: url(x.htc) }",
    "behavior no url": "p { behavior: none }",
    "behavior escaped": r"p { b\65havior: none }",
    "ms behavior": "p { -ms-behavior: none }",
    "moz binding": "p { -moz-binding: url(x.xml#a) }",
    "moz binding escaped": r"p { -m\6fz-binding: none }",
    "moz binding with comment": "p { -moz/**/-binding: none }",
    "javascript url": "p { background: url(javascript:alert(1)) }",
    "javascript url quoted": "p { background: url('javascript:alert(1)') }",
    "javascript url escaped": r"p { background: url('\6a avascript:alert(1)') }",
    "javascript url with tab": "p { background: url('java\\9 script:alert(1)') }",
    "javascript in string": "p::after { content: 'javascript:alert(1)' }",
    "javascript in string spaced": "p::after { content: 'java script :alert(1)' }",
    "vbscript": "p::after { content: 'vbscript:msgbox(1)' }",
    "javascript bare": "p { x: javascript:alert(1) }",
    # breaking out of the style element
    "closing style": "</style><script>alert(1)</script>",
    "closing style inside rule": "p { color: red } </style><img src=x onerror=alert(1)>",
    "closing style upper": "</STYLE>",
    "closing style in string": "p::after { content: '</style>' }",
    "comment opener": "<!-- p { color: red }",
    "cdata-ish": "<![CDATA[ p { color: red } ]]>",
    # unbalanced or unterminated
    "unterminated comment": "p { color: red } /* never closed",
    "unterminated string": "p::after { content: 'abc }",
    "unterminated string newline": "p::after { content: 'abc\n}",
    "unterminated double string": 'p::after { content: "abc }',
    "unclosed block": "p { color: red",
    "unclosed nested block": "@media screen { p { color: red }",
    "unclosed paren": "p { width: calc(1px + 2px }",
    "unclosed bracket": "[a='b' { color: red }",
    "stray close brace": "p { color: red } }",
    "stray close paren": "p { color: red ) }",
    "mismatched": "p { width: calc(1px + 2px] }",
    "bad url": "p { background: url(a(b) }",
    "bad url quote": "p { background: url(a'b) }",
    "unterminated url": "p { background: url(a.png ",
    "rule without block": "p",
    "string eats the rest": "p { content: 'a\\' } @import url(x.css);",
    # characters
    "nul byte": "p { color: red\x00 }",
    "control character": "p { color: red\x07 }",
    "lone surrogate": "p::after { content: '\ud800' }",
}


@pytest.mark.parametrize("css", HOSTILE.values(), ids=list(HOSTILE))
def test_every_hostile_input_is_refused(css: str) -> None:
    assert check_css(css) is not None


def test_the_offender_is_named() -> None:
    assert check_css("@import url(x.css);") == "@import"
    assert check_css("p { width: expression(1) }") == "expression()"
    assert check_css("p { color: red") == "unbalanced brackets"
    assert check_css("/* x") == "an unterminated comment"
    assert (
        check_css("@font-face { src: url(https://a.example/f) }")
        == "an @font-face source that is not a data: URL"
    )
