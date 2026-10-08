"""
Shared dark gray ttk theme for the scanner controller (main.py) and the
post-processing editor (editor.py), so both windows look consistent.
"""

COLOR_BG = "#2b2b2b"
COLOR_BG_TROUGH = "#1e1e1e"
COLOR_FIELD_BG = "#3c3c3c"
COLOR_FG = "#e5e5e5"
COLOR_FG_DISABLED = "#888888"
COLOR_ACCENT = "#6ee7b7"


def apply_dark_theme(root):
    from tkinter import ttk

    root.configure(bg=COLOR_BG)

    # 'clam' is the only built-in ttk theme that honors arbitrary color
    # overrides on Windows; the native 'vista' theme ignores most of them
    # since it draws widgets through the OS theming engine.
    style = ttk.Style(root)
    style.theme_use("clam")

    style.configure("TFrame", background=COLOR_BG)
    style.configure("TLabel", background=COLOR_BG, foreground=COLOR_FG)
    style.configure(
        "TButton", background=COLOR_FIELD_BG, foreground=COLOR_FG,
        borderwidth=1, focusthickness=0,
    )
    style.map(
        "TButton",
        background=[("active", COLOR_ACCENT), ("disabled", COLOR_BG)],
        foreground=[("active", "#1a1a1a"), ("disabled", COLOR_FG_DISABLED)],
    )
    style.configure("TCheckbutton", background=COLOR_BG, foreground=COLOR_FG)
    style.map(
        "TCheckbutton",
        background=[("active", COLOR_BG)],
        indicatorcolor=[("selected", COLOR_ACCENT)],
    )
    style.configure("TRadiobutton", background=COLOR_BG, foreground=COLOR_FG)
    style.map(
        "TRadiobutton",
        background=[("active", COLOR_BG)],
        indicatorcolor=[("selected", COLOR_ACCENT)],
    )
    style.configure(
        "TCombobox", fieldbackground=COLOR_FIELD_BG, background=COLOR_FIELD_BG,
        foreground=COLOR_FG, arrowcolor=COLOR_FG,
    )
    style.map(
        "TCombobox",
        fieldbackground=[("readonly", COLOR_FIELD_BG)],
        foreground=[("readonly", COLOR_FG)],
    )
    style.configure(
        "TEntry", fieldbackground=COLOR_FIELD_BG, foreground=COLOR_FG,
        insertcolor=COLOR_FG,
    )
    style.map("TEntry", fieldbackground=[("disabled", COLOR_BG)])
    style.configure(
        "Horizontal.TProgressbar", troughcolor=COLOR_BG_TROUGH,
        background=COLOR_ACCENT, bordercolor=COLOR_BG_TROUGH,
        lightcolor=COLOR_ACCENT, darkcolor=COLOR_ACCENT,
    )
    style.configure("TScale", background=COLOR_BG, troughcolor=COLOR_FIELD_BG)
    style.configure("Horizontal.TScale", background=COLOR_BG, troughcolor=COLOR_FIELD_BG)
    style.configure("TPanedwindow", background=COLOR_BG)
    style.configure("TScrollbar", background=COLOR_FIELD_BG, troughcolor=COLOR_BG_TROUGH, arrowcolor=COLOR_FG)
    style.configure("TLabelframe", background=COLOR_BG, foreground=COLOR_FG, bordercolor=COLOR_FIELD_BG)
    style.configure("TLabelframe.Label", background=COLOR_BG, foreground=COLOR_FG)

    # The combobox dropdown list is a separate Tk listbox, not a ttk widget,
    # so it needs its colors set directly via option_add.
    root.option_add("*TCombobox*Listbox.background", COLOR_FIELD_BG)
    root.option_add("*TCombobox*Listbox.foreground", COLOR_FG)
    root.option_add("*TCombobox*Listbox.selectBackground", COLOR_ACCENT)
    root.option_add("*TCombobox*Listbox.selectForeground", "#1a1a1a")

    # Plain (non-ttk) widgets such as Listbox/Canvas don't follow ttk styles
    # at all, so callers set their colors directly using these constants.
    return style
