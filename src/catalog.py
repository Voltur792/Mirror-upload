"""Bounded, data-only packages for Astra's built-in local model managers."""
from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path, PurePosixPath
from typing import Any

MAX_COMPONENT_BYTES = 10 * 1024**3
MAX_CATALOG_BYTES = 2 * 1024**2
SUPERTONIC_FILES = (
    "onnx/duration_predictor.onnx", "onnx/text_encoder.onnx",
    "onnx/vector_estimator.onnx", "onnx/vocoder.onnx", "onnx/tts.json",
    "onnx/unicode_indexer.json",
    *(f"voice_styles/{gender}{i}.json" for gender in "FM" for i in range(1, 6)),
)
EMBEDDING_IDS = {
    "m2v-qwen3-small", "m2v-e5-small-multilingual", "m2v-qwen3-1024d",
    "potion-multilingual-128M",
}
EMBEDDING_FILES = {"config.json", "model.safetensors", "tokenizer.json"}
GROUP_NAMES = {"supertonic": "Голоса", "vox": "Голоса", "whisper": "Распознавание", "embeddings": "Поиск"}


def default_data_root() -> Path:
    override = os.environ.get("COMPONENT_MIRROR_ASTRA_DATA")
    if override:
        return Path(override).resolve()
    if os.name != "nt":
        raise ValueError("Эта версия плагина поддерживает Windows. Укажите папку данных в настройках.")
    appdata = os.environ.get("APPDATA")
    if not appdata:
        raise ValueError("Не удалось определить APPDATA. Укажите папку данных Astra в настройках.")
    return (Path(appdata) / "astra" / "astra" / "data").resolve()


def safe_relative(value: Any) -> str:
    if not isinstance(value, str) or not value or len(value) > 240:
        raise ValueError("Некорректное имя файла в каталоге.")
    if "\\" in value or ":" in value or "\x00" in value:
        raise ValueError("Недопустимый путь в каталоге.")
    parts = value.split("/")
    if any(p in {"", ".", ".."} or p.endswith((".", " ")) for p in parts):
        raise ValueError("Недопустимый путь в каталоге.")
    if any(not re.fullmatch(r"[A-Za-z0-9._-]+", p) for p in parts):
        raise ValueError("Недопустимое имя файла в каталоге.")
    if any(p.split(".")[0].upper() in {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))} for p in parts):
        raise ValueError("Зарезервированное имя файла.")
    return str(PurePosixPath(value))


def contained_path(root: Path, relative: str) -> Path:
    relative = safe_relative(relative)
    root = root.resolve()
    candidate = root.joinpath(*relative.split("/"))
    try:
        candidate.resolve().relative_to(root)
    except ValueError as exc:
        raise ValueError("Путь выходит за папку моделей Astra.") from exc
    current = root
    for part in relative.split("/"):
        current /= part
        if current.is_symlink() or (hasattr(current, "is_junction") and current.is_junction()):
            raise ValueError("Ссылки и junction в папке установки не поддерживаются.")
    return candidate


def sha256_file(path: Path, cancel=None, progress=None) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        while chunk := stream.read(1024 * 1024):
            if cancel is not None and cancel.is_set():
                raise InterruptedError("Операция отменена.")
            digest.update(chunk)
            if progress:
                progress(len(chunk))
    return digest.hexdigest()


def _digest(value: Any) -> str:
    if not isinstance(value, str) or not re.fullmatch("[a-f0-9]{64}", value):
        raise ValueError("В каталоге отсутствует корректная SHA-256.")
    return value


def _size(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or not 0 < value <= MAX_COMPONENT_BYTES:
        raise ValueError("Недопустимый размер файла в каталоге.")
    return value


def validate_component(raw: Any) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("Некорректная запись компонента.")
    kind, model = raw.get("kind"), raw.get("model_id")
    if kind not in GROUP_NAMES or not isinstance(model, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}", model):
        raise ValueError("Каталог содержит неподдерживаемый компонент.")
    if kind == "supertonic":
        if model != "supertonic-3":
            raise ValueError("Поддерживается только Supertonic 3.")
        destination = "supertonic-3"
        expected = set(SUPERTONIC_FILES)
    elif kind == "whisper":
        if not re.fullmatch(r"(?:tiny|base|small|medium|large-v1|large-v2|large-v3|large-v3-turbo)(?:\.en)?(?:-(?:q5_0|q5_1|q8_0|tdrz))?", model):
            raise ValueError("Неизвестный формат модели Whisper.")
        destination = "whisper"
        expected = {f"ggml-{model}.bin"}
    elif kind == "vox":
        if model != "astra-neutral":
            raise ValueError("Неизвестный контейнер Vox.")
        destination = "vox"
        expected = {"astra_neutral.avox", "astra_neutral.avox.manifest.json"}
    else:
        # Allow known Model2Vec families; do not guess which missing model a
        # specific Astra version offers. Its exported catalog supplies that ID.
        if not (model in EMBEDDING_IDS or model.startswith("m2v-qwen3-")):
            raise ValueError("Неизвестная модель embeddings.")
        destination = f"embeddings/{model}"
        expected = EMBEDDING_FILES
    files = raw.get("files")
    if not isinstance(files, list) or not 0 < len(files) <= 32:
        raise ValueError("В компоненте нет списка файлов.")
    cleaned = []
    seen = set()
    for record in files:
        if not isinstance(record, dict):
            raise ValueError("Некорректное описание файла.")
        name = safe_relative(record.get("path"))
        if name not in expected or name.casefold() in seen:
            raise ValueError("Лишний или повторяющийся файл в компоненте.")
        seen.add(name.casefold())
        cleaned.append({"path": name, "size": _size(record.get("size")), "sha256": _digest(record.get("sha256"))})
    if {f["path"] for f in cleaned} != expected:
        raise ValueError("Компонент содержит неполный набор обязательных файлов.")
    if sum(f["size"] for f in cleaned) > MAX_COMPONENT_BYTES:
        raise ValueError("Компонент превышает допустимый размер.")
    archive = safe_relative(raw.get("archive"))
    if not archive.endswith(".zip") or len(archive.split("/")) > 3:
        raise ValueError("Компонент должен быть ZIP-пакетом.")
    component_id = f"{kind}:{model}"
    name = raw.get("name", model)
    if not isinstance(name, str) or not 0 < len(name) <= 120:
        raise ValueError("Некорректное название компонента.")
    return {
        "id": component_id, "kind": kind, "model_id": model, "name": name,
        "destination": destination, "archive": archive,
        "archive_size": _size(raw.get("archive_size")),
        "archive_sha256": _digest(raw.get("archive_sha256")), "files": cleaned,
        "group": GROUP_NAMES[kind], "size_bytes": sum(f["size"] for f in cleaned),
        "activation_required": kind == "vox",
    }


def parse_catalog(data: bytes) -> list[dict]:
    if len(data) > MAX_CATALOG_BYTES:
        raise ValueError("Каталог слишком большой.")
    try:
        raw = json.loads(data)
    except (ValueError, UnicodeError) as exc:
        raise ValueError("Не удалось прочитать catalog.json.") from exc
    if not isinstance(raw, dict) or raw.get("schema_version") != 1:
        raise ValueError("Неподдерживаемая версия каталога.")
    entries = raw.get("components")
    if not isinstance(entries, list) or len(entries) > 100:
        raise ValueError("Некорректный список компонентов.")
    result = [validate_component(entry) for entry in entries]
    ids = [item["id"] for item in result]
    archives = [item["archive"].casefold() for item in result]
    if len(set(ids)) != len(ids) or len(set(archives)) != len(archives):
        raise ValueError("В каталоге повторяются компоненты или архивы.")
    return result


def paths_for(component: dict, data_root: Path) -> list[Path]:
    model_root = data_root / "models"
    return [contained_path(model_root, f"{component['destination']}/{f['path']}") for f in component["files"]]


def files_present(component: dict, data_root: Path) -> bool:
    try:
        return all(p.is_file() and p.stat().st_size == f["size"] for p, f in zip(paths_for(component, data_root), component["files"]))
    except (OSError, ValueError):
        return False


def discover_local(data_root: Path) -> list[dict]:
    """Inventory only. No hashes or file reads on a dashboard poll."""
    models = data_root / "models"
    found = []
    candidates = [("supertonic", "supertonic-3", "Локальные голоса Supertonic 3", models / "supertonic-3", SUPERTONIC_FILES),
                  ("vox", "astra-neutral", "Голос Astra · Vox", models / "vox", ("astra_neutral.avox", "astra_neutral.avox.manifest.json"))]
    whisper = models / "whisper"
    if whisper.is_dir():
        for path in sorted(whisper.glob("ggml-*.bin")):
            candidates.append(("whisper", path.stem.removeprefix("ggml-"), f"Whisper {path.stem.removeprefix('ggml-')}", whisper, (path.name,)))
    embedding = models / "embeddings"
    if embedding.is_dir():
        for folder in sorted(embedding.iterdir()):
            if folder.is_dir() and (folder.name in EMBEDDING_IDS or folder.name.startswith("m2v-qwen3-")):
                candidates.append(("embeddings", folder.name, f"Поиск · {folder.name}", folder, tuple(sorted(EMBEDDING_FILES))))
    for kind, model, name, folder, relative in candidates:
        paths = [folder / p for p in relative]
        if not all(p.is_file() and p.stat().st_size > 0 for p in paths):
            continue
        found.append({"id": f"{kind}:{model}", "kind": kind, "model_id": model, "name": name,
                      "group": GROUP_NAMES[kind], "size_bytes": sum(p.stat().st_size for p in paths),
                      "files_count": len(paths), "installed": True, "available": False,
                      "activation_required": kind == "vox", "_folder": folder, "_relative": relative})
    return found
