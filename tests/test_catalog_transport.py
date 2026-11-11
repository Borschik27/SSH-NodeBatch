import os
from pathlib import Path
import pytest
from nodebatch.catalog import catalog, fingerprint
from nodebatch.models import Host, Step
from nodebatch.transport import (command, remote_command, sftp_quote, windows_to_wsl, process_spec)


def test_include_literal_aliases_and_inventory(tmp_path, bundle):
    directory = tmp_path / "with spaces"
    directory.mkdir()
    (directory / "nodes.conf").write_text("Host worker-01 worker-02 *.invalid !excluded\n", encoding="utf-8")
    main = tmp_path / "ssh config"
    main.write_text(f'Host *\n User ops\nInclude "{directory.as_posix()}/*.conf"\nHost duplicate\n', encoding="utf-8")
    profile = bundle["settings"]["profiles"]["native"]
    profile["config"] = str(main)
    override = Host("worker-01", "192.0.2.5", groups=("workers",), os="linux")
    hosts, watched, patterns, _ = catalog(profile, [override])
    assert [h.alias for h in hosts] == ["duplicate", "worker-01", "worker-02"]
    assert hosts[1] == override
    before = fingerprint(watched, patterns)
    (directory / "new.conf").write_text("Host new-host\n", encoding="utf-8")
    assert fingerprint(watched, patterns) != before


def test_include_cycle(tmp_path, bundle):
    a, b = tmp_path / "a", tmp_path / "b"
    a.write_text(f'Host a\nInclude "{b.as_posix()}"\n', encoding="utf-8")
    b.write_text(f'Host b\nInclude "{a.as_posix()}"\n', encoding="utf-8")
    profile = bundle["settings"]["profiles"]["native"]
    profile["config"] = str(a)
    assert len(catalog(profile, [])[0]) == 2


def test_ssh_arguments_and_sudo_stdin(bundle):
    host = Host("alias", "192.0.2.2", "ops", 2222)
    step = Step("x", "printf '%s\\n' 'hello; world'", True)
    binary, argv = command(bundle["settings"]["profiles"]["native"],
                            bundle["settings"]["execution"], host, step, "__PROMPT__")
    assert binary == "ssh"
    assert "StrictHostKeyChecking=ask" in argv
    assert argv[-2] == "alias" and argv[-3] == "--"
    assert "sudo -S" in argv[-1]
    assert "topsecret" not in repr(argv)
    assert "-c" in remote_command(host, step, "__PROMPT__")


def test_sftp_paths():
    assert sftp_quote('a b"c[1]*') == '"a b\\"c\\[1\\]\\*"'
    with pytest.raises(ValueError):
        sftp_quote("bad\nput /etc/passwd /tmp/x")
    assert windows_to_wsl(r"C:\Temp\A Folder\file") == "/mnt/c/Temp/A Folder/file"
    with pytest.raises(ValueError):
        windows_to_wsl(r"\\server\share\file")


def test_wsl_arguments_without_local_shell(bundle, monkeypatch):
    from nodebatch import transport
    monkeypatch.setattr(transport.os, "name", "nt")
    monkeypatch.setattr(transport, "wsl_askpass_script", lambda: r"C:\Temp\askpass.sh")
    profile = dict(bundle["settings"]["profiles"]["native"], backend="wsl", wsl_distribution="Ubuntu",
                   wsl_home="/home/test")
    binary, args, env = process_spec(profile, "ssh", ["--", "alias", "echo '$HOME'"],
                                    {"NODEBATCH_AUTH_SERVER": "x", "NODEBATCH_AUTH_TOKEN": "opaque"},
                                    r"C:\Program Files\NodeBatch\nodebatch-askpass.exe")
    assert binary == "wsl.exe" and "--exec" in args
    assert "SSH_ASKPASS=/mnt/c/Temp/askpass.sh" in args
    assert "NODEBATCH_WINDOWS_ASKPASS=/mnt/c/Program Files/NodeBatch/nodebatch-askpass.exe" in args
    assert args[-1] == "echo '$HOME'"
