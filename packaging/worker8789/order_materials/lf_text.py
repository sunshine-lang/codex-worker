"""Adapt a serialized JSON document arriving through a Langflow string tweak."""

import re


def restore_tweak_newlines(value):
    # Langflow expands the two characters backslash+n even inside serialized JSON.
    # Restore LF only inside quoted strings; leave optional pretty-print whitespace
    # outside strings alone. Keeping preceding backslashes recovers literal "\\n" too.
    if "\n" not in value:
        return value
    pieces = []
    start = 0
    in_string = False
    for token in re.finditer(r'["\n]', value):
        pos = token.start()
        if value[pos] == '"':
            first = pos
            while first and value[first - 1] == "\\":
                first -= 1
            if (pos - first) % 2 == 0:
                in_string = not in_string
        elif in_string:
            pieces.extend((value[start:pos], r"\n"))
            start = pos + 1
    if not pieces:
        return value
    pieces.append(value[start:])
    return "".join(pieces)
