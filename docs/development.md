# Development

[Русский](development.ru.md) | **English**

---

Use Python 3.12+ and a virtual environment. From the repository root:

```sh
python -m pip install -r requirements-dev-lock.txt
python -m pip install --no-deps --no-build-isolation -e .
python tools/check_repo.py
python -m pytest -q
```

Tests use local subprocesses and synthetic hosts. Set `QT_QPA_PLATFORM=offscreen`
for headless GUI tests. `NODEBATCH_TEST_LOOPBACK=1` enables real system SSH/SFTP
against a disposable server on 127.0.0.1. The Windows/WSL integration also needs
`NODEBATCH_TEST_WSL_HELPER`, `NODEBATCH_TEST_WSL_DISTRO`, `NODEBATCH_TEST_WSL_REPO`
and `NODEBATCH_TEST_WSL_PYTHON`, pointing to a local helper and test environment.
It does not use operator remote hosts.

## Languages and source text

Keep Python identifiers, comments, docstrings and source messages in English.
Call `tr("English message", ...)` for application text and add the Russian value
to `src/nodebatch/locales/ru.json`. Preserve format placeholders and never translate
commands or operator-entered data. Qt buttons are translated separately.
Use ordinary `-`, not typographic dashes, in source and documents.

## What belongs in Git

Source, synthetic tests, empty defaults, public documentation and CI belong here.
Operator YAML, private keys, known_hosts, history, logs, environments, screenshots,
machine reports and compiled binaries do not. The root allowlist in `.gitignore`
keeps unexpected top-level files out; add new public root files explicitly.
`check_repo.py` also checks Git candidates, including already tracked files that an
ignore rule cannot hide. It reports paths, never detected secret contents.

Keep local work in `.local/`. It is excluded from Git, wheels, source archives and
application bundles. Historical review notes and machine reports, if present, stay
under `.local/reports/`. Runtime test fixtures should use temporary directories.

## Build

Build separately on Linux and Windows; there is no cross compilation:

```sh
python tools/build.py
python tools/verify_bundle.py dist/linux/NodeBatch
# Windows: python tools/verify_bundle.py dist/windows/NodeBatch
```

Distribute the whole NodeBatch folder, including its helper and runtime libraries.
`--dist-root` writes a verification build elsewhere without replacing dist.
`pyproject.toml` explicitly lists packaged resources. The build copies only those
resources and the public documents listed in `tools/release_files.py`; it never
copies a whole working documentation or configuration directory. Packaged profiles
and inventory must remain empty and generic. CI checks repository hygiene, tests,
builds and verifies both platforms. A local run is not proof that cloud CI ran.
Each build uses a fresh staging directory. A previous output folder is moved aside
as `.previous-*` rather than merged, so stale data cannot enter the new application
folder. CI uploads only the explicit platform/NodeBatch directory.

The project uses MIT; the build also includes THIRD_PARTY.md and licenses/ for
bundled libraries. Keep these files with distributed binaries. CI publishes build
artifacts for inspection; uploading them does not create a GitHub Release.

## First publication

Keep the repository root at the directory containing pyproject.toml and .github/.
Create an empty remote repository, then connect and push the prepared main branch:

```sh
git remote add origin REPOSITORY_URL
git push -u origin main
```

Use the actual remote URL in place of REPOSITORY_URL. Binary folders and local
reports stay outside Git; publish packaged applications separately as release assets.

## Local measurements

```sh
python tools/benchmark_output.py --output .local/reports/benchmark.json
python tools/preview.py .local/previews
```

The load tool offers 5 MiB/s for 60 seconds at concurrency 1, 4 and 100 with long,
short and UTF-8 output. It records actual throughput, event latency, cancellation,
GUI RSS and stored bytes. Generator processes and OS file cache are outside GUI RSS.
Offscreen results do not replace native-window checks, a full process-tree memory
measurement, real sudo policies or real BSD testing. Store results locally, not as
machine-specific claims in the source tree or release bundle.
