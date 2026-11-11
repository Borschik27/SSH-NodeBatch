[0.3.0] - 2026-11-11

Initial Release

First public release of NodeBatch, a cross-platform desktop application for executing commands, managing SSH hosts, and transferring files across multiple remote systems.

Added

SSH Connection Management

Integration with native OpenSSH and SSH configuration files.

Support for SSH keys, SSH agents, password authentication, and encrypted private keys.

Support for ProxyJump and custom SSH connection settings.

Windows Subsystem for Linux (WSL) connection backend.

Automatic discovery of available SSH tools and connection configurations.

Host and Group Management

Host inventory management using YAML configuration files.

Import and discovery of hosts from OpenSSH configuration.

Custom host groups, tags, and search capabilities.

Host selection and filtering for batch operations.

Remote Execution

Sequential and parallel command execution across multiple hosts.

Configurable concurrency limits and execution timeouts.

Multi-step YAML scenarios with parameter substitution.

Remote command execution through a Python helper.

Optional sudo execution with authentication prompts.

Per-host execution status, exit codes, and streamed output.

Cancellation and execution failure reporting.

File Transfers

SFTP-based single-file transfers.

Integration with configured SSH authentication and connection profiles.

Transfer progress and failure reporting.

Security and Reliability

Pre-execution review of selected hosts and commands.

Configurable command safety policies with allow, confirm, and deny rules.

SSH host-key verification through OpenSSH.

Secret parameter handling using dedicated file descriptors.

Masking of known secret values in application output.

Explicit reporting of unconfirmed remote execution outcomes.

Configurable resource limits and log retention.

User Interface

Desktop graphical interface built with Qt / PySide6.

English and Russian language support.

Connection profiles and application settings management.

Real-time execution progress and per-host results.

Execution history stored in SQLite.

Structured JSONL logging and output export.

Distribution

Standalone application packaging for Linux and Windows.

Automated testing and build verification through GitHub Actions.

Bundled documentation, default configuration templates, and third-party license information.

Compatibility

Desktop platforms: Linux and Windows.

Python 3.12 or newer for running from source.

Remote execution targets: Unix-like systems with SSH, a compatible shell, and Python 3.

Remote Windows PowerShell and CMD execution are not supported.
