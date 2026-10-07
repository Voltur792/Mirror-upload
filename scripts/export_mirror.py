"""Prepare public-folder packages from local models, excluding account data."""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import tempfile
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from src.catalog import default_data_root, discover_local, parse_catalog, sha256_file


def export(data_root: Path, output: Path, selected: set[str] | None = None) -> dict:
    data_root, output = data_root.resolve(), output.resolve()
    try:
        output.relative_to(data_root)
    except ValueError:
        pass
    else:
        raise ValueError("Папка зеркала должна находиться отдельно от данных Astra.")
    inventory = discover_local(data_root)
    if selected:
        available = {item["id"] for item in inventory}
        if selected - available:
            raise ValueError("Выбранные компоненты не установлены полностью: " + ", ".join(sorted(selected - available)))
        inventory = [item for item in inventory if item["id"] in selected]
    output.mkdir(parents=True, exist_ok=True)
    packages = output / "packages"
    packages.mkdir(exist_ok=True)
    entries = []
    for item in inventory:
        name = item["id"].replace(":", "-") + ".zip"
        target = packages / name
        if target.exists():
            raise ValueError(f"Пакет уже существует: {target}. Используйте новую пустую папку для следующей версии зеркала.")
        fd, temporary_name = tempfile.mkstemp(prefix=".package-", suffix=".tmp", dir=packages)
        os.close(fd)
        temporary = Path(temporary_name)
        descriptors = []
        try:
            print(f"Подготавливаю: {item['name']} ({item['size_bytes'] / 1024**2:.1f} МБ)", flush=True)
            with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as archive:
                for relative in item["_relative"]:
                    source = item["_folder"] / relative
                    if source.is_symlink():
                        raise ValueError("Экспорт символических ссылок не поддерживается.")
                    descriptor = {"path": relative, "size": source.stat().st_size, "sha256": sha256_file(source)}
                    archive.write(source, relative)
                    descriptors.append(descriptor)
            if item["kind"] == "vox":
                receipt = json.loads((item["_folder"] / "astra_neutral.avox.manifest.json").read_text("utf-8"))
                container = next(f for f in descriptors if f["path"].endswith(".avox"))
                if receipt.get("size") != container["size"] or receipt.get("sha256") != container["sha256"]:
                    raise ValueError("Квитанция Vox не соответствует файлу; штатно восстановите загрузку в Astra.")
            entry = {"kind": item["kind"], "model_id": item["model_id"], "name": item["name"],
                     "archive": "packages/" + name, "archive_size": temporary.stat().st_size,
                     "archive_sha256": sha256_file(temporary), "files": descriptors}
            parse_catalog(json.dumps({"schema_version": 1, "components": [entry]}).encode())
            with zipfile.ZipFile(temporary) as archive:
                for descriptor in descriptors:
                    digest = hashlib.sha256()
                    with archive.open(descriptor["path"]) as stream:
                        while chunk := stream.read(1024 * 1024):
                            digest.update(chunk)
                    if digest.hexdigest() != descriptor["sha256"]:
                        raise ValueError("Модель изменилась при экспорте. Повторите при закрытой Astra.")
            if os.name == "nt":
                os.rename(temporary, target)
            else:
                os.link(temporary, target)
            entries.append(entry)
        finally:
            temporary.unlink(missing_ok=True)
    catalog = {"schema_version": 1, "components": entries}
    parse_catalog(json.dumps(catalog).encode())
    (output / "catalog.json").write_text(json.dumps(catalog, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"Готово: {len(entries)} компонентов. Папка для загрузки на Диск: {output}", flush=True)
    return catalog


def main():
    parser = argparse.ArgumentParser(description="Подготовить пакеты моделей для публичной папки Яндекс Диска.")
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--astra-data", type=Path)
    parser.add_argument("--component", action="append", help="Например whisper:large-v3-turbo-q5_0; по умолчанию все установленные модели.")
    args = parser.parse_args()
    try:
        export(args.astra_data or default_data_root(), args.output, set(args.component) if args.component else None)
    except (OSError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
