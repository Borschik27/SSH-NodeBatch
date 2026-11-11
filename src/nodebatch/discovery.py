"""Read-only discovery; dependency installation is an explicit separate action."""
from .i18n import tr
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from PySide6.QtCore import QProcess
from .catalog import ProfilePaths
from .config import ConfigError
from .transport import resolve_profile


def decode_output(data):
    if data.startswith((b"\xff\xfe", b"\xfe\xff")):
        return data.decode("utf-16").strip()
    if b"\x00" in data[:100]:
        return data.decode("utf-16-le", "replace").strip()
    return data.decode("utf-8", "replace").strip()


def capture(program, args, timeout=10000):
    process = QProcess()
    process.start(program, args)
    if not process.waitForStarted(timeout):
        raise ConfigError(process.errorString())
    if not process.waitForFinished(timeout):
        process.kill()
        process.waitForFinished(1000)
        raise ConfigError(tr('Environment check timed out'))
    output = decode_output(bytes(process.readAllStandardOutput()))
    if process.exitCode():
        raise ConfigError(decode_output(bytes(process.readAllStandardError())) or output)
    return output


def is_file(path):
    try:
        return path.is_file()
    except OSError:
        return False


def discover(profile, dependencies):
    result = {"choices": {}, "keys": [], "missing": [], "invalid": [], "manager": "", "distros": [],
              "profile": dict(profile), "messages": []}
    choices = result["choices"]
    if os.name == "nt":
        wsl = shutil.which("wsl.exe")
        if wsl:
            try:
                result["distros"] = capture(wsl, ["--list", "--quiet"]).splitlines()
            except ConfigError as exc:
                result["messages"].append("WSL: " + str(exc))
        choices["wsl_distribution"] = result["distros"]
    if profile["backend"] == "wsl":
        if os.name != "nt" or not shutil.which("wsl.exe"):
            result["missing"].append("wsl")
            result["messages"].append(tr('WSL not found.'))
            return result
        if profile["wsl_distribution"] not in result["distros"]:
            result["missing"].append("distribution")
            result["messages"].append(tr('Distribution is not installed. Select an installed distribution or install the requested one.'))
            return result
        profile = resolve_profile(profile)
        result["profile"] = profile
        names = list(dict.fromkeys([profile["ssh"], profile["sftp"], "ssh", "sftp", *dependencies["linux"]]))
        script = 'for name in "$@"; do p=$(command -v "$name") || continue; printf "%s\\t%s\\n" "$name" "$p"; done'
        output = capture("wsl.exe", ["--distribution", profile["wsl_distribution"], "--exec",
                                     "sh", "-c", script, "nodebatch-discovery", *names])
        found = dict(line.split("\t", 1) for line in output.splitlines() if "\t" in line)
        for key in ("ssh", "sftp"):
            choices[key] = list(dict.fromkeys(found[n] for n in (profile[key], key)
                                             if found.get(n, "").startswith("/")))
            if not found.get(profile[key], "").startswith("/"):
                result["invalid"].append(key)
            if not choices[key]:
                result["missing"].append(key)
        result["manager"] = next((name for name in dependencies["linux"] if name in found), "")
    else:
        for key in ("ssh", "sftp"):
            candidates = [shutil.which(profile[key]), shutil.which(key)]
            if os.name == "nt":
                candidates.append(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "System32/OpenSSH" / (key + ".exe")))
            else:
                candidates += [f"/usr/bin/{key}", f"/usr/local/bin/{key}"]
            choices[key] = list(dict.fromkeys(p for p in candidates if p and is_file(Path(p))))
            if not shutil.which(profile[key]):
                result["invalid"].append(key)
            if not choices[key]:
                result["missing"].append(key)
        result["manager"] = next((name for name in dependencies["linux"] if shutil.which(name)), "")
    paths = ProfilePaths(profile)
    for key, default in (("config", "~/.ssh/config"), ("known_hosts", "~/.ssh/known_hosts")):
        candidates = [profile[key], paths.expand(default)]
        if key == "config" and os.name == "nt" and profile["backend"] == "native":
            candidates.append(str(Path(os.environ.get("PROGRAMDATA", "C:/ProgramData")) / "ssh/ssh_config"))
        choices[key] = [value for value in dict.fromkeys(candidates) if value and is_file(paths.local(value))]
        if profile[key] and not is_file(paths.local(profile[key])):
            result["messages"].append(tr('{p0}: file not found.', p0=key))
        elif not choices[key]:
            result["messages"].append(tr('{p0}: default file is missing; you can select another path.', p0=key))
    choices["wsl_home"] = [paths.home] if profile["backend"] == "wsl" else []
    home = paths.local("~/.ssh")
    keys = list(profile["keys"])
    for key in keys:
        if not is_file(paths.local(key)):
            result["messages"].append(tr('Key not found: ') + key)
    try:
        keys += [paths.expand("~/.ssh/") + p.name for p in home.iterdir()
                 if (p.name.startswith("id_") or p.suffix == ".pem") and
                 not p.name.endswith((".pub", "-cert")) and is_file(p)]
    except OSError:
        pass
    result["keys"] = [p for p in dict.fromkeys(keys) if is_file(paths.local(p))]
    for key in ("ssh", "sftp"):
        result["messages"].append(f"{key}: " + (choices.get(key, [tr('not found')])[0]
                                  if key not in result["invalid"] else tr('program not found; select a detected program from the list')))
    return result


def _ps_quote(value):
    return "'" + value.replace("'", "''") + "'"


def windows_terminal(program, args, elevate=False):
    # Start-Process takes a Windows command line; quote it separately from PowerShell.
    line = subprocess.list2cmdline(args)
    script = "$p = Start-Process -FilePath " + _ps_quote(program) + \
             " -ArgumentList " + _ps_quote(line) + " -WindowStyle Normal -Wait -PassThru"
    if elevate:
        script += " -Verb RunAs"
    script += "; exit $p.ExitCode"
    return "powershell.exe", ["-NoProfile", "-Command", script]


def installation_plan(profile, detected, dependencies):
    missing = detected["missing"]
    if not missing:
        raise ConfigError(tr('No missing dependencies detected.'))
    if profile["backend"] == "wsl" and any(name in missing for name in ("wsl", "distribution")):
        distro = profile["wsl_distribution"]
        if not distro or distro.startswith("-") or any(c in distro for c in "/\\\r\n\x00"):
            raise ConfigError(tr('Enter a WSL distribution name.'))
        command = [value.replace("{distribution}", distro) for value in dependencies["wsl_install"]]
        launch = windows_terminal(command[0], command[1:], elevate=True)
        return command, launch, tr('Install WSL/distribution. A restart and initial Linux setup may be required.')
    if os.name == "nt" and profile["backend"] == "native":
        command = list(dependencies["windows_openssh"])
        return command, windows_terminal(command[0], command[1:], elevate=True), \
               tr('Install the Windows OpenSSH Client component; administrator approval will be requested.')
    manager = detected["manager"]
    if manager not in dependencies["linux"]:
        raise ConfigError(tr('No supported package manager found. Install OpenSSH Client manually.'))
    command = list(dependencies["linux"][manager])
    if profile["backend"] == "wsl":
        args = ["--distribution", profile["wsl_distribution"], "--exec", "sudo", *command]
        return ["wsl.exe", *args], windows_terminal("wsl.exe", args), \
               tr('Install OpenSSH in the selected WSL distribution. Enter the sudo password in the terminal.')
    if hasattr(os, "geteuid") and os.geteuid() == 0:
        terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
        launch = (terminal, ["-e", *command]) if terminal else (command[0], command[1:])
        return command, launch, tr('Install OpenSSH with the current administrator privileges.')
    if shutil.which("pkexec"):
        return ["pkexec", *command], ("pkexec", command), tr('Install OpenSSH with the system package manager.')
    terminal = shutil.which("x-terminal-emulator") or shutil.which("xterm")
    if not terminal:
        raise ConfigError(tr('Installation requires pkexec or x-terminal-emulator; install OpenSSH manually.'))
    return ["sudo", *command], (terminal, ["-e", "sudo", *command]), \
           tr('Install OpenSSH in a separate terminal; enter the sudo password there.')
