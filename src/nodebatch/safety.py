from .i18n import tr
import posixpath
import re
from dataclasses import dataclass


@dataclass(frozen=True)
class Decision:
    action: str
    reasons: tuple[str, ...]


def assess(safety, steps=(), destination=None, transfer=False):
    matches = []
    command_texts = []
    import shlex
    for step in steps:
        command_texts.append(step.command)
        try:
            words = shlex.split(step.command)
            command_texts.append(" ".join(posixpath.normpath(w) if w.startswith("/") else w for w in words))
        except ValueError:
            matches.append(("confirm", tr('Cannot parse the shell expression.')))
    for rule in safety["rules"]:
        target = rule.get("target", "command")
        texts = command_texts if target == "command" else (
            [("/" + posixpath.normpath(destination).lstrip("/")) if destination.startswith("/")
              else posixpath.normpath(destination)] if destination else [])
        if any(re.search(rule["pattern"], text) for text in texts):
            matches.append((rule["action"], f"{rule['id']}: {tr(rule['explanation'])}"))
    if any(s.sudo for s in steps):
        matches.append(("confirm", tr('Run through sudo as the selected user.')))
    if transfer:
        matches.append(("confirm", tr('File transfer changes the local or remote filesystem.')))
    if destination and not destination.startswith("/"):
        matches.append(("confirm", tr('Relative remote path: check the working directory.')))
    action = max((a for a, _ in matches), key={"allow": 0, "confirm": 1, "deny": 2}.get, default="allow")
    return Decision(action, tuple(dict.fromkeys(reason for _, reason in matches)))
