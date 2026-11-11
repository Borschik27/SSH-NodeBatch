"""Exercise real JobRunner/History/GUI using local synthetic processes only.

Run from the repo with PYTHONPATH=src. Output is JSON; no remote hosts are used.
"""
import argparse
import ctypes
import json
import os
from pathlib import Path
import platform
import shutil
import subprocess
import sys
import tempfile
import time

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
import yaml
from PySide6 import __version__ as qt_binding_version
from PySide6.QtCore import QTimer, qVersion
from PySide6.QtWidgets import QApplication
from nodebatch import transport
from nodebatch.config import bootstrap, ConfigStore
from nodebatch.engine import JobRunner
from nodebatch.gui import MainWindow
from nodebatch.models import Host, Job, Step


def rss():
    if os.name != "nt":
        return int(Path("/proc/self/statm").read_text().split()[1]) * os.sysconf("SC_PAGE_SIZE")
    from ctypes import wintypes
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wintypes.DWORD), ("faults", wintypes.DWORD)] + [
            (name, ctypes.c_size_t) for name in ("peak", "working", "quota_peak_paged", "quota_paged",
                "quota_peak_nonpaged", "quota_nonpaged", "pagefile", "peak_pagefile")]
    counters = Counters()
    counters.cb = ctypes.sizeof(counters)
    get = ctypes.windll.psapi.GetProcessMemoryInfo
    get.argtypes = [wintypes.HANDLE, ctypes.POINTER(Counters), wintypes.DWORD]
    if not get(wintypes.HANDLE(-1), ctypes.byref(counters), counters.cb):
        raise ctypes.WinError()
    return counters.working


def spin_until(app, predicate, seconds):
    deadline = time.monotonic() + seconds
    while not predicate():
        app.processEvents()
        if time.monotonic() > deadline:
            raise TimeoutError("Benchmark stage timed out")
        time.sleep(.001)


def run(app, parallelism, shape, duration, rate):
    with tempfile.TemporaryDirectory(prefix="nodebatch-benchmark-") as temporary:
        root = Path(temporary)
        bootstrap(root / "config")
        path = root / "config/settings.yaml"
        settings = yaml.safe_load(path.read_text())
        settings["execution"].update(mode="parallel", parallelism=parallelism, stop_on_error=False)
        settings["logging"].update(run_max_bytes=1073741824, history_max_bytes=2147483648)
        config = root / "empty-ssh-config"
        config.write_text("")
        settings["profiles"]["native"]["config"] = str(config)
        path.write_text(yaml.safe_dump(settings), encoding="utf-8")
        inventory = {"version": 1, "hosts": [{"alias": f"load-{i:03}", "os": "unix"} for i in range(100)]}
        (root / "config/inventory.yaml").write_text(yaml.safe_dump(inventory), encoding="utf-8")
        store = ConfigStore(root / "config")
        window = MainWindow(store, root / "data")
        window.show()
        spin_until(app, lambda: window.catalog_ready, 30)
        catalog_start = time.monotonic()
        window._select_visible()
        window.search.setText("load-0")
        app.processEvents()
        window.search.clear()
        catalog_ms = (time.monotonic() - catalog_start) * 1000
        producer = root / "load_worker.py"
        shutil.copyfile(Path(__file__).with_name("load_worker.py"), producer)
        def command(profile, execution, host, step=None, marker="", resolve=False, transfer=False):
            mode = "resolve" if resolve else "run" if step.protocol else "uname"
            return getattr(sys, "_base_executable", sys.executable), ["-I", str(producer), mode,
                       "--rate", str(rate // parallelism), "--shape", shape]
        original = transport.command
        transport.command = command
        baseline = rss()
        runner = None
        samples, memory = [], [baseline]
        received = 0
        hosts_started = set()
        timer = QTimer()
        timer.setInterval(10)
        previous = time.monotonic()
        def tick():
            nonlocal previous
            now = time.monotonic()
            samples.append(max(0, now - previous - .010) * 1000)
            previous = now
            memory.append(rss())
        def event(host, step, channel, text):
            nonlocal received
            if step == "load" and channel in ("stdout", "stderr"):
                hosts_started.add(host)
                received += len(text.encode("utf-8"))
        try:
            hosts = tuple(Host(f"load-{i:03}") for i in range(100))
            job = Job.create(hosts, [Step("load", "synthetic load")], store.bundle, ())
            runner = JobRunner(job, window.history, window.auth, helper=sys.executable)
            runner.sources = lambda: ()
            window.runner = runner
            runner.event.connect(window._event)
            runner.event.connect(event)
            runner.start()
            spin_until(app, lambda: len(hosts_started) == parallelism or runner.complete or runner.had_failure, 120)
            if runner.complete or runner.had_failure:
                raise RuntimeError("Load producer failed before measurement: " + repr(window.history.results(job.id)))
            received = 0
            start = time.monotonic()
            previous = start
            timer.timeout.connect(tick)
            timer.start()
            spin_until(app, lambda: time.monotonic() - start >= duration, duration + 10)
            elapsed = time.monotonic() - start
            timer.stop()
            cancel_start = time.monotonic()
            runner.cancel()
            queue_stopped_ms = (time.monotonic() - cancel_start) * 1000
            assert not runner.pending
            spin_until(app, lambda: runner.complete, 30)
            window._flush_output()
            saved = window.history.storage_bytes()
            ordered = sorted(samples)
            return dict(parallelism=parallelism, shape=shape, seconds=elapsed,
                        target_bytes_per_second=rate, received_bytes=received,
                        measured_bytes_per_second=received / elapsed,
                        event_latency_p95_ms=ordered[max(0, int(len(ordered) * .95) - 1)],
                        event_latency_max_ms=max(samples), cancel_queue_ms=queue_stopped_ms,
                        main_rss_baseline=baseline, main_rss_peak=max(memory),
                        main_rss_growth=max(memory) - baseline, saved_directory_bytes=saved,
                        gui_live_bytes=window.live_entries.bytes, gui_summary_bytes=window.summary_entries.bytes,
                        gui_live_evicted=window.live_entries.dropped,
                        persistence_stopped=runner.log_limit_reported, catalog_search_select_ms=catalog_ms)
        finally:
            timer.stop()
            if runner is not None and not runner.complete:
                runner.cancel()
                spin_until(app, lambda: runner.complete, 30)
            transport.command = original
            spin_until(app, lambda: not window.tasks, 30)
            window.close()
            store.poll.stop()
            store.debounce.stop()
            window.deleteLater()
            store.deleteLater()
            app.processEvents()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--seconds", type=float, default=60)
    parser.add_argument("--rate", type=int, default=5 * 1048576)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--parallelism", type=int, nargs="+", default=[1, 4, 100])
    args = parser.parse_args()
    app = QApplication([])
    report = dict(os=platform.platform(), cpu=platform.processor(), logical_cpus=os.cpu_count(),
                  python=sys.version, qt=qVersion(), pyside=qt_binding_version,
                  memory_scope="main GUI process RSS; synthetic producer processes excluded",
                  gui_blocks=2000, live_bytes_limit=4194304, summary_bytes_limit=4194304,
                  output_queue_bytes_limit=1048576, run_log_limit=1073741824,
                  visible_document_utf16_limit=262144, preview_chars_per_event=2048,
                  preview_lines_per_event=20,
                  history_budget=2147483648, cases=[])
    if os.name != "nt":
        report["ram"] = Path("/proc/meminfo").read_text().splitlines()[0]
        report["cpu"] = next((line.split(":", 1)[1].strip() for line in Path("/proc/cpuinfo").read_text().splitlines()
                              if line.startswith("model name")), platform.machine())
    report["disk_free_at_start"] = shutil.disk_usage(tempfile.gettempdir()).free
    report["disk_temporary_directory"] = tempfile.gettempdir()
    ssh = shutil.which("ssh")
    report["openssh"] = subprocess.run([ssh, "-V"], capture_output=True, text=True).stderr.strip() if ssh else "unavailable (synthetic load uses no SSH)"
    if os.name == "nt":
        class MemoryStatus(ctypes.Structure):
            _fields_ = [("length", ctypes.c_ulong), ("load", ctypes.c_ulong)] + [
                (name, ctypes.c_ulonglong) for name in ("total", "available", "total_page", "available_page",
                    "total_virtual", "available_virtual", "extended")]
        status = MemoryStatus()
        status.length = ctypes.sizeof(status)
        if ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(status)):
            report["ram_bytes"] = status.total
        import winreg
        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"HARDWARE\DESCRIPTION\System\CentralProcessor\0") as key:
            report["cpu"] = winreg.QueryValueEx(key, "ProcessorNameString")[0].strip()
    elif shutil.which("findmnt"):
        report["disk_mount"] = subprocess.run(["findmnt", "-T", tempfile.gettempdir(), "-n", "-o", "SOURCE,FSTYPE"],
                                               capture_output=True, text=True).stdout.strip()
    for i, parallelism in enumerate(args.parallelism):
        shape = {1: "long", 4: "short", 100: "utf8"}.get(parallelism, ("long", "short", "utf8")[i % 3])
        report["cases"].append(run(app, parallelism, shape, args.seconds, args.rate))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(report, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"Completed parallelism={parallelism}", flush=True)


if __name__ == "__main__":
    main()
