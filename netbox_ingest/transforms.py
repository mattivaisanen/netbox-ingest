"""
Value transforms, applied to a record's value before it is written.

- regex: search the value; the replacement (\\1, \\g<name>) or the first
  group, or the whole match. No match: the field is left alone.
- lookup: translate through a table. A value not in the table is left
  alone, unless the table has a "*" entry used for everything else.
- jinja: NetBox's sandboxed Jinja2, with value, record and key. An empty
  result leaves the field alone.
"""
from .choices import TransformChoices
from .matching import compile_regex, regex_error

#: Returned when a transform has no value for the field.
SKIP = object()


class TransformError(Exception):
    pass


def config_error(field):
    """A message if the field's transform settings can't work, else ''."""
    if field.transform == TransformChoices.REGEX:
        if not field.expression:
            return 'Enter the regular expression.'
        return regex_error(field.expression)
    if field.transform == TransformChoices.LOOKUP:
        if not isinstance(field.table, dict) or not field.table:
            return 'Enter the lookup table as a JSON object.'
    if field.transform == TransformChoices.JINJA:
        if not field.expression:
            return 'Enter the template.'
        from jinja2 import TemplateSyntaxError
        from jinja2.sandbox import SandboxedEnvironment

        try:
            SandboxedEnvironment().parse(field.expression)
        except TemplateSyntaxError as exc:
            return f'Template error: {exc}'
    return ''


def _text(value):
    if isinstance(value, bool):
        return 'true' if value else 'false'
    return str(value)


def apply(field, value, record=None, key=None):
    """The transformed value, or SKIP."""
    kind = field.transform
    if kind == TransformChoices.NONE:
        return value
    if kind == TransformChoices.REGEX:
        if value is None:
            return SKIP
        match = compile_regex(field.expression).search(_text(value))
        if not match:
            return SKIP
        if field.replacement:
            return match.expand(field.replacement)
        return match.group(1) if match.re.groups else match.group(0)
    if kind == TransformChoices.LOOKUP:
        table = field.table or {}
        text = _text(value) if value is not None else ''
        if text in table:
            return table[text]
        if '*' in table:
            return table['*']
        return SKIP
    if kind == TransformChoices.JINJA:
        from utilities.jinja2 import render_jinja2

        try:
            result = render_jinja2(field.expression, {'value': value, 'record': record, 'key': key})
        except Exception as exc:  # noqa: BLE001 - shown in the report
            raise TransformError(f'Template failed: {exc}') from exc
        result = result.strip()
        return result if result != '' else SKIP
    raise TransformError(f'Unknown transform {kind}')
