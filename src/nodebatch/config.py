from __future__ import annotations
from .i18n import tr
import copy
import os
import re
import tempfile
from importlib.resources import files
from pathlib import Path

import yaml
from PySide6.QtCore import QObject, QFileSystemWatcher, QTimer, Signal

from .models import Host, Scenario, Step

OS_NAMES = {"linux", "freebsd", "openbsd", "netbsd", "darwin", "unix"}
SAFE_ALIAS = re.compile(r"^[A-Za-z0-9_][A-Za-z0-9_.:@%+-]*$")
SAFE_ADDRESS = re.compile(r"^[A-Za-z0-9_:\[][A-Za-z0-9_.:\]%+-]*$")


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node, deep=False):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if not isinstance(key, str) or key in result:
            raise ConfigError(tr('YAML keys must be unique strings'))
        result[key] = loader.construct_object(value_node, deep=deep)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def parse_yaml(text):
    if len(text.encode("utf-8")) > 1024 * 1024:
        raise ConfigError(tr('Configuration file exceeds 1 MiB'))
    try:
        data = yaml.load(text, Loader=UniqueLoader)
    except yaml.YAMLError as exc:
        raise ConfigError(str(exc)) from exc
    if not isinstance(data, dict):
        raise ConfigError(tr('Expected a YAML mapping'))
    return data


def read_yaml(path):
    return parse_yaml(Path(path).read_text(encoding="utf-8-sig"))


def atomic_write(path, text):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".nodebatch-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as out:
            out.write(text)
            out.flush()
            os.fsync(out.fileno())
        import time
        for attempt in range(6):
            try:
                os.replace(temporary, path)
                break
            except PermissionError:
                if attempt == 5:
                    raise
                time.sleep(0.02 * (attempt + 1))
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _require(condition, message):
    if not condition:
        raise ConfigError(message)


def _integer(value, low, high, label):
    _require(type(value) is int and low <= value <= high,
             tr('{p0}: expected an integer between {p1} and {p2}', p0=label, p1=low, p2=high))


def _text(value, label, empty=True):
    _require(isinstance(value, str) and (empty or bool(value.strip())) and
             not any(c in value for c in "\x00\r\n"), tr('{p0}: invalid string', p0=label))


def validate_settings(s):
    _require(s.get("language", "auto") in ("auto", "en", "ru"), tr("language: expected auto, en or ru"))
    _require(s.get("version") == 1, tr('settings: version must be 1'))
    profiles = s.get("profiles")
    _require(isinstance(profiles, dict) and bool(profiles), tr('At least one profile is required'))
    _require(s.get("active_profile") in profiles, tr('active_profile is not defined in profiles'))
    for name, p in profiles.items():
        _text(name, tr('Profile name'), False)
        _require(isinstance(p, dict), tr('Profile {p0}: expected a mapping', p0=name))
        _require(p.get("backend") in ("native", "wsl"), tr('{p0}: backend must be native or wsl', p0=name))
        for key in ("ssh", "sftp", "config", "known_hosts", "wsl_distribution", "wsl_home"):
            _text(p.get(key), f"{name}.{key}", key not in ("ssh", "sftp"))
        _require(isinstance(p.get("keys"), list), tr('{p0}.keys: expected a list', p0=name))
        for key in p["keys"]:
            _text(key, f"{name}.keys", False)
        if p["backend"] == "wsl":
            _require(bool(p["wsl_distribution"]) and not p["wsl_distribution"].startswith("-") and
                     "/" not in p["wsl_distribution"] and "\\" not in p["wsl_distribution"],
                     tr('Invalid WSL distribution'))
            _require(not p["wsl_home"] or p["wsl_home"].startswith("/"),
                     tr('wsl_home must be an absolute Unix path'))
    e, log, watch = s.get("execution", {}), s.get("logging", {}), s.get("watch", {})
    validate_sudo_user(e.get("sudo_user", "root"))
    _require(e.get("mode") in ("sequential", "parallel"), tr('Unknown execution mode'))
    _require(type(e.get("stop_on_error")) is bool, tr('stop_on_error: expected true or false'))
    for key, lo, hi in (("parallelism", 1, 100), ("connect_timeout", 1, 300),
                        ("step_timeout", 1, 86400), ("auth_timeout", 5, 600)):
        _integer(e.get(key), lo, hi, key)
    _integer(log.get("retention_days"), 1, 36500, "retention_days")
    _integer(log.get("max_bytes"), 1024, 1024**4, "max_bytes")
    for key in ("run_max_bytes", "history_max_bytes"):
        if key in log:
            _integer(log[key], 1024, 1024**4, key)
    if "min_free_bytes" in log:
        _integer(log["min_free_bytes"], 0, 1024**4, "min_free_bytes")
    _integer(log.get("gui_blocks"), 100, 100000, "gui_blocks")
    _require(type(log.get("verbose")) is bool, tr('verbose: expected true or false'))
    _integer(watch.get("debounce_ms"), 100, 10000, "debounce_ms")
    _integer(watch.get("poll_ms"), 500, 60000, "poll_ms")


def validate_inventory(data):
    _require(data.get("version") == 1 and isinstance(data.get("hosts"), list),
             tr('inventory: version: 1 and hosts: [] are required'))
    hosts = []
    for row in data["hosts"]:
        _require(isinstance(row, dict), tr('Host must be a mapping'))
        alias = row.get("alias", "")
        _require(isinstance(alias, str) and bool(SAFE_ALIAS.fullmatch(alias)),
                 tr('Invalid host alias'))
        address = row.get("address", "")
        _require(isinstance(address, str) and (not address or bool(SAFE_ADDRESS.fullmatch(address))),
                 tr('{p0}: invalid address', p0=alias))
        user = row.get("user", "")
        _require(isinstance(user, str) and (not user or bool(re.fullmatch(r"[A-Za-z0-9_][A-Za-z0-9_.@\\-]*", user))),
                 tr('{p0}: invalid user', p0=alias))
        if row.get("port") is not None:
            _integer(row["port"], 1, 65535, "port")
        _require(row.get("os", "unix") in OS_NAMES, tr('{p0}: unknown OS', p0=alias))
        _require(row.get("shell", "sh") in ("sh", "bash"), tr('{p0}: shell must be sh or bash', p0=alias))
        for key in ("groups", "tags"):
            _require(isinstance(row.get(key, []), list), tr('{p0}.{p1}: expected a list', p0=alias, p1=key))
            for item in row.get(key, []):
                _text(item, key, False)
        _require(alias not in {h.alias for h in hosts}, tr('Duplicate host: {p0}', p0=alias))
        hosts.append(Host(alias, address, user, row.get("port"), tuple(row.get("groups", [])),
                          tuple(row.get("tags", [])), row.get("os", "unix"), row.get("shell", "sh")))
    return tuple(hosts)


def validate_safety(data):
    _require(data.get("version") == 1 and isinstance(data.get("rules"), list),
             tr('safety: version: 1 and rules: [] are required'))
    ids = set()
    for rule in data["rules"]:
        _require(isinstance(rule, dict), tr('Rule must be a mapping'))
        _text(rule.get("id"), "rule.id", False)
        _require(rule["id"] not in ids, tr('Duplicate rule.id'))
        ids.add(rule["id"])
        _require(rule.get("action") in ("allow", "confirm", "deny"), tr('Unknown rule action'))
        _require(rule.get("target", "command") in ("command", "destination"), tr('Unknown rule target'))
        _text(rule.get("explanation"), "rule.explanation", False)
        _text(rule.get("pattern"), "rule.pattern", False)
        try:
            re.compile(rule["pattern"])
        except re.error as exc:
            raise ConfigError(tr('Rule {p0}: {p1}', p0=rule['id'], p1=exc)) from exc


def validate_sudo_user(value):
    _require(isinstance(value, str) and bool(re.fullmatch(r"(?:[A-Za-z_][A-Za-z0-9_.@-]*[$]?|#[0-9]+)", value)),
             tr('sudo_user: enter a Unix username or #UID'))
    return value


def validate_groups(data):
    groups = data.get("groups", {})
    _require(isinstance(groups, dict), tr('inventory.groups: expected a mapping of names to alias lists'))
    for name, members in groups.items():
        _text(name, tr('Group name'), False)
        _require(isinstance(members, list) and all(isinstance(a, str) and SAFE_ALIAS.fullmatch(a)
                 for a in members), tr('Group {p0}: expected a list of SSH aliases', p0=name))
        _require(len(members) == len(set(members)), tr('Group {p0}: duplicate aliases', p0=name))
    return copy.deepcopy(groups)


def validate_scenario(data):
    for key in ("id", "name"):
        _text(data.get(key), f"scenario.{key}", False)
    _require(bool(re.fullmatch(r"[A-Za-z0-9_-]+", data["id"])), tr('Invalid scenario.id'))
    for key, choices in (("os", OS_NAMES), ("shells", {"sh", "bash"})):
        _require(isinstance(data.get(key), list) and bool(data[key]) and
                 all(v in choices for v in data[key]), tr('scenario.{p0}: invalid list', p0=key))
    parameters = data.get("parameters", {})
    _require(isinstance(parameters, dict), tr('parameters: expected a mapping'))
    for name, spec in parameters.items():
        _require(bool(re.fullmatch(r"[A-Za-z_]\w*", name)) and isinstance(spec, dict),
                 tr('Invalid parameter definition'))
        _require(not spec.get("secret") or not spec.get("default"),
                 tr('{p0}: a secret parameter cannot have a stored default value', p0=name))
        for boolean in ("secret", "required"):
            _require(boolean not in spec or type(spec[boolean]) is bool, f"{name}.{boolean}: true/false")
    _require(isinstance(data.get("steps"), list) and bool(data["steps"]), tr('Scenario steps are required'))
    steps = []
    for item in data["steps"]:
        _require(isinstance(item, dict), tr('Step must be a mapping'))
        _text(item.get("name"), "step.name", False)
        command = item.get("command")
        _require(isinstance(command, str) and bool(command.strip()) and "\x00" not in command,
                 tr('Empty or invalid command'))
        _require(type(item.get("sudo", False)) is bool, "step.sudo: true/false")
        validate_sudo_user(item.get("sudo_user", "root"))
        bindings = item.get("secrets", [])
        _require(isinstance(bindings, list) and len(bindings) <= 32 and
                 all(isinstance(n, str) and n in parameters and parameters[n].get("secret")
                     and bool(re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", n)) for n in bindings),
                 tr('step.secrets: list secret: true parameter names (at most 32)'))
        _require(len(set(bindings)) == len(bindings), tr('step.secrets: duplicate name'))
        if item.get("timeout") is not None:
            _integer(item["timeout"], 1, 86400, "step.timeout")
        from .templates import validate_template
        try:
            validate_template(command, parameters)
        except ValueError as exc:
            raise ConfigError(str(exc)) from exc
        steps.append(Step(item["name"], command, item.get("sudo", False), item.get("timeout"),
                          item.get("sudo_user", "root"), tuple(bindings)))
    return Scenario(data["id"], data["name"], str(data.get("description", "")),
                    tuple(data["os"]), tuple(data["shells"]), copy.deepcopy(parameters), tuple(steps))


def bootstrap(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    for resource in files("nodebatch").joinpath("defaults").iterdir():
        if resource.name.endswith(".yaml") and not (root / resource.name).exists():
            atomic_write(root / resource.name, resource.read_text(encoding="utf-8"))
    (root / "scenarios").mkdir(exist_ok=True)
    examples = files("nodebatch").joinpath("defaults", "scenarios")
    for resource in examples.iterdir():
        target = root / "scenarios" / resource.name
        # Only bootstrap example scenarios when the directory has never existed.
        if not (root / ".initialized").exists() and not target.exists():
            atomic_write(target, resource.read_text(encoding="utf-8"))
    (root / "scenarios").mkdir(exist_ok=True)
    (root / ".initialized").touch(exist_ok=True)


def validate_dependencies(data):
    _require(data.get("version") == 1, tr('dependencies.version must be 1'))
    _require(isinstance(data.get("linux"), dict), tr('dependencies.linux: expected a mapping'))
    for command in [data.get("windows_openssh"), data.get("wsl_install"), *data["linux"].values()]:
        _require(isinstance(command, list) and bool(command), tr('Installation command must be a nonempty list'))
        for argument in command:
            _text(argument, tr('Installer argument'), False)


def load_bundle(root, overrides=None):
    root, overrides = Path(root), overrides or {}
    def load(name):
        return parse_yaml(overrides[name]) if name in overrides else read_yaml(root / name)
    settings, inventory, safety = (load(n) for n in ("settings.yaml", "inventory.yaml", "safety.yaml"))
    dependencies = read_yaml(root / "dependencies.yaml")
    validate_dependencies(dependencies)
    validate_settings(settings)
    hosts = validate_inventory(inventory)
    groups = validate_groups(inventory)
    validate_safety(safety)
    scenarios = [validate_scenario(read_yaml(p)) for p in sorted((root / "scenarios").glob("*.yaml"))]
    _require(len({s.id for s in scenarios}) == len(scenarios), tr('Duplicate scenario IDs'))
    return {"settings": settings, "inventory": inventory, "hosts": hosts, "groups": groups,
            "safety": safety, "dependencies": dependencies, "scenarios": tuple(scenarios)}


class ConfigStore(QObject):
    changed = Signal(object)
    invalid = Signal(str)

    def __init__(self, root, parent=None):
        super().__init__(parent)
        self.root = Path(root)
        bootstrap(self.root)
        self.bundle = load_bundle(self.root)
        self.watcher = QFileSystemWatcher(self)
        self.debounce = QTimer(self)
        self.debounce.setSingleShot(True)
        self.debounce.timeout.connect(self.reload)
        self.watcher.directoryChanged.connect(self.schedule)
        self.watcher.fileChanged.connect(self.schedule)
        self.poll = QTimer(self)
        self.poll.timeout.connect(self.check)
        self._signature = None
        self._last_error = ""
        self.rewatch()
        self.poll.start(self.bundle["settings"]["watch"]["poll_ms"])

    def signature(self):
        paths = [self.root / n for n in ("settings.yaml", "inventory.yaml", "safety.yaml", "dependencies.yaml")]
        paths += sorted((self.root / "scenarios").glob("*.yaml"))
        return tuple((str(p), p.stat().st_mtime_ns, p.stat().st_size) if p.exists()
                     else (str(p), None, None) for p in paths)

    def rewatch(self):
        old = self.watcher.files() + self.watcher.directories()
        if old:
            self.watcher.removePaths(old)
        paths = [self.root, self.root / "scenarios"]
        # Directory watches survive atomic replacements and do not hold a Windows
        # file handle that can deny replacement. Polling also covers missed events.
        self.watcher.addPaths([str(p) for p in paths if p.exists()])
        self._signature = self.signature()

    def schedule(self, *_):
        self.debounce.start(self.bundle["settings"]["watch"]["debounce_ms"])

    def check(self):
        try:
            if self.signature() != self._signature:
                self.schedule()
        except OSError as exc:
            self.invalid.emit(str(exc))

    def reload(self):
        try:
            candidate = load_bundle(self.root)
        except (OSError, ValueError, TypeError, KeyError, RecursionError) as exc:
            message = tr('Configuration error. The last valid configuration remains active.\n{p0}', p0=exc)
            if message != self._last_error:
                self.invalid.emit(message)
                self._last_error = message
            return False
        self.bundle = candidate
        self._last_error = ""
        self.rewatch()
        self.poll.setInterval(candidate["settings"]["watch"]["poll_ms"])
        self.changed.emit(self.bundle)
        return True

    def save(self, name, text):
        if name not in ("settings.yaml", "inventory.yaml", "safety.yaml"):
            raise ConfigError(tr('Invalid settings file'))
        load_bundle(self.root, {name: text})
        atomic_write(self.root / name, text)
        self.reload()
