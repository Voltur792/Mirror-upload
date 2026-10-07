"""Persist only the dependency paths explicitly selected by the user."""
from __future__ import annotations

import ctypes
import json
import os
import tempfile
from pathlib import Path

NAMES = ("Path", "PIP_FIND_LINKS", "UV_FIND_LINKS", "PIP_NO_INDEX", "UV_NO_INDEX")


class WindowsEnvironment:
    def __init__(self, key_path="Environment"):
        import winreg
        self.registry = winreg
        self.key_path = key_path

    def read(self, name):
        r = self.registry
        try:
            with r.OpenKey(r.HKEY_CURRENT_USER, self.key_path) as key:
                value, kind = r.QueryValueEx(key, name)
                if kind not in (r.REG_SZ, r.REG_EXPAND_SZ):
                    raise ValueError("Неподдерживаемый тип переменной Windows: " + name)
                return {"value": value, "kind": kind}
        except FileNotFoundError:
            return None

    def write(self, name, record):
        r = self.registry
        with r.CreateKeyEx(r.HKEY_CURRENT_USER, self.key_path, 0, r.KEY_SET_VALUE) as key:
            if record is None:
                try:
                    r.DeleteValue(key, name)
                except FileNotFoundError:
                    pass
            else:
                r.SetValueEx(key, name, 0, record["kind"], record["value"])

    def notify(self):
        from ctypes import wintypes
        send = ctypes.windll.user32.SendMessageTimeoutW
        send.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM,
                         wintypes.UINT, wintypes.UINT, ctypes.POINTER(ctypes.c_size_t)]
        send.restype = wintypes.LPARAM
        message = ctypes.create_unicode_buffer("Environment")
        result = ctypes.c_size_t()
        send(0xFFFF, 0x001A, 0, ctypes.addressof(message), 0x0002, 5000, ctypes.byref(result))


def save_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".environment-", dir=path.parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def persist(paths, wheels, backup, store=None):
    store = store if store is not None else WindowsEnvironment()
    before = {name: store.read(name) for name in NAMES}
    previous = json.loads(backup.read_text("utf-8")) if backup.exists() else None
    old_path = before["Path"]["value"] if before["Path"] else ""
    additions = list(dict.fromkeys(str(p) for p in paths))
    keys = {p.rstrip("\\/").casefold() for p in additions}
    tail = [p for p in old_path.split(";") if p and p.strip('"').rstrip("\\/").casefold() not in keys]
    applied = {"Path": {"value": ";".join(additions + tail), "kind": before["Path"]["kind"] if before["Path"] else 2}}
    applied.update({name: {"value": wheels.as_uri() if name.endswith("FIND_LINKS") else "1", "kind": 1} for name in NAMES[1:]})
    # Keep the pre-install values through repeat runs; never lose the undo record.
    original = {name: previous["original"][name] if previous and before[name] == previous["applied"][name]
                else before[name] for name in NAMES}
    record = {"original": original, "applied": applied}
    save_json(backup, record)
    try:
        for name in NAMES:
            store.write(name, applied[name])
    except Exception:
        for name in NAMES:
            store.write(name, before[name])
        if previous:
            save_json(backup, previous)
        else:
            backup.unlink(missing_ok=True)
        store.notify()
        raise
    store.notify()
    return record


def is_applied(backup, store=None):
    if not backup.is_file():
        return False
    store = store if store is not None else WindowsEnvironment()
    record = json.loads(backup.read_text("utf-8"))
    return all(store.read(name) == record["applied"][name] for name in NAMES)


def restore(backup, store=None):
    if not backup.is_file():
        return []
    store = store if store is not None else WindowsEnvironment()
    record = json.loads(backup.read_text("utf-8"))
    retained = []
    for name in NAMES:
        if store.read(name) == record["applied"][name]:
            store.write(name, record["original"][name])
        else:
            # Preserve changes the user or another installer made afterwards.
            retained.append(name)
    backup.unlink()
    store.notify()
    return retained
