"""Conservative template context validation, not a shell syntax parser."""
from .i18n import tr
import re

REFERENCE = re.compile(r"\{\{([A-Za-z_]\w*)\}\}")


def validate_template(command, parameters):
    # Shell removes escaped newlines before token boundaries are determined.
    # This is only a validation view: execution retains the original block.
    command = command.replace("\\\n", "")
    refs = set(REFERENCE.findall(command))
    if not refs <= set(parameters):
        raise ValueError(tr('Step references an unknown parameter'))
    if not refs:
        return
    if "<<" in command:
        raise ValueError(tr('Parameters in heredoc/here-string are not supported; use a separate step'))
    if any(s in command for s in ("$(", "${", "`")):
        raise ValueError(tr('Parameter substitution cannot be combined with shell expansion; use a separate step'))
    quote, escaped, comment = None, False, False
    i = 0
    boundaries = " \t\r\n;|&()<>"
    while i < len(command):
        c = command[i]
        match = REFERENCE.match(command, i)
        if match:
            before = command[i - 1] if i else " "
            after = command[match.end()] if match.end() < len(command) else " "
            if quote or escaped or comment or before not in boundaries or after not in boundaries:
                raise ValueError(tr('Parameter {{name}} must be a separate, unquoted word'))
            i = match.end()
            continue
        if comment:
            comment = c != "\n"
        elif escaped:
            escaped = False
        elif c == "\\" and quote != "'":
            escaped = True
        elif quote:
            if c == quote:
                quote = None
        elif c in "\"'":
            quote = c
        elif c == "#" and (i == 0 or command[i - 1] in boundaries):
            comment = True
        i += 1
