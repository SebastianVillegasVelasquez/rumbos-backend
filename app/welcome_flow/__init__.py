"""Validation of the welcome flow: ordered steps configured per map.

The flow definition is data (schemas live in `app.schemas.welcome`). What lives
here are the rules that need more than a type: the HTML and CSS allowlists and
the cross-step checks. Everything here is defense in depth: the real boundary
for author-written HTML and CSS is the frontend's sandboxed, script-less iframe
with a Content-Security-Policy. These checks refuse, and never repair.
"""
