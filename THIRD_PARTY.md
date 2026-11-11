# Third-party software

The MIT license in LICENSE covers NodeBatch's own code. It does not replace the
licenses of Python, Qt, PySide6, Shiboken, PyYAML or the packaging bootloader.
Runtime versions are pinned in requirements-lock.txt; build tools are pinned in
requirements-dev-lock.txt. The operating system supplies OpenSSH separately.

| Component | License information | Upstream source |
|---|---|---|
| CPython runtime | Python license history and notices in licenses/Python.txt | https://github.com/python/cpython |
| Qt libraries | LGPL 3.0 for the Qt modules used here, with third-party components under their own terms | https://code.qt.io/cgit/qt/qtbase.git/ |
| PySide6 and Shiboken6 | LGPL 3.0 open-source option; installed metadata also offers GPL alternatives | https://code.qt.io/cgit/pyside/pyside-setup.git/ |
| PyYAML | MIT, see licenses/PyYAML.txt | https://github.com/yaml/pyyaml |
| PyInstaller bootloader | GPL with the distribution exception in licenses/PyInstaller.txt | https://github.com/pyinstaller/pyinstaller |

The LGPL text and the referenced GPL 3.0 text are included in licenses/. Qt libraries
remain separate shared libraries in the onedir bundle. Do not remove their runtime
files or prevent replacement with compatible library versions. NodeBatch itself
does not modify the bundled libraries.

Qt attribution and component details:
https://doc.qt.io/qt-6/licenses-used-in-qt.html
https://doc.qt.io/qtforpython-6/licenses.html

Keep LICENSE, this file, and licenses/ with redistributed application folders.
