"""Verify in staging before committing any model file. Never overwrite files."""
from __future__ import annotations

import json
import os
import shutil
import stat
import tempfile
import zipfile
from pathlib import Path

from .catalog import contained_path, paths_for, safe_relative, sha256_file


def install_archive(component: dict, archive: Path, data_root: Path, cancel, update):
    archive = archive.resolve()
    if not archive.is_file() or archive.stat().st_size != component["archive_size"]:
        raise ValueError("Размер локального архива отличается от каталога.")
    update(state="verifying", message="Проверяю целостность пакета…", percent=100)
    if sha256_file(archive, cancel) != component["archive_sha256"]:
        raise ValueError("SHA-256 архива не совпала. Установка отменена.")
    models = data_root / "models"
    models.mkdir(parents=True, exist_ok=True)
    staging_parent = contained_path(models, ".component-mirror-staging")
    staging_parent.mkdir(exist_ok=True)
    stage = Path(tempfile.mkdtemp(prefix="install-", dir=staging_parent))
    committed = []
    try:
        expected = {f["path"]: f for f in component["files"]}
        with zipfile.ZipFile(archive) as package:
            entries = package.infolist()
            if len(entries) != len(expected):
                raise ValueError("В архиве лишние файлы или неполный комплект.")
            seen = set()
            for entry in entries:
                name = safe_relative(entry.filename)
                mode = (entry.external_attr >> 16) & 0xFFFF
                if name.casefold() in seen or name not in expected or entry.is_dir() or stat.S_ISLNK(mode) or entry.flag_bits & 1:
                    raise ValueError("Архив содержит неподдерживаемые записи.")
                seen.add(name.casefold())
                descriptor = expected[name]
                if entry.file_size != descriptor["size"]:
                    raise ValueError("Размер файла в архиве отличается от каталога.")
                if cancel.is_set():
                    raise InterruptedError("Установка отменена.")
                output = contained_path(stage, name)
                output.parent.mkdir(parents=True, exist_ok=True)
                update(state="verifying", message=f"Проверяю {Path(name).name}…")
                done = 0
                with package.open(entry) as source, output.open("xb") as target:
                    while chunk := source.read(1024 * 1024):
                        if cancel.is_set():
                            raise InterruptedError("Установка отменена.")
                        done += len(chunk)
                        if done > descriptor["size"]:
                            raise ValueError("Распакованный файл превышает ожидаемый размер.")
                        target.write(chunk)
                if done != descriptor["size"] or sha256_file(output, cancel) != descriptor["sha256"]:
                    raise ValueError("Файл повреждён. Ничего не установлено.")
        if component["kind"] == "vox":
            receipt = json.loads((stage / "astra_neutral.avox.manifest.json").read_text("utf-8"))
            container = expected["astra_neutral.avox"]
            if receipt.get("size") != container["size"] or receipt.get("sha256") != container["sha256"]:
                raise ValueError("Квитанция Vox не соответствует контейнеру.")
        targets = paths_for(component, data_root)
        # Validate every existing file before the first commit. Matching files
        # in an interrupted installation can be retained; conflicting ones
        # require removal through Astra rather than a destructive overwrite.
        retained = set()
        for descriptor, target in zip(component["files"], targets):
            if target.exists():
                if not target.is_file() or sha256_file(target, cancel) != descriptor["sha256"]:
                    raise ValueError("В папке Astra уже есть другой файл. Удалите компонент штатно в Astra перед установкой.")
                retained.add(target)
        update(state="installing", message="Устанавливаю проверенные файлы…")
        for descriptor, target in zip(component["files"], targets):
            if cancel.is_set():
                raise InterruptedError("Установка отменена.")
            # Recheck parents immediately before use, including junctions.
            target = contained_path(models, f"{component['destination']}/{descriptor['path']}")
            if target in retained:
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            source = contained_path(stage, descriptor["path"])
            # Windows rename is atomic and refuses an existing destination.
            # POSIX rename overwrites, so use link there for no-replace commit.
            identity = source.stat()
            if os.name == "nt":
                os.rename(source, target)
            else:
                os.link(source, target)
            committed.append((target, identity.st_dev, identity.st_ino))
        notes = "Файлы установлены. Выберите модель в настройках Astra; при необходимости перезапустите программу."
        if component["kind"] == "vox":
            notes = "Контейнер Vox установлен. Активация выполняется самой Astra на этом компьютере."
        update(state="done", message=notes, percent=100)
    except BaseException:
        for target, device, inode in reversed(committed):
            try:
                # Roll back only files we created, preserving concurrent
                # replacements made by Astra or the user.
                identity = target.stat()
                if identity.st_dev == device and identity.st_ino == inode:
                    target.unlink()
            except OSError:
                pass
        raise
    finally:
        shutil.rmtree(stage)
