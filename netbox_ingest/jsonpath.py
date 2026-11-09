"""
A small path language for JSON documents, so no library is needed.

    items              key "items"
    data.items         nested keys; a leading "$." is allowed and ignored
    checks[0].state    list index (negative counts from the end)
    ["a.b"].c          a key containing dots or brackets
    *                  the document itself

Paths are parsed once and cached.
"""
import re
from functools import lru_cache

MISSING = object()

_TOKEN = re.compile(r"""
    \[\s*(?P<index>-?\d+)\s*\]          # [0]
  | \[\s*(?P<q>["'])(?P<quoted>.*?)(?P=q)\s*\]   # ["key"]
  | (?P<key>[^.\[\]]+)                  # key
  | (?P<dot>\.)
""", re.VERBOSE)


class PathError(ValueError):
    pass


@lru_cache(maxsize=1024)
def parse(path):
    """Path text to a tuple of keys (str) and indexes (int)."""
    path = (path or '').strip()
    if path in ('', '$', '*', '$.*'):
        return ()
    if path.startswith('$.'):
        path = path[2:]
    elif path.startswith('$['):
        path = path[1:]
    parts, pos = [], 0
    while pos < len(path):
        match = _TOKEN.match(path, pos)
        if not match or match.end() == pos:
            raise PathError(f'Cannot read the path at "{path[pos:]}".')
        if match.group('index') is not None:
            parts.append(int(match.group('index')))
        elif match.group('q'):
            parts.append(match.group('quoted'))
        elif match.group('key') is not None:
            parts.append(match.group('key').strip())
        pos = match.end()
    return tuple(parts)


def get(data, path, default=MISSING):
    """The value at path, or default when any step is missing."""
    current = data
    for part in parse(path):
        if isinstance(part, int):
            if not isinstance(current, list) or not -len(current) <= part < len(current):
                return default
            current = current[part]
        elif isinstance(current, dict) and part in current:
            current = current[part]
        else:
            return default
    return current


def error(path):
    """A message if the path cannot be parsed, else ''."""
    try:
        parse(path)
    except PathError as exc:
        return str(exc)
    return ''


def paths(data, prefix='', depth=6, limit=300):
    """
    (path, sample value) for every leaf of a sample record, to help users
    write paths. Lists show their first element only.
    """
    out = []

    def walk(value, path, level):
        if len(out) >= limit:
            return
        if isinstance(value, dict) and value and level < depth:
            for key, child in value.items():
                plain = re.fullmatch(r'[A-Za-z_][\w\-]*', str(key))
                step = f'{path}.{key}' if plain and path else (str(key) if plain else f'{path}["{key}"]')
                walk(child, step, level + 1)
        elif isinstance(value, list) and value and level < depth:
            walk(value[0], f'{path}[0]', level + 1)
        else:
            out.append((path or '*', value))

    walk(data, prefix, 0)
    return out
