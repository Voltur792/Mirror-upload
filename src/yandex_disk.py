"""Public-folder downloads only. Never requests an account OAuth token."""
from __future__ import annotations

import json
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode, urlsplit
from urllib.request import Request, urlopen

from .catalog import MAX_CATALOG_BYTES, safe_relative

API_ROOT = "https://cloud-api.yandex.net/v1/disk/public/resources"
SHARE_HOSTS = {"disk.yandex.ru", "disk.yandex.com", "yadi.sk"}


def validate_public_url(value: str) -> str:
    if not isinstance(value, str) or len(value) > 2048:
        raise ValueError("Вставьте публичную ссылку на отдельную папку Яндекс Диска.")
    value = value.strip()
    parsed = urlsplit(value)
    if parsed.scheme != "https" or parsed.hostname not in SHARE_HOSTS or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise ValueError("Нужна HTTPS-ссылка на публичную папку disk.yandex.ru или yadi.sk.")
    if not parsed.path.startswith("/d/") or len(parsed.path) < 5 or parsed.query or parsed.fragment:
        raise ValueError("Используйте обычную публичную ссылку вида https://disk.yandex.ru/d/… без дополнительных параметров.")
    return value.rstrip("/")


def _request(url: str, timeout: int = 25):
    try:
        response = urlopen(Request(url, headers={"User-Agent": "Astra-Component-Mirror/0.1", "Accept-Encoding": "identity"}), timeout=timeout)
        if urlsplit(response.geturl()).scheme != "https":
            response.close()
            raise ValueError("Сервер перенаправил загрузку на незащищённое соединение.")
        return response
    except HTTPError as exc:
        messages = {403: "Яндекс Диск ограничил скачивание или папка закрыта.",
                    404: "Файл не найден на Диске. Проверьте папку и catalog.json.",
                    429: "Слишком много запросов к Диску. Попробуйте позже."}
        raise ValueError(messages.get(exc.code, f"Яндекс Диск вернул ошибку HTTP {exc.code}.")) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise ValueError("Не удалось соединиться с Яндекс Диском. Проверьте доступ к сервису.") from exc


class PublicFolder:
    def __init__(self, public_url: str):
        self.public_url = validate_public_url(public_url)

    def download_url(self, relative: str) -> str:
        relative = safe_relative(relative)
        query = urlencode({"public_key": self.public_url, "path": "/" + relative})
        with _request(API_ROOT + "/download?" + query) as response:
            data = response.read(64 * 1024 + 1)
        if len(data) > 64 * 1024:
            raise ValueError("Некорректный ответ Яндекс Диска.")
        try:
            href = json.loads(data)["href"]
            parsed = urlsplit(href)
        except (ValueError, KeyError, TypeError) as exc:
            raise ValueError("Диск не вернул ссылку скачивания.") from exc
        host = parsed.hostname or ""
        allowed = host.endswith((".yandex.net", ".yandex.ru", ".yandex.com", ".yandexcloud.net"))
        if parsed.scheme != "https" or not allowed or parsed.username or parsed.password or parsed.port not in (None, 443):
            raise ValueError("Диск вернул неподдерживаемый адрес загрузки.")
        return href

    def read_catalog(self) -> bytes:
        with _request(self.download_url("catalog.json")) as response:
            data = response.read(MAX_CATALOG_BYTES + 1)
        if len(data) > MAX_CATALOG_BYTES:
            raise ValueError("Каталог зеркала слишком большой.")
        return data

    def download(self, relative: str, target: Path, expected_size: int, cancel, progress):
        # Download URLs expire: resolve a fresh URL for every attempt.
        with _request(self.download_url(relative), timeout=20) as response, target.open("xb") as stream:
            header = response.headers.get("Content-Length")
            if header and int(header) != expected_size:
                raise ValueError("Размер пакета на Диске отличается от каталога.")
            done = 0
            last_progress = 0.0
            while True:
                if cancel.is_set():
                    raise InterruptedError("Загрузка отменена.")
                chunk = response.read(1024 * 512)
                if not chunk:
                    break
                done += len(chunk)
                if done > expected_size:
                    raise ValueError("Сервер прислал больше данных, чем указано в каталоге.")
                stream.write(chunk)
                now = time.monotonic()
                if now - last_progress > 0.1:
                    progress(done)
                    last_progress = now
            progress(done)
            if done != expected_size:
                raise ValueError("Пакет скачан не полностью. Повторите загрузку.")
