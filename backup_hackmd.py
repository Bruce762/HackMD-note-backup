#!/usr/bin/env python3
"""以最低 API 呼叫量將 HackMD 單向增量備份到本機。

HackMD 是唯一來源；本程式不會建立、修改或刪除任何遠端筆記。
備份內容存放於 ``raw_hackmd/``，同步狀態存放於其中的
``.hackmd-backup.json``。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.hackmd.io/v1"
STATE_VERSION = 2


def dotenv_value(path: Path, key: str) -> str | None:
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        print(f"錯誤：無法讀取 {path}：{error}", file=sys.stderr)
        return None

    assignment = re.compile(r"^(?:export\s+)?([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)$")
    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = assignment.match(line)
        if not match or match.group(1) != key:
            continue
        value = match.group(2).strip()
        if len(value) >= 2 and value[0] in {'"', "'"} and value[-1] == value[0]:
            value = value[1:-1]
        else:
            value = re.sub(r"\s+#.*$", "", value).strip()
        return value or None
    return None


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def safe_name(value: object, fallback: str, limit: int = 100) -> str:
    text = unicodedata.normalize("NFKC", str(value or "")).strip()
    text = re.sub(r"[\\/:*?\"<>|\x00-\x1f]", "_", text)
    text = re.sub(r"\s+", " ", text).strip(" ._")
    return (text or fallback)[:limit]


def note_relative_path(note: dict[str, Any]) -> Path:
    title = safe_name(note.get("title"), "未命名")
    stable_id = safe_name(note.get("shortId") or str(note.get("id", ""))[:12], "unknown", 40)
    filename = f"{title}--{stable_id}.md"
    team_path = note.get("teamPath")
    if team_path:
        base = Path("teams") / safe_name(team_path, "unknown-team")
    else:
        base = Path("personal")
    tags = note.get("tags")
    if isinstance(tags, list) and tags:
        base /= safe_name(tags[0], "未分類")
    return base / filename


def load_state(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {"version": STATE_VERSION, "notes": {}}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict) or not isinstance(data.get("notes"), dict):
            raise ValueError("狀態檔格式不正確")
        return data
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
        raise RuntimeError(f"無法讀取同步狀態 {path}：{error}") from error


def atomic_write_text(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="",
        prefix=f".{path.name}.",
        suffix=".tmp",
        dir=path.parent,
        delete=False,
    ) as temporary:
        temporary_path = Path(temporary.name)
        temporary.write(text)
    os.replace(temporary_path, path)


def atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    atomic_write_text(path, json.dumps(data, ensure_ascii=False, indent=2) + "\n")


def api_get(path: str, token: str, timeout: float) -> tuple[Any, dict[str, str]]:
    request = Request(
        API_ROOT + path,
        headers={
            "Authorization": f"Bearer {token}",
            "Accept": "application/json",
            "User-Agent": "HackMDIncrementalBackup/1.0",
        },
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            data = json.load(response)
            rate = {
                key: response.headers.get(key, "")
                for key in (
                    "X-RateLimit-UserLimit",
                    "X-RateLimit-UserRemaining",
                    "X-RateLimit-UserReset",
                )
            }
            return data, rate
    except HTTPError as error:
        body = error.read(500).decode("utf-8", errors="replace")
        raise RuntimeError(f"HackMD API HTTP {error.code} {error.reason}: {body}") from error
    except URLError as error:
        raise RuntimeError(f"HackMD API 連線失敗：{error.reason}") from error
    except json.JSONDecodeError as error:
        raise RuntimeError("HackMD API 回傳的不是有效 JSON") from error


def unwrap_notes(data: Any) -> list[dict[str, Any]]:
    if isinstance(data, list):
        notes = data
    elif isinstance(data, dict):
        notes = data.get("data", data.get("notes"))
    else:
        notes = None
    if not isinstance(notes, list) or not all(isinstance(note, dict) for note in notes):
        raise RuntimeError("HackMD 筆記清單格式不符合預期")
    return notes


def fetch_note_content(note: dict[str, Any], token: str, timeout: float) -> tuple[str, dict[str, str]]:
    note_id = str(note.get("id") or note.get("shortId") or "")
    if not note_id:
        raise RuntimeError("筆記缺少 noteId")
    team_path = note.get("teamPath")
    if team_path:
        endpoint = f"/teams/{quote(str(team_path), safe='')}/notes/{quote(note_id, safe='')}"
    else:
        endpoint = f"/notes/{quote(note_id, safe='')}"
    data, rate = api_get(endpoint, token, timeout)
    if isinstance(data, dict) and isinstance(data.get("data"), dict):
        data = data["data"]
    if not isinstance(data, dict) or not isinstance(data.get("content"), str):
        raise RuntimeError(f"筆記 {note_id} 的內容格式不符合預期")
    return data["content"], rate


def unique_preserved_path(output_root: Path, relative: Path, timestamp: str) -> Path:
    folder = output_root / "_local_changes" / relative.parent
    candidate = folder / f"{relative.stem}.local-{timestamp}{relative.suffix}"
    sequence = 2
    while candidate.exists():
        candidate = folder / f"{relative.stem}.local-{timestamp}-{sequence}{relative.suffix}"
        sequence += 1
    return candidate


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="備份專案根目錄（預設：目前目錄）")
    parser.add_argument("--output", default="raw_hackmd", help="原始備份目錄（預設：raw_hackmd）")
    parser.add_argument("--env-file", default=".env", help="token 設定檔（預設：.env）")
    parser.add_argument("--token-env", default="HACKMD_TOKEN", help="token 欄位名稱（預設：HACKMD_TOKEN）")
    parser.add_argument("--timeout", type=float, default=30, help="API 逾時秒數（預設：30）")
    parser.add_argument("--dry-run", action="store_true", help="讀取遠端並顯示計畫，但不寫入本機")
    parser.add_argument(
        "--no-images",
        action="store_true",
        help="備份完成後不要執行圖片下載與 Markdown 整理程式",
    )
    return parser.parse_args(argv)


def refresh_processed_state(output_root: Path, state: dict[str, Any]) -> None:
    """圖片處理後，記錄 Markdown 的實際位置與本機內容雜湊。"""
    markdown_files = [
        path
        for path in output_root.rglob("*.md")
        if "_local_changes" not in path.relative_to(output_root).parts
    ]
    by_name: dict[str, list[Path]] = {}
    for path in markdown_files:
        by_name.setdefault(path.name, []).append(path)

    for note in state.get("notes", {}).values():
        if not isinstance(note, dict) or not note.get("active"):
            continue
        relative_text = note.get("path")
        if not isinstance(relative_text, str):
            continue
        path = output_root / relative_text
        if not path.is_file():
            candidates = by_name.get(Path(relative_text).name, [])
            if len(candidates) != 1:
                note["needsRefresh"] = True
                continue
            path = candidates[0]
            note["path"] = path.relative_to(output_root).as_posix()
        note["contentSha256"] = sha256_file(path)
        note.pop("needsRefresh", None)


def run_image_downloader(
    output_root: Path,
    env_path: Path,
    *,
    dry_run: bool,
) -> int:
    script_path = Path(__file__).resolve().with_name("download_markdown_images.py")
    if not script_path.is_file():
        print(f"錯誤：找不到圖片下載程式：{script_path}", file=sys.stderr)
        return 1
    if not output_root.is_dir():
        print(f"[略過圖片] 備份目錄尚不存在：{output_root}")
        return 0

    command = [
        sys.executable,
        str(script_path),
        str(output_root),
        "--env-file",
        str(env_path),
    ]
    if dry_run:
        command.append("--dry-run")
    print("\n開始下載圖片並整理 Markdown……")
    try:
        return subprocess.run(command, check=False).returncode
    except OSError as error:
        print(f"錯誤：無法啟動圖片下載程式：{error}", file=sys.stderr)
        return 1


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"錯誤：找不到根目錄：{root}", file=sys.stderr)
        return 2
    if args.timeout <= 0:
        print("錯誤：--timeout 必須大於 0", file=sys.stderr)
        return 2

    output_arg = Path(args.output).expanduser()
    output_root = output_arg.resolve() if output_arg.is_absolute() else (root / output_arg).resolve()
    env_arg = Path(args.env_file).expanduser()
    env_path = env_arg.resolve() if env_arg.is_absolute() else root / env_arg
    state_path = output_root / ".hackmd-backup.json"

    environment_token = os.environ.get(args.token_env, "").strip() or None
    token = environment_token or dotenv_value(env_path, args.token_env)
    if not token:
        print(f"錯誤：請在環境變數或 {env_path} 設定 {args.token_env}", file=sys.stderr)
        return 2

    try:
        state = load_state(state_path)
        raw_notes, rate = api_get("/notes", token, args.timeout)
        notes = unwrap_notes(raw_notes)
    except RuntimeError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 1

    old_notes: dict[str, Any] = state.get("notes", {})
    state_version = state.get("version")
    force_content_refresh = state_version != STATE_VERSION
    new_notes: dict[str, Any] = {}
    remote_ids: set[str] = set()
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    api_calls = 1
    added = updated = unchanged = preserved = failed = 0

    for note in sorted(notes, key=lambda item: (str(item.get("teamPath") or ""), str(item.get("title") or ""))):
        note_id = str(note.get("id") or "")
        if not note_id:
            print("[失敗] 某篇筆記缺少 id，已略過。", file=sys.stderr)
            failed += 1
            continue
        remote_ids.add(note_id)
        previous = old_notes.get(note_id, {}) if isinstance(old_notes.get(note_id), dict) else {}
        relative = note_relative_path(note)
        destination = output_root / relative

        old_relative_text = previous.get("path")
        old_relative = Path(old_relative_text) if isinstance(old_relative_text, str) else None
        old_path = output_root / old_relative if old_relative else None
        if old_path and old_path != destination and old_path.exists() and not destination.exists():
            print(f"[重新命名] {old_relative.as_posix()} -> {relative.as_posix()}")
            if not args.dry_run:
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.move(str(old_path), str(destination))

        # HackMD 的筆記清單可能包含 content 欄位，但其值是空字串，不能把它
        # 當成完整內容。清單只用於比對 lastChangedAt；只有新增、遠端變更、
        # 本機內容不符，或舊版狀態需要修復時，才呼叫單篇 API 取得全文。
        current_path = destination if destination.exists() else old_path
        previous_hash = previous.get("contentSha256")
        same_timestamp = previous.get("lastChangedAt") == note.get("lastChangedAt")
        local_matches = bool(
            current_path
            and current_path.is_file()
            and isinstance(previous_hash, str)
            and sha256_file(current_path) == previous_hash
        )
        needs_content = bool(
            force_content_refresh
            or not previous
            or previous.get("needsRefresh") is True
            or not same_timestamp
            or not local_matches
        )

        if not needs_content:
            unchanged += 1
            new_notes[note_id] = {
                **previous,
                "id": note_id,
                "title": note.get("title"),
                "shortId": note.get("shortId"),
                "teamPath": note.get("teamPath"),
                "tags": note.get("tags") if isinstance(note.get("tags"), list) else [],
                "lastChangedAt": note.get("lastChangedAt"),
                "path": relative.as_posix(),
                "contentSha256": previous_hash,
                "active": True,
            }
            continue

        try:
            content, rate = fetch_note_content(note, token, args.timeout)
            api_calls += 1
        except RuntimeError as error:
            print(f"[失敗] {note.get('title') or note_id}：{error}", file=sys.stderr)
            failed += 1
            if previous:
                new_notes[note_id] = {
                    **previous,
                    "path": relative.as_posix(),
                    "active": True,
                    "needsRefresh": True,
                }
            continue

        remote_hash = sha256_text(content)
        local_exists = destination.is_file()
        local_hash = sha256_file(destination) if local_exists else None
        previous_hash = previous.get("contentSha256")

        if local_exists and local_hash == remote_hash:
            unchanged += 1
        else:
            local_was_edited = bool(
                local_exists
                and (
                    not isinstance(previous_hash, str)
                    or local_hash != previous_hash
                )
            )
            if local_was_edited:
                preserved_path = unique_preserved_path(output_root, relative, run_timestamp)
                print(f"[保留本機修改] {relative.as_posix()} -> {preserved_path.relative_to(output_root).as_posix()}")
                if not args.dry_run:
                    preserved_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.copy2(destination, preserved_path)
                preserved += 1

            action = "新增" if not local_exists and not previous else "更新"
            print(f"[{action}] {relative.as_posix()}")
            if not args.dry_run:
                atomic_write_text(destination, content)
            if action == "新增":
                added += 1
            else:
                updated += 1

        new_notes[note_id] = {
            "id": note_id,
            "title": note.get("title"),
            "shortId": note.get("shortId"),
            "teamPath": note.get("teamPath"),
            "tags": note.get("tags") if isinstance(note.get("tags"), list) else [],
            "lastChangedAt": note.get("lastChangedAt"),
            "path": relative.as_posix(),
            "contentSha256": remote_hash,
            "active": True,
        }

    removed = 0
    for note_id, previous in old_notes.items():
        if note_id in remote_ids or not isinstance(previous, dict):
            continue
        retained = dict(previous)
        retained["active"] = False
        retained.setdefault("missingSince", datetime.now(timezone.utc).isoformat())
        new_notes[note_id] = retained
        removed += 1
        print(f"[遠端已移除、本機保留] {previous.get('path', note_id)}")

    new_state = {
        "version": STATE_VERSION,
        "generatedAt": datetime.now(timezone.utc).isoformat(),
        "source": API_ROOT + "/notes",
        "notes": new_notes,
    }
    if not args.dry_run:
        output_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(state_path, new_state)

    print(
        f"備份完成：遠端 {len(notes)} 篇，新增 {added}、更新 {updated}、"
        f"未變更 {unchanged}、保留本機修改 {preserved}、遠端移除但保留 {removed}、失敗 {failed}。"
    )
    print(f"本次 API 呼叫：{api_calls} 次。")
    remaining = rate.get("X-RateLimit-UserRemaining", "")
    limit = rate.get("X-RateLimit-UserLimit", "")
    if remaining or limit:
        print(f"API 額度：剩餘 {remaining or '?'} / {limit or '?'}。")
    if args.dry_run:
        print("這是 dry-run，未寫入任何檔案。")

    if args.no_images:
        print("已使用 --no-images，略過圖片下載與 Markdown 整理。")
        return 1 if failed else 0
    if failed:
        print("備份有失敗項目，已略過圖片下載；下次備份成功後會自動執行。", file=sys.stderr)
        return 1

    image_status = run_image_downloader(output_root, env_path, dry_run=args.dry_run)
    if not args.dry_run:
        refresh_processed_state(output_root, new_state)
        atomic_write_json(state_path, new_state)
        print("已更新備份狀態中的 Markdown 位置與內容雜湊。")
    if image_status != 0:
        print(f"圖片下載或 Markdown 整理未完全成功（結束碼 {image_status}）。", file=sys.stderr)
        return image_status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
