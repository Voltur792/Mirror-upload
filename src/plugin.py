"""Mirror-upload: pinned dependencies and local Astra model packages."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import threading
import zipfile
from pathlib import Path

from astra_plugin_sdk import Plugin, UiContribution, ui_call, ui_page

from .catalog import contained_path, default_data_root, discover_local, files_present, parse_catalog
from .installer import install_archive
from .yandex_disk import PublicFolder
from .quick_setup import DEFAULT_MIRROR, QuickSetup, load_seed, supported_platform
from .user_environment import is_applied, restore

BASE = Path(__file__).resolve().parent.parent
BUSY_STATES = {"preparing", "downloading", "verifying", "installing"}
ICON = (BASE / "ui" / "web" / "assets" / "mirror-upload.svg").read_text("utf-8")


def state_root() -> Path:
    override = os.environ.get("COMPONENT_MIRROR_STATE")
    if override:
        return Path(override).resolve()
    appdata = os.environ.get("APPDATA")
    if appdata:
        return Path(appdata) / "astra" / "astra" / "config" / "component-mirror"
    return BASE / "data"


def atomic_json(path: Path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".mirror-", suffix=".tmp", dir=path.parent)
    temporary = Path(temporary)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


@ui_page("component-mirror", "Mirror-upload", "web/index.html", icon_svg=ICON)
class ComponentMirror(Plugin):
    def __init__(self):
        super().__init__()
        self._lock = threading.RLock()
        self._worker: threading.Thread | None = None
        self._cancel = threading.Event()
        self._source_url = DEFAULT_MIRROR
        self._catalog: list[dict] = []
        self._mirror_state = "empty"
        self._mirror_message = "Зеркало задано по умолчанию. Обновите каталог для проверки подключения."
        self._job = {"state": "idle", "component_id": "", "component_name": "", "downloaded_bytes": 0,
                     "total_bytes": 0, "percent": 0, "message": ""}
        self._storage = state_root()
        self._quick_result = {}
        self._load_state()

    def _load_state(self):
        try:
            self._catalog = parse_catalog((BASE / "src" / "catalog.seed.json").read_bytes())
        except (OSError, ValueError):
            pass
        # Cached catalogs support local ZIP verification after a restart.
        # Availability on Disk must be refreshed before a network install.
        try:
            cached = json.loads((self._storage / "catalog-cache.json").read_text("utf-8"))
            if cached.get("public_url") == self._source_url and self._source_url:
                self._catalog = parse_catalog(json.dumps(cached["catalog"]).encode())
                self._mirror_message = "Каталог сохранён. Нажмите «Обновить», чтобы проверить зеркало."
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            pass
        try:
            result = json.loads((self._storage / "setup" / "result.json").read_text("utf-8"))
            if isinstance(result, dict):
                self._quick_result = result
        except (OSError, ValueError):
            pass

    def _setup_dashboard(self):
        seed = load_seed()
        setup_root = self._storage / "setup"
        ready = bool(self._quick_result.get("ready")) and (setup_root / "start-astra.cmd").is_file()
        if ready:
            import hashlib
            ready = self._quick_result.get("seed_sha256") == hashlib.sha256((BASE / "src" / "dependencies.seed.json").read_bytes()).hexdigest()
            ready = ready and Path(self._quick_result.get("python_path", "")).is_file()
            ready = ready and (setup_root / "runtimes" / "node-v24.21.0-win-x64" / "node.exe").is_file()
            ready = ready and (setup_root / "runtimes" / "uv" / "uv.exe").is_file()
            ready = ready and all((setup_root / wheel["path"]).is_file() and (setup_root / wheel["path"]).stat().st_size == wheel["size"] for wheel in seed["wheels"])
            ready = ready and is_applied(setup_root / "user-environment-backup.json")
        records = seed["runtimes"] + seed["models"] + seed["providers"] + [seed["libraries"]]
        records += [seed[k] for k in ("ffmpeg", "easyvk", "webview2") if seed.get(k)]
        models = parse_catalog((BASE / "src" / "catalog.seed.json").read_bytes())
        return {"supported": supported_platform(), "message": "Среды исполнения, библиотеки и модели из проверенного набора.",
                "snapshot_date": seed["snapshot_date"], "registry_plugins": len({p["id"] for p in seed["registry_plugins"]}),
                "total_download_bytes": sum(record["size"] for record in records) + sum(c["archive_size"] for c in models),
                "ready": ready, "restart_required": ready and Path(self._quick_result.get("shortcut_path", "")).is_file(),
                "environment_saved": (setup_root / "user-environment-backup.json").is_file(),
                "shortcut_path": self._quick_result.get("shortcut_path", "") if ready else "",
                "launcher_path": self._quick_result.get("launcher_path", "") if ready else ""}

    def _data_root(self) -> Path:
        configured = self.config.get("astra_data_dir") if isinstance(self.config, dict) else None
        if isinstance(configured, str) and configured.strip():
            candidate = Path(configured.strip())
            if not candidate.is_absolute():
                raise ValueError("Папка данных Astra должна быть абсолютным путём.")
            return candidate.resolve()
        return default_data_root()

    async def on_config_changed(self, config):
        if not isinstance(config, dict):
            self.config = {}

    async def get_ui_contributions(self) -> list[UiContribution]:
        contributions = await super().get_ui_contributions()
        for contribution in contributions:
            contribution.transparent = True
        return contributions

    async def on_shutdown(self):
        self._cancel.set()

    @ui_call
    async def get_dashboard(self):
        try:
            root = self._data_root()
            local = {c["id"]: c for c in discover_local(root)}
            with self._lock:
                rows = []
                for component in self._catalog:
                    rows.append({"id": component["id"], "name": component["name"], "group": component["group"],
                                 "description": self._description(component["kind"]),
                                 "size_bytes": component["size_bytes"], "files_count": len(component["files"]),
                                 "installed": files_present(component, root),
                                 "available": self._mirror_state == "ready",
                                 "activation_required": component["activation_required"],
                                 "notes": self._notes(component["kind"])})
                    local.pop(component["id"], None)
                for component in local.values():
                    rows.append({k: v for k, v in component.items() if not k.startswith("_")})
                    rows[-1]["description"] = self._description(component["kind"])
                    rows[-1]["notes"] = self._notes(component["kind"])
                return {"mirror_configured": bool(self._source_url), "mirror_state": self._mirror_state,
                        "mirror_message": self._mirror_message, "data_root": str(root),
                        "components": sorted(rows, key=lambda c: (c["group"], c["name"])),
                        "quick_setup": self._setup_dashboard(),
                        "job": dict(self._job), "info": [
                            "Поддерживаются локальные модели Supertonic 3, Vox, Whisper и embeddings. Каталог определяется загруженными пакетами.",
                            "«Файлы на месте» означает наличие файлов. Выбор модели, запуск движка и активация выполняются в Astra.",
                            "Быстрая настройка добавляет среды исполнения, библиотеки и модели текущего каталога. Самих плагинов она не устанавливает.",
                        ]}
        except (OSError, ValueError) as exc:
            return {"mirror_configured": bool(self._source_url), "mirror_state": "error", "mirror_message": str(exc),
                    "data_root": "", "components": [], "job": dict(self._job), "info": []}

    @staticmethod
    def _description(kind):
        return {"supertonic": "Полный набор моделей и десять профилей локального голоса.",
                "vox": "Контейнер голоса Astra. Требует активации на этом компьютере.",
                "whisper": "Локальная модель распознавания речи.",
                "embeddings": "Модель локального семантического поиска."}[kind]

    @staticmethod
    def _notes(kind):
        return "Активация остаётся в Astra. Ключи аккаунта и keybox не копируются." if kind == "vox" else "Выберите модель в настройках Astra; при необходимости перезапустите программу."

    def _busy(self):
        return self._worker is not None and self._worker.is_alive()

    @ui_call
    async def refresh_mirror(self):
        with self._lock:
            if self._busy():
                return {"ok": False, "message": "Уже выполняется другая операция."}
            if not self._source_url:
                return {"ok": False, "message": "Сначала укажите ссылку на публичную папку."}
            self._mirror_state = "loading"
            self._mirror_message = "Получаю каталог с Яндекс Диска…"
            source_url = self._source_url
            self._worker = threading.Thread(target=self._refresh, args=(source_url,), daemon=True)
            self._worker.start()
        return {"ok": True, "message": "Обновление каталога началось."}

    def _refresh(self, source_url):
        try:
            data = PublicFolder(source_url).read_catalog()
            catalog = parse_catalog(data)
            atomic_json(self._storage / "catalog-cache.json", {"public_url": source_url, "catalog": json.loads(data)})
            with self._lock:
                self._catalog = catalog
                self._mirror_state = "ready"
                self._mirror_message = f"Зеркало подключено: компонентов — {len(catalog)}."
        except Exception as exc:
            with self._lock:
                self._mirror_state = "error"
                self._mirror_message = self._human_error(exc)

    def _update(self, **values):
        with self._lock:
            self._job.update(values)

    @staticmethod
    def _human_error(exc):
        if isinstance(exc, (ValueError, InterruptedError)):
            return str(exc)[:400]
        if isinstance(exc, (zipfile.BadZipFile, RuntimeError)):
            return "Архив повреждён или имеет неподдерживаемый формат."
        if isinstance(exc, OSError):
            return "Не удалось записать файлы. Проверьте свободное место, права и работу антивируса."
        return "Операция не завершена. Проверьте пакет и повторите попытку."

    def _begin_install(self, component_id, local_path=None):
        try:
            with self._lock:
                if self._busy():
                    return {"ok": False, "message": "Уже выполняется другая операция."}
                component = next((c for c in self._catalog if c["id"] == component_id), None)
                if component is None:
                    return {"ok": False, "message": "Компонент не найден в проверяемом каталоге."}
                root = self._data_root()
                if files_present(component, root):
                    return {"ok": False, "message": "Файлы уже установлены. Выберите компонент в настройках Astra."}
                if local_path is None and self._mirror_state != "ready":
                    return {"ok": False, "message": "Сначала обновите каталог зеркала."}
                if local_path is not None:
                    if not isinstance(local_path, str) or not Path(local_path).is_absolute():
                        raise ValueError("Укажите абсолютный путь к ZIP-пакету.")
                    path = Path(local_path).resolve()
                    if not path.is_file() or path.suffix.lower() != ".zip":
                        raise ValueError("ZIP-пакет не найден.")
                else:
                    path = None
                root.mkdir(parents=True, exist_ok=True)
                needed = component["size_bytes"] + (component["archive_size"] if path is None else 0) + 64 * 1024**2
                if shutil.disk_usage(root).free < needed:
                    raise ValueError("Для распаковки компонента недостаточно свободного места.")
                self._cancel = threading.Event()
                self._job = {"state": "verifying" if path else "downloading", "job_type": "model", "component_id": component["id"],
                             "component_name": component["name"], "downloaded_bytes": 0,
                             "total_bytes": component["archive_size"], "percent": 0,
                             "message": "Проверяю локальный пакет…" if path else "Начинаю загрузку с Диска…"}
                self._worker = threading.Thread(target=self._install, args=(dict(component), root, path, self._source_url), daemon=True)
                self._worker.start()
            return {"ok": True, "message": "Установка началась."}
        except (OSError, ValueError, TypeError) as exc:
            return {"ok": False, "message": self._human_error(exc)}

    def _install(self, component, root, archive, source_url):
        download_dir = None
        try:
            if archive is None:
                downloads = contained_path(root / "models", ".component-mirror-downloads")
                downloads.mkdir(parents=True, exist_ok=True)
                download_dir = Path(tempfile.mkdtemp(prefix="download-", dir=downloads))
                archive = download_dir / "package.zip"
                def progress(done):
                    self._update(downloaded_bytes=done, percent=round(done * 100 / component["archive_size"], 1))
                PublicFolder(source_url).download(component["archive"], archive, component["archive_size"], self._cancel, progress)
            install_archive(component, archive, root, self._cancel, self._update)
        except InterruptedError:
            self._update(state="cancelled", message="Операция отменена. Установленные ранее файлы сохранены.")
        except Exception as exc:
            self._update(state="error", message=self._human_error(exc))
        finally:
            if download_dir is not None:
                shutil.rmtree(download_dir, ignore_errors=True)

    @ui_call
    async def install_component(self, component_id: str = ""):
        return self._begin_install(component_id)

    @ui_call
    async def import_local(self, component_id: str = "", path: str = ""):
        return self._begin_install(component_id, path)

    @ui_call
    async def start_quick_setup(self):
        with self._lock:
            if self._busy():
                return {"ok": False, "message": "Уже выполняется другая операция."}
            if not supported_platform():
                return {"ok": False, "message": "Быстрая настройка поддерживает Windows x64."}
            try:
                root = self._data_root()
                # Quick setup's snapshot is shipped with the build; mirror data
                # cannot add installers or replace executable hashes.
                models = parse_catalog((BASE / "src" / "catalog.seed.json").read_bytes())
            except (ValueError, OSError) as exc:
                return {"ok": False, "message": self._human_error(exc)}
            self._cancel = threading.Event()
            self._job = {"state": "preparing", "job_type": "quick_setup", "component_id": "quick_setup",
                         "component_name": "Быстрая настройка Астры", "downloaded_bytes": 0, "total_bytes": 0,
                         "percent": 0, "step_index": 0, "steps_total": 19, "stage_label": "Подготовка",
                         "message": "Проверяю компьютер и набор зависимостей…"}
            configured = self.config.get("astra_executable", "") if isinstance(self.config, dict) else ""
            self._worker = threading.Thread(target=self._quick_setup, args=(root, models, configured), daemon=True)
            self._worker.start()
        return {"ok": True, "message": "Быстрая настройка началась."}

    def _quick_setup(self, root, models, astra_path):
        try:
            setup = QuickSetup(self._storage, root, self._source_url, models, self._cancel, self._update,
                               astra_path=astra_path if isinstance(astra_path, str) else "")
            result = setup.run()
            with self._lock:
                self._quick_result = result
            self._update(state="done", percent=100,
                         message="Компоненты готовы. Закройте Astra и один раз используйте ярлык «Mirror-upload - Завершить настройку» на рабочем столе. Затем запускайте Astra обычным способом.")
        except InterruptedError as exc:
            self._update(state="cancelled", message=str(exc))
        except Exception as exc:
            self._update(state="error", message=self._human_error(exc))

    @ui_call
    async def restore_user_environment(self):
        with self._lock:
            if self._busy():
                return {"ok": False, "message": "Дождитесь завершения текущей операции."}
            try:
                retained = restore(self._storage / "setup" / "user-environment-backup.json")
                return {"ok": True, "message": "Настройки Windows восстановлены. Перезапустите программы." + (" Более поздние изменения сохранены: " + ", ".join(retained) if retained else "")}
            except (ValueError, OSError) as exc:
                return {"ok": False, "message": self._human_error(exc)}

    @ui_call
    async def cancel_install(self):
        with self._lock:
            if self._job["state"] not in BUSY_STATES:
                return {"ok": False, "message": "Нет активной установки."}
            self._cancel.set()
        return {"ok": True, "message": "Отменяю операцию…"}


if __name__ == "__main__":
    ComponentMirror().run()
