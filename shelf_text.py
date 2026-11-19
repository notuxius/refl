"""Fit single-line display text to the space allocated by Tk."""

from functools import lru_cache, partial
from tkinter import ttk


TREE_TEXT_PAD = 10
# Tk 8.6 DrawCells insets the cell by four pixels per side before applying the theme.
TK_CELL_INSETS = 8
CELL_TEXT_INSETS = 2 * TREE_TEXT_PAD + TK_CELL_INSETS


def _text_width(tk, display, font, value):
    return tk.getint(tk.call("font", "measure", font, "-displayof", display, value))


def fit_text(widget, font, text, width, suffix="", *, measure=None):
    if measure is None:
        measure = partial(_text_width, widget.tk, widget._w)

    def fits(value):
        return measure(font, value) <= width

    line, newline, _ = text.replace("\r\n", "\n").replace("\r", "\n").partition("\n")
    if not newline and fits(line + suffix):
        return line + suffix
    ending = "..." + suffix
    if not fits(ending):
        ending = "..."
    if not fits(ending):
        return next((dots for dots in ("..", ".") if fits(dots)), "")
    low, high = 0, len(line)
    while low < high:
        middle = (low + high + 1) // 2
        if fits(line[:middle] + ending):
            low = middle
        else:
            high = middle - 1
    return line[:low] + ending


class FullTextTooltip:
    def __init__(self, widget, text_at):
        self.widget = widget
        self.text_at = text_at
        self.pending = None
        self.label = None
        widget.bind("<Motion>", self.motion, add="+")
        for event in ("<Leave>", "<ButtonPress>", "<MouseWheel>", "<Unmap>", "<Destroy>"):
            widget.bind(event, self.hide, add="+")

    def hide(self, _event=None):
        if self.pending is not None:
            self.widget.after_cancel(self.pending)
            self.pending = None
        if self.label is not None:
            self.label.destroy()
            self.label = None

    def motion(self, event):
        self.hide()
        if not event.state & 0x700:  # No tooltip while dragging.
            self.pending = self.widget.after(500, self.show, event.x, event.y)

    def show(self, x, y):
        self.pending = None
        text = self.text_at(x, y)
        if not text:
            self.hide()
            return
        root = self.widget.winfo_toplevel()
        if self.label is None:
            self.label = ttk.Label(root, style="SidebarHint.TLabel", justify="left")
        label = self.label
        label.configure(text=text, wraplength=max(1, min(560, root.winfo_width() - 20)))
        left = self.widget.winfo_rootx() - root.winfo_rootx() + x
        top = self.widget.winfo_rooty() - root.winfo_rooty() + y
        left = max(0, min(left, root.winfo_width() - label.winfo_reqwidth()))
        height = label.winfo_reqheight()
        top = top + 18 if top + 18 + height <= root.winfo_height() else max(0, top - height - 4)
        label.place(x=left, y=top)
        label.lift()
        # A status update or column resize can change the text under a stationary pointer.
        self.pending = self.widget.after(250, self.show, x, y)


def label_tooltip(label, original):
    def clipped_text(_x, _y):
        text = original()
        if text != str(label["text"]):
            return text

    return FullTextTooltip(label, clipped_text)


class FittedLabel(ttk.Label):
    def __init__(self, master, **options):
        super().__init__(master, width=1, **options)
        self.full_text = ""
        label_tooltip(self, lambda: self.full_text)
        self.bind("<Configure>", self.fit)
        self.bind("<<ThemeChanged>>", self.fit)

    def set_text(self, text):
        self.full_text = text
        self.fit()

    def fit(self, _event=None):
        text = self.full_text
        if self.winfo_width() > 1:
            style = ttk.Style(self)
            font = style.lookup(self["style"] or "TLabel", "font")
            # The clam label has a one-pixel border on each side.
            text = fit_text(self, font, text, self.winfo_width() - 2)
        self.configure(text=text)


class FittedTreeview(ttk.Treeview):
    """Keep the original captions so wider columns can reveal the full text."""

    def __init__(self, master, *, cell_insets=CELL_TEXT_INSETS, **options):
        super().__init__(master, **options)
        self.cell_insets = cell_insets
        # Pixel widths stay the same as columns resize. Reuse them across rows and drag events.
        self.measure = lru_cache(maxsize=8192)(partial(_text_width, self.tk, self._w))
        self.rows = {}
        self.displayed_rows = {}
        self.captions = {}
        self.widths = ()
        self.pending_fit = None
        self.style = ttk.Style(self)
        FullTextTooltip(self, self.clipped_text_at)
        for event in ("<Map>", "<Configure>", "<B1-Motion>", "<ButtonRelease-1>"):
            self.bind(event, self.schedule_fit, add="+")
        self.bind("<<ThemeChanged>>", self.theme_changed)
        self.bind("<Double-Button-1>", self.autofit_divider)

    def autofit_divider(self, event):
        if self.identify_region(event.x, event.y) != "separator":
            return
        display = self.identify_column(event.x)
        if display == "#0":
            self.column("#0", width=self.column("#0", "minwidth"))
            self.event_generate("<<DividerFitted>>")
            return "break"
        col = self.column(display, "id")
        heading = "".join(self.captions[col])
        width = self.measure(self.heading_font, heading) + 2 * TREE_TEXT_PAD
        index = self["columns"].index(col)
        for row in self.get_children():
            if row in self.rows:
                value = str(self.rows[row][index]).split("\n", 1)[0].rstrip("\r")
                width = max(width, self.measure(self.row_font, value) + self.cell_insets)

        # Keep the table filled. Borrow space from the nearest columns, right side first.
        columns = list(self["displaycolumns"])
        if columns == ["#all"]:
            columns = list(self["columns"])
        index = columns.index(col)
        delta = max(width, self.column(col, "minwidth")) - self.column(col, "width")
        for other in columns[index + 1:] + columns[:index][::-1]:
            current = self.column(other, "width")
            take = min(delta, current - self.column(other, "minwidth"))
            if take:
                self.column(other, width=current - take)
                self.column(col, width=self.column(col, "width") + take)
                delta -= take
            if not delta:
                break
        self.event_generate("<<DividerFitted>>")
        return "break"

    def clipped_text_at(self, x, y):
        region = self.identify_region(x, y)
        display_col = self.identify_column(x)
        if region == "tree" and not self["displaycolumns"]:
            return self.item(self.identify_row(y), "text")
        if region not in ("heading", "cell") or display_col in ("", "#0"):
            return
        col = self.column(display_col, "id")
        if region == "heading":
            if col in self.captions:
                full = "".join(self.captions[col])
                if full != self.heading(col, "text"):
                    return full
        else:
            row = self.identify_row(y)
            if row in self.rows:
                full = str(self.rows[row][self["columns"].index(col)])
                if full != self.set(row, col):
                    return full

    def theme_changed(self, _event):
        self.measure.cache_clear()
        self.widths = ()
        self.schedule_fit()

    def column(self, column, option=None, **options):
        result = super().column(column, option, **options)
        if options:
            self.schedule_fit()
        return result

    def destroy(self):
        if self.pending_fit is not None:
            self.after_cancel(self.pending_fit)
        self.measure.cache_clear()
        super().destroy()

    def schedule_fit(self, _event=None):
        if self.pending_fit is None:
            self.pending_fit = self.after_idle(self.fit_columns)

    def fit_columns(self):
        self.pending_fit = None
        if not self.winfo_ismapped():
            return
        widths = tuple(self.column(col, "width") for col in self["columns"])
        if widths == self.widths:
            return
        self.widths = widths
        self.row_font = self.style.lookup(self["style"] or "Treeview", "font")
        self.heading_font = self.style.lookup("Treeview.Heading", "font")
        for row, values in self.rows.items():
            self.draw_row(row, values)
        for col, (text, suffix) in self.captions.items():
            self.draw_heading(col, text, suffix)

    def set_values(self, row, values):
        self.rows[row] = values
        self.draw_row(row, values)

    def draw_row(self, row, values):
        if self.widths:
            values = tuple(fit_text(self, self.row_font, str(value), width - self.cell_insets, measure=self.measure)
                           for value, width in zip(values, self.widths, strict=True))
        if self.displayed_rows.get(row) != values:
            self.item(row, values=values)
            self.displayed_rows[row] = values

    def set_caption(self, col, text, suffix=""):
        self.captions[col] = (text, suffix)
        self.draw_heading(col, text, suffix)

    def draw_heading(self, col, text, suffix):
        caption = text + suffix
        if self.widths:
            width = self.widths[self["columns"].index(col)]
            caption = fit_text(self, self.heading_font, text, width - 2 * TREE_TEXT_PAD, suffix, measure=self.measure)
        self.heading(col, text=caption)

    def delete(self, *items):
        super().delete(*items)
        for row in items:
            self.rows.pop(row, None)
            self.displayed_rows.pop(row, None)
