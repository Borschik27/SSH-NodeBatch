from __future__ import annotations
from .i18n import tr
import os
import shlex
import shutil
import sys
import tempfile

_WSL_WRAPPER = None


def wsl_askpass_script():
    global _WSL_WRAPPER
    if _WSL_WRAPPER is None:
        _WSL_WRAPPER = tempfile.TemporaryDirectory(prefix="nodebatch-askpass-")
        with open(os.path.join(_WSL_WRAPPER.name, "askpass.sh"), "w", encoding="utf-8", newline="\n") as out:
            out.write('#!/bin/sh\nexec "$NODEBATCH_WINDOWS_ASKPASS" "$@" </dev/null\n')
    return os.path.join(_WSL_WRAPPER.name, "askpass.sh")
from pathlib import Path

from PySide6.QtCore import QProcess, QProcessEnvironment

from .catalog import ProfilePaths
from .config import ConfigError


def windows_to_wsl(path):
    """Translate only local drive paths; UNC shares cannot be assumed mounted."""
    import re
    match = re.match(r"^([A-Za-z]):[/\\](.*)$", str(path))
    if not match:
        raise ConfigError(tr('WSL files and helper must be on a local Windows drive (C:\\...), not UNC.'))
    return "/mnt/" + match[1].lower() + "/" + match[2].replace("\\", "/")


def askpass_executable():
    suffix = ".exe" if os.name == "nt" else ""
    candidates = [Path(sys.executable).parent / ("nodebatch-askpass" + suffix),
                  Path(sys.executable).parent / "nodebatch-askpass" / ("nodebatch-askpass" + suffix),
                  Path(sys.prefix) / ("Scripts" if os.name == "nt" else "bin") / ("nodebatch-askpass" + suffix)]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate.resolve())
    found = shutil.which("nodebatch-askpass")
    if not found:
        raise ConfigError(tr('nodebatch-askpass not found. Install the project: python -m pip install -e .'))
    return found


def common_options(profile, execution):
    paths = ProfilePaths(profile)
    args = ["-o", "StrictHostKeyChecking=ask", "-o", "BatchMode=no", "-o", "LogLevel=ERROR",
            "-o", "NumberOfPasswordPrompts=3", "-o", "ControlMaster=no",
            "-o", "ControlPath=none", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "PermitLocalCommand=no", "-o", "VerifyHostKeyDNS=no", "-o", "UpdateHostKeys=no",
            "-o", "ServerAliveInterval=15",
            "-o", "ServerAliveCountMax=2", "-o", f"ConnectTimeout={execution['connect_timeout']}"]
    if profile["config"]:
        args += ["-F", paths.expand(profile["config"])]
    if profile["known_hosts"]:
        # OpenSSH parses this value as a list of quoted filenames.
        value = paths.expand(profile["known_hosts"]).replace("\\", "/")
        if '"' in value:
            raise ConfigError(tr('known_hosts: double quotes in paths are not supported'))
        args += ["-o", 'UserKnownHostsFile="' + value + '"']
    for key in profile["keys"]:
        args += ["-i", paths.expand(key)]
    return args


def target_options(host, sftp=False):
    args = []
    if host.address:
        args += ["-o", f"HostName={host.address}"]
    if host.user:
        args += ["-o", f"User={host.user}"]
    if host.port:
        args += ["-o", f"Port={host.port}"]
    return args


def remote_command(host, step, marker):
    shell = host.shell
    script = shlex.quote(step.command)
    if step.protocol:
        from importlib.resources import files
        script = files("nodebatch").joinpath("remote_agent.txt").read_text(encoding="utf-8")
        invocation = "python3 -u -c " + shlex.quote(script) + " " + shlex.quote(step.wire_id)
        if step.sudo:
            return f"sudo -S -p {shlex.quote(marker)} -u {shlex.quote(step.sudo_user)} -- {invocation}"
        return invocation
    if step.sudo:
        # The script is an argument; stdin belongs exclusively to sudo's password reader.
        return f"sudo -S -p {shlex.quote(marker)} -u {shlex.quote(step.sudo_user)} -- {shell} -c {script}"
    return f"{shell} -c {script}"


def command(profile, execution, host, step=None, marker="", resolve=False, transfer=False):
    args = common_options(profile, execution) + target_options(host)
    binary = profile["sftp"] if transfer else profile["ssh"]
    if transfer:
        args += ["-b", "-", host.alias]
    else:
        if resolve:
            args += ["-T", "-G"]
        else:
            args += ["-T", "-o", "ClearAllForwardings=yes", "-o", "RemoteCommand=none"]
        args += ["--", host.alias]
        if step:
            args += [remote_command(host, step, marker)]
    return binary, args


def process_spec(profile, binary, args, auth_env=None, helper=None):
    env = QProcessEnvironment.systemEnvironment()
    if auth_env:
        for key, value in auth_env.items():
            env.insert(key, value)
        env.insert("SSH_ASKPASS", helper)
        env.insert("SSH_ASKPASS_REQUIRE", "force")
        env.insert("DISPLAY", env.value("DISPLAY", "nodebatch:0"))
    if profile["backend"] != "wsl":
        return binary, args, env
    if os.name != "nt":
        raise ConfigError(tr('WSL backend is supported only on Windows'))
    forwarded = []
    if auth_env:
        # Preserve the broker variables when WSL invokes the Windows helper.
        # WSL does not forward arbitrary Linux environment variables to Win32.
        shared = env.value("WSLENV", "").split(":")
        for name in auth_env:
            if name not in {entry.split("/")[0] for entry in shared}:
                shared.append(name)
        env.insert("WSLENV", ":".join(entry for entry in shared if entry))
        forwarded = [f"{key}={value}" for key, value in auth_env.items()]
        # Win32 interop may consume inherited stdin while launching the helper.
        # SFTP uses that stdin for its binary protocol; isolate askpass with /dev/null.
        forwarded += ["SSH_ASKPASS=" + windows_to_wsl(wsl_askpass_script()),
                      "NODEBATCH_WINDOWS_ASKPASS=" + windows_to_wsl(helper),
                      "SSH_ASKPASS_REQUIRE=force", "DISPLAY=nodebatch:0"]
    return "wsl.exe", ["--distribution", profile["wsl_distribution"], "--exec",
                        "env", *forwarded, binary, *args], env


def sftp_quote(value):
    if any(c in value for c in "\r\n\x00"):
        raise ConfigError(tr('Transfer paths cannot contain line breaks or NUL'))
    # SFTP uses globbing even within quotes; escape metacharacters too.
    return '"' + "".join("\\" + c if c in '\\\"*?[]{}' else c for c in value) + '"'


def sync_process(binary, args, timeout=5000):
    """Used in the inventory/diagnostics worker, never on the GUI thread."""
    process = QProcess()
    process.start(binary, args)
    if not process.waitForStarted(timeout):
        raise ConfigError(process.errorString())
    if not process.waitForFinished(timeout):
        process.kill()
        process.waitForFinished(1000)
        raise ConfigError(tr('Diagnostics timed out'))
    output = bytes(process.readAllStandardOutput()).decode("utf-8", "replace")
    error = bytes(process.readAllStandardError()).decode("utf-8", "replace")
    if process.exitCode() != 0:
        raise ConfigError(error or output or tr('Process failed to start'))
    return output.strip()


def resolve_profile(profile):
    profile = dict(profile)
    if profile["backend"] == "wsl" and not profile["wsl_home"]:
        if os.name != "nt":
            raise ConfigError(tr('WSL is available only on Windows'))
        profile["wsl_home"] = sync_process("wsl.exe", ["--distribution", profile["wsl_distribution"],
                                                     "--exec", "printenv", "HOME"])
        if not profile["wsl_home"].startswith("/"):
            raise ConfigError(tr('Cannot determine HOME in WSL'))
    return profile


def diagnostics(profile):
    profile = resolve_profile(profile)
    results = []
    for name in ("ssh", "sftp"):
        binary = profile[name]
        if profile["backend"] == "wsl":
            location = sync_process("wsl.exe", ["--distribution", profile["wsl_distribution"],
                                               "--exec", "which", binary])
        else:
            location = shutil.which(binary)
            if not location:
                raise ConfigError(tr('Cannot find {p0}', p0=binary))
        results.append(f"{name}: {location}")
    binary, args, _ = process_spec(profile, profile["ssh"], ["-V"])
    # ssh -V writes to stderr even on success.
    process = QProcess()
    process.start(binary, args)
    if not process.waitForFinished(5000):
        process.kill()
        process.waitForFinished(1000)
        raise ConfigError(tr('SSH is not responding'))
    if process.exitCode():
        raise ConfigError(tr('Cannot run ssh -V'))
    results.append(bytes(process.readAllStandardError()).decode("utf-8", "replace").strip())
    helper = askpass_executable()
    if profile["backend"] == "wsl":
        windows_to_wsl(helper)
    results.append("askpass: " + helper)
    results.append("HOME: " + str(ProfilePaths(profile).home))
    return profile, "\n".join(results)
