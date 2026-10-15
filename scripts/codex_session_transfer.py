#!/usr/bin/env python3
"""Export/import a credential-filtered Codex rollout without copying a profile.

This is a converted conversation, not a byte-for-byte runtime backup. Internal
reasoning, system/developer instructions and credentials are never exported.
No historical command or new model turn is executed by this utility.
"""
from __future__ import annotations

import argparse
from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import subprocess
import tarfile
import tempfile
import time
from uuid import UUID


TOOLS = {
    "function_call": ("type", "name", "namespace", "arguments", "call_id"),
    "custom_tool_call": ("type", "name", "input", "call_id"),
    "function_call_output": ("type", "call_id", "output"),
    "custom_tool_call_output": ("type", "call_id", "output"),
}
SECRET = r"(?:PASSWORD|(?:[A-Z][A-Z0-9_]*_)?(?:API_KEY|ACCESS_TOKEN|AUTH_TOKEN|TOKEN|SECRET|SECRET_KEY|CLIENT_SECRET))"
ASSIGNMENT = re.compile(
    r"(?i)(?<![\w])['\"]?(?:" + SECRET + r")[ '\"]*\s*(?:=|:)\s*"
    r'''(?:"([^"\r\n]*)"|'([^'\r\n]*)'|([^\s,;}]+))'''
)
PATTERNS = (
    (r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----.*?-----END (?:[A-Z ]+ )?PRIVATE KEY-----", "[PRIVATE_KEY_REMOVED]"),
    (r"\b(?:sk-(?:proj-|svcacct-)?|gh[pousr]_|github_pat_|glpat-|hf_)[A-Za-z0-9_-]{16,}\b", "[TOKEN_REMOVED]"),
    (r"\b(?:AKIA|ASIA)[A-Z0-9]{16}\b", "[ACCESS_KEY_REMOVED]"),
    (r"\bAIza[A-Za-z0-9_-]{30,}\b", "[API_KEY_REMOVED]"),
    (r"\beyJ[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\.[A-Za-z0-9_-]{10,}\b", "[TOKEN_REMOVED]"),
    (r"(?i)\b(Bearer|Basic)\s+[A-Za-z0-9._~+/=-]{16,}", r"\1 [REMOVED]"),
    (r"(?i)(https?://)[^\s/@]+:[^\s/@]+@", r"\1[CREDENTIALS_REMOVED]@"),
    (r"data:[A-Za-z0-9.+/-]+;base64,[A-Za-z0-9+/=]+", "[BINARY_ATTACHMENT_REMOVED]"),
)
FORBIDDEN_KEYS = {
    "encrypted_content", "reasoning", "base_instructions", "developer_instructions",
    "baseInstructions", "developerInstructions", "internal_chat_message_metadata_passthrough",
}


def scrub(value, literals: set[str]):
    if isinstance(value, str):
        if value.lstrip().startswith(("{", "[")):
            try:
                return json.dumps(scrub(json.loads(value), literals), ensure_ascii=False)
            except (ValueError, RecursionError):
                pass
        for literal in sorted(literals, key=len, reverse=True):
            value = value.replace(literal, "[CREDENTIAL_REMOVED]")
        value = ASSIGNMENT.sub("[CREDENTIAL_ASSIGNMENT_REMOVED]", value)
        for pattern, replacement in PATTERNS:
            value = re.sub(pattern, replacement, value, flags=re.S)
        return value
    if isinstance(value, list):
        return [scrub(item, literals) for item in value]
    if isinstance(value, dict):
        if str(value.get("type", "")).lower() == "reasoning" or value.get("role") in {"system", "developer"}:
            return "[INTERNAL_RECORD_REMOVED]"
        if value.get("channel") == "analysis" or value.get("phase") == "analysis":
            return "[INTERNAL_RECORD_REMOVED]"
        return {key: "[CREDENTIAL_REMOVED]" if re.fullmatch(SECRET, key, re.I) else scrub(item, literals)
                for key, item in value.items() if key not in FORBIDDEN_KEYS}
    return value


def visible(payload: dict) -> bool:
    metadata = payload.get("internal_chat_message_metadata_passthrough") or {}
    return (payload.get("role") in {"user", "assistant"}
            and payload.get("channel") != "analysis" and payload.get("phase") != "analysis"
            and not (isinstance(metadata, dict)
                     and (metadata.get("channel") == "analysis" or metadata.get("phase") == "analysis")))


def select_record(obj: dict) -> dict | None:
    body = obj.get("payload", {})
    if obj.get("type") == "event_msg" and body.get("type") == "item_completed":
        item = body.get("item", {})
        if item.get("type") not in {
            "UserMessage", "AgentMessage", "CommandExecution", "FileChange",
            "ImageView", "Extension", "CollabAgentToolCall", "ContextCompaction",
        } or item.get("phase") == "analysis":
            return None
        item = dict(item)
        if item.get("type") in {"UserMessage", "AgentMessage"}:
            item["content"] = [part if part.get("type") in {"text", "Text"}
                               else {"type": "text" if item["type"] == "UserMessage" else "Text",
                                     "text": "[Binary attachment omitted from transfer]"}
                               for part in item.get("content", [])]
        return {"timestamp": obj["timestamp"], "type": "event_msg",
                "payload": {key: item if key == "item" else body[key]
                            for key in ("type", "thread_id", "turn_id", "item", "started_at_ms", "completed_at_ms")
                            if key in body}}
    if obj.get("type") == "event_msg" and body.get("type") in {"task_started", "task_complete", "turn_aborted"}:
        keys = ("type", "turn_id", "last_agent_message", "reason")
        return {"timestamp": obj["timestamp"], "type": "event_msg",
                "payload": {key: body[key] for key in keys if key in body}}
    if obj.get("type") != "response_item":
        return None
    if body.get("type") == "message" and visible(body):
        content = []
        for part in body.get("content", []):
            if part.get("type") in {"input_text", "output_text", "text"}:
                content.append({"type": "input_text" if body["role"] == "user" else "output_text",
                                "text": part.get("text", "")})
            else:
                content.append({"type": "input_text" if body["role"] == "user" else "output_text",
                                "text": "[Binary attachment omitted from transfer]"})
        payload = {"type": "message", "role": body["role"], "content": content}
        if body.get("phase") in {"commentary", "final_answer"}:
            payload["phase"] = body["phase"]
    elif body.get("type") in TOOLS:
        payload = {key: body[key] for key in TOOLS[body["type"]] if key in body}
    else:
        return None
    return {"timestamp": obj["timestamp"], "type": "response_item", "payload": payload}


def digest(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def export_session(home: Path, thread_id: str, destination: Path) -> dict:
    UUID(thread_id)
    sources, selected, metadata, omitted = [], [], None, Counter()
    for folder in (home / "sessions", home / "archived_sessions"):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob(f"*{thread_id}*.jsonl")):
            if path.is_symlink():
                continue
            data = path.read_bytes()
            lines = data.split(b"\n")
            # Never export a partially written active record.
            parsed = [json.loads(line) for line in lines[:-1] if line.strip()]
            meta = next((obj["payload"] for obj in parsed if obj.get("type") == "session_meta"), {})
            if meta.get("id") != thread_id or meta.get("thread_source", "user") != "user":
                continue
            if metadata is None:
                metadata = {key: meta[key] for key in
                            ("id", "timestamp", "originator", "cli_version", "source", "model_provider")
                            if key in meta}
            sources.append({"name": path.name, "bytes": len(data), "sha256": digest(data),
                            "partial_last_line_omitted": bool(lines[-1])})
            for number, obj in enumerate(parsed):
                record = select_record(obj)
                if record is not None:
                    selected.append((record["timestamp"], path.name, number, record))
                else:
                    omitted[obj.get("type", "unknown") + ":" + str(obj.get("payload", {}).get("type", ""))] += 1
    if not metadata or not selected:
        raise RuntimeError("No matching conversation records found")
    selected.sort(key=lambda entry: entry[:3])
    literals = set()
    for *_, record in selected:
        for match in ASSIGNMENT.finditer(json.dumps(record, ensure_ascii=False).replace('\\"', '"')):
            secret = next(group for group in match.groups() if group is not None)
            if len(secret) >= 8 and not secret.startswith(("$", "[", "<")) and "..." not in secret:
                literals.add(secret)
    # Codex's scoped migrate-rollouts command rebuilds the paginated store from
    # these existing item_completed events after the transfer is installed.
    metadata.update(cwd="/workspace", history_mode="legacy", thread_source="user")
    records = [{"timestamp": metadata["timestamp"], "type": "session_meta", "payload": metadata}]
    records.extend(scrub(entry[3], literals) for entry in selected)
    counts = Counter(record["payload"].get("type") for record in records)
    roles = Counter(record["payload"]["role"] for record in records
                    if record["type"] == "response_item" and record["payload"].get("type") == "message")
    content = b"".join((json.dumps(record, ensure_ascii=True) + "\n").encode() for record in records)
    manifest = {
        "format": "codex-filtered-rollout-transfer-v1", "thread_id": thread_id,
        "exported_at": datetime.now(timezone.utc).isoformat(), "source_segments": sources,
        "records": len(records), "messages": dict(roles), "record_counts": dict(counts),
        "first_record_at": selected[0][0], "last_record_at": selected[-1][0],
        "omitted_record_types": dict(omitted), "rollout_sha256": digest(content),
        "rollout_bytes": len(content), "originals_modified": False,
        "conversation_format_converted": True, "raw_runtime_backup": False,
        "internal_reasoning_included": False, "system_developer_instructions_included": False,
        "authentication_files_included": False, "binary_attachments_included": False,
        "redaction": "Best effort credential filtering; distribute the archive encrypted",
        "source_history_mode": "paginated", "transfer_history_mode": "legacy_for_scoped_migration",
    }
    destination.mkdir(parents=True, exist_ok=False)
    destination.chmod(0o700)
    for name, value in (("conversation.jsonl", content),
                        ("MANIFEST.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode())):
        target = destination / name
        target.write_bytes(value)
        target.chmod(0o600)
    return manifest


def load_package(package: Path) -> tuple[dict, bytes]:
    if package.is_dir():
        manifest_bytes = (package / "MANIFEST.json").read_bytes()
        content = (package / "conversation.jsonl").read_bytes()
    else:
        with tarfile.open(package, "r:gz") as archive:
            members = {entry.name: entry for entry in archive.getmembers()}
            if set(members) != {"MANIFEST.json", "conversation.jsonl"}:
                raise RuntimeError("Unexpected archive members")
            if any(not entry.isfile() for entry in members.values()):
                raise RuntimeError("Archive must contain ordinary files only")
            manifest_bytes = archive.extractfile(members["MANIFEST.json"]).read()
            content = archive.extractfile(members["conversation.jsonl"]).read()
    manifest = json.loads(manifest_bytes)
    if digest(content) != manifest["rollout_sha256"] or len(content) != manifest["rollout_bytes"]:
        raise RuntimeError("Transfer checksum mismatch")
    return manifest, content


def install_session(package: Path, home: Path, cwd: Path) -> Path:
    manifest, content = load_package(package)
    thread_id = manifest["thread_id"]
    UUID(thread_id)
    for folder in (home / "sessions", home / "archived_sessions"):
        if folder.exists() and any(folder.rglob(f"*{thread_id}*")):
            raise RuntimeError("This conversation already exists; refusing to overwrite it")
    lines = content.splitlines()
    meta = json.loads(lines[0])
    if meta.get("type") != "session_meta" or meta["payload"]["id"] != thread_id:
        raise RuntimeError("Unexpected session metadata")
    cwd = cwd.expanduser().resolve(strict=True)
    if not cwd.is_dir():
        raise RuntimeError("Workspace must be an existing directory")
    meta["payload"]["cwd"] = str(cwd)
    timestamp = datetime.fromisoformat(meta["payload"]["timestamp"].replace("Z", "+00:00"))
    directory = home / "sessions" / timestamp.strftime("%Y/%m/%d")
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    target = directory / f"rollout-{timestamp.strftime('%Y-%m-%dT%H-%M-%S')}-{thread_id}.jsonl"
    updated = (json.dumps(meta, ensure_ascii=True) + "\n").encode() + b"\n".join(lines[1:]) + b"\n"
    descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "wb") as stream:
        stream.write(updated)
    return target


def pack_session(package: Path, destination: Path, passphrase_file: Path) -> dict:
    manifest, _ = load_package(package)
    if destination.exists() or passphrase_file.exists():
        raise RuntimeError("Refusing to overwrite an archive or an existing encryption key")
    if not package.is_dir():
        raise RuntimeError("Pack requires an exported package directory")
    destination.parent.mkdir(parents=True, exist_ok=True)
    passphrase_file.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    descriptor = os.open(passphrase_file, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(descriptor, "w") as stream:
        stream.write(secrets.token_urlsafe(24) + "\n")
    with tempfile.TemporaryDirectory(prefix="kvc-session-pack-") as temp:
        work = Path(temp)
        archive_path = work / "conversation.tar.gz"
        with tarfile.open(archive_path, "w:gz") as archive:
            for name in ("conversation.jsonl", "MANIFEST.json"):
                archive.add(package / name, arcname=name, recursive=False)
        gpg_home = work / "gnupg"
        gpg_home.mkdir(mode=0o700)
        subprocess.run(["gpg", "--homedir", str(gpg_home), "--batch", "--pinentry-mode", "loopback",
                        "--passphrase-file", str(passphrase_file), "--symmetric", "--cipher-algo", "AES256",
                        "--output", str(destination), str(archive_path)], check=True,
                       stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        recovered = work / "recovered.tar.gz"
        subprocess.run(["gpg", "--homedir", str(gpg_home), "--batch", "--pinentry-mode", "loopback",
                        "--passphrase-file", str(passphrase_file), "--decrypt", "--output", str(recovered),
                        str(destination)], check=True, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        recovered_manifest, _ = load_package(recovered)
        if recovered_manifest != manifest:
            raise RuntimeError("Encrypted archive round-trip check failed")
    return {"file": destination.name, "bytes": destination.stat().st_size,
            "sha256": digest(destination.read_bytes()), "encrypted_roundtrip_verified": True}


def check_session(home: Path, thread_id: str, executable: str, resume: bool = False) -> dict:
    """Check isolated imported history; never start a model turn or run a command."""
    env = dict(os.environ, CODEX_HOME=str(home), CODEX_SQLITE_HOME=str(home))
    env.pop("CODEX_THREAD_ID", None)
    with tempfile.TemporaryFile() as errors:
        proc = subprocess.Popen([executable, "app-server", "--listen", "stdio://"],
                                stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=errors,
                                env=env, bufsize=0)
        selector = selectors.DefaultSelector()
        selector.register(proc.stdout, selectors.EVENT_READ)
        pending = b""

        def send(message):
            proc.stdin.write((json.dumps(message) + "\n").encode())
            proc.stdin.flush()

        def receive(request_id):
            nonlocal pending
            deadline = time.monotonic() + 45
            while time.monotonic() < deadline:
                if b"\n" not in pending:
                    if not selector.select(timeout=min(2, max(0, deadline - time.monotonic()))):
                        continue
                    chunk = os.read(proc.stdout.fileno(), 1024 * 1024)
                    if not chunk:
                        raise RuntimeError("Codex app-server exited before responding")
                    pending += chunk
                    continue
                line, pending = pending.split(b"\n", 1)
                obj = json.loads(line)
                if obj.get("id") == request_id:
                    if "error" in obj:
                        raise RuntimeError(json.dumps(obj["error"], ensure_ascii=False))
                    return obj["result"]
            raise TimeoutError("Codex history read timed out")
        try:
            send({"id": 0, "method": "initialize", "params": {
                "clientInfo": {"name": "kvc_session_check", "version": "1"}}})
            receive(0)
            send({"method": "initialized", "params": {}})
            send({"id": 1, "method": "thread/read", "params": {
                "threadId": thread_id, "includeTurns": True}})
            result = receive(1)
            thread = result["thread"]
            counts = Counter(item["type"] for turn in thread.get("turns", []) for item in turn.get("items", []))
            resumed = False
            if resume:
                send({"id": 2, "method": "thread/resume", "params": {
                    "threadId": thread_id, "excludeTurns": True,
                    "cwd": thread["cwd"], "sandbox": "read-only", "approvalPolicy": "on-request"}})
                resumed = receive(2)["thread"]["id"] == thread_id
            return {"thread_id": thread["id"], "turns": len(thread.get("turns", [])),
                    "items": dict(counts), "resume_verified": resumed, "model_turn_started": False}
        finally:
            selector.close()
            proc.stdin.close()
            try:
                proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                proc.terminate()
                proc.wait(timeout=3)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-home", type=Path, required=True)
    export.add_argument("--thread-id", required=True)
    export.add_argument("--destination", type=Path, required=True)
    install = commands.add_parser("install")
    install.add_argument("--package", type=Path, required=True)
    install.add_argument("--cwd", type=Path, required=True)
    install.add_argument("--codex-home", type=Path, default=Path(os.environ.get("CODEX_HOME", str(Path.home() / ".codex"))))
    pack = commands.add_parser("pack")
    pack.add_argument("--package", type=Path, required=True)
    pack.add_argument("--destination", type=Path, required=True)
    pack.add_argument("--passphrase-file", type=Path, required=True)
    check = commands.add_parser("check")
    check.add_argument("--codex-home", type=Path, required=True)
    check.add_argument("--thread-id", required=True)
    check.add_argument("--codex", default=shutil.which("codex") or "codex")
    check.add_argument("--resume-check", action="store_true",
                       help="Also load the imported thread, without sending a new model turn")
    args = parser.parse_args()
    if args.command == "export":
        result = export_session(args.source_home.expanduser().resolve(), args.thread_id, args.destination)
        print(json.dumps({key: result[key] for key in ("thread_id", "records", "messages", "record_counts", "last_record_at")}, ensure_ascii=False))
    elif args.command == "install":
        path = install_session(args.package, args.codex_home.expanduser().resolve(), args.cwd)
        print(f"Imported conversation: {path}")
        print("Next run: codex migrate-rollouts --thread " + load_package(args.package)[0]["thread_id"] + " --apply")
        print("Then reload the Codex IDE extension, or use codex resume with this conversation ID.")
    elif args.command == "pack":
        print(json.dumps(pack_session(args.package, args.destination, args.passphrase_file), ensure_ascii=False))
    else:
        print(json.dumps(check_session(args.codex_home.expanduser().resolve(), args.thread_id, args.codex,
                                       args.resume_check), ensure_ascii=False))


if __name__ == "__main__":
    main()
