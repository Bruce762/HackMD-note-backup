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
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote
from urllib.request import Request, urlopen


API_ROOT = "https://api.hackmd.io/v1"
STATE_VERSION = 3
HACKMD_ID_RE = re.compile(
    r"^<!--\s*hackmd-id:\s*(?P<id>[A-Za-z0-9_-]+)\s*-->\s*$",
    re.MULTILINE,
)


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
    team_path = note.get("teamPath")
    if team_path:
        base = Path("teams") / safe_name(team_path, "unknown-team")
    else:
        base = Path("personal")
    tags = note.get("tags")
    if isinstance(tags, list) and tags:
        base /= safe_name(tags[0], "未分類")
    return base / f"{title}.md"


def embedded_hackmd_id(text: str) -> str | None:
    """讀取 Markdown 內嵌的 HackMD note id。"""
    matches = {match.group("id") for match in HACKMD_ID_RE.finditer(text)}
    if len(matches) > 1:
        raise ValueError("檔案含有多個不同的 hackmd-id")
    return next(iter(matches), None)


def embed_hackmd_id(text: str, note_id: str) -> str:
    """在 Markdown 尾端加入穩定識別碼，不影響 YAML frontmatter。"""
    existing = embedded_hackmd_id(text)
    if existing and existing != note_id:
        raise ValueError(f"檔案的 hackmd-id 是 {existing}，不是 {note_id}")
    if existing == note_id:
        return text
    separator = "\n" if text.endswith("\n") else "\n\n"
    if not text:
        separator = ""
    return f"{text}{separator}<!-- hackmd-id: {note_id} -->\n"


def markdown_files(output_root: Path) -> list[Path]:
    if not output_root.is_dir():
        return []
    return [
        path
        for path in output_root.rglob("*.md")
        if "_local_changes" not in path.relative_to(output_root).parts
    ]


def index_markdown_by_id(output_root: Path) -> dict[str, Path]:
    """以檔內 hackmd-id 建立索引；檔名與路徑不參與身分辨識。"""
    indexed: dict[str, Path] = {}
    for path in markdown_files(output_root):
        try:
            note_id = embedded_hackmd_id(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, ValueError) as error:
            raise RuntimeError(f"無法讀取筆記識別碼 {path}：{error}") from error
        if not note_id:
            continue
        previous = indexed.get(note_id)
        if previous:
            raise RuntimeError(f"hackmd-id {note_id} 重複出現在 {previous} 與 {path}")
        indexed[note_id] = path
    return indexed


def unique_note_path(output_root: Path, preferred: Path, occupied: set[Path]) -> Path:
    """同名筆記依序使用「標題 (2).md」、「標題 (3).md」。"""
    candidate = preferred
    sequence = 2
    while candidate in occupied or (output_root / candidate).exists():
        candidate = preferred.with_name(f"{preferred.stem} ({sequence}){preferred.suffix}")
        sequence += 1
    return candidate


def legacy_relative_path(note: dict[str, Any]) -> Path:
    """舊版的「標題--shortId.md」路徑，只用於一次性遷移。"""
    title = safe_name(note.get("title"), "未命名")
    stable_id = safe_name(note.get("shortId") or str(note.get("id", ""))[:12], "unknown", 40)
    return note_relative_path(note).with_name(f"{title}--{stable_id}.md")


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
    """圖片處理後，依檔內 ID 記錄 Markdown 的實際位置與內容雜湊。"""
    by_id = index_markdown_by_id(output_root)
    for note_id, note in state.get("notes", {}).items():
        if not isinstance(note, dict) or not note.get("active"):
            continue
        path = by_id.get(note_id)
        if not path:
            note["needsRefresh"] = True
            continue
        note["path"] = path.relative_to(output_root).as_posix()
        note["contentSha256"] = sha256_file(path)
        note.pop("needsRefresh", None)


def run_image_downloader(
    output_root: Path,
    env_path: Path,
    *,
    dry_run: bool,
    markdown_path: Path | None = None,
    download_only: bool = False,
    workers: int | None = None,
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
        "-u",
        str(script_path),
        str(output_root),
        "--env-file",
        str(env_path),
    ]
    if markdown_path is not None:
        try:
            selected = markdown_path.resolve().relative_to(output_root.resolve())
        except ValueError:
            print(f"錯誤：Markdown 不在備份目錄內：{markdown_path}", file=sys.stderr)
            return 2
        command.extend(["--file", selected.as_posix()])
    if download_only:
        command.extend(["--download-only", "--no-organize"])
    if workers is not None:
        command.extend(["--workers", str(workers)])
    if dry_run:
        command.append("--dry-run")
    if markdown_path is None:
        print("\n開始進行圖片與 Markdown 的最後整理……")
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
    force_content_refresh = state_version not in {2, STATE_VERSION}
    new_notes: dict[str, Any] = {}
    remote_ids: set[str] = set()
    run_timestamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    api_calls = 1
    added = updated = unchanged = preserved = failed = 0
    image_executor: ThreadPoolExecutor | None = None
    image_futures: dict[Future[int], Path] = {}
    if not args.no_images and not args.dry_run:
        # 同時處理兩篇筆記，每篇最多四條圖片下載線程，
        # 總並行量與單獨執行圖片程式的預設值 8 相同。
        image_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="hackmd-images")

    def schedule_images(path: Path) -> None:
        if image_executor is None:
            return
        print(f"[圖片排程] {path.relative_to(output_root).as_posix()}")
        future = image_executor.submit(
            run_image_downloader,
            output_root,
            env_path,
            dry_run=False,
            markdown_path=path,
            download_only=True,
            workers=4,
        )
        image_futures[future] = path

    try:
        paths_by_id = index_markdown_by_id(output_root)
    except RuntimeError as error:
        print(f"錯誤：{error}", file=sys.stderr)
        return 1
    occupied = {path.relative_to(output_root) for path in markdown_files(output_root)}

    def save_progress() -> None:
        """每完成一篇就保存進度，程式中斷後仍可接續。"""
        if args.dry_run:
            return
        progress_notes = {
            note_id: dict(note_data)
            for note_id, note_data in old_notes.items()
            if isinstance(note_data, dict)
        }
        progress_notes.update(new_notes)
        output_root.mkdir(parents=True, exist_ok=True)
        atomic_write_json(
            state_path,
            {
                "version": STATE_VERSION,
                "generatedAt": datetime.now(timezone.utc).isoformat(),
                "source": API_ROOT + "/notes",
                "complete": False,
                "notes": progress_notes,
            },
        )

    for note in sorted(notes, key=lambda item: (str(item.get("teamPath") or ""), str(item.get("title") or ""))):
        note_id = str(note.get("id") or "")
        if not note_id:
            print("[失敗] 某篇筆記缺少 id，已略過。", file=sys.stderr)
            failed += 1
            continue
        remote_ids.add(note_id)
        previous = dict(old_notes.get(note_id, {})) if isinstance(old_notes.get(note_id), dict) else {}

        old_relative_text = previous.get("path")
        old_relative = Path(old_relative_text) if isinstance(old_relative_text, str) else None
        old_path = output_root / old_relative if old_relative else None

        # 新版完全依賴 Markdown 內的 hackmd-id。舊版檔案僅在第一次遷移時，
        # 利用狀態檔找到原檔並補上 ID；遷移完成後，使用者可自由改名或搬移。
        current_path = paths_by_id.get(note_id)
        legacy_path = output_root / legacy_relative_path(note)
        if current_path is None and old_path and old_path.is_file():
            current_path = old_path
        elif current_path is None and legacy_path.is_file():
            current_path = legacy_path

        if current_path and current_path.is_file():
            try:
                current_text = current_path.read_text(encoding="utf-8")
                current_id = embedded_hackmd_id(current_text)
            except (OSError, UnicodeDecodeError, ValueError) as error:
                print(f"[失敗] {current_path}：{error}", file=sys.stderr)
                failed += 1
                continue
            if current_id and current_id != note_id:
                print(
                    f"[失敗] {current_path} 的 hackmd-id 是 {current_id}，預期為 {note_id}。",
                    file=sys.stderr,
                )
                failed += 1
                continue

            current_relative = current_path.relative_to(output_root)
            old_generated_names = {
                legacy_relative_path(note).name,
                f"{safe_name(note.get('title'), '未命名')}--{safe_name(note.get('id'), 'unknown', 40)}.md",
            }
            target_relative = current_relative
            if current_relative.name in old_generated_names:
                occupied.discard(current_relative)
                target_relative = unique_note_path(
                    output_root,
                    current_relative.with_name(f"{safe_name(note.get('title'), '未命名')}.md"),
                    occupied,
                )
                occupied.add(target_relative)
                print(f"[重新命名] {current_relative.as_posix()} -> {target_relative.as_posix()}")

            if not current_id:
                marked_text = embed_hackmd_id(current_text, note_id)
                print(f"[加入識別碼] {target_relative.as_posix()}")
            else:
                marked_text = current_text

            if not args.dry_run:
                target_path = output_root / target_relative
                if target_path != current_path:
                    target_path.parent.mkdir(parents=True, exist_ok=True)
                    shutil.move(str(current_path), str(target_path))
                    current_path = target_path
                if marked_text != current_text:
                    old_hash = sha256_text(current_text)
                    atomic_write_text(current_path, marked_text)
                    if previous.get("contentSha256") == old_hash:
                        previous["contentSha256"] = sha256_text(marked_text)
                paths_by_id[note_id] = current_path
            relative = target_relative if not args.dry_run else current_relative
            destination = current_path
        else:
            relative = unique_note_path(output_root, note_relative_path(note), occupied)
            occupied.add(relative)
            destination = output_root / relative

        # HackMD 的筆記清單可能包含 content 欄位，但其值是空字串，不能把它
        # 當成完整內容。清單只用於比對 lastChangedAt；只有新增、遠端變更、
        # 本機內容不符，或舊版狀態需要修復時，才呼叫單篇 API 取得全文。
        current_path = destination if destination.exists() else None
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
            save_progress()
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
                save_progress()
            continue

        try:
            stored_content = embed_hackmd_id(content, note_id)
        except ValueError as error:
            print(f"[失敗] {note.get('title') or note_id}：{error}", file=sys.stderr)
            failed += 1
            continue
        remote_hash = sha256_text(stored_content)
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
                atomic_write_text(destination, stored_content)
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
        save_progress()
        schedule_images(destination)

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
        "complete": True,
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
    if image_executor is not None:
        if image_futures:
            print(f"\n等待 {len(image_futures)} 篇筆記的背景圖片下載完成……")
            for future in as_completed(image_futures):
                path = image_futures[future]
                try:
                    status = future.result()
                except Exception as error:
                    print(f"[背景圖片失敗] {path}：{error}", file=sys.stderr)
                    continue
                if status != 0:
                    print(f"[背景圖片未完成] {path}（結束碼 {status}）", file=sys.stderr)
        image_executor.shutdown(wait=True)

    if failed:
        print("備份有失敗項目；已完成其他筆記的背景圖片下載，下次會接續。", file=sys.stderr)
        return 1

    image_status = run_image_downloader(output_root, env_path, dry_run=args.dry_run)
    if not args.dry_run:
        try:
            refresh_processed_state(output_root, new_state)
        except RuntimeError as error:
            print(f"錯誤：{error}", file=sys.stderr)
            return 1
        atomic_write_json(state_path, new_state)
        print("已更新備份狀態中的 Markdown 位置與內容雜湊。")
    if image_status != 0:
        print(f"圖片下載或 Markdown 整理未完全成功（結束碼 {image_status}）。", file=sys.stderr)
        return image_status
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
