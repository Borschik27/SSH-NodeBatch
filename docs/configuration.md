# Configuration

[Русский](configuration.ru.md) | **English**

---

The application creates operator-owned YAML files on first launch. Packaged defaults
are templates and never contain real hosts, credentials or private paths.

`settings.yaml` contains connection profiles, execution limits and logging settings.
Its schema version remains `1`. `language` accepts `auto`, `en` or `ru`; missing
values mean `auto`. Language changes require restarting. Other valid configuration
changes apply to later runs; malformed files leave the last valid configuration active.

```yaml
version: 1
language: en
```

Do not replace the complete settings file with this fragment. Change the field in
the generated file. Profiles select the native or WSL backend and optional SSH config,
keys and known_hosts paths. For WSL, choose an installed distribution explicitly;
remote paths use Unix syntax.

`inventory.yaml` adds metadata or hosts not listed in SSH config. These are example
aliases and documentation-only addresses:

```yaml
version: 1
hosts:
  - alias: example-node
    address: 192.0.2.10
    user: operator
    os: linux
    shell: sh
    tags: [example]
groups:
  example: [example-node]
```

Put scenarios in the operator configuration directory under `scenarios/`:

```yaml
id: system-check
name: System check
os: [unix]
shells: [sh, bash]
parameters: {}
steps:
  - name: Show system
    command: |
      uname -a
      id
```

Each step starts a new connection. `sudo: true`, `sudo_user` and `timeout` are optional
step fields. Plain `{{name}}` parameters must be separate, unquoted shell words.
Parameterized blocks cannot use heredoc, command substitution, `${}` or backticks.
Blocks without template parameters can contain normal multiline shell/Python heredocs.
The rendered command may not exceed 32768 UTF-8 bytes.

`safety.yaml` defines allow, confirm and deny rules. Deny takes precedence. These
rules catch common mistakes; they do not establish that arbitrary code is safe.
SSH config is trusted, including local commands that OpenSSH may execute from it.

Storage fields are `logging.run_max_bytes`, `history_max_bytes` and `min_free_bytes`.
The old `max_bytes` remains the fallback for missing run/history limits. The budget
includes SQLite, WAL/SHM and logs. Status transactions and SQLite maintenance may
temporarily exceed it. Active logs are never deleted. Idle checks do not rewrite
the database. Cleanup expires old runs and reclaims free pages incrementally under
storage pressure or substantial waste. Budget evictions account for reusable pages
and check actual size after reclamation; WAL growth is not grounds for extra
evictions. Legacy databases migrate once when compaction is needed. Actual cleanup
runs in the background and blocks new job preparation until it finishes. See
[execution details](execution.md#output-and-files) for thresholds and cancellation.
