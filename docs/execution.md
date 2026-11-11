# Execution, secrets and outcomes

[Русский](execution.ru.md) | **English**

---

The client invokes system OpenSSH. User commands run through a small Python 3
helper on the remote machine. Its source and a random session identifier are passed
in argv; the command and bound secret values arrive in a framed request over SSH stdin.
The request has a 1 MiB limit; the command itself has a separate 32 KiB UTF-8 limit
because the helper still invokes `sh -c` or `bash -c`.

## Secrets

Do not substitute `secret: true` parameters into command text. Bind them to a step:

```yaml
id: secret-example
name: Secret input example
os: [unix]
shells: [sh]
parameters:
  token:
    secret: true
steps:
  - name: Read token
    secrets: [token]
    command: |
      python3 <<'PY'
      import os
      fd = int(os.environ['NODEBATCH_SECRET_token_FD'])
      token = os.read(fd, 4096).decode('utf-8')
      os.close(fd)
      # Use token in this process; do not pass it to subprocess argv.
      print('Token received')
      PY
```

Each secret gets an anonymous pipe. Only its descriptor number is put in the
environment. A step accepts up to 32 secret bindings, each at most 4096 UTF-8 bytes.
Stored defaults, line breaks and NUL in secret parameters are rejected. Exact known
values are masked across read boundaries; transformed values such as base64 cannot
be recognized reliably. Python cannot guarantee memory erasure. Code receiving a
secret, privileged host software and processes with equivalent access remain trusted.

## Sudo and stdin

The actual command is:

```text
sudo -S -p <random prompt> -u <target user> -- python3 -u -c <helper source> <session>
```

The helper can execute any requested command, so permitting it means arbitrary
code execution as the target user. A policy allowing only `sh` or `bash` does not
allow this invocation. NodeBatch does not edit sudoers or install remote dependencies.
Restricted policies, requiretty and custom PAM conversations are not generally supported.

Before `ready`, stdin carries only sudo password replies. NOPASSWD needs no dialog;
retries are allowed during authorization. After ready, the client sends one request
and EOF. The program receives `/dev/null` as stdin; secret descriptors are separate.
Late password replies are tied to their original process and cannot reach the next one.
Authorization uses the connection/authentication timeout; the execution timer begins
at ready and includes request transmission and process preparation.

## Protocol v2

SSH stdout carries JSONL messages. Program stdout/stderr have their own pipes and
are encoded as `data` frames; program text is never parsed as a control message.
The session ID binds ready to the invocation; a separate random token binds all
subsequent messages to its request.

- `not_started` confirms preparation failed before creating the program process.
- `started` confirms the process was created; it must precede output and results.
- `result` confirms the shell exit code, including 255. Optional boolean
  `output_incomplete` marks streams closed after the post-exit drain limit.
- `error` after started means the helper failed without a confirmed outcome.

Malformed, partial-terminal, repeated or out-of-order messages cannot confirm success.
If the request was never sent, the client knows the program was not started. After
transmission, missing confirmation leaves an unknown result. SQLite stores the raw SSH
code, confirmed command code and nullable `command_started` separately. No automatic
retry is performed. Cancellation cannot prove that remote descendants stopped.

## Background processes

The result belongs to the main `sh -c` or `bash -c` process, not its descendants.
The helper polls shell completion independently of stdout/stderr and transport
writes. When it observes shell exit, it snapshots the bytes already buffered in
each pipe with Unix `FIONREAD`. These bytes and all previously read data must be
preserved, including when the wire receiver is stalled. The remote Python runtime
must support `fcntl`, `termios.FIONREAD` and polling Unix pipes.

There are two separate limits:

- Acceptance of additional descendant output ends one second after observing shell
  exit. At that deadline, a pipe with no snapshot bytes left is closed. Snapshot
  bytes still unread are drained later under backpressure, then that pipe is closed.
  A final one-byte nonblocking probe distinguishes EOF from an inherited writer;
  if it reads a byte, that byte is retained too. Only further descendant bytes may
  be discarded. An open writer at cutoff sets `output_incomplete: true`; the client
  warns and marks the stored output incomplete while retaining the shell exit code.
- Delivery of retained data and the following result has no one-second deadline.
  It can take longer than the acceptance window. The client execution timeout,
  cancellation and SSH connection lifetime still apply; delivery may therefore
  fail before confirmation. A slow receiver alone never triggers output truncation.

The event loop checks deadlines at intervals of at most 50 ms between nonblocking
operations, subject to OS scheduling. This is not a promise that the helper exits
or that the client receives a result within one second. Closing a pipe can cause
SIGPIPE in a writing descendant. User cancellation and application log limits remain
separate causes of incomplete output.

For a process intended to outlive a step, detach it and redirect all three streams,
for example `nohup my-command </dev/null >my-command.log 2>&1 &`. Prefer a service
manager for a managed background service. A successful shell exit does not confirm
that the background process started correctly, stayed alive or completed its work.

## Output and files

Local process creation uses a bounded background pool. Native stdout/stderr readers
have queues of 256 KiB per channel plus one in-flight 32 KiB chunk. The GUI consumes
up to 64 KiB per channel per turn. Full queues backpressure native pipes rather than
terminate a valid command. Actual pipe errors, cancellation and execution timeouts
are handled separately. Raw output is not spooled to a temporary file.

The remote wire uses nonblocking `os.write`, partial-write tracking and a 256 KiB
queue of unsent encoded frames. One partially sent frame can retain up to another
24 KiB of backing storage, plus normal object overhead. Reads pause when there is
insufficient queue space for a data frame; kernel pipes then backpressure writers.
Reserved space admits cutoff probes and terminal control frames. Already read data
is never removed to make room for a result, and the result follows all retained
output. A blocked wire does not block shell polling or cutoff checks. Kernel pipe
storage, including snapshot bytes still unread, is outside the encoded queue.

The log receives the complete stream until independent storage limits are reached.
GUI previews may omit entries or shorten text; those changes are marked. A stopped
log is recorded as incomplete in history. One process owns each canonical history
directory through an OS lock.

Idle history checks do not rewrite SQLite. Cleanup expires old runs and checks
physical bytes after truncating the WAL. Free pages are reclaimed under budget
pressure, or when they occupy at least 8 MiB and 25% of the database. Small deletions
leave pages available for reuse. New databases use incremental vacuum; a legacy
database is converted with one full VACUUM when compaction is first needed and
enough free space is available. Subsequent reclamation uses batches of at most
256 free pages with WAL checkpoints. Budget evictions accumulate reclaimable pages
until they can satisfy the limit, then check actual disk use before another eviction.

Closing the window requests maintenance cancellation and interrupts active SQLite
compaction without waiting on the GUI thread. The directory lock remains held until
the worker's database connection closes. Cancellation is checked between deletions
and compaction batches; a running filesystem operation cannot be forcibly cancelled.

Large local-script execution is not implemented. Its future contract uses a separate
stream/descriptor created after sudo, without a shared temporary script or relaxed
directory permissions. Single-file SFTP uses independent staging and overwrite checks.
