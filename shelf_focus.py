"""Keyboard focus movement for Relf controls."""


def tab_focus(widget, step):
    command = "tk_focusNext" if step > 0 else "tk_focusPrev"
    widget.tk.call("tk::TabToWindow", widget.tk.call(command, widget))
    return "break"


def arrow_focus(event):
    widget = event.widget
    if str(widget.cget("takefocus")) == "0" or event.state & 0x2000D:
        return

    horizontal = event.keysym in ("Left", "Right")
    direction = 1 if event.keysym in ("Right", "Down") else -1

    def bounds(control):
        x, y = control.winfo_rootx(), control.winfo_rooty()
        width, height = control.winfo_width(), control.winfo_height()
        if horizontal:
            return x, width, y, height
        return y, height, x, width

    start, size, cross, span = bounds(widget)
    seen = {widget}
    candidate = widget.tk_focusNext()
    target, best = None, None
    while candidate not in seen:
        seen.add(candidate)
        near, length, edge, breadth = bounds(candidate)
        forward = direction * (near + length / 2 - start - size / 2)
        if forward > 0:
            aligned = cross < edge + breadth and edge < cross + span
            across = edge + breadth / 2 - cross - span / 2
            score = (not aligned, forward ** 2 + across ** 2)
            if best is None or score < best:
                target, best = candidate, score
        candidate = candidate.tk_focusNext()
    if target is not None:
        widget.tk.call("tk::TabToWindow", target)
    return "break"
