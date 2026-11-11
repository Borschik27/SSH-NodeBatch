from __future__ import annotations
from .i18n import tr
import glob
import os
import posixpath
import shlex
from pathlib import Path

from .config import SAFE_ALIAS, ConfigError
from .models import Host


class ProfilePaths:
    def __init__(self, profile):
        self.profile = profile
        self.wsl = profile["backend"] == "wsl"
        self.home = profile.get("wsl_home") if self.wsl else str(Path.home())
        if self.wsl and not self.home:
            raise ConfigError(tr('Run profile diagnostics or set wsl_home before using WSL.'))

    def expand(self, value):
        if value == "~":
            return self.home
        if value.startswith("~/") or value.startswith("~\\"):
            return self.home.rstrip("/\\") + "/" + value[2:]
        return value

    def local(self, value):
        value = self.expand(value)
        if self.wsl:
            if not value.startswith("/"):
                raise ConfigError(tr('WSL: an absolute Unix path is required'))
            return Path("\\\\wsl.localhost\\" + self.profile["wsl_distribution"] + value.replace("/", "\\"))
        return Path(value).expanduser()

    def default_config(self):
        return self.expand(self.profile["config"] or "~/.ssh/config")

    def include(self, pattern, system=False):
        pattern = self.expand(pattern)
        if self.wsl:
            if not pattern.startswith("/"):
                pattern = posixpath.join("/etc/ssh" if system else self.home + "/.ssh", pattern)
        elif not Path(pattern).is_absolute():
            pattern = str(Path("/etc/ssh") / pattern) if system else str(Path.home() / ".ssh" / pattern)
        return self.local(pattern)


def catalog(profile, inventory, groups=None):
    """Only enumerate literal aliases. OpenSSH remains the connection authority."""
    paths = ProfilePaths(profile)
    aliases, watched, patterns, warnings = set(), set(), set(), []
    seen = set()

    def visit(path, depth=0, system=False):
        path = Path(path)
        watched.add(path)
        if depth > 32:
            raise ConfigError(tr('SSH Include nesting is too deep'))
        if not path.exists():
            return
        key = str(path.resolve())
        if key in seen:
            return
        seen.add(key)
        for line_no, raw in enumerate(path.read_text(encoding="utf-8-sig", errors="replace").splitlines(), 1):
            # OpenSSH allows Keyword=value as well as Keyword value.
            line = raw.strip()
            if not line or line.startswith("#"):
                continue
            parts = line.replace("=", " ", 1).split(None, 1) if "=" in line.split(None, 1)[0] else line.split(None, 1)
            if len(parts) < 2:
                continue
            keyword = parts[0].lower()
            if keyword not in ("host", "include", "identityfile", "certificatefile", "userknownhostsfile"):
                continue
            try:
                lexer = shlex.shlex(parts[1], posix=False)
                lexer.whitespace_split = True
                values = [v[1:-1] if len(v) >= 2 and v[0] == v[-1] and v[0] in "\'\\\"" else v for v in lexer]
            except ValueError as exc:
                raise ConfigError(f"{path}:{line_no}: {exc}") from exc
            if keyword == "host":
                aliases.update(v for v in values if SAFE_ALIAS.fullmatch(v))
            elif keyword == "include":
                for value in values:
                    pattern = str(paths.include(value, system))
                    patterns.add(pattern)
                    # Watch parent directories too, to detect new matching files.
                    for found in sorted(glob.glob(pattern)):
                        visit(found, depth + 1, system)
            else:
                for value in values:
                    if "%" not in value and "$" not in value and value.lower() != "none":
                        try:
                            watched.add(paths.local(value if value.startswith(("~", "/")) or
                                                    (not paths.wsl and Path(value).is_absolute())
                                                    else paths.expand("~/") + value))
                        except (ValueError, OSError):
                            pass

    if profile["config"] and not paths.local(paths.default_config()).is_file():
        raise ConfigError(tr('SSH config file not found: ') + profile["config"])
    visit(paths.local(paths.default_config()))
    if not profile["config"] and (paths.wsl or os.name != "nt"):
        visit(paths.local("/etc/ssh/ssh_config"), system=True)
    patterns.add(str(paths.local("~/.ssh/id_*")))
    for value in profile["keys"]:
        watched.add(paths.local(value))
    for value in (profile["known_hosts"] or "~/.ssh/known_hosts", "~/.ssh/known_hosts2"):
        watched.add(paths.local(value))
    merged = {name: Host(name) for name in aliases}
    merged.update({h.alias: h for h in inventory})
    from dataclasses import replace
    for group, members in (groups or {}).items():
        for alias in members:
            if alias in merged:
                host = merged[alias]
                merged[alias] = replace(host, groups=tuple(sorted(set(host.groups) | {group})))
    return tuple(merged[n] for n in sorted(merged)), tuple(sorted(watched)), tuple(sorted(patterns)), warnings


def fingerprint(watched, patterns=()):
    paths = set(Path(p) for p in watched)
    for pattern in patterns:
        paths.update(Path(p) for p in glob.glob(pattern))
    result = []
    for path in sorted(paths):
        try:
            stat = path.stat()
            # Metadata-only: a private key can have any filename or extension.
            result.append((str(path), stat.st_mtime_ns, stat.st_ctime_ns, stat.st_size))
        except FileNotFoundError:
            result.append((str(path), None))
    return tuple(result)
