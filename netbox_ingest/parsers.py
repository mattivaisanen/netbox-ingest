"""
Response parsers: bytes from the API to Python data.

Only JSON ships today. Another format (XML, CSV, ...) is one function that
returns dicts and lists, registered here; nothing else in the plugin needs to
change, since everything after parsing works on plain Python data.
"""
import json

from django.utils.translation import gettext_lazy as _


class ParseError(ValueError):
    pass


def parse_json(content, encoding=None):
    try:
        return json.loads(content.decode(encoding or 'utf-8', errors='replace'))
    except ValueError as exc:
        raise ParseError(f'The response is not valid JSON: {exc}') from exc


#: name -> (label, function(content: bytes, encoding: str|None) -> data)
PARSERS = {
    'json': (_('JSON'), parse_json),
}


def register(name, label, function):
    PARSERS[name] = (label, function)


def choices():
    return [(name, label) for name, (label, _function) in PARSERS.items()]


def parse(name, content, encoding=None):
    if name not in PARSERS:
        raise ParseError(f'Unknown response format "{name}".')
    return PARSERS[name][1](content, encoding)
