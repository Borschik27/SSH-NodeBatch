"""English source messages with a separate Russian translation catalog."""
from functools import lru_cache
from importlib.resources import files
import json
import locale
import os

SUPPORTED_LANGUAGES = ("auto", "en", "ru")


def resolve_language(language):
    if language not in SUPPORTED_LANGUAGES:
        raise ValueError("Language must be auto, en or ru")
    if language == "auto":
        requested = os.environ.get("NODEBATCH_LANG", "auto")
        if requested in ("en", "ru"):
            return requested
        system_language = locale.getlocale()[0] or "en"
        return "ru" if system_language.lower().startswith("ru") else "en"
    return language


_language = resolve_language("auto")


def set_language(language):
    global _language
    _language = resolve_language(language)
    return _language


def get_language():
    return _language


@lru_cache(maxsize=1)
def russian_catalog():
    return json.loads(files("nodebatch").joinpath("locales", "ru.json").read_text(encoding="utf-8"))


def tr(message, **values):
    translated = russian_catalog().get(message, message) if _language == "ru" else message
    return translated.format(**values) if values else translated


def install_qt_translations(app):
    """Translate Qt buttons too; keep translator instances alive with QApplication."""
    from PySide6.QtCore import QLibraryInfo, QTranslator
    for translator in getattr(app, "_nodebatch_translators", ()):
        app.removeTranslator(translator)
        translator.deleteLater()
    translators = []
    if get_language() == "ru":
        native = QTranslator(app)
        if native.load("qtbase_ru", QLibraryInfo.path(QLibraryInfo.LibraryPath.TranslationsPath)):
            app.installTranslator(native)
            translators.append(native)
        else:
            native.deleteLater()
        class CatalogTranslator(QTranslator):
            def isEmpty(self):
                return False

            def translate(self, context, source_text, disambiguation=None, n=-1):
                return russian_catalog().get(source_text, "")
        fallback = CatalogTranslator(app)
        app.installTranslator(fallback)
        translators.append(fallback)
    app._nodebatch_translators = translators
