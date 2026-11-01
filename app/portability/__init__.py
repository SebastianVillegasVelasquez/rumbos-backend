"""Export and import of maps as a `.rumbos` archive.

`format` defines the archive, `reader` opens one without trusting it, `validation`
checks what it says, and `writer` produces one deterministically. The services
that use them live in `export` and `imports`.
"""
