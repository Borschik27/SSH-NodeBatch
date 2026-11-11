from __future__ import annotations
from .i18n import tr
import copy
import re
import shlex
from dataclasses import dataclass, field, replace
from typing import Any

COMMAND_MAX_BYTES = 32768


@dataclass(frozen=True)
class Host:
    alias: str
    address: str = ""
    user: str = ""
    port: int | None = None
    groups: tuple[str, ...] = ()
    tags: tuple[str, ...] = ()
    os: str = "unix"
    shell: str = "sh"


@dataclass(frozen=True)
class Step:
    name: str
    command: str
    sudo: bool = False
    timeout: int | None = None
    sudo_user: str = "root"
    secret_parameters: tuple[str, ...] = ()
    secret_inputs: tuple[tuple[str, str], ...] = field(default=(), repr=False)
    protocol: bool = False
    wire_id: str = ""

    def __post_init__(self):
        if len(self.command.encode("utf-8")) > COMMAND_MAX_BYTES:
            raise ValueError(tr('Command exceeds 32 KiB UTF-8 after substitution. Split it into steps; large script files are not supported yet.'))


@dataclass(frozen=True)
class Scenario:
    id: str
    name: str
    description: str
    os: tuple[str, ...]
    shells: tuple[str, ...]
    parameters: dict[str, dict[str, Any]]
    steps: tuple[Step, ...]

    def render(self, values):
        if set(values) - set(self.parameters):
            raise ValueError(tr('Unknown scenario parameters'))
        # Reject before reading values, quoting, or creating a rendered command.
        # Redaction cannot protect a secret passed in local/remote process argv.
        refs = {m.group(1) for step in self.steps
                for m in re.finditer(r"\{\{([A-Za-z_]\w*)\}\}", step.command)}
        if any(self.parameters.get(name, {}).get("secret") for name in refs):
            raise ValueError(tr('Secret substitution into commands is disabled: use the secure step.secrets channel and NODEBATCH_SECRET_<name>_FD descriptor; do not remove secret: true.'))
        from .templates import validate_template
        for step in self.steps:
            validate_template(step.command, self.parameters)
            if (len(step.secret_parameters) > 32 or any(
                    name not in self.parameters or not self.parameters[name].get("secret")
                    or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name)
                    for name in step.secret_parameters)):
                raise ValueError(tr('step.secrets: specify secret parameter names (at most 32)'))
        secrets, replacements = [], {}
        for name, spec in self.parameters.items():
            value = str(values.get(name, spec.get("default", "")))
            if spec.get("required", True) and not value:
                raise ValueError(tr('Fill in parameter {p0}', p0=name))
            if any(c in value for c in "\x00\n\r"):
                raise ValueError(tr('Parameter {p0}: line breaks and NUL are not allowed', p0=name))
            if spec.get("secret") and len(value.encode("utf-8")) > 4096:
                raise ValueError(tr('Parameter {p0}: secret exceeds 4096 bytes', p0=name))
            replacements[name] = shlex.quote(value)
            if spec.get("secret") and value:
                secrets.extend([value, shlex.quote(value)])

        def substitute(match):
            key = match.group(1)
            if key not in replacements:
                raise ValueError(tr('Unknown parameter {p0}', p0=key))
            return replacements[key]

        steps = tuple(replace(s, command=re.sub(r"\{\{([A-Za-z_]\w*)\}\}", substitute, s.command),
                              secret_inputs=tuple((name, str(values.get(name, "")))
                                                  for name in s.secret_parameters)) for s in self.steps)
        return steps, tuple(secrets)


@dataclass(frozen=True)
class Transfer:
    direction: str
    local: str
    remote: str
    overwrite: bool = False


@dataclass(frozen=True)
class Job:
    id: str
    hosts: tuple[Host, ...]
    steps: tuple[Step, ...]
    config: dict[str, Any]
    profile: dict[str, Any]
    ssh_fingerprint: tuple
    transfer: Transfer | None = None
    secrets: tuple[str, ...] = field(default=(), repr=False)

    @classmethod
    def create(cls, hosts, steps, config, fingerprint, transfer=None, secrets=()):
        import uuid
        steps = tuple(steps)
        inputs = [value for step in steps for _, value in step.secret_inputs]
        for step in steps:
            if (len(step.secret_inputs) > 32 or
                    len(dict(step.secret_inputs)) != len(step.secret_inputs) or
                    any(not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", name) or
                        not isinstance(value, str) or len(value.encode("utf-8")) > 4096
                        for name, value in step.secret_inputs)):
                raise ValueError(tr('Invalid secret input for this step'))
        frozen = copy.deepcopy(config)
        profile = frozen["settings"]["profiles"][frozen["settings"]["active_profile"]]
        return cls(uuid.uuid4().hex, tuple(hosts), tuple(steps), frozen,
                   profile, fingerprint, transfer, tuple(set(secrets) | set(filter(None, inputs))))


def download_destination(local, alias, remote):
    """Use the same safe host directory in the preview and the transfer engine."""
    import hashlib
    import posixpath
    from pathlib import Path
    safe = re.sub(r'[<>:"/\\|?*]', "_", alias).rstrip(". ")
    reserved = safe.split(".")[0].upper() in (
        {"CON", "PRN", "AUX", "NUL"} | {f"COM{i}" for i in range(1, 10)} | {f"LPT{i}" for i in range(1, 10)})
    if safe != alias or reserved:
        safe += "-" + hashlib.sha256(alias.encode()).hexdigest()[:8]
    return Path(local) / safe / posixpath.basename(remote)
