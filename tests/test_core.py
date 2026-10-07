"""Offline filesystem and real SDK UI-routing regression checks."""
from __future__ import annotations

import copy
import hashlib
import io
import json
import os
import stat
import sys
import tempfile
import threading
import unittest
import zipfile
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from astra_plugin_sdk.testing import Harness, fuzz_configs
from src.catalog import SUPERTONIC_FILES, parse_catalog, validate_component
from src.installer import install_archive
from src.plugin import ComponentMirror
from src.yandex_disk import PublicFolder, validate_public_url


def make_package(folder: Path, kind="whisper", model="tiny", extra=None):
    if kind == "whisper":
        payloads = {f"ggml-{model}.bin": b"test-model-data" * 50}
    elif kind == "supertonic":
        model = "supertonic-3"
        payloads = {name: ("fixture:" + name).encode() for name in SUPERTONIC_FILES}
    elif kind == "embeddings":
        model = "m2v-qwen3-small"
        payloads = {"config.json": b"{}", "tokenizer.json": b"{}", "model.safetensors": b"model2vec"}
    else:
        model = "astra-neutral"
        data = b"encrypted-vox-fixture"
        receipt = {"size": len(data), "sha256": hashlib.sha256(data).hexdigest()}
        payloads = {"astra_neutral.avox": data, "astra_neutral.avox.manifest.json": json.dumps(receipt).encode()}
    archive = folder / (kind + ".zip")
    with zipfile.ZipFile(archive, "w") as package:
        for name, data in payloads.items():
            package.writestr(name, data)
        for name, data in (extra or {}).items():
            package.writestr(name, data)
    raw = {"kind": kind, "model_id": model, "name": "Fixture",
           "archive": "packages/" + archive.name, "archive_size": archive.stat().st_size,
           "archive_sha256": hashlib.sha256(archive.read_bytes()).hexdigest(),
           "files": [{"path": name, "size": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in payloads.items()]}
    return validate_component(raw), archive, payloads


class ModelPackageTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.root = self.folder / "astra-data"
        self.cancel = threading.Event()
        self.updates = []

    def tearDown(self):
        self.temporary.cleanup()

    def update(self, **kwargs):
        self.updates.append(kwargs)

    def test_all_four_model_families_install_original_bytes(self):
        for kind in ("whisper", "supertonic", "embeddings", "vox"):
            with self.subTest(kind=kind):
                component, archive, payloads = make_package(self.folder, kind)
                install_archive(component, archive, self.root, self.cancel, self.update)
                for relative, data in payloads.items():
                    path = self.root / "models" / component["destination"] / relative
                    self.assertEqual(path.read_bytes(), data)
                self.assertEqual(self.updates[-1]["state"], "done")

    def test_corrupt_archive_does_not_install(self):
        component, archive, _ = make_package(self.folder)
        with archive.open("r+b") as stream:
            stream.seek(10)
            stream.write(b"X")
        with self.assertRaisesRegex(ValueError, "SHA-256"):
            install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertFalse((self.root / "models" / "whisper").exists())

    def test_wrong_inner_hash_does_not_install(self):
        component, archive, _ = make_package(self.folder)
        component["files"][0]["sha256"] = "0" * 64
        with self.assertRaisesRegex(ValueError, "повреждён"):
            install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertFalse((self.root / "models" / "whisper").exists())

    def test_extra_file_is_rejected_before_commit(self):
        component, archive, _ = make_package(self.folder, extra={"../settings.json": b"attack"})
        with self.assertRaises(ValueError):
            install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertFalse((self.root / "models" / "whisper").exists())

    def test_symlink_entry_is_rejected(self):
        component, archive, _ = make_package(self.folder)
        entry = zipfile.ZipInfo("ggml-tiny.bin")
        entry.create_system = 3
        entry.external_attr = (stat.S_IFLNK | 0o777) << 16
        with zipfile.ZipFile(archive, "w") as package:
            package.writestr(entry, b"test-model-data" * 50)
        component["archive_size"] = archive.stat().st_size
        component["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        with self.assertRaises(ValueError):
            install_archive(component, archive, self.root, self.cancel, self.update)

    def test_existing_conflicting_model_is_preserved(self):
        component, archive, _ = make_package(self.folder)
        target = self.root / "models" / "whisper" / "ggml-tiny.bin"
        target.parent.mkdir(parents=True)
        target.write_bytes(b"user-model")
        with self.assertRaisesRegex(ValueError, "уже есть другой"):
            install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertEqual(target.read_bytes(), b"user-model")

    def test_identical_partial_files_can_be_retained(self):
        component, archive, payloads = make_package(self.folder, "embeddings")
        target = self.root / "models" / component["destination"] / "config.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(payloads["config.json"])
        install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertTrue((target.parent / "model.safetensors").exists())

    def test_cancel_before_commit_leaves_no_model_files(self):
        component, archive, _ = make_package(self.folder)
        self.cancel.set()
        with self.assertRaises(InterruptedError):
            install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertFalse((self.root / "models" / "whisper").exists())

    def test_commit_failure_rolls_back_only_new_files(self):
        component, archive, payloads = make_package(self.folder, "embeddings")
        target = self.root / "models" / component["destination"] / "config.json"
        target.parent.mkdir(parents=True)
        target.write_bytes(payloads["config.json"])
        original = os.rename if os.name == "nt" else os.link
        calls = 0
        def failing_commit(source, destination):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise OSError("simulated disk failure")
            return original(source, destination)
        method = "src.installer.os.rename" if os.name == "nt" else "src.installer.os.link"
        with patch(method, side_effect=failing_commit):
            with self.assertRaises(OSError):
                install_archive(component, archive, self.root, self.cancel, self.update)
        self.assertEqual(target.read_bytes(), b"{}")
        self.assertFalse((target.parent / "model.safetensors").exists())
        self.assertFalse((target.parent / "tokenizer.json").exists())

    def test_vox_receipt_must_match_container(self):
        component, archive, payloads = make_package(self.folder, "vox")
        payloads["astra_neutral.avox.manifest.json"] = b'{"size":22,"sha256":"wrong"}'
        with zipfile.ZipFile(archive, "w") as package:
            for name, data in payloads.items():
                package.writestr(name, data)
        component["archive_size"] = archive.stat().st_size
        component["archive_sha256"] = hashlib.sha256(archive.read_bytes()).hexdigest()
        for descriptor in component["files"]:
            descriptor["size"] = len(payloads[descriptor["path"]])
            descriptor["sha256"] = hashlib.sha256(payloads[descriptor["path"]]).hexdigest()
        with self.assertRaisesRegex(ValueError, "Квитанция"):
            install_archive(component, archive, self.root, self.cancel, self.update)

    def test_catalog_rejects_credentials_executables_and_paths(self):
        component, _, _ = make_package(self.folder)
        for path in ("../settings.json", "daemon.token", "astra_neutral.keybox", "Astra.exe", "C:/file", "ggml-tiny.bin:stream", "CON.bin"):
            with self.subTest(path=path):
                raw = copy.deepcopy(component)
                raw["files"][0]["path"] = path
                with self.assertRaises(ValueError):
                    validate_component(raw)

    def test_catalog_rejects_duplicate_ids_and_incomplete_bundles(self):
        component, _, _ = make_package(self.folder)
        with self.assertRaises(ValueError):
            parse_catalog(json.dumps({"schema_version": 1, "components": [component, component]}).encode())
        supertonic, _, _ = make_package(self.folder, "supertonic")
        supertonic["files"].pop()
        with self.assertRaises(ValueError):
            validate_component(supertonic)


class FakeResponse(io.BytesIO):
    def __init__(self, data, size=None):
        super().__init__(data)
        self.headers = {"Content-Length": str(size)} if size is not None else {}


class DiskTests(unittest.TestCase):
    def test_public_links_only(self):
        self.assertEqual(validate_public_url(" https://disk.yandex.ru/d/demo "), "https://disk.yandex.ru/d/demo")
        for value in ("http://disk.yandex.ru/d/demo", "https://example.com/d/demo", "https://disk.yandex.ru/client/disk", "https://user:password@disk.yandex.ru/d/demo", "https://disk.yandex.ru/d/demo?token=x"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                validate_public_url(value)

    def test_api_uses_public_key_and_relative_path(self):
        with patch("src.yandex_disk._request", return_value=FakeResponse(b'{"href":"https://downloader.disk.yandex.ru/d/file"}')) as request:
            url = PublicFolder("https://disk.yandex.ru/d/demo").download_url("packages/whisper-tiny.zip")
        self.assertTrue(url.startswith("https://downloader.disk.yandex.ru/"))
        self.assertIn("public_key=https%3A%2F%2Fdisk.yandex.ru%2Fd%2Fdemo", request.call_args.args[0])
        self.assertIn("path=%2Fpackages%2Fwhisper-tiny.zip", request.call_args.args[0])

    def test_non_yandex_download_address_is_rejected(self):
        with patch("src.yandex_disk._request", return_value=FakeResponse(b'{"href":"https://example.com/file"}')):
            with self.assertRaises(ValueError):
                PublicFolder("https://disk.yandex.ru/d/demo").download_url("catalog.json")

    def test_download_stream_enforces_catalog_size(self):
        with tempfile.TemporaryDirectory() as directory:
            folder = PublicFolder("https://disk.yandex.ru/d/demo")
            with patch.object(folder, "download_url", return_value="https://downloader.disk.yandex.ru/file"), patch("src.yandex_disk._request", return_value=FakeResponse(b"longer")):
                with self.assertRaises(ValueError):
                    folder.download("package.zip", Path(directory) / "package.zip", 3, threading.Event(), lambda _: None)


class PluginTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.folder = Path(self.temporary.name)
        self.environment = patch.dict(os.environ, {"COMPONENT_MIRROR_STATE": str(self.folder / "state"), "COMPONENT_MIRROR_ASTRA_DATA": str(self.folder / "astra")})
        self.environment.start()

    def tearDown(self):
        self.environment.stop()
        self.temporary.cleanup()

    def test_real_sdk_routes_dashboard_and_handles_fuzzy_configs(self):
        with Harness(ComponentMirror()) as harness:
            self.assertTrue(harness.health()[0])
            for config in fuzz_configs():
                harness.set_config(config)
            harness.set_config({})
            self.assertEqual(harness.ui_call("get_dashboard").json["mirror_state"], "empty")
            self.assertFalse(harness.ui_call("install_component", component_id="whisper:tiny").json["ok"])

    def test_old_custom_link_is_ignored_and_dashboard_does_not_expose_source(self):
        plugin = ComponentMirror()
        plugin._storage.mkdir(parents=True, exist_ok=True)
        (plugin._storage / "settings.json").write_text(json.dumps({"public_url": "https://disk.yandex.ru/d/demo"}), encoding="utf-8")
        restarted = ComponentMirror()
        self.assertNotEqual(restarted._source_url, "https://disk.yandex.ru/d/demo")
        with Harness(restarted) as harness:
            dashboard = harness.ui_call("get_dashboard").json
            self.assertNotIn("source_url", dashboard)
            self.assertTrue(dashboard["mirror_configured"])
            self.assertFalse(any(c["available"] for c in dashboard["components"]))

    def test_background_local_install_through_real_ui_rpc(self):
        component, archive, payloads = make_package(self.folder)
        plugin = ComponentMirror()
        plugin._catalog = [component]
        with Harness(plugin) as harness:
            result = harness.ui_call("import_local", component_id=component["id"], path=str(archive)).json
            self.assertTrue(result["ok"], result)
            plugin._worker.join(timeout=5)
            dashboard = harness.ui_call("get_dashboard").json
            self.assertEqual(dashboard["job"]["state"], "done", dashboard["job"])
            self.assertTrue(dashboard["components"][0]["installed"])
            self.assertFalse(harness.ui_call("install_component", component_id=component["id"]).json["ok"])
        self.assertEqual((self.folder / "astra/models/whisper/ggml-tiny.bin").read_bytes(), payloads["ggml-tiny.bin"])


if __name__ == "__main__":
    unittest.main()
