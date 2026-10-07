"""Pinned Windows dependencies, isolated libraries and an Astra offline launcher."""
from __future__ import annotations

import hashlib
import json
import os
import platform
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import time
import tomllib
import uuid
import zipfile
from pathlib import Path, PurePosixPath

from .catalog import files_present, sha256_file
from .installer import install_archive
from .yandex_disk import PublicFolder
from .user_environment import persist

DEFAULT_MIRROR = "https://disk.yandex.ru/d/CeRqoRrXAcNKzA"
SEED = Path(__file__).with_name("dependencies.seed.json")


def load_seed():
    # Execution hashes come from this build, never from a user-selected server.
    return json.loads(SEED.read_text("utf-8"))


def supported_platform():
    return os.name == "nt" and struct.calcsize("P") == 8 and platform.machine().lower() in {"amd64", "x86_64"}


def safe_member(name):
    if not isinstance(name, str) or not name or len(name) > 500 or "\\" in name or ":" in name or "\x00" in name:
        raise ValueError("Недопустимый путь в пакете зависимостей.")
    parts = name.rstrip("/").split("/")
    if any(part in {"", ".", ".."} or part.endswith((".", " ")) or any(char in part for char in '<>"|?*')
           or any(ord(char) < 32 for char in part) for part in parts):
        raise ValueError("Недопустимый путь в пакете зависимостей.")
    reserved = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
    if any(part.split(".")[0].upper() in reserved for part in parts):
        raise ValueError("Зарезервированное имя файла в пакете.")
    return PurePosixPath(*parts)


def safe_target(root, name):
    relative = safe_member(name)
    root = root.resolve()
    target = root.joinpath(*relative.parts)
    if not target.resolve().is_relative_to(root):
        raise ValueError("Путь пакета выходит за папку установки.")
    current = root
    for part in relative.parts:
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Ссылки в папке зависимостей не поддерживаются.")
    return target


def unpack_verified(archive, destination, cancel, prefixes=None):
    """Only call after a pinned outer hash check. Preserve conflicting files."""
    destination.mkdir(parents=True, exist_ok=True)
    destination = destination.resolve()
    created = []
    with tempfile.TemporaryDirectory(prefix=".unpack-", dir=destination) as directory:
        staging = Path(directory)
        records = []
        seen = set()
        total = 0
        try:
            with (tarfile.open(archive, "r:gz") if archive.name.endswith(".tgz") else zipfile.ZipFile(archive)) as package:
                members = package.getmembers() if isinstance(package, tarfile.TarFile) else package.infolist()
                if len(members) > 10000:
                    raise ValueError("Слишком много файлов в пакете.")
                for member in members:
                    if cancel.is_set():
                        raise InterruptedError("Настройка отменена.")
                    is_tar = isinstance(package, tarfile.TarFile)
                    name = member.name if is_tar else member.filename
                    relative = safe_member(name)
                    if prefixes and relative.parts[0] not in prefixes:
                        raise ValueError("В пакете найден неподдерживаемый каталог.")
                    if (member.isdir() if is_tar else member.is_dir()):
                        continue
                    mode = member.mode if is_tar else member.external_attr >> 16
                    if (is_tar and not member.isfile()) or (not is_tar and stat.S_IFMT(mode) not in {0, stat.S_IFREG}):
                        raise ValueError("Ссылки и специальные файлы в пакете запрещены.")
                    key = str(relative).casefold()
                    if key in seen:
                        raise ValueError("Повторяющийся путь в пакете.")
                    seen.add(key)
                    size = member.size if is_tar else member.file_size
                    total += size
                    if total > 2 * 1024**3 or size > 1024**3:
                        raise ValueError("Распакованный пакет слишком большой.")
                    target = safe_target(destination, str(relative))
                    staged = safe_target(staging, str(relative))
                    staged.parent.mkdir(parents=True, exist_ok=True)
                    incoming = package.extractfile(member) if is_tar else package.open(member)
                    with incoming, staged.open("xb") as outgoing:
                        value = hashlib.sha256()
                        written = 0
                        while chunk := incoming.read(1024 * 1024):
                            if cancel.is_set():
                                raise InterruptedError("Настройка отменена.")
                            written += len(chunk)
                            if written > size:
                                raise ValueError("Размер файла в пакете неверен.")
                            value.update(chunk)
                            outgoing.write(chunk)
                    if written != size:
                        raise ValueError("Файл в пакете неполный.")
                    sha = value.hexdigest()
                    if target.exists():
                        if not target.is_file() or target.stat().st_size != size or sha256_file(target, cancel) != sha:
                            raise ValueError(f"Сохранён другой файл: {relative}. Он не заменён.")
                    records.append((staged, target))
            # The complete package is validated before writing any new files.
            for staged, target in records:
                if cancel.is_set():
                    raise InterruptedError("Настройка отменена.")
                if target.exists():
                    continue
                target.parent.mkdir(parents=True, exist_ok=True)
                # Rename on Windows never replaces an existing destination.
                if os.name == "nt":
                    os.rename(staged, target)
                else:
                    os.link(staged, target)
                created.append(target)
        except BaseException:
            for target in reversed(created):
                target.unlink(missing_ok=True)
            raise


def child_environment():
    environment = dict(os.environ)
    for name in ["PYTHONHOME", "PYTHONPATH", "VIRTUAL_ENV", "CONDA_PREFIX", "PIP_TARGET", "PIP_PREFIX", "PIP_USER",
                 "_PYI_APPLICATION_HOME_DIR", "_PYI_PARENT_PROCESS_LEVEL", "_PYI_ARCHIVE_FILE", "PSModulePath"]:
        for key in list(environment):
            if key.upper() == name.upper():
                environment.pop(key, None)
    return environment


class QuickSetup:
    def __init__(self, storage, data_root, source_url, models, cancel, update, astra_path="", seed=None):
        self.root = (storage / "setup").resolve()
        self.data_root = data_root.resolve()
        self.folder = PublicFolder(source_url)
        self.models = models
        self.cancel = cancel
        self.update = update
        self.seed = seed if seed is not None else load_seed()
        self.astra_path = astra_path
        self.step_index = 0
        self.steps_total = 11 + len(self.seed["models"]) + len(self.seed["providers"]) + len(models)
        self.python = None

    def step(self, title):
        if self.cancel.is_set():
            raise InterruptedError("Настройка отменена. Завершённые компоненты сохранены.")
        self.step_index += 1
        self.update(state="preparing", component_name=title, stage_label=title, message=title,
                    step_index=self.step_index, steps_total=self.steps_total,
                    downloaded_bytes=0, total_bytes=0, percent=0)

    def run_command(self, command, timeout=900, cancellable=True, environment=None):
        # A frozen app's DLL search directory must not leak into installers.
        if os.name == "nt" and getattr(__import__('sys'), 'frozen', False):
            import ctypes
            ctypes.windll.kernel32.SetDllDirectoryW(None)
        self.root.mkdir(parents=True, exist_ok=True)
        log = self.root / "last-command.log"
        with log.open("wb") as stream:
            process = subprocess.Popen(command, stdout=stream, stderr=subprocess.STDOUT,
                                       env=environment or child_environment(), cwd=self.root,
                                       creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            deadline = time.monotonic() + timeout
            while process.poll() is None:
                if self.cancel.is_set() and cancellable:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    raise InterruptedError("Настройка отменена.")
                if time.monotonic() >= deadline:
                    if cancellable:
                        process.kill()
                        process.wait()
                    raise ValueError("Установка не завершилась вовремя. Подробности: setup/last-command.log.")
                time.sleep(0.2)
        if process.returncode not in {0, 3010}:
            raise ValueError(f"Установщик вернул код {process.returncode}. Подробности: setup/last-command.log.")
        if self.cancel.is_set():
            raise InterruptedError("Настройка отменена после завершения текущего установщика.")

    def asset(self, record):
        cache = self.root / "downloads"
        cache.mkdir(parents=True, exist_ok=True)
        target = safe_target(cache, record["path"])
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists() and target.stat().st_size == record["size"] and sha256_file(target, self.cancel) == record["sha256"]:
            return target
        if target.exists():
            raise ValueError(f"Кеш {record['path']} повреждён. Удалите этот файл и повторите настройку.")
        if shutil.disk_usage(cache).free < record["size"] * 3 + 128 * 1024**2:
            raise ValueError("Для загрузки и распаковки недостаточно свободного места.")
        temporary = target.with_name(target.name + ".part-" + uuid.uuid4().hex)
        self.update(state="downloading", total_bytes=record["size"], percent=0)
        try:
            self.folder.download("dependencies/" + record["path"], temporary, record["size"], self.cancel,
                                 lambda done: self.update(downloaded_bytes=done, percent=round(done * 100 / record["size"], 1)))
            self.update(state="verifying", message="Проверяю SHA-256 пакета…")
            if sha256_file(temporary, self.cancel) != record["sha256"]:
                raise ValueError("SHA-256 пакета зависимостей не совпадает с проверенной сборкой.")
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
            return target
        finally:
            temporary.unlink(missing_ok=True)

    @staticmethod
    def python_is_compatible(path):
        if not path.is_file():
            return False
        try:
            result = subprocess.run([str(path), "-I", "-c", "import sys,struct; print(str(sys.version_info[0])+chr(46)+str(sys.version_info[1])+chr(32)+str(struct.calcsize(chr(80))))"],
                                    capture_output=True, text=True, timeout=15, env=child_environment(),
                                    creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
            return result.returncode == 0 and result.stdout.strip() == "3.13 8"
        except (OSError, subprocess.TimeoutExpired):
            return False

    def prepare_python(self):
        standard = Path(os.environ.get("LOCALAPPDATA", str(self.root))) / "Programs" / "Python" / "Python313" / "python.exe"
        target = self.root / "runtimes" / "python313"
        python = target / "python.exe"
        candidates = [python, standard]
        located = shutil.which("python")
        if located:
            candidates.append(Path(located))
        if os.name == "nt":
            import winreg
            for hive in [winreg.HKEY_CURRENT_USER, winreg.HKEY_LOCAL_MACHINE]:
                try:
                    with winreg.OpenKey(hive, r"Software\Python\PythonCore\3.13\InstallPath", 0, winreg.KEY_READ | winreg.KEY_WOW64_64KEY) as key:
                        candidates.append(Path(winreg.QueryValue(key, None)) / "python.exe")
                except OSError:
                    pass
        for candidate in candidates:
            if self.python_is_compatible(candidate):
                self.python = candidate
                return
        if python.exists():
            raise ValueError("В папке Python Mirror-upload обнаружена другая установка. Она сохранена.")
        installer = self.asset(next(record for record in self.seed["runtimes"] if record["path"].endswith(".exe")))
        environment = child_environment()
        environment["MIRROR_INSTALLER_FILE"] = str(installer)
        self.run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          "$s = Get-AuthenticodeSignature -LiteralPath $env:MIRROR_INSTALLER_FILE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Python Software Foundation') { exit 1 }; exit 0"],
                         environment=environment)
        self.update(state="installing", message="Устанавливаю Python для текущего пользователя. Отмена дождётся установщика.")
        self.run_command([str(installer), "/quiet", "InstallAllUsers=0", f"TargetDir={target}", "Include_launcher=0",
                          "Include_test=0", "Include_doc=0", "Include_dev=0", "Include_tcltk=0", "Shortcuts=0",
                          "PrependPath=0", "AssociateFiles=0", "Include_pip=1"], cancellable=False)
        if not self.python_is_compatible(python):
            raise ValueError("Python установлен, но проверка версии не прошла.")
        self.python = python

    @staticmethod
    def webview2_installed():
        import winreg
        app = r"Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}"
        for hive, prefix in [(winreg.HKEY_CURRENT_USER, "Software\\"),
                             (winreg.HKEY_LOCAL_MACHINE, "SOFTWARE\\WOW6432Node\\")]:
            try:
                with winreg.OpenKey(hive, prefix + app) as key:
                    version = winreg.QueryValueEx(key, "pv")[0]
                    if isinstance(version, str) and version not in {"", "0.0.0.0"}:
                        return True
            except OSError:
                pass
        return False

    def prepare_webview2(self):
        record = self.seed.get("webview2")
        if not record or self.webview2_installed():
            return
        installer = self.asset(record)
        environment = child_environment()
        environment["MIRROR_INSTALLER_FILE"] = str(installer)
        self.run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          "$ErrorActionPreference='Stop'; $s=Get-AuthenticodeSignature -LiteralPath $env:MIRROR_INSTALLER_FILE; if ($s.Status -ne 'Valid' -or $s.SignerCertificate.Subject -notmatch 'Microsoft Corporation') { exit 1 }; exit 0"], environment=environment)
        self.run_command([str(installer), "/silent", "/install"], cancellable=False)
        if not self.webview2_installed():
            raise ValueError("WebView2 установлен, но проверка наличия не прошла.")

    def install_profile(self, profile, target):
        self.update(state="installing", message=f"Устанавливаю библиотеки: {profile}…")
        lock = self.root / "profiles" / profile / "requirements.lock"
        if not lock.is_file():
            raise ValueError("В проверенном наборе отсутствует список библиотек.")
        stamp = target / ".mirror-profile.json"
        expected = {"python": "3.13", "lock_sha256": sha256_file(lock, self.cancel)}
        if stamp.is_file() and json.loads(stamp.read_text("utf-8")) == expected:
            self.run_command([str(target / "Scripts" / "python.exe"), "-I", "-m", "pip", "check"])
            return
        if target.exists():
            raise ValueError(f"Незавершённое окружение {profile} сохранено. Удалите только эту папку setup/environments и повторите.")
        environment = child_environment()
        environment.update(PIP_NO_INDEX="1", PIP_FIND_LINKS=str(self.root / "wheels"), PIP_DISABLE_PIP_VERSION_CHECK="1")
        target.parent.mkdir(parents=True, exist_ok=True)
        # Claim the new directory before entering rollback handling. A host
        # concurrently creating the same venv must never become ours to delete.
        target.mkdir(exist_ok=False)
        try:
            self.run_command([str(self.python), "-I", "-m", "venv", str(target)], environment=environment)
            executable = str(target / "Scripts" / "python.exe")
            self.run_command([executable, "-I", "-m", "pip", "install", "--no-index", "--find-links", str(self.root / "wheels"), "-r", str(lock)], environment=environment)
            self.run_command([executable, "-I", "-m", "pip", "check"], environment=environment)
            stamp.write_text(json.dumps(expected), encoding="utf-8")
        except BaseException:
            # Only this operation's new, not yet committed environment is removed.
            assert target.resolve().is_relative_to(self.root) or target.resolve().is_relative_to(self.data_root.parent / "config" / "plugins")
            shutil.rmtree(target, ignore_errors=True)
            raise

    def prepare_installed_plugins(self):
        installed = self.data_root.parent / "config" / "plugins"
        prepared, retained = [], []
        for plugin in self.seed["registry_plugins"]:
            if not plugin.get("profile"):
                continue
            directory = safe_target(installed, plugin["id"])
            manifest = directory / "plugin.toml"
            if not manifest.is_file():
                continue
            metadata = tomllib.loads(manifest.read_text("utf-8"))["plugin"]
            if metadata.get("id") != plugin["id"] or metadata.get("version") != plugin["version"]:
                if not any(p["id"] == metadata.get("id") and p["version"] == metadata.get("version") for p in self.seed["registry_plugins"]):
                    retained.append(plugin["id"])
                continue
            requirements = directory / "requirements.lock"
            if not requirements.is_file():
                requirements = directory / "requirements.txt"
            if not requirements.is_file() or sha256_file(requirements) != plugin["requirements_hashes"].get(requirements.name):
                retained.append(plugin["id"])
                continue
            target = directory / ".venv"
            if target.exists():
                retained.append(plugin["id"])
                continue
            marker = directory / ".astra-venv.json"
            if marker.exists():
                # A host may already be rebuilding this environment. Leave it alone.
                retained.append(plugin["id"])
                continue
            self.install_profile(plugin["profile"], target)
            with marker.open("x", encoding="utf-8") as stream:
                json.dump({"phase": "ready", "requirements": requirements.name,
                           "requirements_sha256": sha256_file(requirements), "python": "3.13", "installer": "pip"}, stream, indent=2)
            prepared.append(plugin["id"])
        return sorted(set(prepared)), sorted(set(retained))

    def find_astra(self):
        if self.astra_path:
            candidate = Path(self.astra_path)
            if candidate.is_absolute() and candidate.is_file() and candidate.name.lower() == "astra.exe":
                return candidate
            raise ValueError("Путь к Astra.exe в настройках некорректен.")
        candidates = [Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Astra" / "Astra.exe",
                      Path(os.environ.get("PROGRAMFILES", "")) / "Astra" / "Astra.exe"]
        for candidate in candidates:
            if candidate.is_file():
                return candidate.resolve()
        raise ValueError("Не найден Astra.exe. Укажите путь в настройках плагина и повторите настройку.")

    def runtime_paths(self):
        paths = [self.python.parent, self.python.parent / "Scripts",
                 self.root / "runtimes" / "node-v24.21.0-win-x64", self.root / "runtimes" / "uv",
                 self.root / "providers" / "codex", self.root / "providers" / "gemini",
                 self.root / "providers" / "claude" / "package"]
        ffmpeg = self.seed.get("ffmpeg")
        if ffmpeg:
            paths.append(self.root / "runtimes" / ffmpeg["folder"] / "bin")
        if self.seed.get("discord_profile"):
            paths.insert(0, self.root / "environments" / self.seed["discord_profile"] / "Scripts")
        return paths

    def prepare_easyvk(self):
        record = self.seed.get("easyvk")
        if not record:
            return False
        unpack_verified(self.asset(record), self.root, self.cancel, {"node"})
        source = self.root / "node" / "easyvk"
        destination = Path(os.environ.get("APPDATA", str(self.root))) / "music-controller" / "easyvk-node"
        if destination.exists():
            # Preserve an existing runtime; plugin account settings are never read.
            stamp = destination / ".astra-easyvk-lock"
            return stamp.is_file() and stamp.read_text("ascii").strip() == record["lock_sha256"]
        destination.parent.mkdir(parents=True, exist_ok=True)
        with tempfile.TemporaryDirectory(prefix=".mirror-easyvk-", dir=destination.parent) as staging:
            temporary = Path(staging) / "runtime"
            shutil.copytree(source, temporary)
            try:
                os.rename(temporary, destination)
            except FileExistsError:
                return False
        return True

    def speech_model_destinations(self, model):
        appdata = Path(os.environ.get("APPDATA", str(self.root)))
        destinations = [appdata / "voice-text-input" / "models"]
        if model["id"].startswith("vosk"):
            # Discord's published offline setup checks its own models directory.
            # Prepare data there without reading or editing its account settings.
            discord = os.environ.get("DVOICE_DATA_DIR")
            discord_root = Path(discord) if discord and Path(discord).is_absolute() else appdata / "discord-voice-bridge"
            destinations.append(discord_root / "models")
        return destinations

    def clean_downloads(self):
        removed = 0
        records = self.seed["runtimes"] + self.seed["models"] + self.seed["providers"] + [self.seed["libraries"]]
        records += [self.seed[k] for k in ("ffmpeg", "easyvk", "webview2") if self.seed.get(k)]
        files = [(safe_target(self.root / "downloads", r["path"]), r["sha256"]) for r in records]
        files += [(safe_target(self.root / "native-models", c["archive"]), c["archive_sha256"]) for c in self.models]
        for path, expected in files:
            try:
                if path.is_file() and sha256_file(path) == expected:
                    size = path.stat().st_size
                    path.unlink()
                    removed += size
            except OSError:
                # Cache cleanup must not turn a completed installation into failure.
                pass
        return removed

    @staticmethod
    def desktop_directory():
        # Use Windows' redirected Desktop folder (including OneDrive), not a guessed path.
        import ctypes
        buffer = ctypes.create_unicode_buffer(32768)
        if ctypes.windll.shell32.SHGetFolderPathW(None, 0x10, None, 0, buffer):
            raise OSError("Не удалось определить рабочий стол пользователя.")
        return Path(buffer.value)

    def write_launcher(self, astra):
        def quote(value):
            return "'" + str(value).replace("'", "''") + "'"
        paths = self.runtime_paths()
        shortcut = self.desktop_directory() / "Mirror-upload - Завершить настройку.lnk"
        script = "$ErrorActionPreference = 'Stop'\n"
        script += "if (-not (Test-Path -LiteralPath " + quote(self.root / "user-environment-backup.json") + ")) { throw 'Complete Mirror-upload setup before using this shortcut.' }\n"
        script += "$mirrorAstra = " + quote(astra) + "\n"
        script += "if (Get-Process -Name Astra -ErrorAction SilentlyContinue) { throw 'Exit Astra before starting it through Mirror-upload.' }\n"
        script += "$env:PIP_NO_INDEX = '1'\n$env:UV_NO_INDEX = '1'\n$env:PIP_DISABLE_PIP_VERSION_CHECK = '1'\n"
        script += "$env:PIP_FIND_LINKS = " + quote((self.root / "wheels").as_uri()) + "\n"
        script += "$env:UV_FIND_LINKS = $env:PIP_FIND_LINKS\n$env:HF_HUB_OFFLINE = '1'\n$env:HF_HUB_DISABLE_TELEMETRY = '1'\n"
        script += "$env:PATH = " + quote(";".join(map(str, paths)) + ";") + " + $env:PATH\n"
        script += "$mirrorProcess = Start-Process -FilePath $mirrorAstra -WorkingDirectory (Split-Path -Parent $mirrorAstra) -WindowStyle Hidden -PassThru -ErrorAction Stop\n"
        script += "$mirrorShortcut = " + quote(shortcut) + "\n"
        script += "$mirrorLauncher = " + quote(self.root / "start-astra.cmd") + "\n"
        script += "if ($mirrorProcess -and (Test-Path -LiteralPath $mirrorShortcut)) { $mirrorShell = New-Object -ComObject WScript.Shell; $mirrorLink = $mirrorShell.CreateShortcut($mirrorShortcut); if ($mirrorLink.TargetPath -eq $mirrorLauncher) { Remove-Item -LiteralPath $mirrorShortcut } }\n"
        ps1 = self.root / "start-astra.ps1"
        ps1.write_text(script, encoding="utf-8-sig")
        cmd = self.root / "start-astra.cmd"
        cmd.write_text('@echo off\r\npowershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0start-astra.ps1"\r\nif errorlevel 1 pause\r\n', encoding="ascii", newline="")
        environment = child_environment()
        environment["MIRROR_LAUNCHER_FILE"] = str(cmd)
        environment["MIRROR_ASTRA_FILE"] = str(astra)
        environment["MIRROR_SHORTCUT_FILE"] = str(shortcut)
        self.run_command(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command",
                          "$ErrorActionPreference='Stop'; $w=New-Object -ComObject WScript.Shell; $d=Split-Path -Parent $env:MIRROR_SHORTCUT_FILE; New-Item -ItemType Directory -Path $d -Force | Out-Null; $s=$w.CreateShortcut($env:MIRROR_SHORTCUT_FILE); if ((Test-Path -LiteralPath $env:MIRROR_SHORTCUT_FILE) -and $s.TargetPath -ne $env:MIRROR_LAUNCHER_FILE) { throw 'Existing shortcut was preserved.' }; $s.TargetPath=$env:MIRROR_LAUNCHER_FILE; $s.WorkingDirectory=Split-Path -Parent $env:MIRROR_LAUNCHER_FILE; $s.IconLocation=$env:MIRROR_ASTRA_FILE; $s.WindowStyle=7; $s.Save()"], environment=environment)
        return str(cmd), str(shortcut)

    def run(self):
        if not supported_platform():
            raise ValueError("Быстрая настройка поддерживает Windows x64.")
        # Resolve the installed executable before any installation changes.
        astra = self.find_astra()
        self.root.mkdir(parents=True, exist_ok=True)
        self.step("Python 3.13")
        self.prepare_python()
        self.step("Node.js LTS")
        node = next(record for record in self.seed["runtimes"] if "node-" in record["path"])
        unpack_verified(self.asset(node), self.root / "runtimes", self.cancel, {"node-v24.21.0-win-x64"})
        self.run_command([str(self.root / "runtimes" / "node-v24.21.0-win-x64" / "node.exe"), "--version"])
        self.step("Менеджер библиотек uv")
        uv = next(record for record in self.seed["runtimes"] if "uv-" in record["path"])
        unpack_verified(self.asset(uv), self.root / "runtimes" / "uv", self.cancel)
        self.run_command([str(self.root / "runtimes" / "uv" / "uv.exe"), "--version"])
        self.step("Среда WebView2 для музыкального виджета")
        self.prepare_webview2()
        if self.seed.get("ffmpeg"):
            self.step("FFmpeg для музыки Discord")
            ffmpeg = self.seed["ffmpeg"]
            unpack_verified(self.asset(ffmpeg), self.root / "runtimes", self.cancel, {ffmpeg["folder"]})
            self.run_command([str(self.root / "runtimes" / ffmpeg["executable"]), "-version"])
        self.step("Библиотеки Python")
        unpack_verified(self.asset(self.seed["libraries"]), self.root, self.cancel, {"wheels", "profiles"})
        for wheel in self.seed["wheels"]:
            path = safe_target(self.root, wheel["path"])
            if path.stat().st_size != wheel["size"] or sha256_file(path, self.cancel) != wheel["sha256"]:
                raise ValueError("Повреждена библиотека Python.")
        self.step("Окружения библиотек")
        for profile in self.seed["profiles"]:
            self.install_profile(profile["profile"], safe_target(self.root / "environments", profile["profile"]))
        self.step("Библиотеки VK-плеера")
        easyvk_ready = self.prepare_easyvk()
        # Install the optional speech engine together with its pinned core set.
        voice = self.root / "environments" / "voice-text-input-1.1.0" / "Scripts" / "python.exe"
        optional = self.root / "profiles" / "voice-text-input-faster-whisper-optional" / "requirements.lock"
        self.run_command([str(voice), "-I", "-m", "pip", "install", "--no-index", "--find-links", str(self.root / "wheels"), "-r", str(optional)])
        self.run_command([str(voice), "-I", "-m", "pip", "check"])
        self.step("Окружения установленных плагинов")
        prepared, retained = self.prepare_installed_plugins()
        for model in self.seed["models"]:
            self.step(model["id"])
            archive = self.asset(model)
            for model_root in self.speech_model_destinations(model):
                unpack_verified(archive, model_root, self.cancel)
        for provider in self.seed["providers"]:
            self.step("CLI " + provider["id"])
            target = self.root / "providers" / "claude" if provider["path"].endswith(".tgz") else self.root
            unpack_verified(self.asset(provider), target, self.cancel, {"package"} if provider["id"] == "claude" else {"providers"})
        for component in self.models:
            self.step(component["name"])
            if files_present(component, self.data_root):
                continue
            cache = self.root / "native-models"
            cache.mkdir(parents=True, exist_ok=True)
            archive = safe_target(cache, component["archive"])
            archive.parent.mkdir(parents=True, exist_ok=True)
            if not archive.exists():
                self.update(state="downloading", total_bytes=component["archive_size"])
                temporary = archive.with_name(archive.name + ".part-" + uuid.uuid4().hex)
                try:
                    self.folder.download(component["archive"], temporary, component["archive_size"], self.cancel,
                                         lambda done: self.update(downloaded_bytes=done, percent=round(done * 100 / component["archive_size"], 1)))
                    if sha256_file(temporary, self.cancel) != component["archive_sha256"]:
                        raise ValueError("SHA-256 модели не совпадает с проверенной сборкой.")
                    os.rename(temporary, archive)
                finally:
                    temporary.unlink(missing_ok=True)
            install_archive(component, archive, self.data_root, self.cancel,
                            lambda **values: self.update(**{key: value for key, value in values.items() if key != "state" or value != "done"}))
        self.step("Подключение к Astra")
        launcher, shortcut = self.write_launcher(astra)
        self.step("Сохранение путей Windows")
        persist(self.runtime_paths(), self.root / "wheels", self.root / "user-environment-backup.json")
        result = {"ready": True, "restart_required": True, "launcher_path": launcher, "shortcut_path": shortcut,
                  "user_environment_persisted": True,
                  "easyvk_ready": easyvk_ready,
                  "python_path": str(self.python), "seed_sha256": hashlib.sha256(SEED.read_bytes()).hexdigest(),
                  "snapshot_date": self.seed["snapshot_date"], "prepared_plugin_environments": prepared,
                  "retained_plugin_environments": retained}
        (self.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        result["removed_archive_bytes"] = self.clean_downloads()
        (self.root / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        return result
