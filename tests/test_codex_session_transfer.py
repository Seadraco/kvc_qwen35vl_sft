"""Privacy and non-overwriting tests for the explicit conversation transfer."""
import importlib.util
import json
from pathlib import Path
import tarfile

import pytest


SPEC = importlib.util.spec_from_file_location(
    "codex_session_transfer", Path(__file__).resolve().parents[1] / "scripts/codex_session_transfer.py")
transfer = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(transfer)
THREAD = "01a06711-2544-7d51-9b10-b2e8ce59f55e"


def test_select_only_visible_records():
    def obj(payload):
        return {"timestamp": "2026-10-15T00:00:00Z", "type": "response_item", "payload": payload}
    assert transfer.select_record(obj({"type": "reasoning", "text": "private"})) is None
    for role in ("system", "developer"):
        assert transfer.select_record(obj({"type": "message", "role": role})) is None
    for flag in ("channel", "phase"):
        assert transfer.select_record(obj({"type": "message", "role": "assistant", flag: "analysis"})) is None
    assert transfer.select_record(obj({"type": "message", "role": "assistant", "content": []})) is not None


def test_rendered_reasoning_and_nested_internal_records_are_removed():
    assert transfer.select_record({"timestamp": "t", "type": "event_msg", "payload": {
        "type": "item_completed", "item": {"type": "Reasoning", "raw_content": "private"}}}) is None
    nested = json.dumps({"role": "system", "content": "private"})
    result = transfer.scrub({"output": nested, "encrypted_content": "private"}, set())
    assert "private" not in json.dumps(result)
    assert "encrypted_content" not in result


def test_credentials_and_repeated_literals_are_masked():
    text = "PASSWORD='test-secret-value' test-secret-value https://user:pass@example.com"
    result = transfer.scrub(text, {"test-secret-value"})
    assert "test-secret-value" not in result
    assert "user:pass" not in result
    assert "test-secret-value" not in transfer.scrub({"API_KEY": "test-secret-value"}, set())["API_KEY"]


def package(tmp_path):
    folder = tmp_path / "export"
    folder.mkdir()
    data = (json.dumps({"timestamp": "2026-09-03T11:39:32Z", "type": "session_meta", "payload": {
        "id": THREAD, "timestamp": "2026-09-03T11:39:32Z", "cwd": "/old"}}) + "\n").encode()
    manifest = {"thread_id": THREAD, "rollout_bytes": len(data), "rollout_sha256": transfer.digest(data)}
    (folder / "conversation.jsonl").write_bytes(data)
    (folder / "MANIFEST.json").write_text(json.dumps(manifest))
    return folder


def test_install_relocates_only_metadata_and_refuses_existing_session(tmp_path):
    folder = package(tmp_path)
    home = tmp_path / "codex-home"
    path = transfer.install_session(folder, home, tmp_path)
    first = path.read_bytes()
    assert json.loads(first.splitlines()[0])["payload"]["cwd"] == str(tmp_path)
    assert path.stat().st_mode & 0o777 == 0o600
    with pytest.raises(RuntimeError, match="already exists"):
        transfer.install_session(folder, home, tmp_path)
    assert path.read_bytes() == first


def test_archive_checksum_and_member_allowlist(tmp_path):
    folder = package(tmp_path)
    archive = tmp_path / "ok.tar.gz"
    with tarfile.open(archive, "w:gz") as tar:
        for name in ("MANIFEST.json", "conversation.jsonl"):
            tar.add(folder / name, arcname=name)
    assert transfer.load_package(archive)[0]["thread_id"] == THREAD
    (folder / "conversation.jsonl").write_bytes(b"tampered\n")
    with pytest.raises(RuntimeError, match="checksum"):
        transfer.load_package(folder)
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(folder / "MANIFEST.json", arcname="../../auth.json")
    with pytest.raises(RuntimeError, match="Unexpected archive"):
        transfer.load_package(archive)
