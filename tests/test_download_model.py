"""Downloader integrity tests with tiny fixtures and fully mocked networking."""

from __future__ import annotations

import contextlib
import hashlib
import importlib.util
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("kvc_download_model", PROJECT_ROOT / "scripts/download_model.py")
downloader = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(downloader)


class DownloadModelTests(unittest.TestCase):
    content = b"small test weight fixture\n"

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.destination = self.root / "model"
        self.destination.mkdir()

    def item(self, path="model.safetensors", *, lfs=True):
        result = {"path": path, "size": len(self.content)}
        if lfs:
            result["lfs"] = {"oid": hashlib.sha256(self.content).hexdigest()}
        else:
            result["oid"] = hashlib.sha1(f"blob {len(self.content)}\0".encode() + self.content).hexdigest()
        return result

    def fetch(self, item):
        with contextlib.redirect_stdout(io.StringIO()):
            return downloader.fetch_file(item, "Qwen/Qwen3.5-9B", downloader.DEFAULT_REVISION, self.destination)

    @staticmethod
    def response(payload, *, status=200):
        response = io.BytesIO(payload)
        response.status = status
        return response

    def test_lfs_sha256_and_git_blob_hash_are_distinct_and_correct(self):
        path = self.destination / "fixture"
        path.write_bytes(self.content)
        self.assertEqual(downloader.digest_file(path, True), hashlib.sha256(self.content).hexdigest())
        self.assertEqual(
            downloader.digest_file(path, False),
            hashlib.sha1(f"blob {len(self.content)}\0".encode() + self.content).hexdigest(),
        )
        self.assertNotEqual(downloader.digest_file(path, False), hashlib.sha1(self.content).hexdigest())

    def test_existing_verified_file_is_reused_without_network(self):
        (self.destination / "model.safetensors").write_bytes(self.content)
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            metadata = self.fetch(self.item())
        network.assert_not_called()
        self.assertEqual(metadata["hash_algorithm"], "sha256")

    def test_existing_mismatched_file_is_never_overwritten(self):
        target = self.destination / "model.safetensors"
        target.write_bytes(b"user-owned-existing-file")
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "refusing overwrite"):
                self.fetch(self.item())
        network.assert_not_called()
        self.assertEqual(target.read_bytes(), b"user-owned-existing-file")

    def test_safe_nested_file_is_verified_before_final_name(self):
        with mock.patch.object(
            downloader.urllib.request, "urlopen", return_value=self.response(self.content)
        ) as network:
            metadata = self.fetch(self.item("nested/config.json", lfs=False))
        network.assert_called_once()
        self.assertEqual((self.destination / "nested/config.json").read_bytes(), self.content)
        self.assertFalse((self.destination / "nested/config.json.incomplete").exists())
        self.assertEqual(metadata["hash_algorithm"], "git-blob-sha1")
        self.assertTrue(network.call_args.args[0].full_url.startswith("https://huggingface.co/"))

    def test_download_hash_mismatch_does_not_publish_target(self):
        with mock.patch.object(
            downloader.urllib.request, "urlopen", return_value=self.response(b"x" * len(self.content))
        ):
            with self.assertRaisesRegex(RuntimeError, "hash verification"):
                self.fetch(self.item())
        self.assertFalse((self.destination / "model.safetensors").exists())
        self.assertTrue((self.destination / "model.safetensors.incomplete").exists())

    def test_partial_download_resumes_with_range(self):
        partial = self.destination / "model.safetensors.incomplete"
        partial.write_bytes(self.content[:5])
        with mock.patch.object(
            downloader.urllib.request, "urlopen", return_value=self.response(self.content[5:], status=206)
        ) as network:
            self.fetch(self.item())
        self.assertEqual(network.call_args.args[0].get_header("Range"), "bytes=5-")
        self.assertFalse(partial.exists())
        self.assertEqual((self.destination / "model.safetensors").read_bytes(), self.content)

    def test_completed_partial_is_hash_checked_without_network(self):
        (self.destination / "model.safetensors.incomplete").write_bytes(self.content)
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            self.fetch(self.item())
        network.assert_not_called()
        self.assertTrue((self.destination / "model.safetensors").exists())

    def test_oversized_partial_is_not_truncated(self):
        partial = self.destination / "model.safetensors.incomplete"
        oversized = self.content + b"extra"
        partial.write_bytes(oversized)
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            with self.assertRaisesRegex(RuntimeError, "Oversized"):
                self.fetch(self.item())
        network.assert_not_called()
        self.assertEqual(partial.read_bytes(), oversized)

    def test_path_traversal_and_empty_names_are_rejected(self):
        for name in ("../escape", "/tmp/escape", "nested/../../escape", "", "."):
            with self.subTest(name=name), mock.patch.object(downloader.urllib.request, "urlopen") as network:
                with self.assertRaises((ValueError, RuntimeError)):
                    self.fetch(self.item(name))
                network.assert_not_called()

    def test_directory_symlink_cannot_escape_destination(self):
        outside = self.root / "outside"
        outside.mkdir()
        (self.destination / "linked").symlink_to(outside, target_is_directory=True)
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            with self.assertRaises((ValueError, RuntimeError)):
                self.fetch(self.item("linked/model.safetensors"))
        network.assert_not_called()
        self.assertEqual(list(outside.iterdir()), [])

    def test_partial_symlink_cannot_append_outside_destination(self):
        outside = self.root / "outside-file"
        outside.write_bytes(self.content[:5])
        (self.destination / "model.safetensors.incomplete").symlink_to(outside)
        with mock.patch.object(downloader.urllib.request, "urlopen") as network:
            with self.assertRaises((ValueError, RuntimeError)):
                self.fetch(self.item())
        network.assert_not_called()
        self.assertEqual(outside.read_bytes(), self.content[:5])


if __name__ == "__main__":
    unittest.main()
