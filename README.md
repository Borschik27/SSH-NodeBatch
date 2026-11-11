# NodeBatch

[Русский](README.ru.md) | **English**

---

NodeBatch runs commands and scenarios across multiple SSH hosts from one window.
Select servers or a group, review the job, and get a separate result for each host.
It supports sequential and parallel execution, sudo, history, and single-file SFTP
transfers.

This is a batch operations application. Use an ordinary SSH terminal for interactive
server sessions.

- [How it works](#how-it-works)
- [Installation and startup](#installation-and-startup)
- [First setup](#first-setup)
- [Configuration and SSH keys](#configuration-and-ssh-keys)
- [Creating groups](#creating-groups)
- [Running commands and scenarios](#running-commands-and-scenarios)
- [Settings roadmap](#settings-roadmap)
- [Details and limitations](#details-and-limitations)

## How it works

NodeBatch uses system `ssh` and `sftp`. OpenSSH handles connections, keys, SSH agents,
and ProxyJump; the application prepares jobs and presents their progress.

1. Reads the active profile from `settings.yaml`.
2. Collects hosts from SSH config and `inventory.yaml`, adding groups and tags.
3. Shows selected hosts, commands, sudo settings, and safety rule findings for review.
4. Resolves each host through `ssh -G` and checks the remote OS with `uname -s`.
5. Sends the command to a remote Python helper. The helper starts `sh` or `bash`,
   streams output, and confirms the shell exit code.
6. Stores results in SQLite and the job output in a JSONL log.

Sequential mode handles hosts one at a time. Parallel mode runs several hosts at
once, but each host's scenario steps still run in order. One window runs one job
at a time.

Every step creates a separate connection and shell. Working directories, variables,
and activated environments do not survive between steps. Keep dependent operations
in the same shell block.

## Installation and startup

### Requirements

| Location | Required software |
|---|---|
| Packaged application computer | Windows or Linux, system `ssh` and `sftp` |
| Running from source | Additionally Python 3.12+ and project dependencies |
| Remote host | Unix, SSH access, `sh` or `bash`, and the `python3` command |
| Remote helper runtime | Python with `fcntl`, `termios.FIONREAD`, and polling support for Unix pipes |
| Windows with the `wsl` backend | An initialized WSL distribution with OpenSSH inside it |

Inventory recognizes Linux, FreeBSD, OpenBSD, NetBSD, and macOS. This does not mean
all systems have passed real acceptance testing: check shell and scenario commands
on the target OS. Remote Windows PowerShell/cmd hosts are not supported.

### Packaged application

Extract the whole `NodeBatch` folder. Keep `_internal`, `nodebatch-askpass`,
documentation, and licenses with the application.

- Windows: run `NodeBatch.exe` from a local drive.
- Linux: run `./NodeBatch` from the extracted directory.

Packaged applications do not require a local Python installation. Remote hosts
still need `python3`. Test the Linux binary on your distribution; a Linux build
name does not establish compatibility with older glibc versions.

### Source checkout

Create a virtual environment from the project root:

```sh
python -m venv .venv
```

Activate it:

```sh
# Linux
. .venv/bin/activate
```

```powershell
# Windows PowerShell
.\.venv\Scripts\Activate.ps1
```

Install dependencies and the project:

```sh
python -m pip install -r requirements-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
nodebatch
```

`python -m nodebatch` also works. Passwords and host-key confirmations need
`nodebatch-askpass`, so install the project; setting `PYTHONPATH` alone is insufficient.

## First setup

1. Open `Settings` and check the configuration directory path.
2. In `Connections`, choose the backend, SSH/SFTP programs, and SSH config.
3. Click `Detect / check environment`, select the detected programs, and add keys as needed.
4. Check the active profile and save settings.
5. Add hosts through SSH config or `Host sources`.
6. Refresh the host list. Select one server first and run `uname -a` and `id`.
7. After that works, create groups and configure concurrency, timeouts, and logging.

Environment detection looks for local programs and files. It does not confirm server
reachability, passwords, or sudo access. For missing dependencies, a separate button
opens the system installer. The application first shows the command from
`dependencies.yaml`; it never installs software on remote servers automatically.

## Configuration and SSH keys

### File locations

On first launch NodeBatch creates operator-owned YAML files and a `system-info`
example. Files under `src/nodebatch/defaults` are templates. Edit working settings
in the directory shown in the interface.

| Data | Linux | Windows |
|---|---|---|
| Configuration | `~/.config/nodebatch` | `%APPDATA%\NodeBatch` |
| History and logs | `~/.local/state/nodebatch` | `%LOCALAPPDATA%\NodeBatch` |

Linux respects `XDG_CONFIG_HOME` and `XDG_STATE_HOME`. Separate environments can use
explicit directories:

```sh
nodebatch --config-dir ./operator-config --data-dir ./operator-state
```

These are paths on your computer. On Windows, the SQLite history, application, and
local files transferred through WSL must live on a local drive rather than a UNC share.

| File | Purpose |
|---|---|
| `settings.yaml` | Profiles, active connection, execution, language, and storage limits |
| `inventory.yaml` | Additional hosts, OS, shells, groups, and tags |
| `safety.yaml` | `allow`, `confirm`, and `deny` rules for commands and destinations |
| `dependencies.yaml` | Local installation commands for OpenSSH and WSL |
| `scenarios/*.yaml` | Multi-step scenarios and parameters |
| SSH `config` | HostName, User, Port, IdentityFile, ProxyJump, and other OpenSSH options |
| SSH `known_hosts` | Previously recorded server host keys |

### `settings.yaml`

Complete example with the initial limits:

```yaml
version: 1
language: auto
active_profile: native
profiles:
  native:
    backend: native
    ssh: ssh
    sftp: sftp
    config: ''
    keys: []
    known_hosts: ''
    wsl_distribution: ''
    wsl_home: ''
execution:
  mode: sequential
  parallelism: 4
  stop_on_error: true
  connect_timeout: 10
  step_timeout: 600
  auth_timeout: 120
  sudo_user: root
logging:
  retention_days: 30
  max_bytes: 1073741824
  run_max_bytes: 268435456
  history_max_bytes: 1073741824
  min_free_bytes: 16777216
  gui_blocks: 2000
  verbose: false
watch:
  debounce_ms: 500
  poll_ms: 2000
```

| Setting | Behavior |
|---|---|
| `active_profile` | Profile used for new jobs |
| `backend: native` | SSH/SFTP run directly on the current OS |
| `config: ''` | OpenSSH uses normal configuration; host enumeration looks for `~/.ssh/config` |
| `known_hosts: ''` | OpenSSH uses its default files |
| `keys: []` | No additional `-i` arguments; OpenSSH settings and the agent still apply |
| `parallelism` | 1 to 100 concurrent hosts, used in `parallel` mode |
| `stop_on_error` | Do not start new hosts or subsequent steps after a failure; operations already running continue |
| `connect_timeout` | SSH connection establishment timeout in seconds |
| `auth_timeout` | Authentication wait in seconds |
| `step_timeout` | Step timeout, including request transmission and result delivery after `ready` |
| `sudo_user` | Initial sudo target for a free-form command |
| `retention_days` | Retention period for completed jobs |
| `run_max_bytes` | Maximum log size for one job, in bytes |
| `history_max_bytes` | Entire history directory budget, including SQLite and WAL/SHM |
| `min_free_bytes` | Reserved free disk space |
| `gui_blocks` | Display limit; the independently stored log has its own limits |
| `max_bytes` | Legacy fallback when run/history limits are missing |
| `watch` | Reload debounce and file-change polling interval, in milliseconds |

Storage sizes are entered in MiB in the GUI and saved as bytes in YAML.
The edited profile and the active profile are separate choices. Editing a profile
does not activate it. Select `Active profile` and click `Save settings`.

Most saved changes apply to subsequent jobs. A prepared job uses its own settings
snapshot. Malformed YAML leaves the last successfully loaded configuration active;
fix the file before preparing another job. If files changed externally while a form
was being edited, click `Reload files` and reapply unsaved changes.

### Activating keys

NodeBatch stores private-key paths, not private-key contents, in a profile.
Select an existing private key, not its `.pub` public counterpart.

1. Open `Settings > Connections` and choose the profile to edit.
2. Click `Add key` and select the file, or choose one under `Detected keys` and
   click `Add detected key`.
3. Check that its path appears in `IdentityFile`, with one path per line.
4. Make the profile active and save settings.

The corresponding profile fragment is:

```yaml
keys:
  - ~/.ssh/id_ed25519_ops
  - ~/.ssh/id_ed25519_backup
```

Each path becomes an additional `ssh -i` and `sftp -i` argument for every host in
the active profile. Remove its line and save to stop adding that identity.
There are no per-key `enabled` fields or activation checkboxes yet. Detecting a
key alone does not activate it.

Removing a line does not prevent OpenSSH from finding the same key through an
SSH-config `IdentityFile` or an agent. For different identities per server, leave
`keys: []` and configure the keys in SSH config:

```sshconfig
Host web-01
    HostName 192.0.2.10
    User operator
    Port 22
    IdentityFile ~/.ssh/id_ed25519_web
    IdentitiesOnly yes

Host db-01
    HostName 192.0.2.20
    User operator
    IdentityFile ~/.ssh/id_ed25519_db
    IdentitiesOnly yes
```

These are documentation addresses; replace them. `IdentitiesOnly yes` restricts
identity selection to those configured for the connection. Multiple configured
`IdentityFile` entries still apply. `keys: []` alone does not disable the agent or
password authentication.

Generate keys outside NodeBatch, for example with `ssh-keygen -t ed25519`. Install
the public key in the server's `authorized_keys`; keep the private key locally.
The application requests a key's passphrase when needed. Passwords and passphrases
are not saved in YAML. Do not add operator keys to the repository.

Native Windows profiles can use paths such as `C:/Keys/id_ed25519_ops`. WSL profiles
need paths inside the selected distribution, such as `~/.ssh/id_ed25519_ops`.
Native and WSL agents belong to separate environments.

### WSL profile

On Windows, list installed distributions:

```powershell
wsl --list --quiet
```

Add this profile under `profiles`, then choose it through `active_profile` or the
GUI. `Debian` is an example; use the exact installed distribution name.

```yaml
wsl:
  backend: wsl
  ssh: ssh
  sftp: sftp
  config: ~/.ssh/config
  keys: []
  known_hosts: ''
  wsl_distribution: Debian
  wsl_home: ''
```

An empty `wsl_home` enables automatic HOME detection. Config, keys, and known_hosts
use Unix paths inside WSL. NodeBatch, its Windows helper, and local transfer files
must remain on a local Windows drive. Do not copy native paths into a WSL profile
without translating them. Open a new distribution and complete its ordinary first-run
setup before using it here.

### Hosts and `inventory.yaml`

The list requires literal aliases such as `Host web-01`. `Host *.example` does not
expand into a set of servers. Include files are followed; OpenSSH still resolves
actual connection settings.

Inventory can add hosts or enrich existing SSH-config aliases:

```yaml
version: 1
hosts:
  - alias: web-01
    address: 192.0.2.10
    user: operator
    port: 22
    os: linux
    shell: sh
    tags: [web, production]
  - alias: db-01
    os: linux
    shell: bash
    tags: [database]
groups:
  web: [web-01]
  maintenance: [web-01, db-01]
```

Here `db-01` gets its address and user from SSH config. Nonempty `address`, `user`,
and an explicitly supplied `port` in inventory are passed as explicit OpenSSH
options and take precedence. Matching aliases produce one host.

`os: unix` does not require a particular Unix OS; `linux` or `freebsd` is checked
against actual `uname -s` output. `shell` accepts only `sh` and `bash`. Tags help
searching but do not replace groups. Configure per-host keys through SSH config;
an inventory host row has no `keys` setting.

## Creating groups

1. Check the desired servers under `Hosts and commands`.
2. Click `Create group` and enter a name. Currently checked hosts join the new group.
3. Review the members on the right and click `Save`.
4. Use `Manage groups` to add/remove members, rename groups, or delete groups.

Groups are stored under `groups` in `inventory.yaml`, as above. A group can reference
an SSH-config alias without adding a separate `hosts` row. A server can belong to
several groups. Deleting a group does not delete hosts or edit SSH config. Inventory
is shared across profiles; a member missing from the current profile is retained.

Selecting a group in the main window checks its members and clears other selections.
Search only hides rows: hidden checked hosts remain part of a job. `Select visible`
adds visible hosts to the selection. Use `Clear selection` before a fresh selection and
always review the final target list before starting.

## Running commands and scenarios

### Free-form command

1. Choose a group or check hosts manually.
2. Select the free-form command and keep `SSH user` for ordinary execution.
3. Enter, for example:

```sh
uname -a
id
df -h
```

4. Click `Review and run`. Check hosts, commands, and warnings.
5. Confirm the job. On the first connection, check the server fingerprint against
   an independent trusted source, then answer authentication prompts.
6. Follow per-host results. Available job logs are under `History`; output can be exported.

Try the operation on one server first. There is no automatic rollback. Changes to
SSH files after review can pause a job and require another configuration review.

### Scenario

Create `scenarios/inspect-directory.yaml` in your configuration directory, or use
`Settings > Scenarios > New scenario`:

```yaml
id: inspect-directory
name: Inspect directory
description: Show the current user and inspect a directory.
os: [linux]
shells: [sh, bash]
parameters:
  directory:
    required: true
    default: /var/log
steps:
  - name: Show system and user
    command: |
      uname -a
      id
  - name: Inspect directory
    command: ls -ld -- {{directory}}
    timeout: 30
```

Use the separate `Save scenario` button, choose the scenario in the main window,
and fill its parameters. `Save settings` does not replace scenario saving. Scenario
steps and sudo fields apply when a scenario is selected; free-form privilege controls
do not override them.

`{{directory}}` must be a separate, unquoted word. NodeBatch quotes the value for
the shell. `"{{directory}}"` and `prefix{{directory}}` are rejected. A parameterized
block cannot use heredocs, `$()`, `${}`, or backticks. Blocks without template
parameters can use ordinary multiline shell and Python heredocs.

Declare sensitive parameters with `secret: true` and bind them to steps through
`secrets: [parameter_name]`. Substituting secrets through `{{...}}` is rejected.
Steps read them from `NODEBATCH_SECRET_<name>_FD` descriptors. Limits: 32 bindings per
step and 4096 UTF-8 bytes per secret. See the complete [secret-input example](docs/execution.md#secrets).

### Sudo

For a free-form command, select sudo and enter the target user. Scenario steps use
`sudo: true` and `sudo_user: root` or another username. SSH still connects as the
SSH user; sudo selects the command's effective user.

Sudo actually starts `python3 -u -c ...`; Python then creates the selected shell.
A sudoers policy allowing only `sh` or `bash` is insufficient. Allowing this helper
permits arbitrary code execution as the target user. NodeBatch does not edit sudoers;
`requiretty` and arbitrary PAM dialogs are not generally supported. SFTP transfers
use the SSH user and do not use sudo.

### File transfers

Under `Files`, select direction, hosts, and local/remote paths. Host selection is
shared with the commands tab. One file is transferred per job.

- Upload: select a local file and a full remote destination, including filename.
- Download: select a local directory and remote file. Each host's file goes into
  a subdirectory named after its alias, separating identically named files.
- Replacing a file requires separate confirmation. Declining preserves the existing
  file and reports a failed operation for that host.

Directory transfers, recursive synchronization, and sudo-assisted SFTP are not implemented.

## Settings roadmap

Setup sequence for the current version:

| Stage | Location | Check |
|---|---|---|
| 1 | Connections | native/WSL, programs, SSH config, known_hosts |
| 2 | Connections / IdentityFile | Key paths, active profile, correct agent |
| 3 | Host sources and groups | Aliases, expected OS, shell, group membership |
| 4 | Execution | Sequential trial, then concurrency, queue stopping, timeouts |
| 5 | Safety | Rules and confirmations appropriate to your operations |
| 6 | Logs and storage | Retention, directory budget, job limit, disk reserve |
| 7 | Scenarios | One-host check, followed by group execution |

Proposed settings improvements, without committed dates:

- [ ] Per-key activation checkboxes and an effective IdentityFile/agent summary before execution.
- [ ] Host creation/edit forms alongside the YAML editor.
- [ ] Profile cloning/export with explicit removal of local paths and secrets.
- [ ] Settings version history, backups, and restoring a selected version through the GUI.

These items are not implemented yet. Large local scripts and further platform
acceptance tests are separate tasks, not available settings toggles.

## Details and limitations

### Results, cancellation, and background processes

A confirmed result belongs to the main shell. Preparation failure is distinct from
an unknown outcome after request transmission. `255` can be a confirmed command
exit code, so it does not always mean SSH failed.

Cancellation closes the connection but does not prove that the remote command or
its descendants stopped. Missing confirmation leaves the outcome unknown. There
are no automatic retries: inspect server state before repeating a modifying operation.

New descendant output is accepted for one second after observed shell exit. Previously
received output and bytes present in pipes at exit observation survive slow delivery.
Data and result delivery can take longer than one second and count toward the step
timeout. Possible truncation is marked `output_incomplete` without changing the
confirmed shell exit code. Long-lived background services need detached streams,
for example `nohup my-command </dev/null >my-command.log 2>&1 &`, or a service manager.

### Output, history, and safety

- A rendered command is limited to 32 KiB UTF-8. Executing a large local script file
  is not implemented. Split scripts into steps only when the blocks are independent.
- SSH stdin carries the protocol; the command receives `/dev/null`. Interactive
  applications, TTYs, and terminal-driven prompts are not supported.
- Output queues are bounded; slow clients create backpressure. GUI display may be
  shortened, while independently stored logs follow their own storage limits.
- Reaching a storage limit stops log recording with an explicit incomplete-output
  marker. Cleanup does not delete active jobs. It runs while idle; actual maintenance
  can temporarily prevent preparation of another job.
- One process owns a history directory. A second instance needs another `--data-dir`.
  Windows SQLite history cannot be on a UNC share.
- Safety rules inspect text and common mistakes; they do not prove arbitrary scripts
  safe. SSH config is trusted: ProxyCommand and Match exec can run local programs,
  including while connection settings are resolved.
- Exact known secret values are masked; transformations such as base64 cannot be
  recognized automatically. Code receiving a secret FD must protect it too.
- Operator scenario names, host names, and remote output are not translated.

### Language and applying changes

Choose Russian or English in settings and restart, or pass:

```sh
nodebatch --language ru
nodebatch --language en
```

Priority: `--language`, then `settings.yaml: language`. `auto` uses
`NODEBATCH_LANG=ru|en` if set, otherwise the system language. Older application data
directories are not imported automatically. Close the old version, copy the required
YAML files manually, and update secret descriptor references to `NODEBATCH_SECRET_<name>_FD`.

### Troubleshooting

| Symptom | Check |
|---|---|
| Empty host list | Active profile, config path, literal Host aliases, inventory syntax, refresh |
| Key not used | Private file, native/WSL path context, saved active profile, IdentityFile and agent |
| `Permission denied` | User, key permissions, installed public key, OpenSSH authentication methods |
| Missing askpass | Installed project or intact packaged application folder |
| Remote `python3` or Bash missing | Install the remote program or select an available shell |
| Sudo fails | Target user, permission to run the Python helper, sudoers and TTY requirements |
| Cannot save config | YAML validation and external edits; reload before editing again |
| Cannot start another job | Active job or maintenance, history budget, free disk space |
| Unknown result | Server state, connectivity, timeout; this is not proof of non-execution |

Check YAML locally without SSH connections:

```sh
nodebatch --validate-config
```

This does not test host reachability or keys. If the configuration directory has
not been initialized, the command first creates its default files.

## Building and development

In an active virtual environment:

```sh
python -m pip install -r requirements-dev-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
python tools/check_repo.py
python -m pytest -q
python tools/build.py
python tools/verify_bundle.py
```

Build with PyInstaller on each OS separately. Output is `dist/windows/NodeBatch` or
`dist/linux/NodeBatch`; distribute the complete folder. GitHub Actions checks and
uploads build artifacts; it does not automatically create a GitHub Release.
Operator YAML, history, keys, reports, and binaries stay outside Git. Configuration
examples here contain no real servers.

Further reading: [configuration format](docs/configuration.md),
[execution, secrets, and outcomes](docs/execution.md), [development](docs/development.md).
NodeBatch code uses [MIT](LICENSE). [Library notices](THIRD_PARTY.md) and license
texts retain their own terms and are included in application bundles.
