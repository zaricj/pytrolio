"""Trade Republic Helper - a PySide6 desktop front-end for the `pytr` library.

* The window layout comes from assets/ui/main_window.ui (Qt Designer) and all
  styling from assets/theme/theme.qss. This file contains the behaviour only.
* Sign-in runs in-process through the pytr API (``TradeRepublicApi``) so the PIN
  and 2FA code never have to be typed into a hidden console prompt.
* All other tasks run as a ``pytr`` subprocess (QProcess). Their options are
  generated from pytr's own argparse definition (with friendlier labels on top),
  so the GUI stays in sync with the installed pytr version.
* Tabular results are read from the CSV files pytr writes and shown in a table.
"""

from __future__ import annotations

import argparse
import codecs
import csv
import json
import logging
import os
import re
import shlex
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as pkg_version
from pathlib import Path
from string import Template
from types import SimpleNamespace

from PySide6.QtCore import (
    QDateTime,
    QElapsedTimer,
    QLocale,
    QObject,
    QProcess,
    QProcessEnvironment,
    QRunnable,
    QSettings,
    QSortFilterProxyModel,
    Qt,
    QThreadPool,
    QTimer,
    QUrl,
    Signal,
)
from PySide6.QtCore import (
    __version__ as QT_BINDING_VERSION,
)
from PySide6.QtGui import (
    QCloseEvent,
    QColor,
    QDesktopServices,
    QGuiApplication,
    QIcon,
    QKeySequence,
    QPalette,
    QShortcut,
    QStandardItem,
    QStandardItemModel,
    QTextCharFormat,
    QTextCursor,
)
from PySide6.QtUiTools import QUiLoader
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QButtonGroup,
    QCheckBox,
    QComboBox,
    QDialog,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSpinBox,
    QTableWidget,
    QTableWidgetItem,
    QVBoxLayout,
    QWidget,
)

# ============================================================================
# Constants
# ============================================================================

APP_NAME = "Pytrolio"
APP_VERSION = "0.4.0"
APP_AUTHOR = "Jovan"
SETTINGS_ORG, SETTINGS_APP = "Jovan", "Pytrolio"

# Resolve resources relative to the app itself (not the current working dir).
# PyInstaller builds unpack data files to sys._MEIPASS.
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).resolve().parent
else:
    APP_DIR = Path(__file__).resolve().parent
RESOURCE_DIR = Path(getattr(sys, "_MEIPASS", APP_DIR)) / "assets"
ICON_DIR = RESOURCE_DIR / "icon"
# SVG stays sharp at every size (window, taskbar, sidebar); PNG as fallback.
ICON_PATH = next((p for p in (ICON_DIR / "app_icon.svg", ICON_DIR / "app_icon.png", ICON_DIR / "icon.png")
                  if p.exists()), ICON_DIR / "app_icon.png")
THEME_FILE = RESOURCE_DIR / "theme" / "theme.qss"
UI_FILE = RESOURCE_DIR / "ui" / "main_window.ui"

DEFAULT_WORKDIR = Path.home() / "Documents" / "TradeRepublic"

# Options handled by the Account page instead of each task form.
LOGIN_DESTS = {"phone_no", "pin", "waf_token", "store_credentials", "v2", "help"}
# pytr commands that are not shown as tasks.
HIDDEN_COMMANDS = {"help", "login", "completion"}

ANSI_RE = re.compile(r"\x1b\[[0-9;?]*[A-Za-z]|\x1b\][^\x07]*\x07")
LINE_SPLIT_RE = re.compile(r"(\r\n|\n|\r)")
ERROR_RE = re.compile(r"\b(error|critical|fatal|traceback|exception|failed)\b", re.IGNORECASE)
WARN_RE = re.compile(r"\bwarn(ing)?\b", re.IGNORECASE)
PROMPT_RE = re.compile(r"(?i)(code|enter|pin|confirm|\(y/n\))[^\n]*[:?>]\s*$")
PHONE_RE = re.compile(r"^\+\d{7,15}$")
PIN_RE = re.compile(r"^\d{4,6}$")
ISIN_RE = re.compile(r"^[A-Z]{2}[A-Z0-9]{9}[0-9]$")

# pytr is optional at import time so the GUI can tell the user what is missing.
try:
    import pytr.main as pytr_main
    from pytr.api import BASE_DIR as PYTR_DIR
    from pytr.api import CREDENTIALS_FILE, TradeRepublicApi
    PYTR_IMPORT_ERROR: str | None = None
except Exception as _exc:  # noqa: BLE001 - depends on environment
    pytr_main = None
    TradeRepublicApi = None
    PYTR_DIR = Path.home() / ".pytr"
    CREDENTIALS_FILE = PYTR_DIR / "credentials"
    PYTR_IMPORT_ERROR = f"{type(_exc).__name__}: {_exc}"


def package_version(name: str) -> str:
    try:
        return pkg_version(name)
    except PackageNotFoundError:
        return "not installed"


# ----------------------------------------------------------------------------
# Task presentation: friendly names, descriptions and which options are "basic".
# Anything not listed still works – it simply ends up under "Advanced options".
# ----------------------------------------------------------------------------

TASKS = {
    "portfolio": dict(
        title="Portfolio",
        desc="Your current positions with quantity, buy-in, current value and the cash balance.",
        basic=["include_watchlist", "sort_by_column", "sort_ascending", "ignore"]),
    "rates": dict(
        title="Live prices",
        desc="Current price and ask for one or more instruments.",
        basic=["input", "sort_by_column"], required=["input"]),
    "details": dict(
        title="Instrument details",
        desc="Name, exchanges, company facts and recent news for one instrument.",
        basic=["isin"]),
    "get_price_alarms": dict(
        title="Price alarms",
        desc="All your active price alarms. Optionally filtered by ISIN.",
        basic=["input"]),
    "set_price_alarms": dict(
        title="Set price alarms",
        desc="Create or replace price alarms. Enter one instrument per row with its target prices.",
        basic=["remove_current_alarms"], skip=["input"]),  # "input" is replaced by the alarm table
    "get_savings_plans": dict(
        title="Savings plans",
        desc="Your savings plans with amount, interval and execution dates.",
        basic=[]),
    "export_transactions": dict(
        title="Export transactions",
        desc="Writes all deposits, withdrawals, buys, sells and dividends to a CSV file "
             "(e.g. for Portfolio Performance) and shows them in the table.",
        basic=["outputdir", "last_days", "sort", "date_with_time"]),
    "dl_docs": dict(
        title="Download documents",
        desc="Downloads every PDF from your timeline into a folder, sorted into sub-folders, "
             "and exports your transactions.",
        basic=["output", "last_days", "flat", "export_transactions"], required=["output"]),
}

# (command, dest) or dest -> (label, help, placeholder). None keeps pytr's text.
LABELS = {
    ("portfolio", "output"): ("Also save as CSV", "Optional. The result is always shown in the table.", None),
    ("rates", "output"): ("Also save as CSV", "Optional. The result is always shown in the table.", None),
    ("rates", "input"): ("ISINs", "One or more ISINs, separated by spaces or commas.",
                         "DE0005140008 US0378331005"),
    ("rates", "inputfile"): ("ISINs from file", "Text/CSV file with ISINs. Used when the ISINs field is empty.", None),
    ("details", "isin"): ("ISIN", "The 12-character ISIN of the instrument.", "US0378331005"),
    ("get_price_alarms", "input"): ("Only these ISINs", "Optional. Leave empty to list all alarms.",
                                    "DE0005140008 US0378331005"),
    ("get_price_alarms", "outputfile"): ("Also save as CSV", "Optional. The result is always shown in the table.", None),
    ("get_savings_plans", "outputfile"): ("Also save as CSV", "Optional. The result is always shown in the table.", None),
    ("set_price_alarms", "inputfile"): ("Read alarms from CSV", "Optional. A ';'-separated file "
                                        "(ISIN;alarm1;alarm2…). When set, the table above is ignored.", None),
    ("dl_docs", "output"): ("Target folder", "Documents are saved here (relative paths are inside the working folder).",
                            "documents"),
    ("export_transactions", "outputdir"): ("Target folder", "account_transactions.csv is written here.", "."),
    ("export_transactions", "outputfile"): ("File name", "Optional. Overrides the target folder.", None),
    "include_watchlist": ("Include watchlist", "Also list instruments from your watchlist.", None),
    "ignore": ("Hide ISINs", "Comma separated ISINs to leave out.", None),
    "sort_by_column": ("Sort by", None, None),
    "sort_ascending": ("Ascending order", None, None),
    "lang": ("Language", "Language for exported texts (auto = system language).", None),
    "decimal_localization": ("Localized numbers", "Use your locale's number format, e.g. 1.234,56.", None),
    "date_with_time": ("Include time in dates", None, None),
    "sort": ("Sort chronologically", None, None),
    "last_days": ("Only the last N days", "0 = everything. -1 = no update, only re-export.", None),
    "days_until": ("Up to N days ago", "0 = until today.", None),
    "workers": ("Parallel downloads", None, None),
    "format": ("File name pattern", "Variables: {iso_date} {time} {title} {subtitle} {doc_num} {id}", None),
    "universal": ("Platform-independent file names", None, None),
    "flat": ("Put all files in one folder", "Keep the original file names instead of sorting into sub-folders.", None),
    "export_transactions": ("Export transactions", "Also write account_transactions.csv next to the documents.", None),
    "export_format": ("Export format", None, None),
    "store_event_database": ("Keep event database", "Maintain all_events.json for faster incremental updates.", None),
    "scan_for_duplicates": ("Scan for duplicates", None, None),
    "dump_raw_data": ("Dump raw data", "Write raw JSON data for debugging.", None),
    "load_event_database": ("Load event database", "Debug: read events from an all_events.json instead of fetching.", None),
    "dry_run": ("Dry run", "Create the folder structure with empty placeholder files only.", None),
    "remove_current_alarms": ("Remove other alarms",
                              "Delete existing alarms of the listed instruments that are not in the table.", None),
}

# Commands whose result pytr can write as a ';' CSV file -> option dest for the file.
CSV_OUTPUT_DEST = {"portfolio": "output", "rates": "output",
                   "get_price_alarms": "outputfile", "get_savings_plans": "outputfile"}
# Commands whose console output is "key: value" lines.
KV_COMMANDS = {"details", "set_price_alarms"}
HEADER_NAMES = {"instrumentId": "ISIN", "netValue": "Net value", "avgCost": "Avg. cost"}


# ============================================================================
# Theme loading (all colours, fonts and styles live in assets/theme/theme.qss)
# ============================================================================

_BLOCK_RE = re.compile(r"@(palette\s+(\w+)|qpalette)\s*(.*?)@end", re.DOTALL)
_ENTRY_RE = re.compile(r"^\s*([\w.]+)\s*:\s*(.+?)\s*;?\s*$", re.MULTILINE)


@dataclass
class Theme:
    palettes: dict[str, dict[str, str]]
    qpalette: dict[str, str]
    stylesheet: str


def load_theme_file(path: Path = THEME_FILE) -> Theme:
    """Parse the @palette / @qpalette blocks and the stylesheet template."""
    text = path.read_text(encoding="utf-8")
    palettes: dict[str, dict[str, str]] = {}
    qpalette: dict[str, str] = {}
    for match in _BLOCK_RE.finditer(text):
        entries = dict(_ENTRY_RE.findall(match.group(3)))
        if match.group(2):
            palettes[match.group(2)] = entries
        else:
            qpalette = entries
    return Theme(palettes, qpalette, text)


def resolve_scheme(mode: str, available) -> str:
    if mode in available:
        return mode
    dark = QGuiApplication.styleHints().colorScheme() == Qt.ColorScheme.Dark
    return "dark" if dark and "dark" in available else ("light" if "light" in available else next(iter(available)))


def apply_theme(app: QApplication, mode: str) -> dict[str, str]:
    """Load theme.qss, apply its palette + stylesheet and return the active variables."""
    try:
        theme = load_theme_file()
    except OSError as exc:
        print(f"Theme file could not be loaded: {exc}", file=sys.stderr)
        app.setStyleSheet("")
        return {}
    if not theme.palettes:
        app.setStyleSheet(theme.stylesheet)
        return {}
    scheme = resolve_scheme(mode, theme.palettes)
    variables = {**theme.palettes[scheme], "scheme": scheme, "icons": ICON_DIR.as_posix()}

    try:  # tell Qt / Windows 11 about the scheme (dark title bar), Qt >= 6.8
        app.styleHints().setColorScheme(
            Qt.ColorScheme.Unknown if mode == "system"
            else (Qt.ColorScheme.Dark if scheme == "dark" else Qt.ColorScheme.Light))
    except AttributeError:
        pass

    palette = QPalette()
    for role_name, var in theme.qpalette.items():
        group_name, _, role_name = role_name.rpartition(".")
        role = getattr(QPalette.ColorRole, role_name, None)
        group = getattr(QPalette.ColorGroup, group_name, None) if group_name else None
        color = QColor(variables.get(var, var))
        if role is None or not color.isValid():
            continue
        if group is None:
            palette.setColor(role, color)
        else:
            palette.setColor(group, role, color)
    app.setPalette(palette)
    app.setStyleSheet(Template(theme.stylesheet).safe_substitute(variables))
    return variables


# ============================================================================
# UI loading + small widget helpers
# ============================================================================

class UiLoader(QUiLoader):
    """Load a .ui file *into* an existing widget and collect named children.

    Every widget with an objectName becomes an attribute of ``names``.
    """

    def __init__(self, base: QWidget, names: SimpleNamespace):
        super().__init__(base)
        self.base = base
        self.names = names

    def createWidget(self, class_name, parent=None, name=""):  # noqa: N802 - Qt override
        widget = self.base if parent is None else super().createWidget(class_name, parent, name)
        if name:
            setattr(self.names, name, widget)
        return widget


def load_ui(base: QWidget, path: Path = UI_FILE) -> SimpleNamespace:
    names = SimpleNamespace()
    loader = UiLoader(base, names)
    if loader.load(str(path)) is None:
        raise RuntimeError(f"Could not load {path}: {loader.errorString()}")
    # Small things Designer can't express for many widgets at once.
    for btn in base.findChildren(QPushButton):
        btn.setCursor(Qt.PointingHandCursor)
    for scroll in base.findChildren(QScrollArea):
        scroll.viewport().setAutoFillBackground(False)
    return names


def set_prop(widget: QWidget, name: str, value) -> None:
    """Set a dynamic property and re-polish so the stylesheet picks it up."""
    if widget.property(name) == value:
        return
    widget.setProperty(name, value)
    widget.style().unpolish(widget)
    widget.style().polish(widget)


def set_pill(pill: QLabel, text: str, state: str) -> None:
    """Status pills are QLabels with pill=true; the theme colours them by state."""
    pill.setText(f"● {text}")
    set_prop(pill, "state", state)


def transparent(widget: QWidget) -> QWidget:
    widget.setProperty("transparent", True)
    return widget


def label(text: str = "", role: str | None = None, wrap: bool = False) -> QLabel:
    lbl = QLabel(text)
    if role:
        lbl.setProperty("role", role)
    lbl.setWordWrap(wrap)
    return lbl


def button(text: str, variant: str | None = None, tooltip: str = "") -> QPushButton:
    btn = QPushButton(text)
    if variant:
        btn.setProperty("variant", variant)
    if tooltip:
        btn.setToolTip(tooltip)
    btn.setCursor(Qt.PointingHandCursor)
    return btn


def divider() -> QFrame:
    line = QFrame()
    line.setProperty("divider", True)
    return line


def hbox(*widgets, spacing=8, stretch_at=None) -> QHBoxLayout:
    lay = QHBoxLayout()
    lay.setSpacing(spacing)
    for i, w in enumerate(widgets):
        if i == stretch_at:
            lay.addStretch(1)
        if isinstance(w, QWidget):
            lay.addWidget(w)
        else:
            lay.addLayout(w)
    if stretch_at is not None and stretch_at >= len(widgets):
        lay.addStretch(1)
    return lay


def transparent_scroll(inner: QWidget) -> QScrollArea:
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    scroll.setWidget(transparent(inner))
    scroll.viewport().setAutoFillBackground(False)
    return scroll


class SegmentGroup(QObject):
    """Turns checkable buttons from the .ui into a segmented control."""

    changed = Signal(int)

    def __init__(self, buttons: list[QPushButton]):
        super().__init__(buttons[0])
        self.group = QButtonGroup(self)
        self.group.setExclusive(True)
        for i, btn in enumerate(buttons):
            self.group.addButton(btn, i)
        self.group.idClicked.connect(self.changed.emit)

    def select(self, index: int) -> None:
        self.group.button(index).setChecked(True)
        self.changed.emit(index)


class Collapsible(QWidget):
    """A header button that shows/hides its content."""

    def __init__(self, title: str):
        super().__init__()
        transparent(self)
        self._title = title
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(10)
        self.toggle = button("", "link")
        self.toggle.setCheckable(True)
        self.toggle.toggled.connect(self._toggled)
        self.content = transparent(QWidget())
        self.body = QVBoxLayout(self.content)
        self.body.setContentsMargins(0, 0, 0, 0)
        self.body.setSpacing(14)
        lay.addWidget(self.toggle, 0, Qt.AlignLeft)
        lay.addWidget(self.content)
        self._toggled(False)

    def _toggled(self, open_: bool) -> None:
        self.content.setVisible(open_)
        self.toggle.setText(("▾  " if open_ else "▸  ") + self._title)


# ============================================================================
# Background tasks + logging bridge
# ============================================================================

class TaskSignals(QObject):
    done = Signal(object)
    failed = Signal(str)


class Task(QRunnable):
    """Run a blocking callable on the global thread pool."""

    def __init__(self, fn, *args):
        super().__init__()
        self.setAutoDelete(False)  # we keep a reference until the signals fired
        self.fn, self.args = fn, args
        self.signals = TaskSignals()

    def run(self) -> None:
        try:
            result = self.fn(*self.args)
        except BaseException as exc:  # noqa: BLE001 - pytr may call sys.exit()
            signal, payload = self.signals.failed, describe_error(exc)
        else:
            signal, payload = self.signals.done, result
        try:
            signal.emit(payload)
        except RuntimeError:  # window closed while the task was running
            pass


class TaskRunner(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self._tasks: set[Task] = set()

    def start(self, fn, *args, on_done=None, on_failed=None) -> None:
        task = Task(fn, *args)
        self._tasks.add(task)

        def finish(handler, payload):
            self._tasks.discard(task)
            if handler:
                handler(payload)

        task.signals.done.connect(lambda r: finish(on_done, r))
        task.signals.failed.connect(lambda e: finish(on_failed, e))
        QThreadPool.globalInstance().start(task)


def describe_error(exc: BaseException) -> str:
    try:
        import requests
        if isinstance(exc, requests.HTTPError) and exc.response is not None:
            resp = exc.response
            detail = ""
            try:
                errors = resp.json().get("errors") or []
                if errors:
                    detail = errors[0].get("errorMessage") or errors[0].get("errorCode") or ""
            except ValueError:
                pass
            if resp.status_code == 429:
                return "Too many attempts. Please wait a moment and try again."
            return f"Trade Republic returned HTTP {resp.status_code}" + (f": {detail}" if detail else ".")
        if isinstance(exc, requests.ConnectionError):
            return "Could not reach Trade Republic. Check your internet connection."
        if isinstance(exc, requests.Timeout):
            return "The request to Trade Republic timed out."
    except ImportError:
        pass
    if isinstance(exc, SystemExit):
        return "pytr aborted the login (see the log for details)."
    if isinstance(exc, TimeoutError):
        return str(exc) or "The login was not confirmed in time."
    return str(exc) or type(exc).__name__


class LogBridge(QObject):
    message = Signal(str, int)


class QtLogHandler(logging.Handler):
    """Forward pytr log records (emitted from worker threads) to the GUI thread."""

    def __init__(self):
        super().__init__(logging.INFO)
        self.bridge = LogBridge()
        self.setFormatter(logging.Formatter("%(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.bridge.message.emit(self.format(record), record.levelno)
        except RuntimeError:  # bridge already deleted on shutdown
            pass


# ============================================================================
# pytr command introspection
# ============================================================================

@dataclass
class OptionSpec:
    command: str
    dest: str
    label: str
    kind: str                       # flag | bool | choice | int | text | path
    flags: list[str]
    default: object
    help: str
    placeholder: str = ""
    nargs: object = None
    choices: list[str] = field(default_factory=list)
    required: bool = False
    path_mode: str = ""             # dir | open | save

    @property
    def positional(self) -> bool:
        return not self.flags

    @property
    def flag(self) -> str:
        return next((f for f in self.flags if f.startswith("--") and not f.startswith("--no-")),
                    self.flags[0] if self.flags else "")

    @property
    def negative_flag(self) -> str:
        return next((f for f in self.flags if f.startswith("--no-")), "")


@dataclass
class CommandSpec:
    name: str
    title: str
    summary: str
    options: list[OptionSpec]
    basic: list[str]
    help_text: str


def _option_from_action(command: str, act: argparse.Action) -> OptionSpec | None:
    if isinstance(act, (argparse._HelpAction, argparse._VersionAction)):
        return None
    if act.dest in LOGIN_DESTS or act.help == argparse.SUPPRESS:
        return None

    help_text = act.help or ""
    try:
        help_text = help_text % {**vars(act), "default": act.default}
    except (KeyError, TypeError, ValueError):
        pass

    if isinstance(act, argparse.BooleanOptionalAction):
        kind = "bool"
    elif isinstance(act, argparse._StoreTrueAction):
        kind = "flag"
    elif isinstance(act, (argparse._StoreConstAction, argparse._AppendAction, argparse._CountAction)):
        return None
    elif act.choices:
        kind = "choice"
    elif act.type is int:
        kind = "int"
    elif act.type is Path or isinstance(act.type, argparse.FileType) or act.metavar == "PATH":
        kind = "path"
    else:
        kind = "text"

    path_mode = ""
    if kind == "path":
        dest, h = act.dest.lower(), help_text.lower()
        if "dir" in dest or "directory" in h:
            path_mode = "dir"
        elif "r" in getattr(act.type, "_mode", "") or "input" in dest or "load" in dest:
            path_mode = "open"
        else:
            path_mode = "save"

    name = act.dest.replace("_", " ").strip()
    lbl, placeholder = name[:1].upper() + name[1:], ""
    override = LABELS.get((command, act.dest)) or LABELS.get(act.dest)
    if override:
        lbl = override[0]
        help_text = override[1] if override[1] is not None else help_text
        placeholder = override[2] or ""

    positional = not act.option_strings
    required = bool(act.required) if not positional else act.nargs in (None, "+")
    required = required or act.dest in TASKS.get(command, {}).get("required", [])
    return OptionSpec(
        command=command, dest=act.dest, label=lbl, kind=kind, flags=list(act.option_strings),
        default=act.default, help=help_text, placeholder=placeholder, nargs=act.nargs,
        choices=[str(c) for c in (act.choices or [])], required=required, path_mode=path_mode,
    )


def load_command_specs() -> dict[str, CommandSpec]:
    if pytr_main is None:
        return {}
    os.environ.setdefault("COLUMNS", "110")  # width for argparse's help formatter
    parser = pytr_main.get_main_parser()
    parser.prog = "pytr"
    sub_action = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    summaries = {a.dest: a.help for a in sub_action._choices_actions}

    specs: dict[str, CommandSpec] = {}
    names = [n for n in TASKS if n in sub_action.choices]
    names += [n for n in sub_action.choices if n not in TASKS and n not in HIDDEN_COMMANDS]
    for name in names:
        sub = sub_action.choices[name]
        sub.prog = f"pytr {name}"
        options = [o for o in (_option_from_action(name, a) for a in sub._actions) if o]
        meta = TASKS.get(name, {})
        basic = meta["basic"] if "basic" in meta else [o.dest for o in options if o.required or o.positional]
        try:
            help_text = sub.format_help()
        except Exception as exc:  # noqa: BLE001
            help_text = f"Could not render help: {exc}"
        specs[name] = CommandSpec(
            name=name, title=meta.get("title", name.replace("_", " ").title()),
            summary=meta.get("desc") or summaries.get(name) or "", options=options,
            basic=basic, help_text=help_text)
    return specs


def pytr_invocation() -> tuple[str, list[str]]:
    """Program + argument prefix that runs pytr with the *same* Python/environment."""
    if getattr(sys, "frozen", False):
        return sys.executable, ["--pytr-cli"]
    exe = Path(sys.executable)
    if exe.name.lower() == "pythonw.exe" and exe.with_name("python.exe").exists():
        exe = exe.with_name("python.exe")
    return str(exe), ["-m", "pytr"]


def join_command(args: list[str]) -> str:
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def split_values(text: str) -> list[str]:
    return [t for t in re.split(r"[\s,;]+", text.strip()) if t]


# ============================================================================
# Option fields (widgets generated from OptionSpec)
# ============================================================================

class Field:
    def __init__(self, spec: OptionSpec, on_change, browse_root):
        self.spec = spec
        self.on_change = on_change
        self.browse_root = browse_root
        self.widget = self.build()

    is_check = False

    def build(self) -> QWidget: raise NotImplementedError
    def args(self) -> list[str]: return []
    def state(self): return None
    def set_state(self, value) -> None: pass
    def validate(self) -> str: return ""   # error text, "" when valid


class CheckField(Field):
    is_check = True

    def build(self):
        self.box = QCheckBox(self.spec.label)
        self.box.setCursor(Qt.PointingHandCursor)
        self.box.setChecked(self.default)
        self.box.toggled.connect(lambda _: self.on_change())
        return self.box

    @property
    def default(self) -> bool:
        return bool(self.spec.default) if self.spec.kind == "bool" else False

    def args(self):
        checked = self.box.isChecked()
        if checked == self.default:
            return []
        if self.spec.kind == "flag":
            return [self.spec.flag]
        return [self.spec.flag if checked else self.spec.negative_flag]

    def state(self): return self.box.isChecked()
    def set_state(self, value): self.box.setChecked(str(value).lower() in ("true", "1"))


class ChoiceField(Field):
    def build(self):
        self.combo = QComboBox()
        self.combo.addItems(self.spec.choices)
        self.combo.setCurrentText(str(self.spec.default))
        self.combo.currentTextChanged.connect(lambda _: self.on_change())
        return self.combo

    def args(self):
        value = self.combo.currentText()
        if self.spec.positional:
            return [value] if value else []
        return [] if value == str(self.spec.default) else [self.spec.flag, value]

    def state(self): return self.combo.currentText()
    def set_state(self, value):
        if str(value) in self.spec.choices:
            self.combo.setCurrentText(str(value))


class IntField(Field):
    def build(self):
        self.spin = QSpinBox()
        self.spin.setRange(-1, 1_000_000)
        self.spin.setValue(int(self.spec.default or 0))
        self.spin.setMinimumWidth(110)
        self.spin.valueChanged.connect(lambda _: self.on_change())
        w = transparent(QWidget())
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.addWidget(self.spin)
        lay.addStretch(1)
        return w

    def args(self):
        value = self.spin.value()
        return [] if value == int(self.spec.default or 0) else [self.spec.flag, str(value)]

    def state(self): return self.spin.value()
    def set_state(self, value):
        try:
            self.spin.setValue(int(value))
        except (TypeError, ValueError):
            pass


class TextField(Field):
    def build(self):
        self.edit = QLineEdit()
        default = self.spec.default
        if self.spec.placeholder:
            self.edit.setPlaceholderText(self.spec.placeholder)
        elif isinstance(default, (str, int, float, Path)) and str(default) not in ("", "-"):
            self.edit.setPlaceholderText(str(default))
        self.edit.textChanged.connect(self._changed)
        if self.spec.kind != "path":
            return self.edit
        w = transparent(QWidget())
        lay = QHBoxLayout(w)
        lay.setContentsMargins(0, 0, 0, 0)
        lay.setSpacing(6)
        lay.addWidget(self.edit, 1)
        browse = button("Browse…")
        browse.clicked.connect(self._browse)
        lay.addWidget(browse)
        return w

    def _changed(self, _text):
        set_prop(self.edit, "invalid", False)
        self.on_change()

    def _browse(self):
        start = self.edit.text().strip() or self.browse_root()
        if not os.path.isabs(start):
            start = os.path.join(self.browse_root(), start)
        title = f"Choose: {self.spec.label}"
        if self.spec.path_mode == "dir":
            path = QFileDialog.getExistingDirectory(self.widget, title, start)
        elif self.spec.path_mode == "open":
            path, _ = QFileDialog.getOpenFileName(self.widget, title, start)
        else:
            path, _ = QFileDialog.getSaveFileName(self.widget, title, start, "CSV files (*.csv);;All files (*)")
        if path:
            self.edit.setText(os.path.normpath(path))

    def values(self) -> list[str]:
        text = self.edit.text().strip()
        if not text:
            return []
        if self.spec.nargs in ("*", "+"):
            return split_values(text)
        return [text]

    def args(self):
        values = self.values()
        if not values or self.spec.positional:
            return values
        return [self.spec.flag, values[0]]

    def validate(self):
        error = ""
        values = self.values()
        if self.spec.required and not values:
            error = f"“{self.spec.label}” is required."
        elif self.spec.dest in ("isin", "input") and self.spec.command in ("details", "rates", "get_price_alarms"):
            bad = [v for v in values if not ISIN_RE.match(v.upper())]
            if bad:
                error = f"Not a valid ISIN: {', '.join(bad)}"
        set_prop(self.edit, "invalid", bool(error))
        return error

    def state(self): return self.edit.text()
    def set_state(self, value): self.edit.setText(str(value or ""))


def make_field(spec: OptionSpec, on_change, browse_root) -> Field:
    cls = {"flag": CheckField, "bool": CheckField, "choice": ChoiceField,
           "int": IntField}.get(spec.kind, TextField)
    return cls(spec, on_change, browse_root)


def field_row(fld: Field) -> QWidget:
    """Label on top, control, then a short help line – fits narrow panels."""
    holder = transparent(QWidget())
    col = QVBoxLayout(holder)
    col.setContentsMargins(0, 0, 0, 0)
    col.setSpacing(5)
    if not fld.is_check:
        col.addWidget(label(fld.spec.label + ("  *" if fld.spec.required else ""), "field"))
    col.addWidget(fld.widget)
    if fld.spec.help:
        hint = label(fld.spec.help, "small", wrap=True)
        if fld.is_check:
            hint.setContentsMargins(27, 0, 0, 0)
        col.addWidget(hint)
    return holder


class CommandForm(QWidget):
    """Basic options, a collapsible 'Advanced' block, raw extra args and preview."""

    changed = Signal()

    def __init__(self, spec: CommandSpec, browse_root, custom_top: QWidget | None = None):
        super().__init__()
        transparent(self)
        self.spec = spec
        self.fields: dict[str, Field] = {}
        lay = QVBoxLayout(self)
        lay.setContentsMargins(0, 0, 8, 0)
        lay.setSpacing(16)

        if custom_top is not None:
            lay.addWidget(custom_top)
        skip = set(TASKS.get(spec.name, {}).get("skip", []))
        options = [o for o in spec.options if o.dest not in skip]
        basic = [o for o in options if o.dest in spec.basic]
        basic.sort(key=lambda o: spec.basic.index(o.dest))
        advanced = [o for o in options if o.dest not in spec.basic]
        for opt in basic:
            lay.addWidget(field_row(self._make(opt, browse_root)))
        if not basic and custom_top is None:
            lay.addWidget(label("No settings needed — just press Run.", "muted"))

        self.advanced = Collapsible(f"Advanced options ({len(advanced)})" if advanced else "Advanced")
        for opt in advanced:
            self.advanced.body.addWidget(field_row(self._make(opt, browse_root)))
        self.extra = QLineEdit(placeholderText="e.g. --lang de")
        self.extra.setProperty("mono", True)
        self.extra.textChanged.connect(self.changed.emit)
        extra_holder = transparent(QWidget())
        ec = QVBoxLayout(extra_holder)
        ec.setContentsMargins(0, 0, 0, 0)
        ec.setSpacing(5)
        ec.addWidget(label("Extra pytr arguments", "field"))
        ec.addWidget(self.extra)
        ec.addWidget(label("Passed to pytr as typed. For experts.", "small"))
        self.advanced.body.addWidget(extra_holder)

        self.preview = QPlainTextEdit(readOnly=True)
        self.preview.setProperty("mono", True)
        self.preview.setFixedHeight(64)
        self.preview.setToolTip("The command that will be executed (PIN masked)")
        prev_holder = transparent(QWidget())
        pc = QVBoxLayout(prev_holder)
        pc.setContentsMargins(0, 0, 0, 0)
        pc.setSpacing(5)
        copy_btn = button("Copy", "ghost")
        copy_btn.clicked.connect(lambda: QGuiApplication.clipboard().setText(self.preview.toPlainText()))
        help_btn = button("pytr help", "ghost", "Show pytr's own help for this command")
        help_btn.clicked.connect(self._show_help)
        pc.addLayout(hbox(label("Command line", "field"), copy_btn, help_btn, stretch_at=1, spacing=2))
        pc.addWidget(self.preview)
        self.advanced.body.addWidget(prev_holder)
        lay.addWidget(divider())
        lay.addWidget(self.advanced)
        lay.addStretch(1)

    def _make(self, opt: OptionSpec, browse_root) -> Field:
        fld = make_field(opt, self.changed.emit, browse_root)
        self.fields[opt.dest] = fld
        return fld

    def _show_help(self):
        dlg = QDialog(self)
        dlg.setWindowTitle(f"pytr {self.spec.name} --help")
        dlg.resize(820, 560)
        v = QVBoxLayout(dlg)
        text = QPlainTextEdit(self.spec.help_text, readOnly=True)
        text.setProperty("mono", True)
        text.setLineWrapMode(QPlainTextEdit.NoWrap)
        v.addWidget(text)
        dlg.exec()

    def field(self, dest: str) -> Field | None:
        return self.fields.get(dest)

    def args(self) -> list[str]:
        flds = list(self.fields.values())
        positional = [a for f in flds if f.spec.positional for a in f.args()]
        optional = [a for f in flds if not f.spec.positional for a in f.args()]
        return positional + optional

    def extra_args(self) -> list[str]:
        text = self.extra.text().strip()
        return QProcess.splitCommand(text) if text else []

    def validate(self) -> list[str]:
        errors = []
        for dest, fld in self.fields.items():
            error = fld.validate()
            if error:
                errors.append(error)
                if dest not in self.spec.basic:  # make the offending field visible
                    self.advanced.toggle.setChecked(True)
        return errors

    def save(self, settings: QSettings) -> None:
        for dest, f in self.fields.items():
            settings.setValue(f"options/{self.spec.name}/{dest}", f.state())
        settings.setValue(f"options/{self.spec.name}/_extra", self.extra.text())

    def load(self, settings: QSettings) -> None:
        for dest, f in self.fields.items():
            value = settings.value(f"options/{self.spec.name}/{dest}")
            if value is not None:
                f.set_state(value)
        self.extra.setText(settings.value(f"options/{self.spec.name}/_extra", "", type=str))


# ============================================================================
# Price alarm editor (custom UI for set_price_alarms)
# ============================================================================

class AlarmEditor(QWidget):
    """Editable table: one instrument per row, target prices in the second column."""

    changed = Signal()
    load_requested = Signal()

    def __init__(self):
        super().__init__()
        transparent(self)
        v = QVBoxLayout(self)
        v.setContentsMargins(0, 0, 0, 0)
        v.setSpacing(8)
        v.addWidget(label("Alarms", "field"))
        self.table = QTableWidget(0, 2)
        self.table.setHorizontalHeaderLabels(["ISIN", "Target prices"])
        self.table.verticalHeader().hide()
        self.table.setAlternatingRowColors(True)
        self.table.setShowGrid(False)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.AllEditTriggers)
        hh = self.table.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Interactive)
        hh.setStretchLastSection(True)
        self.table.setColumnWidth(0, 135)
        self.table.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.table.setMinimumWidth(0)
        self.table.verticalHeader().setDefaultSectionSize(32)
        self.table.setMinimumHeight(190)
        self.table.itemChanged.connect(lambda _: self.changed.emit())
        v.addWidget(self.table)
        v.addWidget(label("Prices separated by spaces or ';' — e.g.  95.5  110  120,25. "
                          "A row without prices removes all alarms of that instrument.", "small", wrap=True))
        add_btn = button("+ Add row")
        del_btn = button("Remove", "ghost", "Remove the selected rows")
        load_btn = button("Load current alarms", "ghost", "Runs “Price alarms” and fills this table")
        add_btn.clicked.connect(lambda: self.add_row())
        del_btn.clicked.connect(self.remove_selected)
        load_btn.clicked.connect(self.load_requested.emit)
        v.addLayout(hbox(add_btn, del_btn, load_btn, stretch_at=3, spacing=4))
        self.error = label("", "error", wrap=True)
        self.error.hide()
        v.addWidget(self.error)

    def add_row(self, isin: str = "", prices: str = "") -> None:
        row = self.table.rowCount()
        self.table.insertRow(row)
        self.table.setItem(row, 0, QTableWidgetItem(isin))
        self.table.setItem(row, 1, QTableWidgetItem(prices))
        if not isin:
            self.table.setCurrentCell(row, 0)
            self.table.editItem(self.table.item(row, 0))
        self.changed.emit()

    def remove_selected(self) -> None:
        rows = sorted({i.row() for i in self.table.selectedIndexes()}, reverse=True)
        for r in rows:
            self.table.removeRow(r)
        self.changed.emit()

    def rows(self) -> list[tuple[str, str]]:
        out = []
        for r in range(self.table.rowCount()):
            isin = (self.table.item(r, 0).text() if self.table.item(r, 0) else "").strip().upper()
            prices = (self.table.item(r, 1).text() if self.table.item(r, 1) else "").strip()
            if isin or prices:
                out.append((isin, prices))
        return out

    def set_rows(self, rows: list[tuple[str, str]]) -> None:
        self.table.blockSignals(True)
        self.table.setRowCount(0)
        self.table.blockSignals(False)
        for isin, prices in rows:
            self.add_row(isin, prices)

    @staticmethod
    def parse_prices(text: str) -> list[str]:
        prices = []
        for token in [t for t in re.split(r"[\s;]+", text) if t]:
            token = token.replace(",", ".")
            value = Decimal(token)  # raises InvalidOperation
            if value <= 0:
                raise InvalidOperation
            prices.append(str(value))
        return prices

    def tokens(self) -> list[str]:
        out = []
        for isin, prices in self.rows():
            try:
                out += [isin, *self.parse_prices(prices)]
            except InvalidOperation:
                out.append(isin)
        return out

    def validate(self) -> list[str]:
        errors = []
        for i, (isin, prices) in enumerate(self.rows(), start=1):
            if not ISIN_RE.match(isin):
                errors.append(f"Row {i}: “{isin or '(empty)'}” is not a valid ISIN.")
            try:
                self.parse_prices(prices)
            except InvalidOperation:
                errors.append(f"Row {i}: prices must be positive numbers.")
        self.error.setText("\n".join(errors))
        self.error.setVisible(bool(errors))
        return errors

    def state(self) -> str:
        return json.dumps(self.rows())

    def set_state(self, value) -> None:
        try:
            self.set_rows([tuple(r) for r in json.loads(value or "[]")])
        except (ValueError, TypeError):
            pass


class AlarmForm(CommandForm):
    def __init__(self, spec: CommandSpec, browse_root):
        self.editor = AlarmEditor()
        super().__init__(spec, browse_root, custom_top=self.editor)
        self.editor.changed.connect(self.changed.emit)

    def args(self):
        flds = self.fields
        inputfile = flds.get("inputfile")
        from_file = inputfile is not None and bool(inputfile.state())
        positional = [] if from_file else self.editor.tokens()
        optional = [a for f in flds.values() if not f.spec.positional for a in f.args()]
        return positional + optional

    def validate(self):
        inputfile = self.fields.get("inputfile")
        if inputfile is not None and inputfile.state():
            self.editor.error.hide()
            return []
        errors = self.editor.validate()
        if not self.editor.rows():
            errors.append("Add at least one instrument.")
            self.editor.error.setText(errors[-1])
            self.editor.error.show()
        return errors

    def save(self, settings):
        super().save(settings)
        settings.setValue("options/set_price_alarms/_rows", self.editor.state())

    def load(self, settings):
        super().load(settings)
        self.editor.set_state(settings.value("options/set_price_alarms/_rows", "[]", type=str))
        if not self.editor.rows():
            self.editor.add_row()


# ============================================================================
# Results: table + log
# ============================================================================

def to_number(text: str) -> float | None:
    s = text.strip().replace("%", "").replace("€", "").replace(" ", "").strip()
    if not s:
        return None
    for candidate in (s, s.replace(".", "").replace(",", ".")):
        try:
            return float(candidate)
        except ValueError:
            continue
    return None


def pretty_header(name: str) -> str:
    if name in HEADER_NAMES:
        return HEADER_NAMES[name]
    spaced = re.sub(r"([a-z])([A-Z0-9])", r"\1 \2", name).replace("_", " ").strip()
    return spaced[:1].upper() + spaced[1:]


def read_csv_table(path: Path) -> tuple[list[str], list[list[str]]]:
    with open(path, encoding="utf-8-sig", newline="") as f:
        sample = f.read(4096)
        f.seek(0)
        delimiter = ";" if sample.count(";") >= sample.count(",") else ","
        rows = [r for r in csv.reader(f, delimiter=delimiter) if any(c.strip() for c in r)]
    if not rows:
        return [], []
    headers, data = rows[0], rows[1:]
    width = max(len(headers), *(len(r) for r in data)) if data else len(headers)
    headers += [f"Column {i + 1}" for i in range(len(headers), width)]
    return headers, [r + [""] * (width - len(r)) for r in data]


def parse_key_values(text: str) -> list[list[str]]:
    rows = []
    for line in text.splitlines():
        if ":" in line and not line.lstrip().startswith(("Wrote", "Writing")):
            key, _, value = line.partition(":")
            if key.strip() and len(key) < 60:
                rows.append([key.strip(), value.strip()])
    return rows


_NUM = r"(-?\d+(?:\.\d+)?)"
_DEPOT_RE = re.compile(rf"^\s*Depot\s+{_NUM}\s*->\s*{_NUM}\s+{_NUM}\s+{_NUM}%", re.MULTILINE)
_CASH_RE = re.compile(rf"^\s*Cash\s+([A-Z]{{3}})\s+{_NUM}", re.MULTILINE)
_TOTAL_RE = re.compile(rf"^\s*Total\s+{_NUM}\s*->\s*{_NUM}", re.MULTILINE)
CURRENCY_SYMBOLS = {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}


def parse_portfolio_totals(text: str) -> dict | None:
    """Read the Depot / Cash / Total lines pytr prints after `portfolio`.

    pytr formats them with plain '.2f' numbers, e.g.
      Depot      9947.65 ->   13124.42    3176.77    31.9%
      Cash EUR       512.30
      Total     10459.95 ->   13636.72
    """
    depot, cash, total = _DEPOT_RE.search(text), _CASH_RE.search(text), _TOTAL_RE.search(text)
    if not (depot and cash and total):
        return None
    return {
        "invested": float(depot[1]), "depot": float(depot[2]),
        "gain": float(depot[3]), "gain_pct": float(depot[4]),
        "currency": cash[1], "cash": float(cash[2]),
        "total_invested": float(total[1]), "total": float(total[2]),
    }


def format_money(value: float, currency: str = "EUR", signed: bool = False) -> str:
    """Locale-aware amount with currency symbol, e.g. 13.124,42 € (de) or €13,124.42 (en)."""
    locale = QLocale.system()
    text = locale.toCurrencyString(abs(value), CURRENCY_SYMBOLS.get(currency, currency), 2)
    if signed:
        return ("+" if value >= 0 else "−") + text
    return ("−" if value < 0 else "") + text


class PortfolioSummary(QObject):
    """Depot / Cash / Total tiles above the Portfolio table (widgets from the .ui)."""

    def __init__(self, ui: SimpleNamespace):
        super().__init__(ui.portfolioSummary)
        self.ui = ui
        self.hide()

    def hide(self) -> None:
        self.ui.portfolioSummary.hide()

    def show(self, totals: dict, updated: str) -> None:
        ui, cur = self.ui, totals["currency"]
        ui.depotValue.setText(format_money(totals["depot"], cur))
        ui.depotInvested.setText(f"Invested {format_money(totals['invested'], cur)}")

        gain, pct = totals["gain"], totals["gain_pct"]
        direction = "up" if gain > 0 else "down" if gain < 0 else "flat"
        arrow = {"up": "▲", "down": "▼", "flat": "■"}[direction]
        pct_text = QLocale.system().toString(abs(pct), "f", 1)
        sign = "+" if gain > 0 else "−" if gain < 0 else "±"
        ui.depotDelta.setText(f"{arrow} {format_money(gain, cur, signed=True)}  ({sign}{pct_text} %)")
        set_prop(ui.depotDelta, "delta", direction)

        ui.cashValue.setText(format_money(totals["cash"], cur))
        share = totals["cash"] / totals["total"] * 100 if totals["total"] else 0.0
        ui.cashShare.setText(f"{QLocale.system().toString(share, 'f', 1)} % of total · available")

        ui.totalValue.setText(format_money(totals["total"], cur))
        ui.totalInvested.setText(f"Depot + cash · invested {format_money(totals['total_invested'], cur)}")
        ui.portfolioUpdated.setText(updated)
        ui.portfolioSummary.show()


class Pill:
    """Small wrapper so code can write pill.set(text, state)."""

    def __init__(self, lbl: QLabel):
        self.label = lbl

    def set(self, text: str, state: str) -> None:
        set_pill(self.label, text, state)

    def text(self) -> str:
        return self.label.text()


class ResultTable(QObject):
    """Sortable, filterable results table (widgets come from the .ui file)."""

    def __init__(self, ui: SimpleNamespace):
        super().__init__(ui.tableView)
        self.view: QAbstractItemView = ui.tableView
        self.summary: QLabel = ui.tableSummary
        self.filter: QLineEdit = ui.tableFilter
        self.count: QLabel = ui.tableCount
        self.stack = ui.tableStack
        self.empty = ui.tableEmptyPage
        self.source_file: Path | None = None

        self.model = QStandardItemModel(self)
        self.proxy = QSortFilterProxyModel(self)
        self.proxy.setSourceModel(self.model)
        self.proxy.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.proxy.setFilterKeyColumn(-1)
        self.proxy.setSortRole(Qt.UserRole)
        self.view.setModel(self.proxy)
        self.view.horizontalHeader().setDefaultAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        QShortcut(QKeySequence.Copy, self.view, activated=self.copy_selection)
        self.filter.textChanged.connect(self._filter)
        self.clear()

    def clear(self) -> None:
        self.model.clear()
        self.source_file = None
        self.summary.hide()
        self.filter.clear()
        self.filter.hide()
        self.count.setText("")
        self.stack.setCurrentWidget(self.empty)

    def has_data(self) -> bool:
        return self.model.rowCount() > 0

    def set_data(self, headers: list[str], rows: list[list[str]], summary: str = "",
                 source: Path | None = None) -> None:
        self.model.clear()
        self.model.setHorizontalHeaderLabels([pretty_header(h) for h in headers])
        numeric = []
        for c in range(len(headers)):
            values = [r[c] for r in rows if c < len(r) and r[c].strip()]
            numeric.append(bool(values) and all(to_number(x) is not None for x in values))
        for r in rows:
            items = []
            for c, text in enumerate(r[:len(headers)]):
                item = QStandardItem(text)
                if numeric[c]:
                    num = to_number(text)
                    item.setData(num if num is not None else float("-inf"), Qt.UserRole)
                    item.setTextAlignment(Qt.AlignRight | Qt.AlignVCenter)
                else:
                    item.setData(text.lower(), Qt.UserRole)
                items.append(item)
            self.model.appendRow(items)
        self.source_file = source
        self.summary.setText(summary)
        self.summary.setVisible(bool(summary))
        self.filter.show()
        self.view.sortByColumn(-1, Qt.AscendingOrder)
        self.view.resizeColumnsToContents()
        for c in range(len(headers)):
            self.view.setColumnWidth(c, min(max(self.view.columnWidth(c) + 16, 80), 360))
        self.stack.setCurrentWidget(self.view)
        self._filter(self.filter.text())

    def _filter(self, text: str) -> None:
        self.proxy.setFilterFixedString(text)
        total, shown = self.model.rowCount(), self.proxy.rowCount()
        self.count.setText(f"{shown} of {total} rows" if text else f"{total} rows")

    def rows_as_lists(self, visible_only=True) -> tuple[list[str], list[list[str]]]:
        headers = [self.model.headerData(c, Qt.Horizontal) for c in range(self.model.columnCount())]
        model = self.proxy if visible_only else self.model
        rows = [[model.index(r, c).data() or "" for c in range(model.columnCount())]
                for r in range(model.rowCount())]
        return headers, rows

    def copy_selection(self) -> None:
        sel = self.view.selectionModel().selectedRows()
        if not sel:
            return
        lines = ["\t".join(self.proxy.index(i.row(), c).data() or "" for c in range(self.proxy.columnCount()))
                 for i in sorted(sel, key=lambda i: i.row())]
        QGuiApplication.clipboard().setText("\n".join(lines))

    def export_csv(self, path: str) -> None:
        headers, rows = self.rows_as_lists()
        with open(path, "w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f, delimiter=";")
            writer.writerow(headers)
            writer.writerows(rows)


class Console(QObject):
    """Output log on top of the .ui's QPlainTextEdit (plain text, \\r-aware)."""

    def __init__(self, edit: QPlainTextEdit):
        super().__init__(edit)
        self.edit = edit
        self._pending_cr = False
        self._formats: dict[str, QTextCharFormat] = {}

    def set_colors(self, variables: dict[str, str]) -> None:
        """Line colours come from the theme's console_* variables."""
        # "out" keeps the default format, so it follows the palette after theme switches.
        self._formats = {"out": QTextCharFormat()}
        for name, value in variables.items():
            if name.startswith("console_") and QColor(value).isValid():
                fmt = QTextCharFormat()
                fmt.setForeground(QColor(value))
                self._formats[name.removeprefix("console_")] = fmt

    def _format(self, kind: str, text: str) -> QTextCharFormat:
        if kind == "log":  # stderr: pytr logs normal info there, only flag real problems
            if ERROR_RE.search(text):
                kind = "error"
            elif WARN_RE.search(text):
                kind = "warn"
        return self._formats.get(kind) or QTextCharFormat()

    def write(self, text: str, kind: str = "out") -> None:
        """Append plain text (never interpreted as HTML). Handles \\r progress lines."""
        text = ANSI_RE.sub("", text)
        if not text:
            return
        bar = self.edit.verticalScrollBar()
        follow = bar.value() >= bar.maximum() - 4
        cur = QTextCursor(self.edit.document())
        cur.movePosition(QTextCursor.End)
        for part in LINE_SPLIT_RE.split(text):
            if not part:
                continue
            if part in ("\n", "\r\n"):
                cur.insertText("\n")
                self._pending_cr = False
            elif part == "\r":
                self._pending_cr = True
            else:
                if self._pending_cr:  # carriage return: overwrite current line
                    cur.movePosition(QTextCursor.StartOfBlock, QTextCursor.KeepAnchor)
                    cur.removeSelectedText()
                    self._pending_cr = False
                cur.insertText(part, self._format(kind, part))
        if follow:
            bar.setValue(bar.maximum())

    def line(self, text: str, kind: str = "sys") -> None:
        if self.edit.document().lastBlock().text():
            self.write("\n")
        self._pending_cr = False
        self.write(text + "\n", kind)

    def clear(self) -> None:
        self.edit.clear()

    def toPlainText(self) -> str:  # noqa: N802 - mirrors the Qt name
        return self.edit.toPlainText()


# ============================================================================
# Account page (in-process login through pytr's API)
# ============================================================================

def _auth_start(phone, pin, remember, v2, waf):
    api = TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=remember,
                           waf_token=waf, use_v2_login=v2)
    if api.resume_websession():
        return {"api": api, "resumed": True}
    countdown = api.initiate_weblogin()
    needs_code = (not v2) or api.weblogin_needs_authenticator
    return {"api": api, "resumed": False, "countdown": countdown, "needs_code": needs_code}


def _auth_complete(api, code):
    api.complete_weblogin(code)
    return True


def _auth_resend(api):
    api.resend_weblogin()
    return True


def _check_session(phone, pin):
    api = TradeRepublicApi(phone_no=phone, pin=pin, save_cookies=True)
    return api.resume_websession()


def read_stored_credentials() -> tuple[str, str] | None:
    try:
        lines = CREDENTIALS_FILE.read_text(encoding="utf-8").splitlines()
        if len(lines) >= 2 and lines[0].strip():
            return lines[0].strip(), lines[1].strip()
    except OSError:
        pass
    return None


def store_credentials(phone: str, pin: str) -> None:
    PYTR_DIR.mkdir(parents=True, exist_ok=True)
    CREDENTIALS_FILE.write_text(f"{phone}\n{pin}\n", encoding="utf-8", newline="\n")
    try:
        os.chmod(CREDENTIALS_FILE, 0o600)
    except OSError:
        pass


def cookie_file(phone: str) -> Path:
    return PYTR_DIR / f"cookies.{phone}.txt"


def mask_phone(phone: str) -> str:
    if len(phone) <= 6:
        return "•" * len(phone)
    return phone[:4] + "•" * (len(phone) - 6) + phone[-2:]


WAF_VALUES = ["default", "awswaf", "playwright", "none"]   # same order as wafCombo in the .ui
METHOD_VALUES = ["v2", "v1"]                               # same order as methodCombo


class AccountPage(QObject):
    session_changed = Signal(str, str)  # state, text
    log = Signal(str, str)              # text, console kind

    def __init__(self, ui: SimpleNamespace, settings: QSettings, tasks: TaskRunner):
        super().__init__(ui.accountPage)
        self.window: QWidget = ui.MainWindow
        self.settings = settings
        self.tasks = tasks
        self.api = None
        self._gen = 0                    # invalidates results of cancelled logins
        self._busy = False
        self.signed_in = False
        self._pending_credentials = ("", "")
        self._countdown_end = 0.0
        self._countdown_timer = QTimer(self, interval=500, timeout=self._tick_countdown)

        # Widgets from main_window.ui
        self.phone: QLineEdit = ui.phoneEdit
        self.pin: QLineEdit = ui.pinEdit
        self.show_pin: QCheckBox = ui.showPinCheck
        self.method: QComboBox = ui.methodCombo
        self.waf: QComboBox = ui.wafCombo
        self.waf_label: QLabel = ui.wafLabel
        self.remember: QCheckBox = ui.rememberCheck
        self.message: QLabel = ui.accountMessage
        self.signin_btn: QPushButton = ui.signInButton
        self.signout_btn: QPushButton = ui.signOutButton
        self.status_pill = Pill(ui.accountPill)
        self.verify_card: QFrame = ui.verifyCard
        self.verify_text: QLabel = ui.verifyText
        self.progress = ui.verifyProgress
        self.code: QLineEdit = ui.codeEdit
        self.verify_btn: QPushButton = ui.verifyButton
        self.resend_btn: QPushButton = ui.resendButton
        self.countdown_lbl: QLabel = ui.countdownLabel
        self.cancel_btn: QPushButton = ui.cancelButton

        for i, value in enumerate(METHOD_VALUES):
            self.method.setItemData(i, value)
        for i, value in enumerate(WAF_VALUES):
            self.waf.setItemData(i, value)
        ui.infoStoragePath.setText(str(PYTR_DIR))
        ui.accountPytrBanner.setVisible(bool(PYTR_IMPORT_ERROR))
        if PYTR_IMPORT_ERROR:
            ui.accountPytrBannerText.setText(f"pytr could not be loaded ({PYTR_IMPORT_ERROR}). "
                                             "Install it with:  pip install pytr")
        self.message.hide()
        self.verify_card.hide()

        # --- wiring -----------------------------------------------------------
        self.show_pin.toggled.connect(lambda on: self.pin.setEchoMode(
            QLineEdit.Normal if on else QLineEdit.Password))
        self.signin_btn.clicked.connect(self.sign_in)
        self.signout_btn.clicked.connect(self.sign_out)
        self.phone.returnPressed.connect(self.sign_in)
        self.pin.returnPressed.connect(self.sign_in)
        self.verify_btn.clicked.connect(self.submit_code)
        self.code.returnPressed.connect(self.submit_code)
        self.resend_btn.clicked.connect(self.resend)
        self.cancel_btn.clicked.connect(self.cancel)
        self.method.currentIndexChanged.connect(self._sync_method)
        for w in (self.phone, self.pin):
            w.textChanged.connect(lambda _, w=w: set_prop(w, "invalid", False))

        self._load()

    def waf_value(self) -> str | None:
        value = self.waf.currentData()
        return None if value in (None, "none") else value

    # -- persistence ----------------------------------------------------------
    def _load(self):
        s = self.settings
        self.method.setCurrentIndex(max(self.method.findData(s.value("auth/method", "v2")), 0))
        waf = s.value("auth/waf", "default")
        self.waf.setCurrentIndex(max(self.waf.findData(waf), 0))
        self.remember.setChecked(s.value("auth/remember", True, type=bool))
        stored = read_stored_credentials()
        if stored:
            self.phone.setText(stored[0])
            self.pin.setText(stored[1])
        else:
            self.phone.setText(s.value("auth/phone", "", type=str))
        self._sync_method()
        self._set_state("idle", "Signed out")

    def save(self):
        s = self.settings
        s.setValue("auth/method", self.method.currentData())
        s.setValue("auth/waf", self.waf.currentData() or "none")
        s.setValue("auth/remember", self.remember.isChecked())
        s.setValue("auth/phone", self.phone.text().strip())

    def _sync_method(self):
        # The WAF token only matters for the legacy login – hide it otherwise.
        legacy = self.method.currentData() == "v1"
        self.waf.setVisible(legacy)
        self.waf_label.setVisible(legacy)

    # -- public helpers -------------------------------------------------------
    def check_session_on_startup(self):
        if TradeRepublicApi is None:
            self._set_state("error", "pytr missing")
            return
        stored = read_stored_credentials()
        if not stored or not cookie_file(stored[0]).exists():
            return
        self._set_state("busy", "Checking session…")
        gen = self._gen
        self.tasks.start(_check_session, *stored,
                         on_done=lambda ok: self._session_checked(gen, ok),
                         on_failed=lambda err: self._session_checked(gen, False, err))

    def _session_checked(self, gen, ok, err=""):
        if gen != self._gen:
            return
        if ok:
            self.signed_in = True
            self._set_state("ok", "Signed in")
            self.log.emit("Existing Trade Republic session resumed.", "ok")
        else:
            self._set_state("warn", "Session expired")
            if err:
                self.log.emit(f"Session check failed: {err}", "warn")

    def command_login_args(self, masked: bool = False) -> list[str]:
        """Login options appended to every pytr command."""
        args = []
        if self.method.currentData() == "v2":
            args.append("--v2")
        else:
            waf = self.waf_value()
            if waf is None:
                args.append("--no-waf-token")
            elif waf != "default":
                args += ["--waf-token", waf]
        if not self.uses_stored_credentials():
            phone, pin = self.phone.text().strip(), self.pin.text().strip()
            if phone and pin:
                args += ["-n", phone, "-p", "••••" if masked else pin]
        return args

    def uses_stored_credentials(self) -> bool:
        return self.remember.isChecked() and read_stored_credentials() is not None

    # -- state ----------------------------------------------------------------
    def _set_state(self, state: str, text: str):
        self.status_pill.set(text, state)
        self.session_changed.emit(state, text)

    def _show_message(self, text: str, role: str = "muted"):
        self.message.setText(text)
        self.message.setVisible(bool(text))
        set_prop(self.message, "role", role)

    def _set_busy(self, busy: bool):
        self._busy = busy
        for w in (self.phone, self.pin, self.method, self.waf, self.remember, self.signin_btn,
                  self.show_pin, self.signout_btn):
            w.setEnabled(not busy)

    # -- sign-in flow ---------------------------------------------------------
    def sign_in(self):
        if self._busy:
            return
        if TradeRepublicApi is None:
            self._show_message("pytr is not installed.", "error")
            return
        phone = self.phone.text().strip().replace(" ", "")
        pin = self.pin.text().strip()
        ok = True
        if not PHONE_RE.match(phone):
            set_prop(self.phone, "invalid", True)
            ok = False
        if not PIN_RE.match(pin):
            set_prop(self.pin, "invalid", True)
            ok = False
        if not ok:
            self._show_message("Enter your phone number in international format (e.g. +4917…) "
                               "and your 4-digit PIN.", "error")
            return
        self.phone.setText(phone)
        self.save()
        self._gen += 1
        gen = self._gen
        v2 = self.method.currentData() == "v2"
        waf = self.waf_value()
        self._set_busy(True)
        self.signed_in = False
        self._show_message("")
        self._set_state("busy", "Signing in…")
        self.log.emit(f"Signing in as {mask_phone(phone)} "
                      f"({'app confirmation' if v2 else 'legacy code'} login)…", "sys")
        self.tasks.start(_auth_start, phone, pin, self.remember.isChecked(), v2, waf,
                         on_done=lambda r: self._started(gen, r, phone, pin, v2),
                         on_failed=lambda e: self._failed(gen, e))

    def _started(self, gen, result, phone, pin, v2):
        if gen != self._gen:
            return
        self.api = result["api"]
        self._pending_credentials = (phone, pin)
        if result["resumed"]:
            self._succeeded(gen, resumed=True)
            return
        self.verify_card.show()
        self.code.clear()
        if result["needs_code"]:
            self.progress.hide()
            self.code.show()
            self.verify_btn.show()
            if v2:
                self.verify_text.setText("Enter the 6-digit code from your authenticator app.")
                self.resend_btn.hide()
                self.countdown_lbl.hide()
            else:
                self.verify_text.setText("Enter the code Trade Republic sent to your app as a notification. "
                                         "No notification? Request an SMS once the timer runs out.")
                self.resend_btn.show()
                self.resend_btn.setEnabled(False)
                self.countdown_lbl.show()
                self._countdown_end = time.time() + float(result.get("countdown") or 0)
                self._countdown_timer.start()
                self._tick_countdown()
            self._set_state("warn", "Waiting for code")
            self.code.setFocus()
        else:
            self.verify_text.setText("Open the Trade Republic app on your phone and confirm the login. "
                                     "This page continues automatically.")
            self.progress.show()
            for w in (self.code, self.verify_btn, self.resend_btn, self.countdown_lbl):
                w.hide()
            self._set_state("warn", "Confirm in app")
            self.tasks.start(_auth_complete, self.api, None,
                             on_done=lambda _: self._succeeded(gen),
                             on_failed=lambda e: self._failed(gen, e))

    def _tick_countdown(self):
        remaining = int(self._countdown_end - time.time())
        if remaining > 0:
            self.countdown_lbl.setText(f"SMS available in {remaining}s")
        else:
            self._countdown_timer.stop()
            self.countdown_lbl.setText("")
            self.resend_btn.setEnabled(True)

    def submit_code(self):
        code = self.code.text().strip()
        if not self.api or not code:
            set_prop(self.code, "invalid", True)
            return
        set_prop(self.code, "invalid", False)
        gen = self._gen
        self.verify_btn.setEnabled(False)
        self.code.setEnabled(False)
        self.progress.show()
        self._set_state("busy", "Verifying…")
        self.tasks.start(_auth_complete, self.api, code,
                         on_done=lambda _: self._succeeded(gen),
                         on_failed=lambda e: self._code_failed(gen, e))

    def _code_failed(self, gen, err):
        if gen != self._gen:
            return
        self.progress.hide()
        self.verify_btn.setEnabled(True)
        self.code.setEnabled(True)
        self.code.selectAll()
        self.code.setFocus()
        self._set_state("error", "Code rejected")
        self.verify_text.setText(f"{err}\nCheck the code and try again, or cancel and start over.")
        self.log.emit(f"Verification failed: {err}", "error")

    def resend(self):
        if not self.api:
            return
        self.resend_btn.setEnabled(False)
        gen = self._gen

        def done(_):
            if gen == self._gen:
                self.log.emit("SMS requested.", "sys")
                self.verify_text.setText("SMS requested. Enter the code from the SMS.")

        self.tasks.start(_auth_resend, self.api, on_done=done,
                         on_failed=lambda e: self._code_failed(gen, e))

    def cancel(self):
        self._gen += 1  # results of the running request are ignored from now on
        self.api = None
        self._reset_verification()
        self._set_busy(False)
        self._set_state("idle", "Signed out")
        self.log.emit("Sign-in cancelled.", "warn")

    def _reset_verification(self):
        self._countdown_timer.stop()
        self.verify_card.hide()
        self.code.clear()
        self.code.setEnabled(True)
        self.verify_btn.setEnabled(True)

    def _succeeded(self, gen, resumed: bool = False):
        if gen != self._gen:
            return
        phone, pin = self._pending_credentials
        if self.remember.isChecked():
            try:
                store_credentials(phone, pin)
            except OSError as exc:
                self.log.emit(f"Could not store credentials: {exc}", "error")
        self._reset_verification()
        self._set_busy(False)
        self.signed_in = True
        self._set_state("ok", "Signed in")
        msg = "Existing session resumed." if resumed else "Signed in successfully."
        if not self.remember.isChecked():
            msg += " Credentials are not stored, so each task will sign in again."
        self._show_message(msg + " Pick a task in the sidebar to continue.", "muted")
        self.log.emit(msg, "ok")

    def _failed(self, gen, err):
        if gen != self._gen:
            return
        self._reset_verification()
        self._set_busy(False)
        self._set_state("error", "Sign-in failed")
        hint = ""
        if "WAF" in err or "awselb" in err:
            hint = "\nTip: use “Confirm in the Trade Republic app”, which needs no WAF token."
        self._show_message(err + hint, "error")
        self.log.emit(f"Sign-in failed: {err}", "error")

    def sign_out(self):
        phone = self.phone.text().strip()
        box = QMessageBox(self.window)
        box.setWindowTitle("Sign out")
        box.setIcon(QMessageBox.Question)
        box.setText("Sign out of Trade Republic?")
        box.setInformativeText("This deletes the saved session. You can also delete the stored "
                               "phone number and PIN.")
        signout = box.addButton("Sign out", QMessageBox.AcceptRole)
        forget = box.addButton("Sign out && forget credentials", QMessageBox.DestructiveRole)
        box.addButton(QMessageBox.Cancel)
        box.exec()
        clicked = box.clickedButton()
        if clicked not in (signout, forget):
            return
        self._gen += 1
        self.api = None
        stored = read_stored_credentials()
        for p in {phone, stored[0] if stored else ""} - {""}:
            try:
                cookie_file(p).unlink(missing_ok=True)
            except OSError as exc:
                self.log.emit(f"Could not delete cookies: {exc}", "error")
        if clicked is forget:
            try:
                CREDENTIALS_FILE.unlink(missing_ok=True)
            except OSError as exc:
                self.log.emit(f"Could not delete credentials: {exc}", "error")
            self.pin.clear()
        self._reset_verification()
        self.signed_in = False
        self._set_state("idle", "Signed out")
        self._show_message("Signed out." + (" Stored credentials deleted." if clicked is forget else ""))
        self.log.emit("Signed out.", "sys")


# ============================================================================
# Task page (pytr subprocess + results)
# ============================================================================

@dataclass
class ResultPlan:
    kind: str = ""                   # csv | kv | ""
    path: Path | None = None
    temp: bool = False
    on_rows: object = None           # optional callback(headers, rows)


class TaskPage(QObject):
    status = Signal(str)
    running_changed = Signal(bool)
    go_account = Signal()

    def __init__(self, ui: SimpleNamespace, settings: QSettings, account: AccountPage,
                 workdir_provider, verbosity_provider):
        super().__init__(ui.taskPage)
        self.window: QWidget = ui.MainWindow
        self.settings = settings
        self.account = account
        self.workdir = workdir_provider
        self.verbosity = verbosity_provider
        self.proc: QProcess | None = None
        self._stopped = False
        self._elapsed = QElapsedTimer()
        self._decoders = {}
        self._stdout = ""
        self._plan = ResultPlan()
        self._running_name = ""
        self.specs = load_command_specs() if pytr_main else {}
        self.forms: dict[str, CommandForm] = {}
        self.form_scrolls: dict[str, QScrollArea] = {}
        self.results: dict[str, dict] = {}    # task -> last result (table data, totals, time)
        self.current = ""

        # Widgets from main_window.ui
        self.title: QLabel = ui.taskTitle
        self.desc: QLabel = ui.taskDescription
        self.run_btn: QPushButton = ui.runButton
        self.stop_btn: QPushButton = ui.stopButton
        self.banner: QFrame = ui.sessionBanner
        self.banner_text: QLabel = ui.sessionBannerText
        self.split = ui.taskSplitter
        self.form_stack = ui.formStack          # one scroll area per task form is added here
        self.view_switch = SegmentGroup([ui.tableViewButton, ui.logViewButton])
        self.run_pill = Pill(ui.runPill)
        self.open_dir_btn: QPushButton = ui.openFolderButton
        self.export_btn: QPushButton = ui.exportButton
        self.clear_btn: QPushButton = ui.clearButton
        self.run_progress = ui.runProgress
        self.result_stack = ui.resultStack
        self.table = ResultTable(ui)
        self.overview = PortfolioSummary(ui)
        self.console = Console(ui.console)
        self.input_row: QWidget = ui.inputRow
        self.stdin: QLineEdit = ui.stdinEdit
        self.hide_input: QCheckBox = ui.hideInputCheck
        self.send_btn: QPushButton = ui.sendButton

        self.banner.hide()
        ui.taskPytrBanner.setVisible(bool(PYTR_IMPORT_ERROR))
        if PYTR_IMPORT_ERROR:
            ui.taskPytrBannerText.setText(f"pytr could not be loaded ({PYTR_IMPORT_ERROR}). "
                                          "Install it with:  pip install pytr")
        self.split.setStretchFactor(0, 0)
        self.split.setStretchFactor(1, 1)
        self.split.setSizes([380, 800])
        state = self.settings.value("ui/task_split")
        if state:
            self.split.restoreState(state)

        # --- wiring ---------------------------------------------------------------
        ui.sessionBannerButton.clicked.connect(self.go_account.emit)
        self.hide_input.toggled.connect(lambda on: self.stdin.setEchoMode(
            QLineEdit.Password if on else QLineEdit.Normal))
        self.view_switch.changed.connect(self.result_stack.setCurrentIndex)
        self.run_btn.clicked.connect(self.run)
        self.stop_btn.clicked.connect(self.stop)
        self.send_btn.clicked.connect(self.send_input)
        self.stdin.returnPressed.connect(self.send_input)
        self.clear_btn.clicked.connect(self.clear_view)
        self.export_btn.clicked.connect(self.export_view)
        self.open_dir_btn.clicked.connect(self.open_workdir)
        account.session_changed.connect(self._on_session)
        self._set_running(False)

    # -- task selection -------------------------------------------------------------
    def select(self, name: str) -> None:
        if name not in self.specs:
            return
        spec = self.specs[name]
        if name not in self.forms:
            form = (AlarmForm if name == "set_price_alarms" else CommandForm)(spec, self.workdir)
            form.load(self.settings)
            form.changed.connect(self.update_preview)
            if isinstance(form, AlarmForm):
                form.editor.load_requested.connect(self.load_current_alarms)
            self.forms[name] = form
            scroll = transparent_scroll(form)
            self.form_scrolls[name] = scroll
            self.form_stack.addWidget(scroll)
        self.current = name
        self.form_stack.setCurrentWidget(self.form_scrolls[name])
        self.title.setText(spec.title)
        self.desc.setText(spec.summary)
        self._show_cached(name)  # every task keeps its own last result
        self.update_preview()

    def current_form(self) -> CommandForm | None:
        return self.forms.get(self.current)

    def _on_session(self, state: str, _text: str) -> None:
        self.banner.setVisible(state in ("idle", "warn", "error") and not self.account.uses_stored_credentials()
                               or state == "warn")
        if state == "warn":
            self.banner_text.setText("Your saved session has expired. Sign in again, "
                                     "or tasks will ask you to confirm a new login.")
        else:
            self.banner_text.setText("You are not signed in. Sign in once on the Account page "
                                     "so tasks can run without asking.")
        self.update_preview()

    def build_args(self, name: str, cmd_args: list[str], masked: bool = False) -> list[str]:
        return ["-v", self.verbosity(), name, *cmd_args, *self.account.command_login_args(masked=masked)]

    def update_preview(self, *_):
        form = self.current_form()
        if form:
            args = self.build_args(form.spec.name, form.args() + form.extra_args(), masked=True)
            form.preview.setPlainText(join_command(["pytr", *args]))

    # -- result planning ------------------------------------------------------------
    def _resolve(self, text: str) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(text)))
        return p if p.is_absolute() else Path(self.workdir()) / p

    def _plan_for(self, form: CommandForm) -> tuple[ResultPlan, list[str]]:
        name = form.spec.name
        if name in CSV_OUTPUT_DEST:
            fld = form.field(CSV_OUTPUT_DEST[name])
            user = fld.state().strip() if fld else ""
            if user:
                return ResultPlan("csv", self._resolve(user)), []
            tmp = Path(tempfile.gettempdir()) / f"trhelper-{name}-{uuid.uuid4().hex[:8]}.csv"
            return ResultPlan("csv", tmp, temp=True), [fld.spec.flag if fld else "--output", str(tmp)]
        if name == "export_transactions":
            fmt = form.field("export_format")
            if fmt and fmt.state() != "csv":
                return ResultPlan(), []
            out_file, out_dir = form.field("outputfile"), form.field("outputdir")
            if out_file and out_file.state().strip():
                return ResultPlan("csv", self._resolve(out_file.state().strip())), []
            folder = out_dir.state().strip() if out_dir else ""
            return ResultPlan("csv", self._resolve(folder or ".") / "account_transactions.csv"), []
        if name == "dl_docs":
            exp, fmt, out = form.field("export_transactions"), form.field("export_format"), form.field("output")
            if exp and exp.state() and (not fmt or fmt.state() == "csv") and out and out.state().strip():
                return ResultPlan("csv", self._resolve(out.state().strip()) / "account_transactions.csv"), []
            return ResultPlan(), []
        if name in KV_COMMANDS:
            return ResultPlan("kv"), []
        return ResultPlan(), []

    # -- running ----------------------------------------------------------------------
    def is_running(self) -> bool:
        return self.proc is not None and self.proc.state() != QProcess.NotRunning

    def _set_running(self, running: bool):
        self.run_btn.setEnabled(not running and bool(self.specs))
        self.run_btn.setText("Running…" if running else "Run")
        self.stop_btn.setVisible(running)
        self.input_row.setVisible(running)
        self.run_progress.setVisible(running)
        self.form_stack.setEnabled(not running)
        self.running_changed.emit(running)

    def run(self):
        if self.is_running():
            self.status.emit("A task is already running.")
            return
        form = self.current_form()
        if not form:
            return
        errors = form.validate()
        if errors:
            self.status.emit(errors[0])
            self.run_pill.set("Check settings", "error")
            return
        form.save(self.settings)
        plan, result_args = self._plan_for(form)
        self._launch(form.spec.name, form.args() + result_args + form.extra_args(), plan,
                     display_args=form.args() + form.extra_args())

    def load_current_alarms(self):
        form = self.forms.get("set_price_alarms")
        if self.is_running() or form is None:
            return
        tmp = Path(tempfile.gettempdir()) / f"trhelper-alarms-{uuid.uuid4().hex[:8]}.csv"

        def fill(headers, rows):
            form.editor.set_rows([(r[0], "  ".join(c for c in r[1:] if c.strip())) for r in rows])
            if not rows:
                form.editor.add_row()
            self.status.emit(f"Loaded alarms for {len(rows)} instrument(s).")

        self._launch("get_price_alarms", ["--outputfile", str(tmp)],
                     ResultPlan("csv", tmp, temp=True, on_rows=fill), display_args=[])

    def _launch(self, name: str, cmd_args: list[str], plan: ResultPlan, display_args: list[str]):
        workdir = Path(self.workdir())
        try:
            workdir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self.console.line(f"Cannot create working folder {workdir}: {exc}", "error")
            self.view_switch.select(1)
            return

        program, prefix = pytr_invocation()
        env = QProcessEnvironment.systemEnvironment()
        for key, value in {"PYTHONUNBUFFERED": "1", "PYTHONIOENCODING": "utf-8", "PYTHONUTF8": "1",
                           "NO_COLOR": "1", "COLUMNS": "160"}.items():
            env.insert(key, value)

        self.proc = QProcess(self)
        self.proc.setProgram(program)
        self.proc.setArguments(prefix + self.build_args(name, cmd_args))
        self.proc.setWorkingDirectory(str(workdir))
        self.proc.setProcessEnvironment(env)
        self.proc.readyReadStandardOutput.connect(self._read_stdout)
        self.proc.readyReadStandardError.connect(self._read_stderr)
        self.proc.errorOccurred.connect(self._on_error)
        self.proc.finished.connect(self._on_finished)
        self._decoders = {"out": codecs.getincrementaldecoder("utf-8")("replace"),
                          "log": codecs.getincrementaldecoder("utf-8")("replace")}
        self._stopped = False
        self._stdout = ""
        self._plan = plan
        self._running_name = name
        title = self.specs[name].title if name in self.specs else name

        self.console.line(f"▶ {title}", "sys")
        self.console.line("$ " + join_command(["pytr", *self.build_args(name, display_args, masked=True)]), "log")
        self._elapsed.start()
        self.run_pill.set(f"Running {title}…", "busy")
        self.status.emit(f"Running {title}…")
        self._set_running(True)
        if not plan.kind:
            self.view_switch.select(1)
        self.proc.start()

    def stop(self):
        if self.is_running():
            self._stopped = True
            self.proc.kill()  # pytr is a console app: terminate() has no effect on Windows

    def _read(self, kind: str):
        if not self.proc:
            return
        data = (self.proc.readAllStandardOutput() if kind == "out" else self.proc.readAllStandardError()).data()
        text = self._decoders[kind].decode(data)
        if not text:
            return
        self.console.write(text, kind)
        if kind == "out":
            self._stdout += text
            if PROMPT_RE.search(self._stdout[-200:]):
                self.view_switch.select(1)
                self.stdin.setFocus()
                self.status.emit("pytr is waiting for your input (see Log).")

    def _read_stdout(self): self._read("out")
    def _read_stderr(self): self._read("log")

    def _on_error(self, error):
        if error == QProcess.FailedToStart:
            self.console.line(f"Could not start pytr: {self.proc.errorString()}", "error")
            self._cleanup("Failed to start", "error")
            self.view_switch.select(1)

    def _on_finished(self, code: int, status):
        self._read("out")
        self._read("log")
        for kind, dec in self._decoders.items():
            tail = dec.decode(b"", final=True)
            self.console.write(tail, kind)
            if kind == "out":
                self._stdout += tail
        secs = self._elapsed.elapsed() / 1000
        if self._stopped:
            self.console.line(f"■ Stopped after {secs:.1f}s", "warn")
            self._cleanup("Stopped", "warn")
        elif status == QProcess.CrashExit:
            self.console.line(f"✖ pytr crashed after {secs:.1f}s", "error")
            self._cleanup("Crashed", "error")
            self.view_switch.select(1)
        elif code == 0:
            self.console.line(f"✔ Finished in {secs:.1f}s", "ok")
            shown = self._show_result()
            self._cleanup(f"Done in {secs:.1f}s", "ok")
            if not shown:
                self.view_switch.select(1)
        else:
            self.console.line(f"✖ Failed with exit code {code} after {secs:.1f}s — see the log above.", "error")
            self._cleanup("Failed", "error")
            self.view_switch.select(1)
        self._remove_temp()

    def _show_result(self) -> bool:
        plan = self._plan
        if plan.kind == "csv" and plan.path:
            if not plan.path.exists():
                self.console.line(f"No result file was written ({plan.path}).", "warn")
                return False
            try:
                headers, rows = read_csv_table(plan.path)
            except (OSError, csv.Error, UnicodeDecodeError) as exc:
                self.console.line(f"Could not read {plan.path}: {exc}", "error")
                return False
            if plan.on_rows:
                plan.on_rows(headers, rows)
                return True
            source = None if plan.temp else plan.path
            totals = parse_portfolio_totals(self._stdout) if self._running_name == "portfolio" else None
            return self._set_result(headers, rows, source, totals)
        if plan.kind == "kv":
            rows = parse_key_values(self._stdout)
            if rows:
                return self._set_result(["Field", "Value"], rows, None, None)
        return False

    def _set_result(self, headers, rows, source, totals) -> bool:
        self.results[self._running_name] = {
            "headers": headers, "rows": rows, "source": source, "totals": totals,
            "updated": QLocale.system().toString(QDateTime.currentDateTime(), "HH:mm"),
        }
        if self._running_name == self.current:
            self._show_cached(self.current)
            self.view_switch.select(0)
        return True

    def _show_cached(self, name: str) -> None:
        result = self.results.get(name)
        if not result:
            self.table.clear()
            self.overview.hide()
            return
        updated = f"Updated at {result['updated']}"
        lines = [f"Saved to {result['source']}"] if result["source"] else []
        if result["totals"]:
            # Portfolio: totals as tiles, the time sits in the tile header.
            self.overview.show(result["totals"], updated)
        else:
            self.overview.hide()
            lines.append(updated)
        self.table.set_data(result["headers"], result["rows"], "\n".join(lines), result["source"])

    def _remove_temp(self):
        if self._plan.temp and self._plan.path:
            try:
                self._plan.path.unlink(missing_ok=True)
            except OSError:
                pass

    def _cleanup(self, text: str, state: str):
        if self.proc:
            self.proc.deleteLater()
            self.proc = None
        self.run_pill.set(text, state)
        self.status.emit(text)
        self._set_running(False)

    def send_input(self):
        if not self.is_running():
            return
        text = self.stdin.text()
        self.proc.write((text + "\n").encode("utf-8"))
        shown = "•" * len(text) if self.hide_input.isChecked() else text
        self.console.line(f"› {shown}", "input")
        self.stdin.clear()

    def kill_and_wait(self):
        if self.is_running():
            self._stopped = True
            self.proc.kill()
            self.proc.waitForFinished(3000)

    # -- toolbar ------------------------------------------------------------------------
    def clear_view(self):
        if self.result_stack.currentIndex() == 0:
            self.table.clear()
            self.overview.hide()
            self.results.pop(self.current, None)
        else:
            self.console.clear()

    def export_view(self):
        stamp = time.strftime("%Y%m%d-%H%M%S")
        if self.result_stack.currentIndex() == 0:
            if not self.table.has_data():
                self.status.emit("The table is empty.")
                return
            path, _ = QFileDialog.getSaveFileName(
                self.window, "Export table", str(Path(self.workdir()) / f"{self.current}-{stamp}.csv"),
                "CSV files (*.csv);;All files (*)")
            writer = self.table.export_csv
        else:
            path, _ = QFileDialog.getSaveFileName(
                self.window, "Save log", str(Path(self.workdir()) / f"pytr-{stamp}.log"),
                "Log files (*.log *.txt);;All files (*)")
            writer = lambda p: Path(p).write_text(self.console.toPlainText(), encoding="utf-8")  # noqa: E731
        if path:
            try:
                writer(path)
                self.status.emit(f"Saved {path}")
            except OSError as exc:
                QMessageBox.warning(self.window, "Save failed", str(exc))

    def open_workdir(self):
        path = Path(self.workdir())
        path.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(path)))

    def save(self):
        self.settings.setValue("ui/task_split", self.split.saveState())
        for form in self.forms.values():
            form.save(self.settings)


# ============================================================================
# Settings page
# ============================================================================

THEME_VALUES = ["system", "dark", "light"]            # same order as themeCombo in the .ui
VERBOSITY_VALUES = ["info", "warning", "debug"]       # same order as verbosityCombo


class SettingsPage(QObject):
    theme_changed = Signal(str)
    changed = Signal()

    def __init__(self, ui: SimpleNamespace, settings: QSettings):
        super().__init__(ui.settingsPage)
        self.window: QWidget = ui.MainWindow
        self.settings = settings
        self.theme: QComboBox = ui.themeCombo
        self.workdir: QLineEdit = ui.workdirEdit
        self.verbosity: QComboBox = ui.verbosityCombo
        self.check_session: QCheckBox = ui.checkSessionCheck

        for i, value in enumerate(THEME_VALUES):
            self.theme.setItemData(i, value)
        for i, value in enumerate(VERBOSITY_VALUES):
            self.verbosity.setItemData(i, value)
        self.theme.setCurrentIndex(max(self.theme.findData(settings.value("ui/theme", "system")), 0))
        self.verbosity.setCurrentIndex(max(self.verbosity.findData(settings.value("run/verbosity", "info")), 0))
        self.workdir.setText(settings.value("run/workdir", str(DEFAULT_WORKDIR), type=str))
        self.check_session.setChecked(settings.value("auth/check_on_start", True, type=bool))

        program, prefix = pytr_invocation()
        ui.aboutAppValue.setText(f"{APP_NAME} {APP_VERSION} · by {APP_AUTHOR}")
        ui.aboutPytrValue.setText(package_version("pytr"))
        ui.aboutQtValue.setText(QT_BINDING_VERSION)
        ui.aboutPythonValue.setText(sys.version.split()[0])
        ui.aboutDataValue.setText(str(PYTR_DIR))
        ui.aboutRunnerValue.setText(join_command([program, *prefix]))

        self.theme.currentIndexChanged.connect(lambda _: self.theme_changed.emit(self.theme.currentData()))
        self.verbosity.currentIndexChanged.connect(lambda _: self.changed.emit())
        self.workdir.editingFinished.connect(self.changed.emit)
        ui.browseWorkdirButton.clicked.connect(self._browse)
        ui.openWorkdirButton.clicked.connect(
            lambda: QDesktopServices.openUrl(QUrl.fromLocalFile(self.workdir_path())))
        ui.openDataButton.clicked.connect(self._open_data)

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self.window, "Choose working folder", self.workdir.text())
        if path:
            self.workdir.setText(os.path.normpath(path))
            self.changed.emit()

    def _open_data(self):
        PYTR_DIR.mkdir(parents=True, exist_ok=True)
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(PYTR_DIR)))

    def workdir_path(self) -> str:
        return self.workdir.text().strip() or str(DEFAULT_WORKDIR)

    def verbosity_value(self) -> str:
        return self.verbosity.currentData() or "info"

    def theme_value(self) -> str:
        return self.theme.currentData() or "system"

    def save(self):
        s = self.settings
        s.setValue("ui/theme", self.theme_value())
        s.setValue("run/workdir", self.workdir_path())
        s.setValue("run/verbosity", self.verbosity_value())
        s.setValue("auth/check_on_start", self.check_session.isChecked())


# ============================================================================
# Main window
# ============================================================================

PAGE_ACCOUNT, PAGE_TASK, PAGE_SETTINGS = 0, 1, 2
NAV_ACCOUNT, NAV_SETTINGS = 0, 1          # task buttons use ids >= 100


class MainWindow(QMainWindow):
    def __init__(self, app: QApplication):
        super().__init__()
        self.app = app
        self.ui = load_ui(self)              # layout from assets/main_window.ui
        ui = self.ui
        self.setWindowIcon(QIcon(str(ICON_PATH)))
        ui.brandIcon.setPixmap(QIcon(str(ICON_PATH)).pixmap(60, 60))
        self.setWindowTitle(APP_NAME)
        ui.appTitle.setText(APP_NAME)
        ui.appVersion.setText(f"Version {APP_VERSION}")

        self.settings = QSettings(SETTINGS_ORG, SETTINGS_APP)
        self.tasks = TaskRunner(self)
        self.settings_page = SettingsPage(ui, self.settings)
        self.account_page = AccountPage(ui, self.settings, self.tasks)
        self.task_page = TaskPage(ui, self.settings, self.account_page,
                                  self.settings_page.workdir_path, self.settings_page.verbosity_value)
        self.task_names = list(self.task_page.specs)

        self.apply_theme(self.settings_page.theme_value())
        self.settings_page.theme_changed.connect(self.apply_theme)
        self.settings_page.changed.connect(self.task_page.update_preview)
        self.app.styleHints().colorSchemeChanged.connect(
            lambda _: self.settings_page.theme_value() == "system" and self.apply_theme("system"))

        self._build_navigation()

        self.clock = QLabel()
        self.statusBar().addPermanentWidget(self.clock)
        self.clock_timer = QTimer(self, interval=1000, timeout=self._tick)
        self.clock_timer.start()
        self._tick()

        self.account_page.session_changed.connect(self._on_session)
        self.account_page.log.connect(lambda text, kind: self.task_page.console.line(text, kind))
        self.task_page.status.connect(lambda msg: self.statusBar().showMessage(msg, 8000))
        self.task_page.go_account.connect(lambda: self.navigate(NAV_ACCOUNT))
        self.task_page.running_changed.connect(self._on_running)

        self.log_handler = QtLogHandler()
        self.log_handler.bridge.message.connect(self._on_pytr_log)
        logging.getLogger("api").addHandler(self.log_handler)

        QShortcut(QKeySequence("Ctrl+Return"), self, activated=self._run_shortcut)
        QShortcut(QKeySequence("Ctrl+Enter"), self, activated=self._run_shortcut)
        QShortcut(QKeySequence("Escape"), self, activated=self.task_page.stop)
        QShortcut(QKeySequence("Ctrl+L"), self, activated=self.task_page.clear_view)

        geometry = self.settings.value("ui/geometry")
        if geometry:
            self.restoreGeometry(geometry)
        default_nav = 100 if read_stored_credentials() and self.task_names else NAV_ACCOUNT
        self.navigate(self.settings.value("ui/nav", default_nav, type=int))
        self.task_page._on_session("idle", "Signed out")

        self.task_page.console.line(f"{APP_NAME} {APP_VERSION} · pytr {package_version('pytr')}", "sys")
        if self.settings_page.check_session.isChecked():
            QTimer.singleShot(300, self.account_page.check_session_on_startup)

    # -- navigation ---------------------------------------------------------------
    def _build_navigation(self):
        ui = self.ui
        self.session_pill = Pill(ui.sessionPill)
        self.nav = QButtonGroup(self)
        self.nav.setExclusive(True)
        self.nav.addButton(ui.navAccount, NAV_ACCOUNT)
        self.nav.addButton(ui.navSettings, NAV_SETTINGS)
        # Task buttons depend on the installed pytr version -> created here.
        layout = ui.navTasks.layout()
        for i, name in enumerate(self.task_names):
            spec = self.task_page.specs[name]
            btn = QPushButton(spec.title)
            btn.setProperty("nav", True)
            btn.setCheckable(True)
            btn.setCursor(Qt.PointingHandCursor)
            btn.setToolTip(f"pytr {name}\n{spec.summary}")
            self.nav.addButton(btn, 100 + i)
            layout.addWidget(btn)
        self.nav.idClicked.connect(self.navigate)

    def navigate(self, nav_id: int):
        if nav_id >= 100 and nav_id - 100 >= len(self.task_names):
            nav_id = NAV_ACCOUNT
        btn = self.nav.button(nav_id)
        if btn:
            btn.setChecked(True)
        pages = self.ui.pages
        if nav_id == NAV_ACCOUNT:
            pages.setCurrentIndex(PAGE_ACCOUNT)
        elif nav_id == NAV_SETTINGS:
            pages.setCurrentIndex(PAGE_SETTINGS)
        else:
            self.task_page.select(self.task_names[nav_id - 100])
            pages.setCurrentIndex(PAGE_TASK)
        self._nav_id = nav_id

    # -- slots ------------------------------------------------------------------------
    def apply_theme(self, mode: str):
        variables = apply_theme(self.app, mode)
        self.task_page.console.set_colors(variables)

    def _tick(self):
        self.clock.setText(time.strftime("%H:%M:%S  ·  %d.%m.%Y"))

    def _on_session(self, state: str, text: str):
        self.session_pill.set(text, state)
        self.statusBar().showMessage(f"Session: {text}", 5000)

    def _on_running(self, running: bool):
        # Switching tasks while one runs would hide its form – lock task navigation.
        for nav_id in range(100, 100 + len(self.task_names)):
            btn = self.nav.button(nav_id)
            btn.setEnabled(not running or btn.isChecked())

    def _on_pytr_log(self, text: str, level: int):
        kind = "error" if level >= logging.ERROR else "warn" if level >= logging.WARNING else "log"
        self.task_page.console.line(text, kind)

    def _run_shortcut(self):
        if self.ui.pages.currentIndex() == PAGE_TASK:
            self.task_page.run()

    def closeEvent(self, event: QCloseEvent):  # noqa: N802 - Qt override
        if self.task_page.is_running():
            answer = QMessageBox.question(self, "Task running",
                                          "A pytr task is still running. Stop it and quit?")
            if answer != QMessageBox.Yes:
                event.ignore()
                return
            self.task_page.kill_and_wait()
        logging.getLogger("api").removeHandler(self.log_handler)
        self.settings.setValue("ui/geometry", self.saveGeometry())
        self.settings.setValue("ui/nav", getattr(self, "_nav_id", NAV_ACCOUNT))
        self.settings_page.save()
        self.account_page.save()
        self.task_page.save()
        self.settings.sync()
        super().closeEvent(event)


# ============================================================================
# Entry point
# ============================================================================

def main() -> int:
    # Frozen builds run pytr through the app executable itself: `app.exe --pytr-cli <args>`.
    if sys.argv[1:2] == ["--pytr-cli"]:
        if pytr_main is None:
            print(f"pytr is not available: {PYTR_IMPORT_ERROR}", file=sys.stderr)
            return 1
        sys.argv = ["pytr", *sys.argv[2:]]
        return pytr_main.main() or 0

    if os.name == "nt":  # own taskbar icon instead of the Python one
        try:
            import ctypes
            ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("jovan.traderepublichelper")
        except (AttributeError, OSError):
            pass

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setOrganizationName(SETTINGS_ORG)
    app.setStyle("Fusion")
    app.setWindowIcon(QIcon(str(ICON_PATH)))

    window = MainWindow(app)
    window.show()
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
