"""The app's colours and widget stylesheets, in one place.

Redesign prep: every ``setStyleSheet`` string and UI colour literal
lives here so a restyle edits one file. Values were lifted verbatim
from the call sites — several greys/darks are near-duplicates kept
distinct on purpose (byte-identical migration); merge them when the
redesign picks the final palette.

Layout: a small palette of cross-cutting tokens first (colours that
code needs directly, or that many styles share), then style strings
grouped by component. Dynamic styles are functions.
"""

# ---------------------------------------------------------------- palette

TEXT = "#d7dde2"          # primary light text
TEXT_MUTED = "#9aa0a6"    # captions, version labels
TEXT_DIM = "#888888"      # hints, disabled-ish labels
TEXT_FAINT = "#7f8c8d"    # smallest annotations (card keys, dialog hints)

SUCCESS = "#2e7d32"        # sync-done button
SUCCESS_BRIGHT = "#2ecc71" # lock-on glyph, buffer "add" events
DANGER = "#ff4d4f"         # lock-off glyph
WARNING_BORDER = "#cc8800" # invalid input field

LCD_GREEN = "#00ff66"

# Fleetwide legend / graph accents (also used inline in rich text)
LEGEND_OPERATION = "#e74c3c"
LEGEND_STARTUP = "#f39c12"

# Stop-report category accents (blended with the widget palette)
STOP_ACCENT_ESTOP = "#c85c5c"
STOP_ACCENT_CAUTION = "#c98732"
STOP_ACCENT_OPERATOR = "#4b7fc7"

# Target-buffer card palette
CARD_BG = "#1e2630"
CARD_BG_ALT = "#243040"    # odd product_id — slightly lighter
CARD_BORDER = "#2c3e50"
HEADER_BG = "#111820"
CARD_INVALID_BG = "#2a1a1a"
CARD_INVALID_BORDER = "#5c2020"
GAP_CLOSE_BORDER = "#f1c40f"
GAP_CLOSE_BG_ODD = "#3a3314"
GAP_CLOSE_BG_EVEN = "#332d12"
GAP_WIDE_BORDER = "#4aa3ff"
GAP_WIDE_BG_ODD = "#152d45"
GAP_WIDE_BG_EVEN = "#13283d"

# ------------------------------------------------------------ shared roles

MUTED_LABEL = f"color: {TEXT_MUTED};"
DIM_LABEL = f"color: {TEXT_DIM};"
HINT_LABEL = f"color: {TEXT_FAINT}; font-size: 10px;"
TITLE_LABEL = "font-size: 22px; font-weight: bold;"
ERROR_LABEL = "color: #ff8a80;"
PRIMARY_ACTION_BUTTON = "font-weight: bold; padding: 6px;"
MONO_VALUE_LABEL = f"font-family: monospace; color: {TEXT};"

LCD_DISPLAY = f"QLCDNumber {{ background-color: #000000; color: {LCD_GREEN}; }}"

SLIDER_CAPTION = f"color: {TEXT_MUTED}; font-size: 10px;"
SLIDER_VALUE = f"color: {TEXT}; font-family: Consolas, monospace; font-size: 10px;"

SYNC_DONE_BUTTON = f"background-color: {SUCCESS}; color: white;"
LOCK_ON_LABEL = f"color: {SUCCESS_BRIGHT};"
LOCK_OFF_LABEL = f"color: {DANGER};"
INPUT_WARNING_BORDER = f"border: 1px solid {WARNING_BORDER};"


def solid_button(color_name: str) -> str:
    """Colour-swatch button (annotation colour picker)."""
    return f"background-color: {color_name};"


# ----------------------------------------------------------------- viewer

LOG_LIST = """
    QListView::item:selected {
        background-color: #cc2222;
        color: white;
    }
    QListView::item:selected:!active {
        background-color: #882222;
        color: white;
    }
"""

PIN_BUTTON = (
    "QPushButton { border: none; background: transparent; font-size: 13px; }"
    "QPushButton:checked { background: rgba(255,255,255,30); border-radius: 3px; }"
)

# ------------------------------------------------------------ main window

TOP_BAR_LABEL = f"color: {TEXT}; padding-left: 8px;"

SHUTDOWN_POPUP = (
    f"QWidget {{ background-color: #10151a; color: {TEXT};"
    " border: 1px solid #4a5560; font-size: 12px; }"
    "QProgressBar { border: 1px solid #31414d; background: #0f1419;"
    " height: 12px; text-align: center; }"
    "QProgressBar::chunk { background-color: #5e9bff; }"
)
POPUP_TITLE = "border: none; font-weight: bold;"
POPUP_STEP = f"border: none; color: {TEXT_MUTED};"

# ------------------------------------------------------------- date picker

SIM_BUTTON = (
    "QPushButton { padding: 2px 6px; } "
    "QPushButton:checked { "
    "  background-color: #ffe7ba; "
    "  border: 1px solid #ffb84d; "
    "  color: #5a2a00; "
    "}"
)

CUSTOMER_HEADER_BUTTON = (
    "QPushButton { "
    f"text-align: left; padding: 6px 8px; color: {TEXT}; font-weight: bold; "
    "background: #202a31; border: 1px solid #31414d; } "
    "QPushButton:hover { background: #25313a; }"
)

SYSTEM_BUTTON = (
    "QPushButton { "
    "  padding: 2px 6px; text-align: left; "
    "  background-color: #2b2b2b; color: #f2f4f7; "
    "  border: 1px solid #4a4f55; border-right: 0px; "
    "  border-top-left-radius: 6px; border-bottom-left-radius: 6px; "
    "  border-top-right-radius: 0px; border-bottom-right-radius: 0px; "
    "} "
    "QPushButton:hover { background-color: #343941; } "
    "QPushButton:checked { background-color: #cce5ff; border: 1px solid #5b8def; border-right: 0px; color: #0b1a33; }"
)

TODAY_BUTTON = (
    "QPushButton { "
    "  padding: 0px; font-weight: bold; "
    "  background-color: #3d434a; color: #f2f4f7; "
    "  border: 1px solid #4a4f55; border-left: 1px solid #59616a; "
    "  border-top-left-radius: 0px; border-bottom-left-radius: 0px; "
    "  border-top-right-radius: 6px; border-bottom-right-radius: 6px; "
    "} "
    "QPushButton:hover { background-color: #4a525b; } "
    "QPushButton:pressed { background-color: #5b6470; }"
)

# --------------------------------------------------------------- overview

OVERVIEW_STATUS = "color: #cfcfcf;"
PANEL_SURFACE = "background: #11161a; border: 1px solid #28323a;"
PANEL_BG = "background: #11161a;"
HOVER_PREVIEW = "background: #0f1419; border: 1px solid #31414d; padding: 4px;"
LOADING_BADGE = (
    "background: rgba(9, 13, 17, 180);"
    "color: #e7edf3;"
    "padding: 10px 14px;"
    "border: 1px solid #31414d;"
    "border-radius: 6px;"
    "font-size: 14px;"
    "font-weight: 600;"
)

# -------------------------------------------------------------- fleetwide

FLEETWIDE_CARD = "QFrame#fleetwideSystemCard { background: #1b232b; border: 1px solid #34414c; border-radius: 8px; }"
FLEETWIDE_MUTED = "color: #9aa6b2;"
FLEETWIDE_NOTE = "color: #8f9aa3;"
FLEETWIDE_EMPTY = "color: #9aa6b2; padding: 30px;"

# ------------------------------------------------------------ stop report

MEDIA_HOLDER = "background: #000000; border-radius: 8px;"
THUMB_BUTTON = (
    "QPushButton { border: none; border-radius: 8px; background: transparent; }"
    "QPushButton:disabled { color: #d9d9d9; background-color: #3a3a3a; }"
)


def report_row_style(bg_name: str, border_name: str) -> str:
    """Stop-report entry row, colours blended per category at runtime."""
    return f"background-color: {bg_name}; border: 1px solid {border_name}; border-radius: 6px;"


# ---------------------------------------------------------- target buffer

VALUE_LABEL = "color: #ecf0f1; font-size: 10px;"
CARD_TITLE = "color: #ecf0f1; font-weight: bold; font-size: 11px;"
CARD_TIME = f"color: {TEXT_FAINT}; font-size: 10px; margin-left: 4px;"
CHEVRON = "color: #4a6070; font-size: 8px;"
SEPARATOR = f"color: {CARD_BORDER};"
EMPTY_NOTE_INLINE = "color:#566573;font-size:10px;font-style:italic;"
EMPTY_STATE = "color: #566573; font-size: 11px; font-style: italic; padding: 16px;"
NO_DATA_STATE = "color: #3d4f5c; font-size: 11px; font-style: italic; padding: 16px;"
BUFFER_HEADER = (
    f"background: {HEADER_BG}; color: {TEXT}; font-weight: bold; "
    "padding: 6px 8px; font-size: 12px;"
)
BUFFER_SCROLL = "QScrollArea { background: #161d25; border: none; }"
BUFFER_BG = "background: #161d25;"


def target_card_style(bg: str, border: str) -> str:
    """Target card frame; bg/border picked from validity + gap status."""
    return (
        f"QFrame {{ background: {bg}; border: 1px solid {border}; "
        "border-radius: 4px; margin: 2px 4px; }"
    )


# ------------------------------------------------------- dialogs / pages

ABOUT_PAGE = (
    "QDialog { background: #12181f; }"
    "QLabel { color: #e8eef4; }"
    "QTabWidget::pane { border: 1px solid #2c3946; background: #12181f; }"
    "QTabBar::tab { background: #1a222b; color: #9fb0c0; padding: 6px 16px; }"
    "QTabBar::tab:selected { background: #24435f; color: #e8eef4; }"
    "QPushButton { background: #24435f; color: #e8eef4; border: 1px solid #5b9bd5;"
    " border-radius: 4px; padding: 5px 18px; }"
    "QScrollArea { border: none; background: #12181f; }"
    "QTextBrowser { background: #12181f; border: none; }"
)
ABOUT_MUTED = "color: #9fb0c0;"

LOGO_PREVIEW = f"border: 1px solid #3a4650; background: #11161a; color: {TEXT_MUTED};"
