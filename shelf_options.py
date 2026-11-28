"""Settings for the Relf desktop app."""

from pathlib import Path
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from releaseshelf import (app_folder_preference_path, load_targets, previous_versions_preference_path,
                          read_destination, validate_app_folder)
from shelf_i18n import LANGUAGES, tr
from shelf_icons import make_icon
from shelf_language_menu import LanguageMenu
from shelf_message import error_detail
from shelf_paint import stage_window
from shelf_scroll import grid_scrollable
from shelf_text import FittedLabel
from shelf_theme import PALETTES, THEME_MODES, initial_geometry, style_titlebar, write_preference


def read_language(path):
    try:
        code = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeError):
        return "en"
    return code if code in LANGUAGES else "en"


class OptionsWindow(tk.Toplevel):
    def __init__(self, parent, mode, palette_name, on_change, palette, *, language, on_language_change,
                 show_app_icons, on_app_icons_change, destination, destination_file, app_folder,
                 previous_versions, on_previous_versions_change):
        super().__init__(parent)
        stage_window(self)
        self.transient(parent)
        self.resizable(False, False)
        self.palette = palette
        self.mode_buttons = {}
        self.mode_images = {}
        self.palette_buttons = {}
        self.swatches = {}
        panel = ttk.Frame(self)
        self.viewport = tk.Canvas(panel, highlightthickness=0, borderwidth=0, takefocus=False)
        grid_scrollable(self.viewport)
        self.body = body = ttk.Frame(self.viewport, padding=(20, 16, 20, 0))
        scroll_window = self.viewport.create_window(0, 0, window=body, anchor="nw")
        self.viewport.bind("<Configure>", lambda event: self.viewport.itemconfigure(scroll_window, width=event.width))
        body.bind("<Configure>", lambda _: self.viewport.configure(scrollregion=self.viewport.bbox(scroll_window)))
        self.bind("<MouseWheel>", lambda event: self.viewport.yview_scroll(-event.delta // 120, "units"))
        self.bind("<FocusIn>", self.show_focused_control)
        heading_font = ("Segoe UI Semibold", 11)

        self.language_heading = ttk.Label(body, font=heading_font)
        self.language_heading.pack(anchor="w")
        self.language_button = ttk.Button(body, style="LanguagePicker.TMenubutton", compound="left")
        self.language_button.pack(fill="x", pady=(6, 16))
        self.language_menu = LanguageMenu(self.language_button, on_language_change)

        self.destination = destination
        self.destination_file = destination_file
        try:
            folder = (read_destination(destination_file) or destination).resolve()
        except (OSError, ValueError):
            folder = destination
        self.download_folder = folder
        self.download_heading = ttk.Label(body, font=heading_font)
        self.download_heading.pack(anchor="w")
        downloads = ttk.Frame(body)
        downloads.pack(fill="x", pady=(6, 12))
        downloads.columnconfigure(0, weight=1)
        self.download_path = FittedLabel(downloads, anchor="w", takefocus=False)
        self.download_path.grid(row=0, column=0, sticky="nsew")
        self.download_path.set_text(str(folder))
        self.browse_button = ttk.Button(downloads, command=self.choose_download_folder)
        self.browse_button.grid(row=0, column=1, padx=(6, 0), sticky="e")
        self.download_hint = ttk.Label(downloads, wraplength=390, style="Muted.TLabel", font=("Segoe UI", 9))
        self.download_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.app_folder = app_folder
        self.app_folder_file = app_folder_preference_path()
        try:
            self.selected_app_folder = read_destination(self.app_folder_file) or app_folder
        except (OSError, ValueError):
            self.selected_app_folder = app_folder
        self.app_folder_heading = ttk.Label(body, font=heading_font)
        self.app_folder_heading.pack(anchor="w")
        app_location = ttk.Frame(body)
        app_location.pack(fill="x", pady=(6, 12))
        app_location.columnconfigure(0, weight=1)
        self.app_folder_path = FittedLabel(app_location, anchor="w", takefocus=False)
        self.app_folder_path.grid(row=0, column=0, sticky="ew")
        self.app_folder_browse = ttk.Button(app_location, command=self.choose_app_folder)
        self.app_folder_browse.grid(row=0, column=1, padx=(6, 0))
        self.app_folder_hint = ttk.Label(app_location, wraplength=390, style="Muted.TLabel", font=("Segoe UI", 9))
        self.app_folder_hint.grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

        self.on_previous_versions_change = on_previous_versions_change
        self.previous_versions = tk.StringVar(self, value=str(previous_versions))
        retention = ttk.Frame(body)
        retention.pack(fill="x", pady=(0, 12))
        retention.columnconfigure(0, weight=1)
        self.previous_versions_label = ttk.Label(retention)
        self.previous_versions_label.grid(row=0, column=0, sticky="w")
        self.previous_versions_entry = ttk.Spinbox(retention, from_=0, to=float("inf"), width=5,
                                                  textvariable=self.previous_versions)
        self.previous_versions_entry.grid(row=0, column=1, padx=6)
        self.previous_versions_entry.bind("<Return>", lambda _: self.save_previous_versions())
        self.save_previous_versions_button = ttk.Button(retention, command=self.save_previous_versions)
        self.save_previous_versions_button.grid(row=0, column=2)

        self.appearance_heading = ttk.Label(body, font=heading_font)
        self.appearance_heading.pack(anchor="w")
        modes = ttk.Frame(body, style="Line.TFrame", padding=1)
        modes.pack(fill="x", pady=(6, 8))
        for column, name in enumerate(THEME_MODES):
            modes.columnconfigure(column, weight=1, uniform="mode")
            button = ttk.Radiobutton(modes, value=name, variable=mode, command=on_change,
                                     style="Theme.Toolbutton", compound="left")
            button.grid(row=0, column=column, sticky="ew")
            self.mode_buttons[name] = button

        self.app_icons_button = ttk.Checkbutton(body, variable=show_app_icons, command=on_app_icons_change,
                                               style="Options.TCheckbutton")
        self.app_icons_button.pack(anchor="w", pady=(0, 12))

        self.palette_heading = ttk.Label(body, font=heading_font)
        self.palette_heading.pack(anchor="w", pady=(0, 5))
        choices = ttk.Frame(body)
        choices.pack(fill="x")
        for column in (0, 1):
            choices.columnconfigure(column, weight=1, uniform="palette")
        for index, name in enumerate(PALETTES):
            swatch = tk.PhotoImage(master=self, width=46, height=24)
            self.swatches[name] = swatch
            button = ttk.Radiobutton(choices, image=swatch, compound="left",
                                     variable=palette_name, value=name, command=on_change,
                                     style="Palette.Theme.Toolbutton")
            row, column = divmod(index, 2)
            button.grid(row=row, column=column, sticky="ew", padx=(0, 6) if column == 0 else 0, pady=3)
            self.palette_buttons[name] = button

        footer = ttk.Frame(self, padding=(20, 14, 20, 16))
        self.close_button = ttk.Button(footer, compound="left", command=self.destroy)
        self.close_button.pack(anchor="e")
        footer.pack(side="bottom", fill="x")
        panel.pack(fill="both", expand=True)
        self.bind("<Escape>", lambda _: self.destroy())
        self.bind("<Map>", lambda event: style_titlebar(self, self.palette.is_dark)
                  if event.widget is self else None)
        self.apply_language(language)
        self.apply_theme(palette)
        self.fit_controls()
        self.grab_set()
        self.mode_buttons[mode.get()].focus_set()

    def fit_controls(self):
        self.update_idletasks()
        self.viewport.configure(width=self.body.winfo_reqwidth(), height=self.body.winfo_reqheight())
        initial_geometry(self, max(460, self.winfo_width()), 0, parent=self.master)

    def show_focused_control(self, event):
        if not str(event.widget).startswith(str(self.body) + "."):
            return
        top = self.viewport.canvasy(0)
        height = self.viewport.winfo_height()
        y = event.widget.winfo_rooty() - self.body.winfo_rooty()
        bottom = y + event.widget.winfo_height()
        if y < top:
            self.viewport.yview_moveto(y / self.body.winfo_height())
        elif bottom > top + height:
            self.viewport.yview_moveto((bottom - height) / self.body.winfo_height())

    def apply_language(self, code):
        self.title(tr("Options"))
        self.language_heading.configure(text=tr("Language"))
        self.language_button.configure(text=LANGUAGES[code][0])
        self.language_menu.select(code)
        self.download_heading.configure(text=tr("Download folder"))
        self.browse_button.configure(text=tr("Browse…"))
        self.app_folder_heading.configure(text=tr("App folder"))
        self.app_folder_browse.configure(text=tr("Browse…"))
        self.previous_versions_label.configure(text=tr("Previous versions to keep per app"))
        self.save_previous_versions_button.configure(text=tr("Save"))
        self.update_download_hint()
        self.appearance_heading.configure(text=tr("Appearance"))
        self.app_icons_button.configure(text=tr("Show item icons"))
        self.palette_heading.configure(text=tr("Palette"))
        self.close_button.configure(text=tr("Close"))
        for name, button in (*self.mode_buttons.items(), *self.palette_buttons.items()):
            button.configure(text=tr(name))
        if self.winfo_ismapped():
            self.fit_controls()

    def choose_download_folder(self):
        folder = filedialog.askdirectory(parent=self, title=tr("Download folder"),
                                         initialdir=str(self.download_folder), mustexist=True)
        if not folder:
            return
        try:
            destination = Path(folder).resolve(strict=True)
            if self.selected_app_folder is not None:
                validate_app_folder(self.selected_app_folder, destination)
            load_targets(destination / "targets.json", destination)
            write_preference(self.destination_file, f"{destination}\n")
        except (OSError, TypeError, ValueError) as exc:
            messagebox.showerror(tr("Download folder"), tr("Could not save preference: {error}",
                                                           error=error_detail(exc)), parent=self)
            return
        self.download_folder = destination
        self.download_path.set_text(str(destination))
        self.update_download_hint()

    def choose_app_folder(self):
        folder = filedialog.askdirectory(parent=self, title=tr("App folder"),
                                         initialdir=str(self.selected_app_folder or Path.home()), mustexist=True)
        if not folder:
            return
        try:
            app_folder = validate_app_folder(Path(folder).resolve(strict=True), self.download_folder)
            write_preference(self.app_folder_file, f"{app_folder}\n")
        except (OSError, ValueError) as exc:
            messagebox.showerror(tr("App folder"), tr("Could not save preference: {error}",
                                                     error=error_detail(exc)), parent=self)
            return
        self.selected_app_folder = app_folder
        self.update_download_hint()

    def save_previous_versions(self):
        title = tr("Previous versions to keep per app")
        try:
            count = int(self.previous_versions.get())
            if count < 0:
                raise ValueError
        except ValueError:
            messagebox.showerror(title, tr("Enter a non-negative number of previous versions."), parent=self)
            return
        try:
            write_preference(previous_versions_preference_path(), f"{count}\n")
        except OSError as exc:
            messagebox.showerror(title, tr("Could not save preference: {error}", error=error_detail(exc)), parent=self)
            return
        self.previous_versions.set(str(count))
        self.on_previous_versions_change(count)

    def update_download_hint(self):
        pending = self.download_folder != self.destination
        if pending:
            self.download_hint.configure(text=tr("Restart Relf to use the selected folder."))
            self.download_hint.grid()
        else:
            self.download_hint.grid_remove()
        self.app_folder_path.set_text(str(self.selected_app_folder) if self.selected_app_folder is not None
                                      else tr("Use download folder"))
        if self.selected_app_folder != self.app_folder:
            self.app_folder_hint.configure(text=tr("Restart Relf to use the selected folder."))
            self.app_folder_hint.grid()
        else:
            self.app_folder_hint.grid_remove()
        if self.winfo_ismapped():
            self.fit_controls()

    def apply_theme(self, palette):
        self.palette = palette
        self.configure(background=palette.canvas)
        self.viewport.configure(background=palette.canvas)
        ttk.Style(self).configure("TSpinbox", fieldbackground=palette.surface, foreground=palette.text,
                                  background=palette.surface, arrowcolor=palette.muted, insertcolor=palette.text,
                                  bordercolor=palette.line, lightcolor=palette.surface, darkcolor=palette.surface,
                                  selectbackground=palette.text, selectforeground=palette.surface, padding=4)
        self.close_icon = make_icon(self, "cancel", palette.text)
        self.close_button.configure(image=self.close_icon)
        self.language_icon = make_icon(self, "language", palette.accent)
        self.language_button.configure(image=self.language_icon)
        self.language_menu.apply_theme(palette)
        mode = "Dark" if palette.is_dark else "Light"
        for name, swatch in self.swatches.items():
            colors = PALETTES[name][mode]
            swatch.put(colors.line, to=(0, 0, 46, 24))
            swatch.put(colors.canvas, to=(1, 1, 45, 23))
            swatch.put(colors.surface, to=(3, 3, 43, 21))
            swatch.put(colors.accent, to=(5, 5, 20, 10))
            swatch.put(colors.subtle, to=(22, 5, 35, 10))
            swatch.put(colors.selection, to=(3, 13, 43, 18))
        for name, button in self.mode_buttons.items():
            icon_name = name.lower()
            normal = make_icon(self, icon_name, palette.muted)
            selected = make_icon(self, icon_name, palette.on_accent)
            active = make_icon(self, icon_name, palette.text)
            button.configure(image=(normal, "selected", selected, "active", active))
            self.mode_images[name] = (normal, selected, active)
        if self.winfo_ismapped():
            style_titlebar(self, palette.is_dark)
