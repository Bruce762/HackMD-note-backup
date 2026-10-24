# HackMD 備份圖片下載工具

此工具會下載 Markdown 裡的遠端圖片、改成本機連結，並依第一個 tag 整理筆記位置。（要先備份好資料）

## 使用方式

先去下載hackmd的筆記
![1792827168857](image/README/1792827168857.png)

hackmd token獲取方法
![1792827057526](image/README/1792827057526.png)

需要 Python 3，不必安裝其他套件。

1. 在 `.env` 填入 HackMD API token：

```dotenv
HACKMD_TOKEN=你的token
```

2. 先預覽，不修改檔案：

```bash
python3 download_markdown_images.py --dry-run
```

3. 正式執行：

```bash
python3 download_markdown_images.py
```

## 整理規則

- 有 tag：`a/teach.md` 搬到 `a/code/teach.md`。
- 沒有 tag：Markdown 留在原位。
- 圖片放在 `a/image/teach/`。
- 圖片已存在時直接沿用，不重新下載。
- 公開圖片匿名下載；失敗時才使用 `.env` 的 token。
- ` ```cpp= `、` ```python= ` 等 HackMD code fence 會移除尾端的 `=`。
- 預設不建立 `.bak`；需要原始備份時才加 `--backup`。

只下載圖片、不依 tag 搬移：

```bash
python3 download_markdown_images.py --no-organize
```

## 程式碼

### .env

```
# 請把 HackMD Settings -> API 產生的 token 貼在等號後面。
# 此檔已列入 .gitignore，請勿分享或提交。
HACKMD_TOKEN=輸入hackmd的token
```

### download_markdown_images.py

```python
#!/usr/bin/env python3
"""下載 Markdown 內的遠端圖片，並把連結改成本機相對路徑。

預設會遞迴處理目前目錄下的 ``*.md``，將圖片存成：

    image/<Markdown 檔案名稱>/<原檔名>-<網址雜湊>.<副檔名>

有 tag 的 Markdown 會搬到 ``<第一個 tag>/<檔名>.md``；沒有 tag 的留在原位。
只有成功下載的圖片才會改寫；修改或搬移前預設會建立 ``.md.bak`` 備份。
HackMD token 可放在根目錄的 ``.env``：``HACKMD_TOKEN=你的token``。
本程式只使用 Python 標準函式庫。
"""

from __future__ import annotations

import argparse
import hashlib
import mimetypes
import os
import re
import shutil
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import quote, unquote, urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


IMAGE_SUFFIXES = {
    ".avif",
    ".bmp",
    ".gif",
    ".heic",
    ".jpeg",
    ".jpg",
    ".png",
    ".svg",
    ".tif",
    ".tiff",
    ".webp",
}
FENCE_RE = re.compile(r"^[ \t]{0,3}(`{3,}|~{3,})")
IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.IGNORECASE | re.DOTALL)
IMG_ATTR_RE = re.compile(
    r"\b(?P<name>src|srcset)\s*=\s*(?:\"(?P<double>[^\"]*)\"|'(?P<single>[^']*)'|(?P<bare>[^\s>]+))",
    re.IGNORECASE,
)
HTTP_URL_RE = re.compile(r"https?://[^\s,]+", re.IGNORECASE)
HACKMD_SIZE_RE = re.compile(r"=\d*%?x\d*%?$", re.IGNORECASE)
REFERENCE_IMAGE_RE = re.compile(
    r"!\[(?P<alt>[^]\n]*)\](?:\[(?P<label>[^]\n]*)\])?",
    re.MULTILINE,
)
REFERENCE_DEFINITION_RE = re.compile(
    r"^[ \t]{0,3}\[(?P<label>[^]\n]+)\]:[ \t]*(?:<(?P<angle>[^>\n]+)>|(?P<bare>\S+))",
    re.MULTILINE,
)


@dataclass(frozen=True)
class Occurrence:
    start: int
    end: int
    url: str


@dataclass
class DocumentPlan:
    path: Path
    text: str
    occurrences: list[Occurrence]
    destinations: dict[str, Path]


@dataclass(frozen=True)
class MovePlan:
    source: Path
    destination: Path
    tags: tuple[str, ...]


@dataclass(frozen=True)
class DownloadResult:
    ok: bool
    path: Path | None = None
    error: str | None = None
    reused: bool = False
    authenticated: bool = False


def fenced_ranges(text: str) -> list[tuple[int, int]]:
    """回傳 fenced code blocks 的字元範圍，避免改到範例程式碼。"""
    ranges: list[tuple[int, int]] = []
    offset = 0
    opened_at: int | None = None
    marker = ""
    marker_len = 0

    for line in text.splitlines(keepends=True):
        match = FENCE_RE.match(line)
        if match:
            token = match.group(1)
            if opened_at is None:
                opened_at = offset
                marker = token[0]
                marker_len = len(token)
            elif token[0] == marker and len(token) >= marker_len:
                ranges.append((opened_at, offset + len(line)))
                opened_at = None
        offset += len(line)

    if opened_at is not None:
        ranges.append((opened_at, len(text)))
    return ranges


def in_ranges(position: int, ranges: list[tuple[int, int]]) -> bool:
    return any(start <= position < end for start, end in ranges)


def find_closing_bracket(text: str, start: int, closing: str) -> int | None:
    escaped = False
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == closing:
            return index
        elif char == "\n":
            return None
    return None


def find_closing_parenthesis(text: str, start: int) -> int | None:
    depth = 1
    escaped = False
    quote_char: str | None = None
    for index in range(start, len(text)):
        char = text[index]
        if escaped:
            escaped = False
            continue
        if char == "\\":
            escaped = True
            continue
        if quote_char:
            if char == quote_char:
                quote_char = None
            continue
        if char in {'"', "'"}:
            quote_char = char
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0:
                return index
        elif char == "\n":
            return None
    return None


def markdown_url_span(text: str, inner_start: int, inner_end: int) -> tuple[int, int] | None:
    cursor = inner_start
    while cursor < inner_end and text[cursor].isspace():
        cursor += 1
    if cursor >= inner_end:
        return None

    if text[cursor] == "<":
        end = text.find(">", cursor + 1, inner_end)
        return (cursor + 1, end) if end != -1 else None

    url_start = cursor
    depth = 0
    escaped = False
    while cursor < inner_end:
        char = text[cursor]
        if escaped:
            escaped = False
        elif char == "\\":
            escaped = True
        elif char == "(":
            depth += 1
        elif char == ")" and depth:
            depth -= 1
        elif char.isspace() and depth == 0:
            break
        cursor += 1

    # HackMD 也接受 ``url.png=50%x`` 這種沒有空白的尺寸語法。
    raw_url = text[url_start:cursor]
    size_match = HACKMD_SIZE_RE.search(raw_url)
    if size_match:
        cursor = url_start + size_match.start()
    return (url_start, cursor) if cursor > url_start else None


def find_markdown_images(text: str, ignored: list[tuple[int, int]]) -> list[Occurrence]:
    found: list[Occurrence] = []
    cursor = 0
    while True:
        image_start = text.find("![", cursor)
        if image_start == -1:
            break
        cursor = image_start + 2
        if image_start > 0 and text[image_start - 1] == "\\":
            continue
        if in_ranges(image_start, ignored):
            continue

        alt_end = find_closing_bracket(text, cursor, "]")
        if alt_end is None or alt_end + 1 >= len(text) or text[alt_end + 1] != "(":
            continue
        inner_start = alt_end + 2
        image_end = find_closing_parenthesis(text, inner_start)
        if image_end is None:
            continue
        span = markdown_url_span(text, inner_start, image_end)
        if span:
            url = text[span[0] : span[1]]
            if url.lower().startswith(("http://", "https://")):
                found.append(Occurrence(span[0], span[1], url))
        cursor = image_end + 1
    return found


def find_html_images(text: str, ignored: list[tuple[int, int]]) -> list[Occurrence]:
    found: list[Occurrence] = []
    for tag_match in IMG_TAG_RE.finditer(text):
        if in_ranges(tag_match.start(), ignored):
            continue
        tag = tag_match.group(0)
        for attr_match in IMG_ATTR_RE.finditer(tag):
            value_group = next(
                name for name in ("double", "single", "bare") if attr_match.group(name) is not None
            )
            value = attr_match.group(value_group)
            value_start = tag_match.start() + attr_match.start(value_group)
            if attr_match.group("name").lower() == "src":
                stripped = value.strip()
                if stripped.lower().startswith(("http://", "https://")):
                    leading = len(value) - len(value.lstrip())
                    found.append(
                        Occurrence(value_start + leading, value_start + leading + len(stripped), stripped)
                    )
            else:
                for url_match in HTTP_URL_RE.finditer(value):
                    found.append(
                        Occurrence(
                            value_start + url_match.start(),
                            value_start + url_match.end(),
                            url_match.group(0),
                        )
                    )
    return found


def normalize_reference_label(label: str) -> str:
    return " ".join(label.split()).casefold()


def find_reference_images(text: str, ignored: list[tuple[int, int]]) -> list[Occurrence]:
    """找出 ``![alt][id]``、``![alt][]`` 與 ``![id]`` 使用的定義網址。"""
    wanted_labels: set[str] = set()
    for match in REFERENCE_IMAGE_RE.finditer(text):
        if in_ranges(match.start(), ignored):
            continue
        # ``![alt](url)`` 屬於行內語法，由 find_markdown_images 處理。
        if match.end() < len(text) and text[match.end()] == "(":
            continue
        label = match.group("label")
        wanted_labels.add(normalize_reference_label(label if label else match.group("alt")))

    found: list[Occurrence] = []
    for match in REFERENCE_DEFINITION_RE.finditer(text):
        if in_ranges(match.start(), ignored):
            continue
        if normalize_reference_label(match.group("label")) not in wanted_labels:
            continue
        value_group = "angle" if match.group("angle") is not None else "bare"
        url = match.group(value_group)
        size_match = HACKMD_SIZE_RE.search(url)
        if size_match:
            url = url[: size_match.start()]
        if url.lower().startswith(("http://", "https://")):
            start = match.start(value_group)
            found.append(Occurrence(start, start + len(url), url))
    return found


def find_image_occurrences(text: str) -> list[Occurrence]:
    ignored = fenced_ranges(text)
    occurrences = (
        find_markdown_images(text, ignored)
        + find_reference_images(text, ignored)
        + find_html_images(text, ignored)
    )
    unique = {(item.start, item.end): item for item in occurrences}
    return sorted(unique.values(), key=lambda item: item.start)


def safe_filename_part(value: str, fallback: str = "image") -> str:
    value = unquote(value).strip().replace("\x00", "")
    value = re.sub(r"[\\/:*?\"<>|\r\n\t]", "_", value)
    value = re.sub(r"\s+", "_", value).strip(" ._")
    return (value or fallback)[:100]


def frontmatter_tags(text: str) -> list[str]:
    """以標準函式庫解析第一段 YAML frontmatter 裡的 tags 欄位。"""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return []

    end = next((index for index in range(1, len(lines)) if lines[index].strip() == "---"), None)
    if end is None:
        return []

    tags: list[str] = []
    index = 1
    while index < end:
        match = re.match(r"^[ \t]*(?:tags?|標籤)[ \t]*:[ \t]*(.*)$", lines[index], re.IGNORECASE)
        if not match:
            index += 1
            continue
        value = match.group(1).strip()
        if value.startswith("[") and value.endswith("]"):
            values = value[1:-1].split(",")
            tags.extend(item.strip().strip("'\"") for item in values if item.strip())
        elif value:
            tags.extend(item.strip().strip("'\"") for item in value.split(",") if item.strip())
        else:
            # 支援：tags:\n  - Coding\n  - Python
            index += 1
            while index < end:
                item_match = re.match(r"^[ \t]+-[ \t]+(.+?)\s*$", lines[index])
                if not item_match:
                    break
                tags.append(item_match.group(1).strip().strip("'\""))
                index += 1
            continue
        index += 1

    # 保留原順序，同時去除重複與空值。
    seen: set[str] = set()
    result: list[str] = []
    for tag in tags:
        if tag and tag not in seen:
            seen.add(tag)
            result.append(tag)
    return result


def destination_base(url: str, folder: Path) -> Path:
    path_name = Path(urlsplit(url).path).name
    original = safe_filename_part(path_name)
    suffix = Path(original).suffix.lower()
    if suffix not in IMAGE_SUFFIXES:
        suffix = ""
    stem = safe_filename_part(Path(original).stem if suffix else original)
    digest = hashlib.sha256(url.encode("utf-8")).hexdigest()[:10]
    return folder / f"{stem}-{digest}{suffix}"


def suffix_from_content_type(content_type: str) -> str:
    mime = content_type.split(";", 1)[0].strip().lower()
    overrides = {
        "image/jpeg": ".jpg",
        "image/svg+xml": ".svg",
        "image/tiff": ".tiff",
    }
    suffix = overrides.get(mime) or mimetypes.guess_extension(mime) or ".img"
    return suffix if suffix in IMAGE_SUFFIXES else ".img"


def existing_destination(base: Path) -> Path | None:
    if base.is_file():
        return base
    if not base.suffix and base.parent.exists():
        matches = sorted(path for path in base.parent.glob(f"{base.name}.*") if path.is_file())
        if matches:
            return matches[0]
    return None


def host_is_allowed_for_token(url: str, allowed_hosts: tuple[str, ...]) -> bool:
    hostname = (urlsplit(url).hostname or "").casefold().rstrip(".")
    return any(
        hostname == allowed.casefold().rstrip(".")
        or hostname.endswith("." + allowed.casefold().rstrip("."))
        for allowed in allowed_hosts
        if allowed.strip()
    )


class StripCrossHostAuthorization(HTTPRedirectHandler):
    """跨網域重新導向時移除 Authorization，避免 HackMD token 洩漏或干擾 S3。"""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected is None:
            return None
        old_host = (urlsplit(req.full_url).hostname or "").casefold().rstrip(".")
        new_host = (urlsplit(newurl).hostname or "").casefold().rstrip(".")
        if old_host != new_host:
            for header_map in (redirected.headers, redirected.unredirected_hdrs):
                for name in list(header_map):
                    if name.casefold() == "authorization":
                        del header_map[name]
        return redirected


def open_request(request: Request, timeout: float):
    """開啟網址，保留同網域驗證，但不把 Authorization 帶到其他網域。"""
    return build_opener(StripCrossHostAuthorization()).open(request, timeout=timeout)


def download_one(
    url: str,
    base: Path,
    *,
    timeout: float,
    retries: int,
    overwrite: bool,
    max_bytes: int,
    token: str | None = None,
    token_hosts: tuple[str, ...] = ("hackmd.io",),
) -> DownloadResult:
    if not overwrite:
        existing = existing_destination(base)
        if existing:
            return DownloadResult(True, existing, reused=True)

    base.parent.mkdir(parents=True, exist_ok=True)
    last_error = "未知錯誤"
    can_use_token = bool(token) and host_is_allowed_for_token(url, token_hosts)
    # 有 token 時先匿名嘗試一次；失敗後才改用 token。沒有 token 時維持一般重試。
    attempts = [False]
    if can_use_token:
        attempts.extend([True] * (retries + 1))
    else:
        attempts.extend([False] * retries)

    for attempt, authenticated in enumerate(attempts):
        temporary_path: Path | None = None
        try:
            headers = {
                "User-Agent": "Mozilla/5.0 (compatible; MarkdownImageDownloader/1.0)",
                "Accept": "image/avif,image/webp,image/svg+xml,image/*,*/*;q=0.8",
            }
            if authenticated:
                headers["Authorization"] = f"Bearer {token}"
            request = Request(
                url,
                headers=headers,
            )
            with open_request(request, timeout=timeout) as response:
                content_type = response.headers.get("Content-Type", "")
                declared_length = response.headers.get("Content-Length")
                if declared_length and int(declared_length) > max_bytes:
                    raise ValueError(f"檔案超過上限 {max_bytes // (1024 * 1024)} MiB")
                if content_type.lower().startswith("text/html"):
                    raise ValueError(f"伺服器回傳 HTML，不是圖片（{content_type}）")

                final_path = base if base.suffix else base.with_suffix(suffix_from_content_type(content_type))
                with tempfile.NamedTemporaryFile(
                    mode="wb", prefix=f".{final_path.name}.", suffix=".tmp", dir=final_path.parent, delete=False
                ) as temporary:
                    temporary_path = Path(temporary.name)
                    downloaded = 0
                    first_chunk = b""
                    while True:
                        chunk = response.read(1024 * 1024)
                        if not chunk:
                            break
                        if not first_chunk:
                            first_chunk = chunk[:256].lstrip().lower()
                        downloaded += len(chunk)
                        if downloaded > max_bytes:
                            raise ValueError(f"檔案超過上限 {max_bytes // (1024 * 1024)} MiB")
                        temporary.write(chunk)
                if first_chunk.startswith((b"<!doctype html", b"<html")):
                    raise ValueError("下載內容是 HTML，不是圖片")
                os.replace(temporary_path, final_path)
                return DownloadResult(True, final_path, authenticated=authenticated)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError) as error:
            last_error = str(error)
            if temporary_path and temporary_path.exists():
                temporary_path.unlink()
            # 從匿名切換成 token 時立即重試；同一模式重試才做退避等待。
            if attempt < len(attempts) - 1 and attempts[attempt + 1] == authenticated:
                time.sleep(min(2**attempt, 4))
    return DownloadResult(False, error=last_error)


def local_markdown_path(markdown_path: Path, image_path: Path) -> str:
    relative = os.path.relpath(image_path, start=markdown_path.parent)
    posix = Path(relative).as_posix()
    # Markdown 的括號與空白會影響目的地解析；保留中文字，僅編碼語法敏感字元。
    return quote(posix, safe="/-._~:@\u0080-\uffff")


def build_plans(root: Path, image_root: Path) -> list[DocumentPlan]:
    plans: list[DocumentPlan] = []
    for path in sorted(root.rglob("*.md")):
        if image_root == path or image_root in path.parents:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except UnicodeDecodeError as error:
            print(f"[略過] {path}: 不是 UTF-8（{error}）", file=sys.stderr)
            continue
        occurrences = find_image_occurrences(text)
        if not occurrences:
            continue
        # 即使 Markdown 搬入 tag 資料夾，圖片仍固定放在 image/<文件名稱>/。
        folder = image_root / safe_filename_part(path.stem, fallback="未命名")
        destinations = {url: destination_base(url, folder) for url in {item.url for item in occurrences}}
        plans.append(DocumentPlan(path, text, occurrences, destinations))
    return plans


def build_move_plans(root: Path, image_root: Path) -> list[MovePlan]:
    moves: list[MovePlan] = []
    for path in sorted(root.rglob("*.md")):
        if image_root == path or image_root in path.parents:
            continue
        try:
            tags = frontmatter_tags(path.read_text(encoding="utf-8"))
        except UnicodeDecodeError:
            continue
        if not tags:
            continue
        tag_folder = safe_filename_part(tags[0], fallback="未分類")
        destination = root / tag_folder / path.name
        if path != destination:
            moves.append(MovePlan(path, destination, tuple(tags)))
    return moves


def ensure_backup(path: Path) -> None:
    backup_path = path.with_suffix(path.suffix + ".bak")
    if not backup_path.exists():
        shutil.copy2(path, backup_path)


def organize_documents(
    moves: list[MovePlan], *, backup: bool
) -> tuple[dict[Path, Path], int]:
    """搬移筆記，回傳原路徑到實際路徑的對照與失敗數。"""
    path_map: dict[Path, Path] = {}
    failures = 0
    for move in moves:
        if move.destination.exists():
            print(
                f"[搬移失敗] 目的地已存在：{move.source} -> {move.destination}",
                file=sys.stderr,
            )
            path_map[move.source] = move.source
            failures += 1
            continue
        try:
            if backup:
                ensure_backup(move.source)
            move.destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(move.source), str(move.destination))
            path_map[move.source] = move.destination
            extra = f"（使用第一個 tag；其他 tag：{', '.join(move.tags[1:])}）" if len(move.tags) > 1 else ""
            print(f"[已分類] {move.source} -> {move.destination}{extra}")
        except OSError as error:
            print(f"[搬移失敗] {move.source}：{error}", file=sys.stderr)
            path_map[move.source] = move.source
            failures += 1
    return path_map, failures


def rewrite_document(
    plan: DocumentPlan,
    results: dict[tuple[str, Path], DownloadResult],
    *,
    backup: bool,
) -> tuple[int, int]:
    replacements: list[tuple[int, int, str]] = []
    failures = 0
    for occurrence in plan.occurrences:
        base = plan.destinations[occurrence.url]
        result = results[(occurrence.url, base)]
        if result.ok and result.path:
            replacements.append(
                (
                    occurrence.start,
                    occurrence.end,
                    local_markdown_path(plan.path, result.path),
                )
            )
        else:
            failures += 1

    if not replacements:
        return 0, failures
    updated = plan.text
    for start, end, replacement in reversed(replacements):
        updated = updated[:start] + replacement + updated[end:]

    if updated != plan.text:
        if backup:
            ensure_backup(plan.path)
        temporary = plan.path.with_suffix(plan.path.suffix + ".tmp")
        temporary.write_text(updated, encoding="utf-8")
        os.replace(temporary, plan.path)
    return len(replacements), failures


def parse_args(argv: Iterable[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", nargs="?", default=".", help="Markdown 備份根目錄（預設：目前目錄）")
    parser.add_argument("--image-dir", default="image", help="圖片目錄名稱（預設：image）")
    parser.add_argument("--workers", type=int, default=8, help="同時下載數（預設：8）")
    parser.add_argument("--timeout", type=float, default=30, help="單次連線逾時秒數（預設：30）")
    parser.add_argument("--retries", type=int, default=2, help="下載失敗後重試次數（預設：2）")
    parser.add_argument("--max-size-mb", type=int, default=100, help="單張圖片大小上限（預設：100 MiB）")
    parser.add_argument("--overwrite", action="store_true", help="重新下載已存在的圖片")
    parser.add_argument("--no-backup", action="store_true", help="不建立原始 Markdown 的 .bak 備份")
    parser.add_argument("--no-organize", action="store_true", help="不要依第一個 tag 搬移 Markdown")
    parser.add_argument(
        "--token-env",
        default="HACKMD_TOKEN",
        help="從環境變數或 .env 讀取 token 的變數名稱（預設：HACKMD_TOKEN）",
    )
    parser.add_argument(
        "--env-file",
        default=".env",
        help=".env 路徑；相對路徑以備份根目錄為準（預設：.env）",
    )
    parser.add_argument(
        "--token-host",
        action="append",
        default=None,
        help="允許傳送 token 的網域，可重複指定（預設：hackmd.io）",
    )
    parser.add_argument("--dry-run", action="store_true", help="只列出計畫，不下載也不修改")
    return parser.parse_args(argv)


def dotenv_value(path: Path, key: str) -> str | None:
    """讀取單一 dotenv 欄位；不修改 os.environ，也不需要 python-dotenv。"""
    if not path.is_file():
        return None
    try:
        lines = path.read_text(encoding="utf-8-sig").splitlines()
    except (OSError, UnicodeDecodeError) as error:
        print(f"警告：無法讀取 {path}：{error}", file=sys.stderr)
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
            if match.group(2).strip().startswith('"'):
                value = value.replace(r"\n", "\n").replace(r"\r", "\r").replace(r"\t", "\t")
                value = value.replace(r'\"', '"').replace(r"\\", "\\")
        else:
            # dotenv 的未加引號值，只有空白後的 # 才視為註解。
            value = re.sub(r"\s+#.*$", "", value).strip()
        return value or None
    return None


def main(argv: Iterable[str] | None = None) -> int:
    args = parse_args(argv)
    root = Path(args.root).expanduser().resolve()
    if not root.is_dir():
        print(f"錯誤：找不到目錄：{root}", file=sys.stderr)
        return 2
    if args.workers < 1 or args.retries < 0 or args.max_size_mb < 1:
        print("錯誤：workers/max-size-mb 必須大於 0，retries 不得小於 0", file=sys.stderr)
        return 2

    image_root = (root / args.image_dir).resolve()
    env_file = Path(args.env_file).expanduser()
    if not env_file.is_absolute():
        env_file = root / env_file
    environment_token = os.environ.get(args.token_env, "").strip() or None
    token = environment_token or dotenv_value(env_file, args.token_env)
    token_hosts = tuple(args.token_host or ["hackmd.io"])
    plans = build_plans(root, image_root)
    moves = [] if args.no_organize else build_move_plans(root, image_root)
    jobs = {
        (url, destination)
        for plan in plans
        for url, destination in plan.destinations.items()
    }
    occurrence_count = sum(len(plan.occurrences) for plan in plans)
    print(f"找到 {len(plans)} 份含遠端圖片的 Markdown，共 {occurrence_count} 個圖片連結。")
    print(f"需要處理 {len(jobs)} 個下載項目，圖片目錄：{image_root}")
    print(f"需要依 tag 搬移 {len(moves)} 份 Markdown。")
    if token:
        token_source = f"環境變數 {args.token_env}" if environment_token else str(env_file)
        print(
            f"已從 {token_source} 載入 HackMD token；只有匿名下載失敗且網址屬於 "
            + ", ".join(token_hosts)
            + " 時才會送出 token。"
        )
    else:
        print(f"未在環境變數或 {env_file} 設定 {args.token_env}；下載將不使用 token。")

    if args.dry_run:
        for move in moves:
            extra = f"（其他 tag：{', '.join(move.tags[1:])}）" if len(move.tags) > 1 else ""
            print(f"[預覽搬移] {move.source} -> {move.destination}{extra}")
        for url, destination in sorted(jobs, key=lambda item: (str(item[1]), item[0])):
            print(f"[預覽] {url} -> {destination}")
        return 0
    if not jobs and not moves:
        return 0

    results: dict[tuple[str, Path], DownloadResult] = {}
    if jobs:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = {
                executor.submit(
                    download_one,
                    url,
                    destination,
                    timeout=args.timeout,
                    retries=args.retries,
                    overwrite=args.overwrite,
                    max_bytes=args.max_size_mb * 1024 * 1024,
                    token=token,
                    token_hosts=token_hosts,
                ): (url, destination)
                for url, destination in jobs
            }
            completed = 0
            for future in as_completed(futures):
                key = futures[future]
                result = future.result()
                results[key] = result
                completed += 1
                if result.ok:
                    if result.reused:
                        status = "沿用"
                    elif result.authenticated:
                        status = "token 完成"
                    else:
                        status = "匿名完成"
                    print(f"[{completed}/{len(jobs)} {status}] {key[0]} -> {result.path}")
                else:
                    print(f"[{completed}/{len(jobs)} 失敗] {key[0]}：{result.error}", file=sys.stderr)

    moved_paths, move_failures = organize_documents(moves, backup=not args.no_backup)

    rewritten = 0
    failed_occurrences = 0
    changed_documents = 0
    for plan in plans:
        original_path = plan.path
        if original_path in moved_paths:
            plan.path = moved_paths[original_path]
        count, failures = rewrite_document(
            plan,
            results,
            # 搬移前已在原位置備份；未搬移的檔案仍需在改寫前備份。
            backup=not args.no_backup and plan.path == original_path,
        )
        if count:
            changed_documents += 1
            rewritten += count
        failed_occurrences += failures

    successful_jobs = sum(result.ok for result in results.values())
    print(
        f"處理完成：下載項目成功 {successful_jobs}/{len(jobs)}，"
        f"搬移成功 {len(moves) - move_failures}/{len(moves)} 份 Markdown，"
        f"修改 {changed_documents} 份 Markdown、改寫 {rewritten} 個連結。"
    )
    if failed_occurrences:
        print(f"另有 {failed_occurrences} 個連結因下載失敗而保留原網址。", file=sys.stderr)
    if move_failures:
        print(f"另有 {move_failures} 份 Markdown 搬移失敗。", file=sys.stderr)
    if failed_occurrences or move_failures:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```
