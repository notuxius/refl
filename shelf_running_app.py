"""Identify Windows apps by executable path and request a normal shutdown."""

from contextlib import ExitStack, contextmanager
import ctypes
from ctypes import wintypes as win
from functools import cache
import os
from pathlib import Path
import sys
from time import monotonic


WM_CLOSE = 0x0010
GW_OWNER = 4
PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0


def running_executable(data):
    value = data.get("running_exe", "")
    if isinstance(value, str):
        if not value.strip():
            return None
        path = Path(os.path.expandvars(value.strip()))
        if path.is_absolute() and path.suffix.lower() == ".exe":
            return path
    raise ValueError("Enter the full path to the running app's EXE file.")


class WindowsProcesses:
    def __init__(self):
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        user = ctypes.WinDLL("user32", use_last_error=True)
        self.window_callback = ctypes.WINFUNCTYPE(win.BOOL, win.HWND, win.LPARAM)

        def bind(library, name, result, *args):
            function = getattr(library, name)
            function.restype, function.argtypes = result, args
            return function

        self.enum_processes = bind(kernel, "K32EnumProcesses", win.BOOL, win.LPDWORD, win.DWORD, win.LPDWORD)
        self.session_id = bind(kernel, "ProcessIdToSessionId", win.BOOL, win.DWORD, win.LPDWORD)
        self.open_process = bind(kernel, "OpenProcess", win.HANDLE, win.DWORD, win.BOOL, win.DWORD)
        self.image_name = bind(kernel, "QueryFullProcessImageNameW", win.BOOL,
                               win.HANDLE, win.DWORD, win.LPWSTR, win.LPDWORD)
        self.close_handle = bind(kernel, "CloseHandle", win.BOOL, win.HANDLE)
        self.wait = bind(kernel, "WaitForSingleObject", win.DWORD, win.HANDLE, win.DWORD)
        self.enum_windows = bind(user, "EnumWindows", win.BOOL, self.window_callback, win.LPARAM)
        self.window_process = bind(user, "GetWindowThreadProcessId", win.DWORD, win.HWND, win.LPDWORD)
        self.get_window = bind(user, "GetWindow", win.HWND, win.HWND, win.UINT)
        self.post_message = bind(user, "PostMessageW", win.BOOL, win.HWND, win.UINT, win.WPARAM, win.LPARAM)

    def process_ids(self):
        capacity = 256
        used = win.DWORD()
        while True:
            processes = (win.DWORD * capacity)()
            size = ctypes.sizeof(processes)
            if not self.enum_processes(processes, size, ctypes.byref(used)):
                raise ctypes.WinError(ctypes.get_last_error())
            if used.value < size:
                return processes[:used.value // ctypes.sizeof(win.DWORD)]
            capacity *= 2

    @contextmanager
    def matching(self, path):
        path = path.resolve()
        matches = {}
        own_pid = os.getpid()
        own_session, session = win.DWORD(), win.DWORD()
        if not self.session_id(own_pid, ctypes.byref(own_session)):
            raise ctypes.WinError(ctypes.get_last_error())
        image = ctypes.create_unicode_buffer(32768)
        with ExitStack() as handles:
            # A frozen Relf has a launcher process with the same image as its GUI process.
            if not (getattr(sys, "frozen", False) and path == Path(sys.executable).resolve()):
                for pid in self.process_ids():
                    if (pid in (0, own_pid) or not self.session_id(pid, ctypes.byref(session))
                            or session.value != own_session.value):
                        continue
                    handle = self.open_process(PROCESS_QUERY_LIMITED_INFORMATION | SYNCHRONIZE, False, pid)
                    if not handle:
                        continue
                    with ExitStack() as candidate:
                        candidate.callback(self.close_handle, handle)
                        length = win.DWORD(len(image))
                        if self.image_name(handle, 0, image, ctypes.byref(length)) and Path(image.value) == path:
                            matches[pid] = handle
                            handles.enter_context(candidate.pop_all())
            yield matches

    def request_close(self, matches):
        @self.window_callback
        def close_window(window, _):
            pid = win.DWORD()
            # Leave owned windows such as save prompts to the app's normal close flow.
            if not self.get_window(window, GW_OWNER) and self.window_process(window, ctypes.byref(pid)):
                if pid.value in matches:
                    self.post_message(window, WM_CLOSE, 0, 0)
            return True

        self.enum_windows(close_window, 0)


@cache
def windows_processes():
    return WindowsProcesses()


def app_is_running(path):
    if os.name != "nt":
        return False
    with windows_processes().matching(path) as matches:
        return bool(matches)


def close_running_app(path, *, timeout=15):
    """Request shutdown for every instance and wait before allowing installation."""
    api = windows_processes()
    with api.matching(path) as matches:
        if not matches:
            return True
        api.request_close(matches)
        deadline = monotonic() + timeout
        for handle in matches.values():
            remaining = max(0, int((deadline - monotonic()) * 1000))
            if api.wait(handle, remaining) != WAIT_OBJECT_0:
                return False
    return not app_is_running(path)
