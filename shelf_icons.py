"""Themed command and heading icons for Tk."""

from functools import lru_cache
from pathlib import Path
import re
import struct
import tkinter as tk
from zlib import crc32


NAMES = ("eye", "updates", "problems", "search", "add", "check",
         "download", "verify", "edit", "reload", "more", "close", "settings", "collapse", "expand",
         "system", "light", "dark", "language", "picture_frame", "hourglass", "save", "cancel",
         "window_close", "info", "folders", "list_checks", "diskette_x", "pause_circle", "folder",
         "folder_open", "tag", "file_x", "folder_x", "headphones", "code_xml", "monitor",
         "refresh_cw", "hard_drive", "gamepad", "cpu", "globe", "disc", "layers", "briefcase",
         "shield", "wrench", "package_minus", "paintbrush", "inbox", "tags",
         "list_chevron_down", "list_chevron_up")

CATEGORY_SYMBOLS = {
    "audio": "headphones", "video": "headphones", "development": "code_xml",
    "display": "monitor", "hdr": "monitor", "drivers": "refresh_cw", "updates": "refresh_cw",
    "files": "hard_drive", "storage": "hard_drive", "gaming": "gamepad", "games": "gamepad",
    "hardware": "cpu", "internet": "globe", "networking": "globe", "recovery": "disc",
    "setup": "layers", "virtualization": "layers", "productivity": "briefcase",
    "security": "shield", "utilities": "wrench", "tools": "wrench",
    "uninstallers": "package_minus", "customization": "paintbrush",
}


def category_icon(category):
    if not category:
        return "inbox"
    for word in re.findall(r"[^\W_]+", category.casefold()):
        if word in CATEGORY_SYMBOLS:
            return CATEGORY_SYMBOLS[word]
    return "folder"


@lru_cache(maxsize=128)
def tinted_strip(filename, color):
    # The indexed PNG stores line coverage in its transparency table. Tinting
    # its single palette preserves antialiasing against any background.
    data = (Path(__file__).parent / "assets" / filename).read_bytes()
    start = data.index(b"PLTE")
    length = struct.unpack_from(">I", data, start - 4)[0]
    palette = b"PLTE" + bytes.fromhex(color.removeprefix("#")) * (length // 3)
    checksum = struct.pack(">I", crc32(palette))
    return data[:start] + palette + checksum + data[start + 4 + length + 4:]


def make_icon(master, name, color, size=16):
    y, cell_width = {16: (0, 20), 20: (16, 20), 28: (36, 28)}[size]
    # Each tile is aligned to both cell widths, so no icon crosses a tile boundary.
    tile, x = divmod(NAMES.index(name) * cell_width, 140)
    root = master._root()
    if not hasattr(root, "_command_tiles"):
        root._command_tiles = {}
    key = (tile, color)
    if key not in root._command_tiles:
        atlas = tinted_strip(f"commands/{tile}.png", color)
        root._command_tiles[key] = tk.PhotoImage(master=root, data=atlas, format="png")
    strip = root._command_tiles[key]
    icon = tk.PhotoImage(master=master, width=size, height=size)
    icon.tk.call(str(icon), "copy", str(strip), "-from", x, y, x + size, y + size)
    return icon


def set_button_icon(button, name, color, disabled_color, size=16, *, list_chevron=None):
    normal = make_icon(button, name, color, size)
    disabled = make_icon(button, name, disabled_color, size)
    if list_chevron is not None:
        # Center just the chevron from the list icon below the main icon.
        for image, tint in ((normal, color), (disabled, disabled_color)):
            chevron = make_icon(button, list_chevron, tint, 20)
            image.configure(height=size + 6)
            image.tk.call(str(image), "copy", str(chevron),
                          "-from", 9, 7, 18, 13, "-to", (size - 9) // 2, size)
    button.configure(image=(normal, "disabled", disabled))
    return normal, disabled
