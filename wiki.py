#!/usr/bin/env python3
"""로컬 위키 - 마크다운 파일 기반 개인 위키 서버.

문서는 pages/ 아래에 폴더 구조 그대로 .md 파일로, 첨부는 files/ 에 저장됩니다.
실행: python wiki.py [포트]
"""
from __future__ import annotations

import html
import http.server
import json
import mimetypes
import os
import re
import shutil
import socket
import subprocess
import socketserver
import sys
import threading
import time
import urllib.parse
import webbrowser
from datetime import datetime
from html.parser import HTMLParser
from pathlib import Path
from xml.etree import ElementTree

import markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor
from markdown.treeprocessors import Treeprocessor

# exe 로 묶여 돌 때는 파일이 임시 폴더에 풀리므로, 글은 exe 가 놓인 자리에서 찾습니다.
ROOT = Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent
LOCATION = ROOT / "location.json"  # 이 기기에서 쓸 데이터 폴더 (기기마다 다르므로 저장소에 올리지 않음)
DATA = ROOT
PAGES = DATA / "pages"
FILES = DATA / "files"
CONFIG = DATA / "config.json"
COMMENTS = DATA / "comments.json"   # 글마다 달린 댓글
TRASH = DATA / "trash"              # 지운 글·폴더를 되살릴 수 있게 담아 두는 곳
HOME = "홈"
ORDER_FILE = ".order"
DEFAULT_NAME = "위키"
SEARCH_LIMIT = 50
DEFAULT_PORT = 8800

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WIKILINK_RE = r"\[\[([^\[\]]+?)\]\]"
NOTE_RE = r"\{\{([\s\S]+?)\|\|([\s\S]+?)\}\}"  # 메모는 여러 줄일 수 있습니다
# 메모 안 줄바꿈을 한 줄로 눌러 둘 때 쓰는 표시. 마크다운이 속으로 쓰는 \x02..\x03 과
# 겹치지 않게, 글에 쓰일 일이 없는 사용자 영역 문자를 씁니다.
NOTE_NEWLINE = "\ue000"


# ---------------------------------------------------------------- 데이터 폴더
# 글과 첨부를 어디에 둘지는 기기마다 고를 수 있습니다. 동기화 폴더를 지정하면
# 다른 기기와 같은 내용을 보게 됩니다.

def use_data_dir(path: Path) -> None:
    global DATA, PAGES, FILES, CONFIG, COMMENTS, TRASH
    DATA = path
    PAGES = DATA / "pages"
    FILES = DATA / "files"
    CONFIG = DATA / "config.json"
    COMMENTS = DATA / "comments.json"
    TRASH = DATA / "trash"
    PAGES.mkdir(parents=True, exist_ok=True)
    FILES.mkdir(parents=True, exist_ok=True)


def saved_data_dir() -> Path:
    try:
        raw = json.loads(LOCATION.read_text(encoding="utf-8")).get("data")
    except (OSError, ValueError):
        return ROOT
    return Path(raw) if raw else ROOT


def save_data_dir(path: Path) -> None:
    if path == ROOT:
        LOCATION.unlink(missing_ok=True)
        return
    LOCATION.write_text(
        json.dumps({"data": str(path)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def move_data_to(target: Path) -> tuple[bool, str]:
    """데이터 폴더를 옮깁니다. 대상에 이미 위키 내용이 있으면 그것을 그대로 씁니다."""
    if target == DATA:
        return True, "이미 그 폴더를 쓰고 있습니다."
    try:
        target.mkdir(parents=True, exist_ok=True)
    except OSError as error:
        return False, f"폴더를 만들 수 없습니다: {error}"
    if (target / "pages").is_dir():
        message = "그 폴더에 있던 내용을 그대로 씁니다."
    else:
        try:
            for name in ("pages", "files", "config.json", "comments.json", "trash"):
                source = DATA / name
                if source.exists():
                    shutil.move(str(source), str(target / name))
        except OSError as error:
            return False, f"옮기지 못했습니다: {error}"
        message = "지금 내용을 그 폴더로 옮겼습니다."
    save_data_dir(target)
    use_data_dir(target)
    return True, message


# ---------------------------------------------------------------- 내 PC 파일 열기

EDITORS = [
    ("auto", "켜져 있는 것으로 (자동)"),
    ("vscode", "Visual Studio Code"),
    ("vs", "Visual Studio"),
    ("system", "윈도우 기본 프로그램"),
]
QUIET = {"creationflags": 0x08000000} if os.name == "nt" else {}   # 검은 창 없이 실행


def is_running(image: str) -> bool:
    try:
        found = subprocess.run(
            ["tasklist", "/FI", f"IMAGENAME eq {image}", "/NH"],
            capture_output=True, text=True, timeout=5, **QUIET,
        )
        return image.lower() in found.stdout.lower()
    except (OSError, subprocess.SubprocessError):
        return False


def devenv_path() -> str:
    """설치된 Visual Studio 실행 파일을 찾습니다."""
    found = shutil.which("devenv")
    if found:
        return found
    vswhere = Path(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"))
    vswhere = vswhere / "Microsoft Visual Studio" / "Installer" / "vswhere.exe"
    if not vswhere.is_file():
        return ""
    try:
        asked = subprocess.run(
            [str(vswhere), "-latest", "-property", "productPath"],
            capture_output=True, text=True, timeout=10, **QUIET,
        )
        return asked.stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def open_local(path: str, line: int, choice: str) -> tuple[bool, str]:
    """내 PC 파일을 엽니다. 줄 번호가 있으면 코드 편집기의 그 줄로 갑니다."""
    target = Path(path)
    if not target.exists():
        return False, f"그 자리에 없습니다: {path}"
    if not line or target.is_dir():
        os.startfile(str(target))
        return True, f"{target.name} 을(를) 열었습니다."

    if choice == "auto":
        choice = "vs" if is_running("devenv.exe") else "vscode"
    if choice == "vscode" and shutil.which("code"):
        subprocess.Popen(["cmd", "/c", "code", "-g", f"{target}:{line}"], **QUIET)
        return True, f"VS Code 에서 {target.name}:{line} 을(를) 열었습니다."
    if choice == "vs":
        devenv = devenv_path()
        if devenv:
            subprocess.Popen(
                [devenv, "/edit", str(target), "/command", f"Edit.Goto {line}"], **QUIET)
            return True, f"Visual Studio 에서 {target.name}:{line} 을(를) 열었습니다."
    os.startfile(str(target))
    return True, f"{target.name} 을(를) 기본 프로그램으로 열었습니다. (줄 이동은 못 했습니다)"


def sync_places() -> list[str]:
    """동기화 폴더로 쓸 만한 곳을 제안합니다."""
    places = []
    for key in ("OneDrive", "OneDriveCommercial", "OneDriveConsumer"):
        root = os.environ.get(key)
        if root and Path(root).is_dir():
            places.append(str(Path(root) / "wiki"))
    for name in ("Dropbox", "Google Drive", "GoogleDrive"):
        folder = Path.home() / name
        if folder.is_dir():
            places.append(str(folder / "wiki"))
    return sorted(set(places))


# ---------------------------------------------------------------- 문서 경로
# 문서는 "폴더/하위폴더/제목" 형태의 ref 로 가리킵니다. 폴더가 없으면 제목만 씁니다.

def normalize_ref(ref: str) -> str:
    return "/".join(part.strip() for part in ref.split("/") if part.strip())


def is_valid_name(name: str) -> bool:
    name = name.strip()
    return bool(name) and len(name) <= 100 and not INVALID_CHARS.search(name) and name not in (".", "..")


def is_valid_ref(ref: str) -> bool:
    ref = normalize_ref(ref)
    return bool(ref) and len(ref) <= 200 and all(is_valid_name(part) for part in ref.split("/"))


def folder_of(ref: str) -> str:
    return ref.rsplit("/", 1)[0] if "/" in ref else ""


def title_of(ref: str) -> str:
    return ref.rsplit("/", 1)[-1]


def page_path(ref: str) -> Path:
    parts = normalize_ref(ref).split("/")
    return PAGES.joinpath(*parts[:-1], parts[-1] + ".md")


def folder_path(folder: str) -> Path:
    folder = normalize_ref(folder)
    return PAGES.joinpath(*folder.split("/")) if folder else PAGES


def page_exists(ref: str) -> bool:
    return bool(ref) and page_path(ref).is_file()


def resolve_ref(ref: str) -> str:
    """[[제목]] 처럼 폴더를 생략한 링크는 같은 제목의 문서가 하나뿐일 때 찾아 줍니다."""
    ref = normalize_ref(ref)
    if page_exists(ref):
        return ref
    # 링크가 옛 자리를 가리키더라도 같은 이름의 글이 하나뿐이면 그리로 보내 줍니다.
    matches = scan()["titles"].get(title_of(ref), [])
    return matches[0] if len(matches) == 1 else ref


# ---------------------------------------------------------------- 저장소

def read_page(ref: str) -> str:
    path = page_path(ref)
    return path.read_text(encoding="utf-8") if page_exists(ref) else ""


def write_page(ref: str, text: str) -> None:
    path = page_path(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")
    forget_scan()


def delete_page(ref: str) -> None:
    page_path(ref).unlink(missing_ok=True)
    forget_scan()


# ---------------------------------------------------------------- 댓글
# 글마다 달린 댓글을 데이터 폴더의 comments.json 한 곳에 모아 둡니다.
# 글이 옮겨지거나 지워지면 댓글도 따라 옮겨지고 지워집니다.

COMMENT_LOCK = threading.Lock()


def read_comments() -> dict:
    try:
        stored = json.loads(COMMENTS.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return stored if isinstance(stored, dict) else {}


def write_comments(all_comments: dict) -> None:
    keep = {ref: rows for ref, rows in all_comments.items() if rows}
    if not keep:
        COMMENTS.unlink(missing_ok=True)
        return
    COMMENTS.write_text(json.dumps(keep, ensure_ascii=False, indent=1), encoding="utf-8")


def page_comments(ref: str) -> list[dict]:
    rows = read_comments().get(ref, [])
    return sorted(rows, key=lambda row: row.get("at", ""))


def add_comment(ref: str, text: str) -> tuple[bool, str]:
    with COMMENT_LOCK:
        all_comments = read_comments()
        all_comments.setdefault(ref, []).append({
            "at": datetime.now().isoformat(timespec="microseconds"),
            "text": text.strip(),
        })
        write_comments(all_comments)
    return True, "댓글을 남겼습니다."


def change_comment(ref: str, at: str, text: str | None) -> tuple[bool, str]:
    """댓글 내용을 고치거나(text), 지웁니다(text=None)."""
    with COMMENT_LOCK:
        all_comments = read_comments()
        rows = all_comments.get(ref, [])
        for index, row in enumerate(rows):
            if row.get("at") != at:
                continue
            if text is None:
                rows.pop(index)
                done = "댓글을 지웠습니다."
            else:
                row["text"] = text.strip()
                row["edited"] = datetime.now().isoformat(timespec="seconds")
                done = "댓글을 고쳤습니다."
            all_comments[ref] = rows
            write_comments(all_comments)
            return True, done
    return False, "그 댓글을 찾지 못했습니다."


def move_comments(old_ref: str, new_ref: str) -> None:
    """글이나 폴더가 옮겨지면 그 아래 글들의 댓글도 함께 옮깁니다."""
    with COMMENT_LOCK:
        all_comments = read_comments()
        moved = {}
        for ref, rows in all_comments.items():
            if ref == old_ref:
                moved[new_ref] = rows
            elif ref.startswith(old_ref + "/"):
                moved[new_ref + ref[len(old_ref):]] = rows
            else:
                moved[ref] = rows
        if moved != all_comments:
            write_comments(moved)


CODE_SPAN = re.compile(r"(?P<ticks>`+)(?P<body>.+?)(?P=ticks)")
FENCE_LINE = re.compile(r"^[ 	]*(`{3,}|~{3,})", re.M)


def code_blocks(text: str) -> list[tuple[int, int]]:
    """``` 로 감싼 코드 블록이 차지한 구간을 돌려줍니다."""
    spans: list[tuple[int, int]] = []
    open_at, mark = -1, ""
    for line in FENCE_LINE.finditer(text):
        ticks = line.group(1)
        if open_at < 0:
            open_at, mark = line.start(), ticks[0]
        elif ticks[0] == mark:
            spans.append((open_at, line.end()))
            open_at, mark = -1, ""
    if open_at >= 0:  # 닫히지 않은 블록은 글 끝까지로 봅니다
        spans.append((open_at, len(text)))
    return spans


def widen_over_code(text: str, at: int, end: int) -> tuple[int, int]:
    """고른 자리가 `코드` 표시에 걸쳐 있으면 그 표시 바깥까지 넓힙니다.

    메모 기호를 백틱 안에 넣으면 메모가 아니라 글자 그대로 보이기 때문입니다.
    """
    for span in CODE_SPAN.finditer(text):
        if span.start() < end and at < span.end():
            at, end = min(at, span.start()), max(end, span.end())
    return at, end


def add_note(ref: str, quote: str, note: str) -> tuple[bool, str]:
    """글의 고른 부분을 메모 표시로 감쌉니다."""
    text = read_page(ref)
    at = text.find(quote) if quote else -1
    if at < 0 and quote:
        # 화면에서 고른 글자는 줄바꿈·공백이 원문과 다를 수 있어 느슨하게 한 번 더 찾습니다.
        loose = re.compile(r"\s+".join(re.escape(part) for part in quote.split()))
        found = loose.search(text)
        at, quote = (found.start(), found.group(0)) if found else (-1, quote)
    if at < 0:
        return False, "고른 글자를 본문에서 찾지 못했습니다. 글을 고친 뒤 다시 해 주세요."

    end = at + len(quote)
    if any(start <= at < stop for start, stop in code_blocks(text)):
        return False, "코드 블록 안에는 메모를 붙일 수 없습니다. 블록 밖의 글자를 골라 주세요."
    at, end = widen_over_code(text, at, end)
    quote = text[at:end]

    note = note.strip().replace("}}", "} }")
    write_page(ref, text[:at] + "{{" + quote + "||" + note + "}}" + text[end:])
    return True, "메모를 붙였습니다."


def change_note(ref: str, quote: str, note: str | None) -> tuple[bool, str]:
    """메모 내용을 고치거나(note), 표시를 걷어냅니다(note=None)."""
    text = read_page(ref)
    for found in re.finditer(NOTE_RE, text):
        # 화면에서 온 글자에는 `백틱` 같은 서식 기호가 없으므로 그것까지 벗겨 견줍니다.
        inner = found.group(1)
        if quote not in (inner, inner.strip("`")):
            continue
        if note is None:
            body = inner
            done = "메모를 지웠습니다."
        else:
            body = "{{" + inner + "||" + note.strip().replace("}}", "} }") + "}}"
            done = "메모를 고쳤습니다."
        write_page(ref, text[:found.start()] + body + text[found.end():])
        return True, done
    return False, "그 메모를 찾지 못했습니다."


def move_children(old_ref: str, new_ref: str) -> bool:
    """글 아래에 달린 글들을 새 자리로 함께 옮깁니다."""
    kids = folder_path(old_ref)
    if not kids.is_dir():
        return True
    target = folder_path(new_ref)
    if target.exists():
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    kids.rename(target)
    forget_scan()
    return True


def relink(old_ref: str, new_ref: str) -> int:
    """글이나 폴더가 자리를 옮기면 다른 글에 걸린 [[링크]]도 새 자리로 고칩니다."""
    if old_ref == new_ref or not old_ref:
        return 0

    def swap(found: re.Match) -> str:
        raw, bar, label = found.group(1).partition("|")
        target = normalize_ref(raw)
        if target == old_ref:
            moved = new_ref
        elif target.startswith(old_ref + "/"):      # 아래에 달린 글들도 같이 따라갑니다
            moved = new_ref + target[len(old_ref):]
        else:
            return found.group(0)
        return "[[" + moved + (bar + label if bar else "") + "]]"

    fixed = 0
    for ref, _ in list_pages():
        text = read_page(ref)
        changed = re.sub(WIKILINK_RE, swap, text)
        if changed != text:
            write_page(ref, changed)
            fixed += 1
    return fixed


def list_pages() -> list[tuple[str, datetime]]:
    return scan()["pages"]


# 폴더를 매번 훑으면 글이 많아질수록 느려지므로, 훑은 결과를 잠깐 담아 둡니다.
# 우리가 글을 고치면 바로 버리고, 밖에서(탐색기·동기화) 바뀐 것도 CACHE_TTL 안에 반영됩니다.
CACHE_TTL = 2.0
_scan_lock = threading.Lock()
_scan: dict = {"at": 0.0, "root": None}


def forget_scan() -> None:
    _scan["at"] = 0.0


def scan() -> dict:
    with _scan_lock:
        now = time.monotonic()
        if _scan["root"] == PAGES and now - _scan["at"] < CACHE_TTL:
            return _scan
        pages, folders = [], []
        names: dict[str, list[tuple[str, bool]]] = {"": []}

        def walk(base: Path, parent: str) -> None:
            # os.scandir 은 디렉터리 정보를 한 번에 읽어 오므로 파일마다 다시 묻지 않습니다.
            try:
                entries = list(os.scandir(base))
            except OSError:
                return
            for entry in entries:
                if entry.is_dir():
                    folder = f"{parent}/{entry.name}" if parent else entry.name
                    folders.append(folder)
                    names.setdefault(folder, [])
                    names.setdefault(parent, []).append((entry.name, True))
                    walk(Path(entry.path), folder)
                elif entry.name.endswith(".md"):
                    title = entry.name[:-3]
                    ref = f"{parent}/{title}" if parent else title
                    pages.append((ref, datetime.fromtimestamp(entry.stat().st_mtime)))
                    names.setdefault(parent, []).append((title, False))

        walk(PAGES, "")

        titles: dict[str, list[str]] = {}
        for ref, _ in pages:
            titles.setdefault(title_of(ref), []).append(ref)

        children = {}
        for folder, found in names.items():
            # 글과 같은 이름의 폴더는 그 글의 자식들을 담는 곳이므로 따로 세지 않습니다.
            here = {name for name, is_folder in found if not is_folder}
            found = [item for item in found if not (item[1] and item[0] in here)]
            found.sort(key=lambda item: (not item[1], item[0]))
            saved = read_order(folder)
            children[folder] = ([item for item in saved if item in found]
                                + [item for item in found if item not in saved])

        _scan.update({
            "at": now, "root": PAGES,
            "pages": sorted(pages, key=lambda item: item[1], reverse=True),
            "folders": sorted(folders, key=lambda folder: folder.split("/")),
            "titles": titles, "children": children,
        })
        return _scan


def read_order(folder: str) -> list[tuple[str, bool]]:
    """폴더에 저장해 둔 표시 순서를 (이름, 폴더인가) 목록으로 읽습니다."""
    path = folder_path(folder) / ORDER_FILE
    if not path.is_file():
        return []
    items = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line.endswith("/"):
            items.append((line[:-1], True))
        elif line:
            items.append((line, False))
    return items


def write_order(folder: str, items: list[tuple[str, bool]]) -> None:
    lines = [f"{name}/" if is_folder else name for name, is_folder in items]
    path = folder_path(folder) / ORDER_FILE
    path.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    forget_scan()


def children_of(folder: str) -> list[tuple[str, bool]]:
    """폴더 바로 아래의 하위 폴더와 문서를 저장된 순서대로 돌려줍니다."""
    return scan()["children"].get(folder, [])


def has_children(ref: str) -> bool:
    return bool(children_of(ref))


def ordered_refs() -> list[str]:
    """모든 문서를 저장된 표시 순서대로 나열합니다."""
    refs = []

    def walk(folder: str) -> None:
        for name, is_folder in children_of(folder):
            path = f"{folder}/{name}" if folder else name
            if not is_folder:
                refs.append(path)
            walk(path)   # 글 아래에 달린 글도 이어서 봅니다

    walk("")
    return refs


def list_folders() -> list[str]:
    return scan()["folders"]


def in_folder(ref: str, folder: str) -> bool:
    return not folder or ref.startswith(folder + "/")


def create_folder(folder: str) -> tuple[bool, str]:
    path = folder_path(folder)
    if path.exists():
        return False, f"‘{folder}’ 폴더가 이미 있습니다."
    path.mkdir(parents=True)
    forget_scan()
    return True, f"‘{folder}’ 폴더를 만들었습니다."


def rename_folder(folder: str, to: str) -> tuple[bool, str]:
    """폴더 안의 문서와 하위 폴더를 통째로 새 경로로 옮깁니다."""
    source, target = folder_path(folder), folder_path(to)
    if target.exists():
        return False, f"‘{to}’ 폴더가 이미 있습니다. 다른 이름을 써 주세요."
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    forget_scan()
    move_comments(folder, to)
    moved = relink(folder, to)
    tail = f" 링크 {moved}곳도 고쳤습니다." if moved else ""
    return True, f"‘{folder}’ 폴더를 ‘{to}’ 로 옮겼습니다.{tail}"


# ---------------------------------------------------------------- 휴지통
# 지운 글·폴더는 곧바로 없애지 않고 데이터 폴더의 trash/ 로 옮겨 둡니다.
# 한 번 지울 때마다 trash/<지운 때-이름>/ 하나가 생기고, 그 안에
#   meta.json — 어디에 있던 무엇인지, 딸려 간 글 목록, 달려 있던 댓글
#   content/  — 글 파일과 하위 폴더 그대로
# 를 담아 둡니다. 되살리기는 이것을 원래 자리로 되돌려 놓는 일입니다.

TRASH_META = "meta.json"
TRASH_BODY = "content"


def pages_under(ref: str) -> list[str]:
    """이 글(또는 폴더) 자신과 그 아래 딸린 글을 모두 돌려줍니다."""
    if not ref:
        return []
    return sorted(name for name, _ in list_pages() if name == ref or in_folder(name, ref))


def folders_under(ref: str) -> list[str]:
    """이 폴더 아래 딸린 하위 폴더입니다 (자신은 넣지 않습니다)."""
    if not ref:
        return []
    return sorted(name for name in list_folders() if in_folder(name, ref))


def trash_spot(ref: str) -> Path:
    """휴지통 안에 쓸 새 자리를 만듭니다. 이름이 겹치면 뒤에 번호를 붙입니다."""
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    safe = INVALID_CHARS.sub("_", ref.replace("/", "_"))[:60] or "이름없음"
    spot = TRASH / f"{stamp}-{safe}"
    number = 2
    while spot.exists():
        spot = TRASH / f"{stamp}-{safe}-{number}"
        number += 1
    return spot


def to_trash(ref: str, kind: str) -> tuple[bool, str]:
    """글이나 폴더를 아래 딸린 것까지 통째로 휴지통으로 옮깁니다."""
    page = page_path(ref)
    inside = folder_path(ref)
    if not page.is_file() and not inside.is_dir():
        return False, f"‘{ref}’ 는 없습니다."

    refs = pages_under(ref)
    with COMMENT_LOCK:
        all_comments = read_comments()
        taken = {
            other: rows for other, rows in all_comments.items()
            if other == ref or in_folder(other, ref)
        }
        spot = trash_spot(ref)
        body = spot / TRASH_BODY
        try:
            body.mkdir(parents=True)
            if page.is_file():
                shutil.move(str(page), str(body / page.name))
            if inside.is_dir():
                shutil.move(str(inside), str(body / title_of(ref)))
        except OSError as error:
            shutil.rmtree(spot, ignore_errors=True)
            return False, f"휴지통으로 옮기지 못했습니다: {error}"
        (spot / TRASH_META).write_text(
            json.dumps({
                "kind": kind, "ref": ref, "at": datetime.now().isoformat(timespec="seconds"),
                "pages": refs, "folders": folders_under(ref), "comments": taken,
            }, ensure_ascii=False, indent=1),
            encoding="utf-8",
        )
        if taken:
            write_comments({
                other: rows for other, rows in all_comments.items() if other not in taken
            })
    forget_scan()
    what = "폴더를" if kind == "folder" else "글을"
    deeper = [name for name in refs if name != ref]
    tail = f" 함께 딸린 글 {len(deeper)}개도 옮겼습니다." if deeper else ""
    return True, f"‘{ref}’ {what} 휴지통으로 옮겼습니다.{tail}"


def trash_rows() -> list[dict]:
    """휴지통에 든 것을 최근에 지운 것부터 돌려줍니다."""
    rows = []
    if not TRASH.is_dir():
        return rows
    for spot in TRASH.iterdir():
        if not spot.is_dir():
            continue
        try:
            meta = json.loads((spot / TRASH_META).read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        meta["id"] = spot.name
        meta["pages"] = meta.get("pages") or []
        rows.append(meta)
    rows.sort(key=lambda row: row.get("at", ""), reverse=True)
    return rows


def trash_files(spot: Path, ref: str) -> tuple[Path, Path]:
    """휴지통 자리에 담긴 글 파일과 하위 폴더의 경로입니다 (없을 수도 있습니다)."""
    body = spot / TRASH_BODY
    return body / (title_of(ref) + ".md"), body / title_of(ref)


def from_trash(entry: str) -> tuple[bool, str]:
    """휴지통에 든 것을 원래 자리로 되돌립니다."""
    spot = TRASH / entry
    if "/" in entry or "\\" in entry or not (spot / TRASH_META).is_file():
        return False, "휴지통에 없는 것입니다."
    meta = json.loads((spot / TRASH_META).read_text(encoding="utf-8"))
    ref = normalize_ref(meta.get("ref", ""))
    if not is_valid_ref(ref):
        return False, "되살릴 자리를 알 수 없습니다."
    page, inside = trash_files(spot, ref)
    if page.is_file() and page_path(ref).exists():
        return False, f"‘{ref}’ 글이 이미 있습니다. 그 글을 먼저 옮기거나 지워 주세요."
    if inside.is_dir() and folder_path(ref).exists():
        return False, f"‘{ref}’ 폴더가 이미 있습니다. 그 폴더를 먼저 옮기거나 지워 주세요."

    with COMMENT_LOCK:
        try:
            page_path(ref).parent.mkdir(parents=True, exist_ok=True)
            if page.is_file():
                shutil.move(str(page), str(page_path(ref)))
            if inside.is_dir():
                shutil.move(str(inside), str(folder_path(ref)))
        except OSError as error:
            return False, f"되살리지 못했습니다: {error}"
        back = meta.get("comments") or {}
        if back:
            all_comments = read_comments()
            for other, rows in back.items():
                all_comments.setdefault(other, []).extend(rows)
            write_comments(all_comments)
    shutil.rmtree(spot, ignore_errors=True)
    forget_scan()
    deeper = [name for name in (meta.get("pages") or []) if name != ref]
    tail = f" 함께 딸린 글 {len(deeper)}개도 돌아왔습니다." if deeper else ""
    return True, f"‘{ref}’ 를 되살렸습니다.{tail}"


def drop_trash(entry: str) -> tuple[bool, str]:
    """휴지통에 든 하나를 완전히 지웁니다."""
    spot = TRASH / entry
    if "/" in entry or "\\" in entry or not (spot / TRASH_META).is_file():
        return False, "휴지통에 없는 것입니다."
    shutil.rmtree(spot, ignore_errors=True)
    return True, "완전히 지웠습니다."


def empty_trash() -> tuple[bool, str]:
    rows = trash_rows()
    if not rows:
        return True, "휴지통이 이미 비어 있습니다."
    for row in rows:
        shutil.rmtree(TRASH / row["id"], ignore_errors=True)
    return True, f"휴지통을 비웠습니다. {len(rows)}개를 완전히 지웠습니다."


def delete_folder(folder: str) -> tuple[bool, str]:
    """폴더를 안에 든 글·하위 폴더까지 통째로 휴지통으로 보냅니다."""
    if not folder_path(folder).is_dir():
        return False, f"‘{folder}’ 폴더가 없습니다."
    return to_trash(folder, "folder")


def store_upload(filename: str, data: bytes, replace: bool = False) -> str:
    """첨부를 files/ 에 저장하고 실제 저장된 파일명을 반환합니다."""
    name = INVALID_CHARS.sub("_", Path(filename).name) or "file"
    target = FILES / name
    if not replace:
        stem, suffix = Path(name).stem, Path(name).suffix
        index = 1
        while target.exists():
            target = FILES / f"{stem}-{index}{suffix}"
            index += 1
    target.write_bytes(data)
    return target.name


def read_config() -> dict:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def write_config(values: dict) -> None:
    CONFIG.write_text(
        json.dumps({**read_config(), **values}, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def wiki_name() -> str:
    return read_config().get("name") or DEFAULT_NAME


def home_page() -> str:
    """위키 이름을 눌렀을 때 열 글. 정해 두지 않았으면 문서 목록으로 갑니다."""
    home = normalize_ref(read_config().get("home", ""))
    return home if page_exists(home) else ""


def search_pages(query: str) -> list[tuple[str, datetime, list[str]]]:
    """제목과 본문에서 검색어를 찾아 (문서 ref, 수정시각, 본문 발췌) 목록을 돌려줍니다."""
    needle = query.lower()
    found = []
    for ref, when in list_pages():
        lines = [line.strip() for line in read_page(ref).splitlines() if needle in line.lower()]
        if needle in ref.lower() or lines:
            found.append((ref, when, lines[:3]))
    return found


def highlight(text: str, query: str) -> str:
    if not query:
        return html.escape(text)
    pattern = re.compile(re.escape(html.escape(query)), re.IGNORECASE)
    return pattern.sub(lambda m: f"<mark>{m.group(0)}</mark>", html.escape(text))


# ---------------------------------------------------------------- 마크다운

class WikiLinkProcessor(InlineProcessor):
    """[[문서]], [[폴더/문서]], [[폴더/문서|보일 글자]] 를 내부 링크로 바꿉니다."""

    def handleMatch(self, m, data):
        raw, _, label = m.group(1).partition("|")
        ref = resolve_ref(raw)
        element = ElementTree.Element("a")
        element.text = label.strip() or raw.strip()
        element.set("data-wiki", m.group(1))  # 서식 편집에서 되돌릴 때 씁니다
        if page_exists(ref):
            element.set("href", "/w/" + urllib.parse.quote(ref))
        else:
            element.set("href", "/e/" + urllib.parse.quote(ref))
            element.set("class", "new")
            element.set("title", "아직 없는 문서입니다. 누르면 새로 만듭니다.")
        return element, m.start(0), m.end(0)


class NoteProcessor(InlineProcessor):
    """{{고른 글자||메모}} 를 눌러서 볼 수 있는 메모 표시로 바꿉니다."""

    def handleMatch(self, m, data):
        element = ElementTree.Element("span")
        element.text = m.group(1).replace(NOTE_NEWLINE, " ")
        element.set("class", "note")
        element.set("data-note", m.group(2).replace(NOTE_NEWLINE, "\n"))
        return element, m.start(0), m.end(0)


LOCAL_PATH = re.compile(r"^(?:file:///)?([A-Za-z]:[\\/].*?)(?::(\d+))?$")


class LocalLinkProcessor(Treeprocessor):
    """내 PC 경로로 건 링크는 눌렀을 때 서버가 열어 주도록 표시해 둡니다."""

    def run(self, root):
        for link in root.iter("a"):
            found = LOCAL_PATH.match(urllib.parse.unquote(link.get("href", "")))
            if not found:
                continue
            link.set("data-path", found.group(1).replace("/", "\\"))
            if found.group(2):
                link.set("data-line", found.group(2))
            link.set("class", (link.get("class", "") + " local").strip())
            link.set("href", "#")


class WikiLinkExtension(Extension):
    def extendMarkdown(self, md):
        md.inlinePatterns.register(WikiLinkProcessor(WIKILINK_RE, md), "wikilink", 170)
        md.inlinePatterns.register(NoteProcessor(NOTE_RE, md), "wikinote", 171)
        md.treeprocessors.register(LocalLinkProcessor(md), "locallink", 5)


try:  # 코드에 색을 입히는 데 씁니다. 없으면 색 없이 그대로 보여 줍니다.
    from pygments.formatters import HtmlFormatter

    CODE_EXTENSION = ["codehilite"]
    CODE_CSS = (
        HtmlFormatter(style="default").get_style_defs(".highlight")
        + "\n@media (prefers-color-scheme: dark) {\n"
        + HtmlFormatter(style="monokai").get_style_defs(":root:not([data-theme=light]) .highlight")
        + "\n}\n"
    )
except ImportError:
    CODE_EXTENSION = []
    CODE_CSS = ""

def build_markdown(highlight: bool) -> markdown.Markdown:
    # extra 에서 각주(footnotes)만 빼고 씁니다. 메모는 각주가 아니라 표시로 답니다.
    extensions = [
        "abbr", "attr_list", "def_list", "fenced_code", "md_in_html", "tables",
        "sane_lists", "nl2br", "toc", WikiLinkExtension(),
    ]
    return markdown.Markdown(
        extensions=extensions + (CODE_EXTENSION if highlight else []),
        extension_configs={"codehilite": {"guess_lang": False, "css_class": "highlight"}},
        output_format="html",
    )


MD = build_markdown(True)
# 색을 입히면 코드 종류가 HTML 에서 사라져 서식 편집으로 되돌릴 수 없으므로,
# 편집 화면에는 색 없이 종류만 남긴 판을 씁니다.
MD_PLAIN = build_markdown(False)


MD_LOCK = threading.Lock()
BLOCK_START = re.compile(r"^\s*(?:[-*+] |\d+[.)] |\|)")


def loosen(text: str) -> str:
    """문단 바로 아래에 붙여 쓴 목록·표·코드블록도 블록으로 인식되도록 빈 줄을 넣습니다.
    빈 줄을 여럿 두면 마크다운은 하나로 합치므로, 누른 Enter 만큼 실제로 띄워 보이게 합니다."""
    lines = []
    fenced = False
    blanks = 0
    for line in text.split("\n"):
        starts_block = BLOCK_START.match(line) or line.lstrip().startswith("```")
        if not fenced and starts_block and lines and lines[-1].strip():
            if not BLOCK_START.match(lines[-1]) and not lines[-1].lstrip().startswith("```"):
                lines.append("")
        if line.lstrip().startswith("```"):
            fenced = not fenced
        if not fenced and not line.strip():
            blanks += 1
            # 첫 빈 줄은 문단을 나누고, 그 뒤로 이어지는 빈 줄은 한 줄씩 띄웁니다.
            # <br> 앞뒤를 빈 줄로 감싸야 옆 문단에 붙지 않고 한 줄로 남습니다.
            if blanks > 1 and any(kept.strip() for kept in lines):
                lines.append("<br>")
                lines.append("")
                continue
        elif not fenced:
            blanks = 0
        lines.append(line)
    return "\n".join(lines)


def press_notes(text: str) -> str:
    """여러 줄에 걸친 {{고른 글자||메모}} 를 한 줄로 눌러 둡니다.

    메모 안에 줄바꿈이 있으면 목록·제목 규칙이 메모를 두 덩이로 끊어 읽어서,
    메모 표시가 아니라 {{ }} 기호째로 보였습니다. 코드블록 안은 그대로 둡니다.
    """
    if "{{" not in text:
        return text
    fenced = code_blocks(text)

    def press(found):
        if any(start <= found.start() < stop for start, stop in fenced):
            return found.group(0)
        return re.sub(r"\n[ \t]*", NOTE_NEWLINE, found.group(0))

    return re.sub(NOTE_RE, press, text)


def render(text: str, highlight: bool = True) -> str:
    engine = MD if highlight else MD_PLAIN
    with MD_LOCK:
        engine.reset()
        # 메모를 먼저 한 줄로 눌러 두어야 loosen() 이 메모 속을 끊지 않습니다.
        return engine.convert(loosen(press_notes(text)))


class MarkdownWriter(HTMLParser):
    """서식 편집 모드에서 고친 화면(HTML)을 다시 마크다운으로 되돌립니다."""

    HEADINGS = {f"h{level}": "#" * level for level in range(1, 7)}
    SKIP = {"script", "style", "head", "meta", "colgroup", "col"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.out: list[str] = []
        self.line = ""
        self.lists: list[list] = []          # [마커, 번호] 중첩 목록
        self.quote = 0
        self.pre = 0
        self.skip = 0
        self.link: list[tuple] = []          # 링크 여는 태그 정보 (종류, 가리키는 곳, 글자 시작 자리)
        self.cells: list[str] | None = None  # 표 한 줄
        self.fence_at: int | None = None     # 코드블록 여는 줄 자리 (언어 이름을 나중에 채움)
        self.note: str | None = None         # 메모 표시 안에 있는 동안 담아 두는 메모 내용
        self.table_head = False
        self.table_width = 0

    # -- 줄 다루기 ------------------------------------------------------
    def add(self, text: str) -> None:
        self.line += text

    def flush(self, blank: bool = False) -> None:
        text = self.line.rstrip()
        self.line = ""
        if text:
            prefix = "> " * self.quote
            if self.lists:
                pad = "    " * (len(self.lists) - 1)
                marker, number = self.lists[-1]
                bullet = f"{number}. " if marker == "1" else "- "
                self.lists[-1][1] = number + 1
                prefix += pad + bullet
            self.out.append(prefix + text)
        elif not blank:
            return
        if blank and (not self.out or self.out[-1] != ""):
            self.out.append("")

    # -- 태그 -----------------------------------------------------------
    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in self.SKIP:
            self.skip += 1
        elif tag in self.HEADINGS:
            self.flush(blank=True)
            self.add(self.HEADINGS[tag] + " ")
        elif tag in ("p", "div", "tbody", "thead", "table"):
            if tag == "table":
                self.table_head = True
                self.table_width = 0
            self.flush(blank=tag in ("p", "div", "table"))
        elif tag == "br":
            if self.cells is not None:
                self.add(" ")   # 표 칸 안에서 줄을 나누면 표가 깨집니다
            elif not self.line and not self.lists and not self.quote:
                self.out.append("")   # Enter 만 누른 빈 줄은 그대로 남깁니다
            else:
                self.flush()
        elif tag == "hr":
            self.flush(blank=True)
            self.out.append("---")
            self.out.append("")
        elif tag in ("strong", "b"):
            self.add("**")
        elif tag in ("em", "i"):
            self.add("*")
        elif tag == "code" and not self.pre:
            self.add("`")
        elif tag == "code" and self.fence_at is not None:
            for name in values.get("class", "").split():
                if name.startswith("language-"):
                    self.out[self.fence_at] = "```" + name[len("language-"):]
            self.fence_at = None
        elif tag == "pre":
            self.flush(blank=True)
            self.fence_at = len(self.out)
            self.out.append("```")
            self.pre += 1
        elif tag in ("ul", "ol"):
            self.flush()
            self.lists.append(["1" if tag == "ol" else "-", 1])
        elif tag == "li":
            self.flush()
        elif tag == "blockquote":
            self.flush(blank=True)
            self.quote += 1
        elif tag == "a":
            wiki = values.get("data-wiki")
            if wiki is not None:
                # 위키 링크는 가리키는 글만 지켜 두고, 보이는 글자는 화면에서 고친 대로 씁니다.
                self.link.append(("wiki", wiki.partition("|")[0], len(self.line)))
            else:
                self.link.append(("url", values.get("href", ""), 0))
                self.add("[")
        elif tag == "span" and "data-note" in values:
            self.add("{{")
            self.note = values["data-note"]
        elif tag == "img":
            source = values.get("src", "")
            self.add(f'![{values.get("alt", "")}]({source})')
        elif tag == "tr":
            self.cells = []
        elif tag in ("td", "th"):
            self.line = ""

    def handle_endtag(self, tag):
        if tag in self.SKIP:
            self.skip = max(0, self.skip - 1)
        elif tag in self.HEADINGS or tag in ("p", "div"):
            self.flush(blank=True)
        elif tag in ("strong", "b"):
            self.add("**")
        elif tag in ("em", "i"):
            self.add("*")
        elif tag == "code" and not self.pre:
            self.add("`")
        elif tag == "pre":
            self.flush()
            self.pre = max(0, self.pre - 1)
            self.out.append("```")
            self.out.append("")
        elif tag in ("ul", "ol"):
            self.flush()
            if self.lists:
                self.lists.pop()
            if not self.lists:
                self.flush(blank=True)
        elif tag == "li":
            self.flush()
        elif tag == "blockquote":
            self.flush()
            self.quote = max(0, self.quote - 1)
            self.flush(blank=True)
        elif tag == "a" and self.link:
            kind, target, at = self.link.pop()
            if kind != "wiki":
                self.add(f"]({target})")
                return
            shown = self.line[at:].strip()   # 화면에서 고친 링크 글자
            self.line = self.line[:at]
            self.add(f"[[{target}]]" if not shown or shown == target
                     else f"[[{target}|{shown}]]")
        elif tag == "span" and self.note is not None:
            self.add("||" + self.note + "}}")
            self.note = None
        elif tag in ("td", "th") and self.cells is not None:
            self.cells.append(self.line.strip().replace("|", r"\|"))
            self.line = ""
        elif tag == "tr" and self.cells is not None:
            cells = self.cells or [""]
            # 칸 속에 공백을 두지 않습니다. 파이프 사이에 그 칸 내용만 들어갑니다.
            self.out.append("|" + "|".join(cells) + "|")
            if self.table_head:
                self.out.append("|" + "|".join("---" for _ in cells) + "|")
                self.table_head = False
            self.cells = None
        elif tag == "table":
            self.table_head = False
            self.out.append("")

    def handle_data(self, data):
        if self.skip:
            return
        if self.pre:
            for index, piece in enumerate(data.split("\n")):
                if index:
                    self.flush()
                self.add(piece)
            return
        # 폭 없는 공백(U+200B)은 링크 글자를 고칠 때 자리만 잡아 두는 글자라 저장하지 않습니다.
        clean = data.replace("\xa0", " ").replace("\u200b", "")
        text = re.sub(r"[ \t]*\n[ \t]*", " ", clean)
        if not text.strip() and not self.line:
            return
        self.add(text)

    def result(self) -> str:
        self.flush()
        lines = self.out
        while lines and not lines[0].strip():
            lines.pop(0)
        while lines and not lines[-1].strip():
            lines.pop()
        return "\n".join(lines) + "\n" if lines else ""


def to_markdown(html_text: str) -> str:
    writer = MarkdownWriter()
    writer.feed(html_text.replace(" ", " "))
    writer.close()
    return writer.result()


# ---------------------------------------------------------------- 화면

CSS = """
:root {
  --bg: #ffffff; --fg: #1f2328; --muted: #656d76; --line: #d8dee4;
  --accent: #0969da; --new: #cf222e; --code-bg: #f6f8fa; --card: #f6f8fa;
  --mark: #fff3c4;
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0d1117; --fg: #e6edf3; --muted: #9198a1; --line: #30363d;
    --accent: #4493f8; --new: #ff7b72; --code-bg: #161b22; --card: #161b22;
    --mark: #5a4a00;
  }
}
* { box-sizing: border-box; }
[hidden] { display: none !important; }
html { --head: 61px; --side: 260px; }
body {
  margin: 0; background: var(--bg); color: var(--fg);
  font: 16px/1.7 "Pretendard", "Malgun Gothic", -apple-system, sans-serif;
}
header {
  display: flex; align-items: center; gap: 10px; padding: 12px 24px;
  border-bottom: 1px solid var(--line); position: sticky; top: 0;
  background: var(--bg); z-index: 10; flex-wrap: wrap;
}
header .brand {
  font-weight: 700; font-size: 18px; text-decoration: none; color: var(--fg); margin-right: 6px;
}
header .spacer { flex: 1; }
.layout { display: flex; align-items: flex-start; }
aside {
  width: var(--side); flex: none; padding: 16px 8px; border-right: 1px solid var(--line);
  position: sticky; top: var(--head); height: calc(100vh - var(--head)); overflow-y: auto;
}
.grip {
  flex: none; width: 6px; cursor: col-resize; position: sticky; top: var(--head);
  height: calc(100vh - var(--head));
}
.grip:hover, .grip.on { background: var(--accent); }
html.nosidebar aside, html.nosidebar .grip { display: none; }
@media (max-width: 820px) { aside, .grip { display: none; } }
aside .side-head {
  display: flex; align-items: center; justify-content: space-between; gap: 8px;
  font-size: 13px; color: var(--muted); padding: 0 8px 8px;
}
aside .side-acts { display: flex; align-items: center; gap: 6px; flex: none; }
aside .side-acts .btn { padding: 2px 8px; font-size: 12px; line-height: 1.6; }
#side-sort { padding: 2px 6px; font-size: 12px; max-width: 108px; }
aside .item { display: flex; align-items: center; border-radius: 6px; cursor: grab; }
aside .item.dragging { opacity: .4; }
aside .item.drop > a { outline: 2px dashed var(--accent); outline-offset: -2px; }
aside .item.drop-before { box-shadow: inset 0 2px 0 var(--accent); }
aside .item.drop-after { box-shadow: inset 0 -2px 0 var(--accent); }
aside.drop { background: var(--card); }
aside a {
  display: block; flex: 1; min-width: 0; padding: 4px 8px; border-radius: 6px;
  font-size: 14px; text-decoration: none; color: var(--fg);
  overflow: hidden; text-overflow: ellipsis; white-space: nowrap;
}
aside a:hover { background: var(--card); }
aside a.on { background: var(--accent); color: #fff; }
aside a.folder { color: var(--muted); font-weight: 600; }
aside a.folder.on { color: #fff; }
aside .acts { display: none; gap: 1px; padding-right: 2px; }
aside .item:hover .acts { display: flex; }
aside .acts button {
  border: none; background: none; color: var(--muted); cursor: pointer;
  font-size: 13px; line-height: 1; padding: 4px 5px; border-radius: 4px;
}
aside .acts button:hover { color: var(--accent); background: var(--card); }
aside .twist {
  border: none; background: none; color: var(--muted); cursor: pointer;
  width: 20px; padding: 6px 0; font-size: 12px; flex: none; border-radius: 4px;
}
aside .twist:hover { color: var(--accent); background: var(--card); }
aside .none { padding: 8px; font-size: 13px; color: var(--muted); }
main { flex: 1; min-width: 0; max-width: 900px; margin: 0 auto; padding: 24px; }
a { color: var(--accent); }
a.new { color: var(--new); border-bottom: 1px dashed var(--new); text-decoration: none; }
a.local { text-decoration: none; border-bottom: 1px dotted var(--accent); }
a.local::before { content: "📄 "; font-size: .9em; }
a.local[data-line]::before { content: "</> "; font-family: Consolas, monospace; font-size: .85em; }
mark { background: var(--mark); color: inherit; border-radius: 3px; }
h1, h2, h3 { line-height: 1.3; margin-top: 1.6em; }
h1 { margin-top: 0; padding-bottom: .3em; border-bottom: 1px solid var(--line); }
h2 { padding-bottom: .3em; border-bottom: 1px solid var(--line); }
code { background: var(--code-bg); padding: .15em .35em; border-radius: 4px; font-size: .9em; }
pre { background: var(--code-bg); padding: 14px; border-radius: 8px; overflow-x: auto; }
pre code { background: none; padding: 0; }
blockquote { margin: 0; padding: 0 1em; color: var(--muted); border-left: 3px solid var(--line); }
table { border-collapse: collapse; display: block; overflow-x: auto; }
th, td { border: 1px solid var(--line); padding: 6px 12px; }
img { max-width: 100%; border-radius: 6px; }
.btn {
  display: inline-block; padding: 6px 14px; border: 1px solid var(--line);
  border-radius: 6px; background: var(--card); color: var(--fg);
  text-decoration: none; cursor: pointer; font: inherit; font-size: 14px;
}
.btn:hover { border-color: var(--accent); color: var(--accent); }
.btn.primary { background: var(--accent); border-color: var(--accent); color: #fff; }
.btn.danger { color: var(--new); border-color: var(--new); }
.btn.danger:hover { background: var(--new); border-color: var(--new); color: #fff; }
.meta { color: var(--muted); font-size: 13px; }
.crumbs { font-size: 14px; margin-bottom: 10px; }
.crumbs a { text-decoration: none; }
.group { margin-top: 26px; }
.group h2 {
  display: flex; align-items: center; gap: 4px;
  font-size: 15px; color: var(--muted); border: none; padding: 0; margin: 0;
}
.group h2 .twist {
  border: none; background: none; color: var(--muted); cursor: pointer;
  font-size: 13px; padding: 6px 8px; border-radius: 4px;
}
.group h2 .twist:hover { color: var(--accent); background: var(--card); }
.list { list-style: none; padding: 0; margin: 6px 0 0; }
.list li { padding: 12px 4px; border-bottom: 1px solid var(--line); }
.list .row { display: flex; align-items: center; justify-content: space-between; gap: 12px; }
.list .row > a { flex: 1; min-width: 0; }
.list .acts { display: none; gap: 6px; }
.list li:hover .acts { display: flex; }
.list .acts .btn { padding: 2px 10px; font-size: 12px; }
.list a { text-decoration: none; font-weight: 500; }
.list .snippet { color: var(--muted); font-size: 14px; margin-top: 4px; }
.empty { color: var(--muted); padding: 40px 0; text-align: center; }
.titlerow { display: flex; gap: 10px; margin-bottom: 10px; }
#folder { width: 34%; }
#title { flex: 1; font-weight: 700; }
#folder, #title {
  padding: 10px 12px; font-size: 20px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--bg); color: var(--fg); min-width: 0;
}
#folder { font-size: 15px; color: var(--muted); }
#editor {
  width: 100%; min-height: 55vh; padding: 14px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card); color: var(--fg);
  font: 14px/1.7 "Cascadia Mono", Consolas, monospace; resize: vertical;
}
#editor.drag { border-color: var(--accent); border-style: dashed; }
.mdbar, .drawbar { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; align-items: center; }
.mdbar .btn, .drawbar .btn { padding: 4px 10px; font-size: 13px; }
.tabs { display: flex; gap: 6px; margin-bottom: 8px; }
#rich {
  min-height: 55vh; padding: 14px 18px; border: 1px solid var(--line); border-radius: 8px;
  overflow-x: auto;
}
#rich:focus { outline: none; border-color: var(--accent); }
#rich > :first-child { margin-top: 0; }
#rich:empty::before { content: "여기에 바로 쓰면 됩니다."; color: var(--muted); }
.mdbar .btn[disabled] { opacity: .4; cursor: default; }

/* 편집 화면은 글이 길어져도 화면이 통째로 흘러가지 않게, 편집창에만 스크롤을 답니다.
   저장 단추·제목·서식 단추는 늘 같은 자리에 붙어 있습니다.
   자리가 아주 좁아 편집창이 더 줄어들 수 없을 때만 화면 스크롤이 대신 나옵니다. */
body.editing { overflow-y: auto; }
body.editing main {
  display: flex; flex-direction: column;
  height: calc(100vh - var(--head));
  height: calc(100dvh - var(--head));
  padding: 12px 24px 14px; max-width: 1200px;
}
body.editing main > * { flex: none; }
body.editing #editor, body.editing #rich {
  flex: 1 1 auto; min-height: 160px; height: auto; resize: none; overflow-y: auto;
}
/* 편집창에 자리를 더 내주려고 위쪽 단추·제목 줄을 조금 좁힙니다. */
body.editing main > .toolbar { margin: 0 0 8px; }
body.editing main > .titlerow { margin-bottom: 8px; }
body.editing #folder, body.editing #title { padding: 7px 11px; font-size: 18px; }
body.editing #folder { font-size: 14px; }
body.editing main > .tabs, body.editing main > .mdbar { margin-bottom: 6px; }
details.hint { margin: 10px 0 0; }
details.hint > summary {
  cursor: pointer; color: var(--muted); font-size: 13px; user-select: none;
}
details.hint > summary:hover { color: var(--accent); }
details.hint > p { margin: 8px 0 0; }
/* 휴지통 화면과, 지우기 전에 묻는 팝업 */
.trash { list-style: none; padding: 0; margin: 12px 0; }
.trash li {
  border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px;
  margin-bottom: 8px; background: var(--card);
}
.trash .trash-head { display: flex; align-items: center; gap: 8px; flex-wrap: wrap; }
.trash .trash-head .spacer { flex: 1; }
.trash .trash-head .btn { padding: 2px 10px; font-size: 13px; }
.trash details { margin-top: 6px; }
.trash details summary { cursor: pointer; font-size: 13px; }
.trash details ul { margin: 6px 0 0; padding-left: 20px; font-size: 13px; color: var(--muted); }
#drop-modal .sheet { width: 460px; }
#drop-modal .warn { color: var(--new); font-weight: 600; margin: 10px 0 4px; }
ul.drop-list {
  margin: 4px 0 0; padding-left: 20px; max-height: 180px; overflow-y: auto;
  font-size: 13px; color: var(--muted);
}
.modal {
  position: fixed; inset: 0; z-index: 30; padding: 20px; background: rgba(0, 0, 0, .45);
  display: flex; align-items: center; justify-content: center;
}
.modal .sheet {
  background: var(--bg); border: 1px solid var(--line); border-radius: 10px;
  padding: 20px; max-width: 92vw; max-height: 88vh; overflow: auto;
}
.modal h2 { margin: 0; border: none; padding: 0; font-size: 18px; }
#grid { border-collapse: collapse; display: table; margin: 12px 0; }
#grid td { border: 1px solid var(--line); padding: 0; }
#grid input {
  border: none; background: none; color: var(--fg); font: inherit;
  padding: 7px 10px; width: 150px; border-radius: 0;
}
#grid input:focus { outline: 2px solid var(--accent); outline-offset: -2px; }
#grid tr:first-child input { font-weight: 700; background: var(--card); }
#link-modal .sheet { width: min(680px, 92vw); }
#link-folder { width: 100%; font-size: 15px; color: var(--muted); margin-top: 8px; }
#link-title { width: 100%; font-size: 18px; font-weight: 700; }
#link-new { margin-top: 4px; }
#link-pick {
  width: 100%; padding: 9px 12px; font-size: 16px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--bg); color: var(--fg);
}
#link-folder, #link-title {
  padding: 8px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--bg); color: var(--fg); min-width: 0;
}
#link-text {
  width: 100%; min-height: 220px; padding: 12px; border: 1px solid var(--line);
  border-radius: 8px; background: var(--card); color: var(--fg);
  font: 14px/1.7 "Cascadia Mono", Consolas, monospace; resize: vertical;
}
#canvas {
  border: 1px solid var(--line); border-radius: 8px; touch-action: none; cursor: crosshair;
  max-width: 100%; height: auto; background: #ffffff;
}
.swatch {
  width: 24px; height: 24px; border: 2px solid var(--line); border-radius: 50%;
  cursor: pointer; padding: 0;
}
.swatch.on { border-color: var(--accent); transform: scale(1.15); }
#rich img { cursor: pointer; }
#note-bubble {
  position: absolute; z-index: 20; padding: 4px 12px; font-size: 13px;
  box-shadow: 0 2px 8px rgba(0, 0, 0, .25);
}
#comments { margin-top: 36px; border-top: 1px solid var(--line); padding-top: 8px; }
#comments h2 { border: none; padding: 0; }
.comments { list-style: none; padding: 0; margin: 0 0 16px; }
.comments li {
  border: 1px solid var(--line); border-radius: 8px; padding: 8px 12px;
  margin-bottom: 8px; background: var(--card);
}
.comments .comment-head { display: flex; align-items: center; gap: 6px; }
.comments .comment-head .spacer { flex: 1; }
.comments .comment-head .btn { padding: 1px 8px; font-size: 12px; visibility: hidden; }
.comments li:hover .comment-head .btn { visibility: visible; }
.comments .comment-text > :first-child { margin-top: 4px; }
.comments .comment-text > :last-child { margin-bottom: 0; }
#comments textarea {
  width: 100%; padding: 10px 12px; border: 1px solid var(--line); border-radius: 8px;
  background: var(--card); color: var(--fg); font: inherit; resize: vertical;
}
#comments .toolbar { margin: 8px 0 0; }
.note {
  background: var(--mark); border-bottom: 2px solid #d4a72c; cursor: pointer;
  border-radius: 2px;
}
.note::after { content: "📝"; font-size: .75em; vertical-align: super; margin-left: 2px; }
.note.open { outline: 2px solid var(--accent); outline-offset: 1px; }
#note-card {
  position: absolute; z-index: 20; width: 260px; max-width: 80vw; padding: 10px 12px;
  border: 1px solid var(--line); border-left: 3px solid #d4a72c; border-radius: 8px;
  background: var(--bg); box-shadow: 0 4px 14px rgba(0, 0, 0, .2);
  font-size: 14px; line-height: 1.6; white-space: pre-wrap;
}
#note-card .note-text {
  outline: none; border-radius: 4px; padding: 2px 4px; margin: -2px -4px; cursor: text;
}
#note-card .note-text:hover { background: var(--card); }
#note-card .note-text:focus { background: var(--card); box-shadow: 0 0 0 2px var(--accent); }
#note-card .note-foot {
  display: flex; justify-content: space-between; align-items: center;
  margin-top: 8px; font-size: 12px; color: var(--muted);
}
#note-card .drop-note {
  font-size: 12px; color: var(--muted); background: none; border: none;
  padding: 0; cursor: pointer;
}
#note-card .drop-note:hover { color: var(--new); }
.toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }
.hint { color: var(--muted); font-size: 13px; }
input[type=text], input[type=search], select {
  padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); font: inherit;
}
"""


def side_children(parent: str, sort: str) -> list[tuple[str, bool]]:
    """왼쪽 목록에 보일 순서. 폴더는 늘 위에 두고 글만 고른 기준으로 세웁니다."""
    items = children_of(parent)
    if sort == "order":
        return items
    folders = sorted((item for item in items if item[1]), key=lambda item: item[0].casefold())
    pages = [item for item in items if not item[1]]
    if sort == "name":
        pages.sort(key=lambda item: item[0].casefold())
    else:
        changed = dict(list_pages())
        pages.sort(
            key=lambda item: changed.get(f"{parent}/{item[0]}" if parent else item[0]),
            reverse=(sort == "recent"),
        )
    return folders + pages


def sidebar_rows(
    parent: str, depth: int, current_ref: str, current_folder: str, closed: set[str],
    sort: str = "order",
) -> list[str]:
    rows = []
    for name, is_folder in side_children(parent, sort):
        path = f"{parent}/{name}" if parent else name
        common = (
            f'<div class="item" draggable="true" '
            f'data-parent="{html.escape(parent, quote=True)}" '
            f'data-name="{html.escape(name, quote=True)}" '
        )
        indent = 8 + depth * 14
        if is_folder:
            state = " on" if path == current_folder else ""
            folded = path in closed
            rows.append(
                f'{common}data-folder="{html.escape(path, quote=True)}">'
                f'<button class="twist" data-act="fold" style="margin-left:{indent}px" '
                f'title="접기/펴기">{"▸" if folded else "▾"}</button>'
                f'<a class="folder{state}" draggable="false" style="padding-left:2px" '
                f'href="#" data-fold="1" title="눌러서 접기/펴기">📁 {html.escape(name)}</a>'
                '<span class="acts">'
                '<button data-act="write" title="이 폴더에 새 글">📄</button>'
                '<button data-act="add" title="하위 폴더 추가">＋</button>'
                '<button data-act="rename" title="폴더 이름·위치 바꾸기">✎</button>'
                '<button data-act="remove" title="폴더 삭제">✕</button>'
                "</span></div>"
            )
            if not folded:
                rows += sidebar_rows(path, depth + 1, current_ref, current_folder, closed, sort)
        else:
            state = " on" if path == current_ref else ""
            nested = has_children(path)
            folded = path in closed
            twist = (
                f'<button class="twist" data-act="fold" style="margin-left:{indent}px" '
                f'title="접기/펴기">{"▸" if folded else "▾"}</button>'
                if nested else f'<span class="twist" style="margin-left:{indent}px"></span>'
            )
            rows.append(
                f'{common}data-ref="{html.escape(path, quote=True)}" '
                f'data-folder="{html.escape(parent, quote=True)}">{twist}'
                f'<a class="doc{state}" draggable="false" style="padding-left:2px" '
                f'href="/w/{urllib.parse.quote(path)}">{html.escape(name)}</a>'
                '<span class="acts">'
                '<button data-act="under" title="이 글 아래에 새 글">📄</button>'
                '<button data-act="edit" title="글 제목 바꾸기">✎</button>'
                '<button data-act="erase" title="글 삭제">✕</button>'
                "</span></div>"
            )
            if nested and not folded:
                rows += sidebar_rows(path, depth + 1, current_ref, current_folder, closed, sort)
    return rows


def sidebar(
    current_ref: str = "", current_folder: str = "", closed: set[str] = frozenset(),
    sort: str = "order",
) -> str:
    # 지금 보고 있는 문서·폴더로 가는 길목은 접혀 있어도 펼쳐서 보여 줍니다.
    trail = current_folder or folder_of(current_ref)
    parts = trail.split("/") if trail else []
    on_path = {"/".join(parts[:depth]) for depth in range(1, len(parts) + 1)}
    body = "".join(sidebar_rows("", 0, current_ref, current_folder, set(closed) - on_path, sort))
    body = body or '<div class="none">아직 문서가 없습니다.</div>'
    choices = "".join(
        f'<option value="{key}"{" selected" if key == sort else ""}>{label}</option>'
        for key, label in SORTS
    )
    return (
        '<div class="side-head">'
        '<span class="side-acts">'
        '<a class="btn" href="/new" title="새 글 쓰기">＋ 글</a>'
        '<button class="btn" id="side-create" title="새 폴더 만들기">＋ 폴더</button>'
        "</span>"
        f'<select id="side-sort" title="목록 정렬">{choices}</select>'
        "</div>" + body
    )


COMMON_SCRIPT = """
async function post(path, payload) {
  const res = await fetch(path, {
    method: 'POST', headers: {'Content-Type': 'application/json; charset=utf-8'},
    body: JSON.stringify(payload),
  });
  if (!res.ok) { alert(await res.text()); return null; }
  return res.json();
}

function goFolder(folder) {
  location.href = folder ? '/?folder=' + encodeURIComponent(folder) : '/';
}

// 지우기 전에 무엇이 함께 지워지는지 보여 주고 한 번 더 묻습니다.
const dropModal = document.getElementById('drop-modal');
let dropAnswer = null;

function closeDrop(answer) {
  if (!dropAnswer) { return; }
  dropModal.hidden = true;
  const say = dropAnswer;
  dropAnswer = null;
  say(answer);
}

function dropLines(refs) {
  const list = document.createElement('ul');
  list.className = 'drop-list';
  for (const ref of refs.slice(0, 30)) {
    const item = document.createElement('li');
    item.textContent = ref;
    list.append(item);
  }
  if (refs.length > 30) {
    const more = document.createElement('li');
    more.textContent = '… 그리고 ' + (refs.length - 30) + '개 더';
    list.append(more);
  }
  return list;
}

/** {ref: 글} 또는 {folder: 폴더} 를 받아 지워도 되는지 묻습니다. */
async function askDrop(target) {
  const plan = await post('/delete/plan', target);
  if (!plan) { return false; }
  const what = plan.kind === 'folder' ? '폴더를' : '글을';
  const deeper = (plan.pages || []).filter((ref) => ref !== plan.ref);
  const folders = plan.folders || [];

  const head = document.getElementById('drop-what');
  head.textContent = '';
  const name = document.createElement('b');
  name.textContent = plan.ref;
  head.append(name, ' ' + what + ' 휴지통으로 보냅니다.');

  const box = document.getElementById('drop-list');
  box.textContent = '';
  if (deeper.length || folders.length) {
    const bits = [];
    if (deeper.length) { bits.push('아래 딸린 글 ' + deeper.length + '개'); }
    if (folders.length) { bits.push('하위 폴더 ' + folders.length + '개'); }
    const line = document.createElement('p');
    line.className = 'warn';
    line.textContent = bits.join(', ') + '도 함께 지워집니다.';
    box.append(line);
    if (deeper.length) { box.append(dropLines(deeper)); }
  }
  dropModal.hidden = false;
  document.getElementById('drop-no').focus();
  return new Promise((say) => { dropAnswer = say; });
}

if (dropModal) {
  document.getElementById('drop-yes').onclick = () => closeDrop(true);
  document.getElementById('drop-no').onclick = () => closeDrop(false);
  dropModal.onclick = (e) => { if (e.target === dropModal) { closeDrop(false); } };
  document.addEventListener('keydown', (e) => {
    if (!dropModal.hidden && e.key === 'Escape') { closeDrop(false); }
  });
}

async function createFolder(base) {
  const name = prompt('만들 폴더 경로를 넣어 주세요. / 로 여러 단계를 쓸 수 있습니다.',
                      base ? base + '/' : '');
  if (!name) { return; }
  if (await post('/folder/create', {folder: name})) { goFolder(name); }
}

async function renameFolder(folder) {
  const to = prompt('새 폴더 경로를 넣어 주세요. / 로 여러 단계를 쓸 수 있습니다.', folder);
  if (!to || to === folder) { return; }
  if (await post('/folder/rename', {folder: folder, to: to})) { goFolder(to); }
}

async function removeFolder(folder) {
  if (!await askDrop({folder: folder})) { return; }
  if (await post('/folder/delete', {folder: folder})) { goFolder(''); }
}

async function renamePage(ref, title) {
  const to = prompt('글 제목을 고쳐 주세요.', title);
  if (!to || to === title) { return; }
  if (await post('/move', {ref: ref, title: to})) { location.reload(); }
}

async function removePage(ref) {
  if (!await askDrop({ref: ref})) { return; }
  if (await post('/delete', {ref: ref})) { location.reload(); }
}

// 내 PC 파일 링크는 브라우저가 직접 열지 못하므로 위키 서버에 부탁합니다.
document.addEventListener('click', async (e) => {
  const link = e.target.closest('a.local');
  if (!link) { return; }
  e.preventDefault();
  const done = await post('/open', {path: link.dataset.path, line: link.dataset.line || 0});
  if (done && window.status !== undefined) { console.log(done.message); }
});
"""

LAYOUT_SCRIPT = """
const head = document.querySelector('header');
const fitHead = () => document.documentElement.style.setProperty('--head', head.offsetHeight + 'px');
fitHead();
addEventListener('resize', fitHead);
document.getElementById('toggle').onclick = () => {
  const off = document.documentElement.classList.toggle('nosidebar');
  localStorage.setItem('sidebar', off ? 'off' : 'on');
};

// 목록과 본문 사이를 끌어 목록 너비를 조절합니다. 정한 너비는 다음에도 그대로 씁니다.
const grip = document.getElementById('grip');
grip.addEventListener('pointerdown', (e) => {
  e.preventDefault();
  grip.setPointerCapture(e.pointerId);
  grip.classList.add('on');
  const drag = (move) => {
    const wide = Math.min(560, Math.max(160, Math.round(move.clientX)));
    document.documentElement.style.setProperty('--side', wide + 'px');
  };
  const stop = () => {
    grip.classList.remove('on');
    grip.removeEventListener('pointermove', drag);
    localStorage.setItem('sideWidth',
      document.documentElement.style.getPropertyValue('--side') || '260px');
  };
  grip.addEventListener('pointermove', drag);
  grip.addEventListener('pointerup', stop, {once: true});
  grip.addEventListener('pointercancel', stop, {once: true});
});

const aside = document.querySelector('aside');

document.getElementById('side-create').onclick = () => createFolder('');
document.getElementById('side-sort').onchange = (e) => {
  document.cookie = 'sideSort=' + e.target.value + ';path=/;max-age=31536000;samesite=lax';
  location.reload();
};
aside.onclick = (e) => {
  const foldLink = e.target.closest('a[data-fold]');   // 폴더 이름을 눌러도 접고 폅니다
  if (foldLink) {
    e.preventDefault();
    toggleFolder(foldLink.closest('.item'));
    return;
  }
  const button = e.target.closest('button[data-act]');
  if (!button) { return; }
  const item = button.closest('.item');
  const act = button.dataset.act;
  if (act === 'fold') { toggleFolder(item); }
  else if (act === 'add') { createFolder(item.dataset.folder); }
  else if (act === 'rename') { renameFolder(item.dataset.folder); }
  else if (act === 'remove') { removeFolder(item.dataset.folder); }
  else if (act === 'write') { location.href = '/new?folder=' + encodeURIComponent(item.dataset.folder); }
  else if (act === 'under') { location.href = '/new?folder=' + encodeURIComponent(item.dataset.ref); }
  else if (act === 'edit') { renamePage(item.dataset.ref, item.dataset.name); }
  else if (act === 'erase') { removePage(item.dataset.ref); }
};

// 접어 둔 폴더는 서버가 아예 그리지 않습니다. 글이 많아져도 목록이 가볍게 유지됩니다.
function closedFolders() {
  const found = document.cookie.split(';').find((c) => c.trim().startsWith('closed='));
  const raw = found ? found.trim().slice('closed='.length) : '';
  return new Set(raw.split('|').filter(Boolean).map(decodeURIComponent));
}

function saveClosed(folders) {
  const value = [...folders].map(encodeURIComponent).join('|');
  document.cookie = 'closed=' + value + ';path=/;max-age=31536000;samesite=lax';
}

async function toggleFolder(item) {
  const folder = item.dataset.ref || item.dataset.folder;   // 글 아래 글도 접고 폅니다
  const folders = closedFolders();
  const twist = item.querySelector('.twist');
  if (folders.has(folder)) {
    folders.delete(folder);
    saveClosed(folders);
    const res = await fetch('/tree?folder=' + encodeURIComponent(folder));
    item.insertAdjacentHTML('afterend', await res.text());
    twist.textContent = '▾';
  } else {
    folders.add(folder);
    saveClosed(folders);
    for (const el of [...aside.querySelectorAll('.item')]) {
      const parent = el.dataset.parent;
      if (parent === folder || parent.startsWith(folder + '/')) { el.remove(); }
    }
    twist.textContent = '▸';
  }
}

// 끌어다 놓기: 폴더 가운데에 놓으면 그 안으로, 줄의 위아래 가장자리에 놓으면 그 자리로
// 순서가 바뀝니다. 목록 빈 곳에 놓으면 맨 바깥 맨 뒤로 나옵니다.
let dragged = null;
let dropMark = '';

function itemKey(item) {
  return item.dataset.name + (item.dataset.ref ? '' : '/');
}

function dropSpot(e) {
  const item = e.target.closest('.item');
  if (!item || item === dragged) { return {mode: 'root'}; }
  const box = item.getBoundingClientRect();
  const offset = (e.clientY - box.top) / box.height;
  // 줄 한가운데에 놓으면 그 폴더 안으로, 글이면 그 글 아래로 들어갑니다.
  if (offset > 0.3 && offset < 0.7) { return {mode: 'inside', item: item}; }
  return {mode: offset < 0.5 ? 'before' : 'after', item: item};
}

function markDrop(spot) {
  const key = spot.mode + (spot.item ? spot.item.dataset.name : '');
  if (key === dropMark) { return; }
  dropMark = key;
  clearMarks();
  if (spot.mode === 'root') { aside.classList.add('drop'); }
  else { spot.item.classList.add(spot.mode === 'inside' ? 'drop' : 'drop-' + spot.mode); }
}

function clearMarks() {
  aside.querySelectorAll('.drop, .drop-before, .drop-after')
       .forEach((el) => el.classList.remove('drop', 'drop-before', 'drop-after'));
  aside.classList.remove('drop');
}

function clearDrop() {
  dropMark = '';
  clearMarks();
}

async function reorder(parent, target, mode, moving) {
  const siblings = [...aside.querySelectorAll('.item')]
    .filter((el) => el.dataset.parent === parent && el !== moving)
    .map(itemKey);
  const at = target ? siblings.indexOf(itemKey(target)) + (mode === 'after' ? 1 : 0)
                    : siblings.length;
  siblings.splice(at < 0 ? siblings.length : at, 0, itemKey(moving));
  return post('/order', {folder: parent, names: siblings});
}

async function dropInto(item, folder) {
  if (item.dataset.ref) {
    if (item.dataset.folder === folder) { return true; }
    return Boolean(await post('/move', {ref: item.dataset.ref, folder: folder}));
  }
  const source = item.dataset.folder;
  const to = folder ? folder + '/' + item.dataset.name : item.dataset.name;
  if (to === source) { return true; }
  if (folder === source || folder.startsWith(source + '/')) { return false; }
  return Boolean(await post('/folder/rename', {folder: source, to: to}));
}

aside.addEventListener('dragstart', (e) => {
  dragged = e.target.closest('.item');
  if (!dragged) { return; }
  dragged.classList.add('dragging');
  e.dataTransfer.effectAllowed = 'move';
  e.dataTransfer.setData('text/plain', itemKey(dragged));
});

aside.addEventListener('dragend', () => {
  if (dragged) { dragged.classList.remove('dragging'); }
  dragged = null;
  clearDrop();
});

aside.addEventListener('dragover', (e) => {
  if (!dragged) { return; }
  e.preventDefault();
  e.dataTransfer.dropEffect = 'move';
  markDrop(dropSpot(e));
});

aside.addEventListener('dragleave', (e) => {
  if (!aside.contains(e.relatedTarget)) { clearDrop(); }
});

aside.addEventListener('drop', async (e) => {
  e.preventDefault();
  const item = dragged;          // dragend 가 먼저 와도 잃지 않도록 붙잡아 둡니다
  const spot = dropSpot(e);
  clearDrop();
  if (!item) { return; }

  const parent = spot.mode === 'inside' ? (spot.item.dataset.ref || spot.item.dataset.folder)
               : spot.mode === 'root' ? '' : spot.item.dataset.parent;
  const openHere = item.dataset.ref && item.querySelector('a.on');
  const name = item.dataset.name;
  try {
    if (!await dropInto(item, parent)) { return; }
    // 폴더 안으로 넣을 때는 순서를 건드리지 않습니다. 접혀 있으면 형제를 알 수 없기 때문입니다.
    if (spot.mode !== 'inside') { await reorder(parent, spot.item, spot.mode, item); }
  } catch (error) {
    alert('옮기지 못했습니다: ' + error);
    return;
  }
  if (openHere) {
    location.href = '/w/' + encodeURIComponent(parent ? parent + '/' + name : name);
  } else {
    location.reload();
  }
});
"""


def shell(
    title: str, body: str, script: str = "", query: str = "",
    current_ref: str = "", current_folder: str = "", closed: set[str] = frozenset(),
    side_sort: str = "order", body_class: str = "",
) -> bytes:
    page = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}{CODE_CSS}</style>
<script>
if (localStorage.getItem('sidebar') === 'off') {{
  document.documentElement.classList.add('nosidebar');
}}
const savedSide = localStorage.getItem('sideWidth');
if (savedSide) {{ document.documentElement.style.setProperty('--side', savedSide); }}
</script>
</head><body class="{body_class}">
<header>
  <button class="btn" id="toggle" title="문서 목록 접기/펴기">☰</button>
  <a class="brand" href="{home_link()}" title="{home_title()}">📚 {html.escape(wiki_name())}</a>
  <span class="spacer"></span>
  <form action="/search" method="get" style="display:flex;gap:6px">
    <input type="search" name="q" placeholder="제목·본문 검색"
           value="{html.escape(query, quote=True)}">
    <button class="btn" type="submit">검색</button>
  </form>
  <a class="btn" href="/trash" title="휴지통 — 지운 글 되살리기">🗑</a>
  <a class="btn" href="/settings" title="위키 이름 바꾸기">⚙</a>
</header>
<div class="layout">
  <aside>{sidebar(current_ref, current_folder, closed, side_sort)}</aside>
  <div class="grip" id="grip" title="끌어서 너비 조절"></div>
  <main>{body}</main>
</div>
<div class="modal" id="drop-modal" hidden><div class="sheet">
  <h2>지울까요?</h2>
  <p id="drop-what"></p>
  <div id="drop-list"></div>
  <p class="hint">지운 것은 <b>휴지통</b>으로 갑니다. 오른쪽 위 🗑 에서 되살릴 수 있습니다.</p>
  <div class="toolbar">
    <span class="spacer"></span>
    <button class="btn" id="drop-no">취소</button>
    <button class="btn primary" id="drop-yes">휴지통으로 보내기</button>
  </div>
</div></div>
<script>{COMMON_SCRIPT}{LAYOUT_SCRIPT}{script}</script>
</body></html>"""
    return page.encode("utf-8")


def home_link() -> str:
    home = home_page()
    return f"/w/{urllib.parse.quote(home)}" if home else "/"


def home_title() -> str:
    home = home_page()
    return f"{home} (홈)" if home else "문서 목록"


def crumbs(folder: str) -> str:
    """이 글이 어느 폴더에 있는지 알려 줍니다. 폴더는 왼쪽 목록에서 찾습니다."""
    if not folder:
        return ""
    return f'<div class="crumbs meta">📁 {html.escape(folder)}</div>'


def start_body() -> str:
    """홈으로 정한 글이 없을 때 보여 주는 첫 화면."""
    if not list_pages():
        return ('<h1>새 위키</h1><p class="empty">아직 글이 없습니다. '
                '왼쪽 목록의 <b>＋ 글</b> 로 시작해 보세요.</p>')
    return ("<h1>어디로 갈까요</h1>"
            '<p class="empty">왼쪽 목록에서 글을 고르세요.<br>'
            '자주 여는 글은 <a href="/settings">설정</a> 의 '
            "<b>홈으로 쓸 글</b> 로 정해 두면 여기서 바로 열립니다.</p>")


def entry_rows(
    entries: list[tuple[str, datetime, list[str]]], query: str = "", show_folder: bool = False
) -> str:
    rows = []
    for ref, when, snippets in entries:
        folder = folder_of(ref)
        where = (
            f'<div class="meta">📁 {html.escape(folder)}</div>'
            if show_folder and folder else ""
        )
        snippet_html = "".join(
            f'<div class="snippet">{highlight(line, query)}</div>' for line in snippets
        )
        rows.append(
            f'<li data-ref="{html.escape(ref, quote=True)}" '
            f'data-name="{html.escape(title_of(ref), quote=True)}">{where}<div class="row">'
            f'<a href="/w/{urllib.parse.quote(ref)}">{highlight(title_of(ref), query)}</a>'
            '<span class="acts">'
            f'<a class="btn" href="/e/{urllib.parse.quote(ref)}">고치기</a>'
            '<button class="btn" data-row="rename">제목</button>'
            '<button class="btn danger" data-row="remove">삭제</button></span>'
            f'<span class="meta">{when:%Y-%m-%d %H:%M}</span></div>{snippet_html}</li>'
        )
    return f'<ul class="list">{"".join(rows)}</ul>'


SORTS = [
    ("order", "내가 정한 순서"),
    ("recent", "최근에 고친 순"),
    ("old", "오래전에 고친 순"),
    ("name", "이름 순"),
]


def page_links(params: dict, page: int, total: int, size: int) -> str:
    """결과가 많을 때 아래쪽에 붙는 이전·다음 단추."""
    last = max(1, (total + size - 1) // size)
    if last <= 1:
        return ""

    def link(to: int, label: str) -> str:
        if to < 1 or to > last:
            return f'<span class="btn" style="opacity:.4">{label}</span>'
        query = urllib.parse.urlencode({**params, "page": to}, doseq=True)
        return f'<a class="btn" href="?{query}">{label}</a>'

    first = (page - 1) * size + 1
    return (
        f'<div class="toolbar">{link(page - 1, "← 이전")}'
        f'<span class="meta">{first}–{min(page * size, total)} / {total}개 '
        f"({page}/{last} 쪽)</span>{link(page + 1, '다음 →')}</div>"
    )


LIST_SCRIPT = """
// 문서 목록에서도 폴더 묶음을 접었다 펼 수 있습니다. 접은 것은 다음에도 그대로입니다.
function foldedGroups() {
  try { return new Set(JSON.parse(localStorage.getItem('foldedGroups') || '[]')); }
  catch (e) { return new Set(); }
}

function paintGroup(group, folded) {
  group.querySelector('ul').hidden = folded;
  group.querySelector('.twist').textContent = folded ? '▸' : '▾';
}

const startFolded = foldedGroups();
for (const group of document.querySelectorAll('.group[data-folder]')) {
  if (startFolded.has(group.dataset.folder)) { paintGroup(group, true); }
}

document.querySelector('main').addEventListener('click', (e) => {
  const twist = e.target.closest('button[data-fold]');
  if (twist) {
    const group = twist.closest('.group');
    const folded = foldedGroups();
    const shut = !folded.has(group.dataset.folder);
    if (shut) { folded.add(group.dataset.folder); } else { folded.delete(group.dataset.folder); }
    localStorage.setItem('foldedGroups', JSON.stringify([...folded]));
    paintGroup(group, shut);
    return;
  }
  const button = e.target.closest('button[data-row]');
  if (!button) { return; }
  const row = button.closest('li');
  if (button.dataset.row === 'rename') { renamePage(row.dataset.ref, row.dataset.name); }
  else { removePage(row.dataset.ref); }
});
"""

def search_body(query: str, page: int = 1) -> str:
    if not query:
        return '<h1>검색</h1><p class="empty">위쪽 검색창에 찾을 말을 넣어 주세요.</p>'
    found = search_pages(query)
    head = f'<h1>‘{html.escape(query)}’ 검색 결과 <span class="meta">({len(found)}개)</span></h1>'
    if not found:
        return (
            f'{head}<p class="empty">찾은 문서가 없습니다. '
            f'<a href="/new">이 내용으로 새 글을 써 보세요.</a></p>'
        )
    page = max(1, min(page, (len(found) + SEARCH_LIMIT - 1) // SEARCH_LIMIT))
    shown = found[(page - 1) * SEARCH_LIMIT:page * SEARCH_LIMIT]
    return (
        head + entry_rows(shown, query, show_folder=True)
        + page_links({"q": query}, page, len(found), SEARCH_LIMIT)
    )


def view_body(ref: str) -> str:
    quoted = urllib.parse.quote(ref)
    folder = folder_of(ref)
    if not page_exists(ref):
        return (
            f"{crumbs(folder)}<h1>{html.escape(title_of(ref))}</h1>"
            '<p class="empty">아직 없는 문서입니다.</p>'
            f'<p><a class="btn primary" href="/e/{quoted}">이 문서 만들기</a></p>'
        )
    when = datetime.fromtimestamp(page_path(ref).stat().st_mtime)
    return (
        f"{crumbs(folder)}"
        f'<div class="toolbar" id="page-tools" data-ref="{html.escape(ref, quote=True)}" '
        f'data-folder="{html.escape(folder, quote=True)}">'
        f'<span class="meta">마지막 수정 {when:%Y-%m-%d %H:%M}</span>'
        f'<span class="spacer"></span>'
        f'<a class="btn primary" href="/e/{quoted}">글 수정</a>'
        f'<a class="btn" href="/new?folder={quoted}">아래에 새 글</a>'
        f'<button class="btn danger" id="remove">삭제</button></div>'
        f"<h1>{html.escape(title_of(ref))}</h1>{render(read_page(ref))}"
        f"{children_list(ref)}"
        f"{comments_body(ref)}"
        f'<button class="btn primary" id="note-bubble" '
        f'data-ref="{html.escape(ref, quote=True)}" hidden>📝 메모 붙이기</button>'
        '<div id="note-card" hidden></div>'
    )


def comments_body(ref: str) -> str:
    """글 아래에 붙는 댓글 목록과 쓰는 칸."""
    rows = []
    for comment in page_comments(ref):
        text = comment.get("text", "")
        try:
            when = datetime.fromisoformat(comment.get("at", "")).strftime("%Y-%m-%d %H:%M")
        except ValueError:
            when = ""
        edited = ' <span class="meta">(고침)</span>' if comment.get("edited") else ""
        rows.append(
            f'<li data-at="{html.escape(comment.get("at", ""), quote=True)}" '
            f'data-text="{html.escape(text, quote=True)}">'
            f'<div class="comment-head"><span class="meta">{when}</span>{edited}'
            '<span class="spacer"></span>'
            '<button class="btn" data-act="edit" title="댓글 고치기">✎</button>'
            '<button class="btn danger" data-act="remove" title="댓글 지우기">✕</button></div>'
            f'<div class="comment-text">{render(text)}</div></li>'
        )
    count = f' <span class="meta">{len(rows)}</span>' if rows else ""
    listing = f'<ul class="comments">{"".join(rows)}</ul>' if rows else ""
    return (
        f'<div id="comments" data-ref="{html.escape(ref, quote=True)}">'
        f"<h2>댓글{count}</h2>{listing}"
        '<textarea id="comment-text" rows="3" spellcheck="false" '
        'placeholder="댓글을 씁니다. 마크다운을 쓸 수 있고 Ctrl+Enter 로 등록됩니다."></textarea>'
        '<div class="toolbar"><span class="hint" id="comment-hint"></span>'
        '<span class="spacer"></span>'
        '<button class="btn primary" id="comment-add">댓글 쓰기</button></div>'
        "</div>"
    )


def children_list(ref: str) -> str:
    """이 글 아래에 달린 글 목록."""
    kids = children_of(ref)
    if not kids:
        return ""
    changed = dict(list_pages())
    rows = []
    for name, is_folder in kids:
        path = f"{ref}/{name}"
        if is_folder:
            rows.append(
                f'<li><div class="row"><span class="meta">📁 {html.escape(name)}</span>'
                "</div></li>"
            )
        else:
            when = changed.get(path)
            stamp = f'<span class="meta">{when:%Y-%m-%d %H:%M}</span>' if when else ""
            rows.append(
                f'<li><div class="row">'
                f'<a href="/w/{urllib.parse.quote(path)}">{html.escape(name)}</a>{stamp}'
                "</div></li>"
            )
    return f'<div class="group"><h2>이 글 아래</h2><ul class="list">{"".join(rows)}</ul></div>'


VIEW_SCRIPT = """
// 본문에서 글자를 고르면 그 자리에 단추가 떠서, 고른 부분에 메모를 붙일 수 있습니다.
const noteBubble = document.getElementById('note-bubble');
const readArea = document.querySelector('main');
let noteQuote = '';

document.addEventListener('selectionchange', () => {
  const chosen = document.getSelection();
  const text = String(chosen).trim();
  if (!text || !chosen.rangeCount || !readArea.contains(chosen.anchorNode)) {
    noteBubble.hidden = true;
    return;
  }
  noteQuote = text;
  const box = chosen.getRangeAt(0).getBoundingClientRect();
  noteBubble.style.top = (box.bottom + scrollY + 6) + 'px';
  noteBubble.style.left = (box.left + scrollX) + 'px';
  noteBubble.hidden = false;
});

noteBubble.onclick = async () => {
  const note = prompt('‘' + noteQuote.slice(0, 40) + '’ 에 붙일 메모', '');
  if (!note) { return; }
  noteBubble.hidden = true;
  if (await post('/note', {ref: noteBubble.dataset.ref, quote: noteQuote, note: note})) {
    location.reload();
  }
};

// 메모 표시를 누르면 옆에 메모가 뜹니다. 표시 자체는 껐다 켤 수 있습니다.
const noteCard = document.getElementById('note-card');
let openNote = null;

function closeNote() {
  noteCard.hidden = true;
  if (openNote) { openNote.classList.remove('open'); }
  openNote = null;
}

readArea.addEventListener('click', (e) => {
  const mark = e.target.closest('.note');
  if (!mark) {
    if (!e.target.closest('#note-card')) { closeNote(); }
    return;
  }
  if (mark === openNote) { closeNote(); return; }
  closeNote();
  openNote = mark;
  mark.classList.add('open');
  noteCard.replaceChildren();

  // 메모 칸을 눌러 바로 고칩니다. 다른 곳을 누르면(또는 Ctrl+Enter) 저장됩니다.
  const hint = document.createElement('span');
  hint.className = 'note-hint';
  hint.textContent = '눌러서 고치기';

  const noteBox = document.createElement('div');
  noteBox.className = 'note-text';
  noteBox.contentEditable = 'true';
  noteBox.textContent = mark.dataset.note;
  noteBox.onkeydown = (key) => {
    if (key.key === 'Escape') { noteBox.textContent = mark.dataset.note; noteBox.blur(); }
    else if (key.key === 'Enter' && (key.ctrlKey || key.metaKey)) {
      key.preventDefault();
      noteBox.blur();
    }
  };
  noteBox.onblur = async () => {
    const next = noteBox.textContent.trim();
    if (!next || next === mark.dataset.note) { noteBox.textContent = mark.dataset.note; return; }
    const done = await post('/note/edit',
      {ref: noteBubble.dataset.ref, quote: mark.textContent, note: next});
    if (done) { mark.dataset.note = next; hint.textContent = '고쳤습니다'; }
    else { noteBox.textContent = mark.dataset.note; }
  };
  noteCard.appendChild(noteBox);
  const drop = document.createElement('button');
  drop.className = 'drop-note';
  drop.textContent = '메모 지우기';
  drop.onclick = async () => {
    if (await post('/note/remove', {ref: noteBubble.dataset.ref, quote: mark.textContent})) {
      location.reload();
    }
  };
  const foot = document.createElement('div');
  foot.className = 'note-foot';
  foot.append(hint, drop);
  noteCard.appendChild(foot);
  const box = mark.getBoundingClientRect();
  const room = innerWidth - box.right - 20;
  noteCard.hidden = false;
  noteCard.style.top = (box.top + scrollY) + 'px';
  noteCard.style.left = room > 280
    ? (box.right + scrollX + 12) + 'px'
    : (Math.max(8, box.left + scrollX - 40)) + 'px';
});

const pageTools = document.getElementById('page-tools');
if (pageTools) {
  document.getElementById('remove').onclick = async () => {
    const ref = pageTools.dataset.ref;
    if (!await askDrop({ref: ref})) { return; }
    const removed = await post('/delete', {ref: ref});
    if (removed) { goFolder(removed.folder); }
  };
}

// 글 아래 댓글: 쓰기, 고치기, 지우기.
const comments = document.getElementById('comments');
if (comments) {
  const box = document.getElementById('comment-text');
  const hint = document.getElementById('comment-hint');

  async function addComment() {
    const text = box.value.trim();
    if (!text) { box.focus(); return; }
    hint.textContent = '올리는 중...';
    if (await post('/comment', {ref: comments.dataset.ref, text: text})) { location.reload(); }
    else { hint.textContent = ''; }
  }

  document.getElementById('comment-add').onclick = addComment;
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter' && (e.ctrlKey || e.metaKey)) { e.preventDefault(); addComment(); }
  });

  comments.addEventListener('click', async (e) => {
    const button = e.target.closest('button[data-act]');
    if (!button) { return; }
    const row = button.closest('li');
    const at = row.dataset.at;
    if (button.dataset.act === 'remove') {
      if (!confirm('이 댓글을 지울까요?')) { return; }
      if (await post('/comment/remove', {ref: comments.dataset.ref, at: at})) { location.reload(); }
      return;
    }
    if (row.querySelector('textarea')) { return; }   // 이미 고치는 중입니다
    const shown = row.querySelector('.comment-text');
    const edit = document.createElement('textarea');
    edit.rows = 3;
    edit.value = row.dataset.text;
    const keep = document.createElement('button');
    keep.className = 'btn primary';
    keep.textContent = '저장';
    const drop = document.createElement('button');
    drop.className = 'btn';
    drop.textContent = '취소';
    const foot = document.createElement('div');
    foot.className = 'toolbar';
    foot.append(keep, drop);
    shown.hidden = true;
    row.append(edit, foot);
    edit.focus();
    drop.onclick = () => { edit.remove(); foot.remove(); shown.hidden = false; };
    keep.onclick = async () => {
      const text = edit.value.trim();
      if (!text) { edit.focus(); return; }
      if (await post('/comment/edit', {ref: comments.dataset.ref, at: at, text: text})) {
        location.reload();
      }
    };
    edit.addEventListener('keydown', (key) => {
      if (key.key === 'Escape') { drop.onclick(); }
      else if (key.key === 'Enter' && (key.ctrlKey || key.metaKey)) {
        key.preventDefault();
        keep.onclick();
      }
    });
  });
}
"""


MD_BUTTONS = [
    ("제목", 'data-prefix="## " data-rich="formatBlock:h2"'),
    ("굵게", 'data-wrap="**" data-hint="굵은 글씨" data-rich="bold"'),
    ("기울임", 'data-wrap="*" data-hint="기울인 글씨" data-rich="italic"'),
    ("코드", 'data-wrap="`" data-hint="코드"'),
    ("목록", 'data-prefix="- " data-rich="insertUnorderedList"'),
    ("숫자 목록", 'data-prefix="1. " data-rich="insertOrderedList"'),
    ("인용", 'data-prefix="&gt; " data-rich="formatBlock:blockquote"'),
    ("링크", 'data-snippet="[보일 글자](https://)"'),
    ("내 PC 파일", 'data-local="1" data-rich="local"'),
    ("글 연결", 'data-connect="1" data-rich="connect"'),
    ("표", 'data-table="1" data-rich="table"'),
    ("그림", 'data-draw="1" data-rich="draw"'),
]

CODE_LANGS = [
    ("", "글자 그대로"), ("csharp", "C#"), ("cpp", "C++"), ("c", "C"), ("python", "Python"),
    ("javascript", "JavaScript"), ("typescript", "TypeScript"), ("java", "Java"), ("go", "Go"),
    ("rust", "Rust"), ("lua", "Lua"), ("sql", "SQL"), ("json", "JSON"), ("xml", "XML"),
    ("html", "HTML"), ("css", "CSS"), ("bash", "셸"), ("powershell", "PowerShell"),
    ("yaml", "YAML"), ("ini", "INI"), ("diff", "Diff"), ("markdown", "마크다운"),
]

DRAW_TOOLS = [
    ("select", "선택"), ("pen", "자유선"), ("line", "직선"), ("rect", "사각형"),
    ("ellipse", "타원"), ("arrow", "화살표"), ("erase", "지우개"),
]
DRAW_COLORS = ["#1f2328", "#cf222e", "#0969da", "#1a7f37", "#bf8700"]

EDITOR_SCRIPT = """
const editor = document.getElementById('editor');
const titleInput = document.getElementById('title');
const folderInput = document.getElementById('folder');
const status = document.getElementById('status');
const mdbar = document.getElementById('mdbar');
const rich = document.getElementById('rich');
let richMode = true;   // 꾸며진 화면에서 바로 고치는 쪽을 기본으로 씁니다
let original = editor.dataset.original;

function typeText(text) {
  editor.focus();
  if (!document.execCommand('insertText', false, text)) {
    const start = editor.selectionStart, end = editor.selectionEnd;
    editor.setRangeText(text, start, end, 'end');
  }
}

function wrapSelection(mark, hint) {
  const selected = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  typeText(mark + (selected || hint) + mark);
}

// 표·코드블록은 줄 맨 앞에서 시작해야 마크다운이 덩어리로 알아봅니다.
// 커서가 글 가운데 있으면 줄을 먼저 바꾸고, 뒤에 남은 글도 다음 줄로 내려 보냅니다.
function typeBlock(text) {
  const here = lineHere();
  const ahead = editor.value.slice(editor.selectionStart, here.end);
  const above = editor.value.slice(0, here.start).replace(/\\n$/, '');
  let head = '';
  if (here.text.trim()) { head = '\\n\\n'; }
  else if (above.slice(above.lastIndexOf('\\n') + 1).trim()) { head = '\\n'; }
  typeText(head + text + (ahead.trim() ? '\\n' : ''));
}

// 창(글 연결·표·그림)을 열거나 제목·폴더 칸으로 옮겨 가면 서식 편집 커서 자리를 잃어버립니다.
// 그대로 focus() 를 주면 커서가 글 맨 앞으로 가면서 편집창도 맨 위로 올라가 버리므로,
// 고치던 자리를 늘 기억해 두고 돌아올 때 되살립니다.
let richSpot = null;

function keepRichSpot() {
  const chosen = document.getSelection();
  // 자리를 알 수 없을 때는 지우지 않고 마지막으로 알던 자리를 그대로 둡니다.
  if (richMode && chosen.rangeCount && rich.contains(chosen.anchorNode)) {
    richSpot = chosen.getRangeAt(0).cloneRange();
  }
}

document.addEventListener('selectionchange', keepRichSpot);

/** 커서가 편집창 밖으로 밀려 있으면 보이는 자리까지만 살짝 굴립니다. */
function showCaret(pane) {
  const chosen = document.getSelection();
  if (!chosen.rangeCount) { return; }
  let spot = chosen.getRangeAt(0).getBoundingClientRect();
  if (!spot.height && !spot.top) {       // 빈 줄에서는 크기가 0 으로 나옵니다
    const node = chosen.anchorNode;
    const near = node && (node.nodeType === 1 ? node : node.parentElement);
    if (!near) { return; }
    spot = near.getBoundingClientRect();
  }
  const box = pane.getBoundingClientRect();
  if (spot.top < box.top + 8) { pane.scrollTop -= box.top + 8 - spot.top; }
  else if (spot.bottom > box.bottom - 8) { pane.scrollTop += spot.bottom - box.bottom + 8; }
}

function focusRichSpot() {
  rich.focus();
  if (richSpot) {
    const chosen = document.getSelection();
    chosen.removeAllRanges();
    chosen.addRange(richSpot);
  }
  showCaret(rich);
}

/** 창이나 다른 칸에서 아까 고치던 자리로 그대로 돌아옵니다. */
function backToEditSpot() {
  if (richMode) { focusRichSpot(); return; }
  editor.focus();   // 마크다운 칸은 커서와 스크롤을 브라우저가 지켜 줍니다
}

function prefixLine(prefix) {
  const here = lineHere();
  editor.setSelectionRange(here.start, here.start);
  typeText(prefix);
  // 커서를 줄 끝으로 보냅니다. 표시 뒤에 두면 이어 쓴 글자와 Enter 가 엉뚱한 자리로 갑니다.
  const at = here.end + prefix.length;
  editor.setSelectionRange(at, at);
}

// 글 연결: 목록에서 글을 골라 링크를 겁니다. ‘＋ 새 글 만들기’ 를 고르면 새 글을 만들어 연결합니다.
const linkModal = document.getElementById('link-modal');
const linkPick = document.getElementById('link-pick');
const linkFolder = document.getElementById('link-folder');
const linkTitle = document.getElementById('link-title');
const linkBody = document.getElementById('link-text');
const linkNew = document.getElementById('link-new');
const linkMake = document.getElementById('link-make');
let linkSpan = null;    // 마크다운 모드에서 고른 자리
let linkLabel = '';     // 고른 글자 (링크에 보일 글자)

// 새 글 만들기를 골랐을 때만 제목·폴더·내용 칸을 보여 줍니다.
function showLinkMode() {
  linkNew.hidden = linkPick.value !== '+';
}

function openLink() {
  if (richMode) {
    linkLabel = String(document.getSelection()).trim();
  } else {
    linkSpan = {start: editor.selectionStart, end: editor.selectionEnd};
    linkLabel = editor.value.slice(linkSpan.start, linkSpan.end).trim();
  }
  document.getElementById('link-label').textContent = linkLabel
    ? '‘' + linkLabel + '’ 에 링크를 겁니다.'
    : '고른 글자가 없어 글 제목이 그대로 보입니다.';
  // 고른 글자가 글 제목과 같으면 그 글을, 아니면 목록 첫 글을 미리 골라 둡니다.
  const pages = [...linkPick.options].filter((option) => option.value !== '+');
  const same = pages.find((option) => option.value.toLowerCase() === linkLabel.toLowerCase());
  linkPick.value = same ? same.value : (pages.length ? pages[0].value : '+');
  linkFolder.value = folderInput.value.trim();
  linkTitle.value = linkLabel.slice(0, 100);
  linkBody.value = '';
  showLinkMode();
  linkModal.hidden = false;
  linkPick.focus();
}

function closeLinkModal() {
  linkModal.hidden = true;
  backToEditSpot();
}

function putLink(ref) {
  const label = (linkLabel && linkLabel !== ref) ? linkLabel : '';
  const inner = label ? ref + '|' + label : ref;
  if (richMode) {
    focusRichSpot();
    const safe = (label || ref).replace(/&/g, '&amp;').replace(/</g, '&lt;');
    document.execCommand('insertHTML', false,
      '<a data-wiki="' + inner.replace(/"/g, '&quot;') + '" '
      + 'href="/w/' + encodeURIComponent(ref) + '">' + safe + '</a>');
  } else {
    editor.focus();
    editor.setSelectionRange(linkSpan.start, linkSpan.end);
    typeText('[[' + inner + ']]');
  }
}

linkPick.addEventListener('change', () => {
  showLinkMode();
  if (linkPick.value === '+') { linkTitle.focus(); linkTitle.select(); }
});

linkMake.onclick = async () => {
  if (!linkPick.value) {
    document.getElementById('link-label').textContent = '연결할 글을 목록에서 골라 주세요.';
    linkPick.focus();
    return;
  }
  if (linkPick.value !== '+') {          // 이미 있는 글에 연결
    const ref = linkPick.value;
    closeLinkModal();
    putLink(ref);
    status.textContent = '‘' + ref + '’ 로 링크를 걸었습니다.';
    return;
  }
  const title = linkTitle.value.trim();  // 새 글을 만들어 연결
  if (!title) { linkTitle.focus(); return; }
  const saved = await post('/save', {
    original: '', folder: linkFolder.value.trim(), title: title, text: linkBody.value,
  });
  if (!saved) { return; }
  const fresh = document.createElement('option');   // 만든 글을 목록에도 넣어 둡니다
  fresh.value = saved.ref;
  fresh.textContent = saved.ref;
  linkPick.appendChild(fresh);
  closeLinkModal();
  putLink(saved.ref);
  status.textContent = '‘' + saved.ref + '’ 를 만들고 링크를 걸었습니다. 이 글도 저장해 주세요.';
};

document.getElementById('link-cancel').onclick = closeLinkModal;
linkModal.onclick = (e) => { if (e.target === linkModal) { closeLinkModal(); } };
for (const box of [linkPick, linkTitle, linkFolder]) {
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); linkMake.click(); }
  });
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !linkModal.hidden) { closeLinkModal(); }
});

// 내 PC 파일이나 코드 위치로 가는 링크를 넣습니다.
function insertLocalLink() {
  const chosen = richMode ? String(document.getSelection()).trim()
    : editor.value.slice(editor.selectionStart, editor.selectionEnd).trim();
  const typed = prompt(
    '파일 경로를 붙여 넣어 주세요. 줄 번호까지 가려면 뒤에 :줄번호 를 붙입니다.\\n'
    + '예) E:\\\\P4V\\\\GServer\\\\GCore\\\\Foo.cs:123', chosen);
  if (!typed) { return; }
  const path = typed.trim();
  const label = (chosen && chosen !== path) ? chosen : path.split(/[\\\\/]/).pop();
  const url = 'file:///' + encodeURI(path.replace(/\\\\/g, '/'));
  if (richMode) {
    rich.focus();
    document.execCommand('insertHTML', false,
      '<a href="' + url + '">' + label.replace(/&/g, '&amp;').replace(/</g, '&lt;') + '</a>');
  } else {
    typeText('[' + label + '](' + url + ')');
  }
}

// 고른 영역을 코드 블록으로 감쌉니다. 종류를 고르면 그 문법에 맞춰 색이 입혀집니다.
function insertCodeBlock(toRich) {
  const lang = document.getElementById('code-lang').value;
  if (toRich) {
    const chosen = String(document.getSelection()).trim();
    const body = (chosen || '여기에 코드를 씁니다')
      .replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/\\n/g, '<br>');
    rich.focus();
    document.execCommand('insertHTML', false,
      '<pre><code' + (lang ? ' class="language-' + lang + '"' : '') + '>' + body
      + '</code></pre><p><br></p>');
    return;
  }
  const chosen = editor.value.slice(editor.selectionStart, editor.selectionEnd);
  const body = chosen.replace(/\\n+$/, '') || '여기에 코드를 씁니다';
  if (chosen) { typeText('```' + lang + '\\n' + body + '\\n```\\n'); }
  else { typeBlock('```' + lang + '\\n' + body + '\\n```\\n'); }
}

async function save(leave) {
  const name = titleInput.value.trim();
  if (!name) { status.textContent = '제목을 입력해 주세요.'; titleInput.focus(); return; }
  status.textContent = '저장 중...';
  if (richMode && !await pullFromRich()) { return; }
  const res = await fetch('/save', {
    method: 'POST', headers: {'Content-Type': 'application/json; charset=utf-8'},
    body: JSON.stringify({
      original: original, folder: folderInput.value, title: name, text: editor.value,
    }),
  });
  if (!res.ok) { status.textContent = await res.text(); return; }
  const saved = await res.json();
  if (leave) { location.href = '/w/' + encodeURIComponent(saved.ref); return; }
  original = saved.ref;
  history.replaceState(null, '', '/e/' + encodeURIComponent(saved.ref));
  status.textContent = '저장됨 ' + new Date().toLocaleTimeString();
}

async function upload(files) {
  for (const file of files) {
    status.textContent = file.name + ' 올리는 중...';
    const res = await fetch('/upload', {
      method: 'POST',
      headers: {'X-Filename': encodeURIComponent(file.name || 'clipboard.png')},
      body: file,
    });
    if (!res.ok) { status.textContent = '업로드 실패'; return; }
    const saved = await res.json();
    if (richMode) {
      rich.focus();
      const link = '/f/' + encodeURIComponent(saved.name);
      document.execCommand('insertHTML', false, saved.markdown.startsWith('!')
        ? '<img src="' + link + '" alt="' + saved.name + '">'
        : '<a href="' + link + '">' + saved.name + '</a>');
    } else {
      typeText(saved.markdown + '\\n');
    }
    status.textContent = saved.name + ' 첨부됨 (저장을 눌러야 반영됩니다)';
  }
}

document.getElementById('save').onclick = () => save(false);
document.getElementById('done').onclick = () => save(true);
document.getElementById('picker').onchange = (e) => upload(e.target.files);
// 크롬은 줄 모양을 바꾸면(목록·제목·인용) 커서를 줄 맨 앞으로 되돌려 놓습니다.
// 그대로 두면 이어 쓴 글자가 앞에 끼어들고 Enter 는 빈 항목만 만듭니다.
const BLOCK_COMMANDS = ['insertUnorderedList', 'insertOrderedList', 'formatBlock'];
const BLOCKS = 'li, h1, h2, h3, h4, h5, h6, blockquote, p, div';

function caretToLineEnd() {
  const chosen = document.getSelection();
  let node = chosen.anchorNode;
  if (!node) { return; }
  if (node.nodeType === 3) { node = node.parentNode; }
  const line = node.closest(BLOCKS);
  if (!line || !rich.contains(line)) { return; }
  const range = document.createRange();
  range.selectNodeContents(line);
  range.collapse(false);
  chosen.removeAllRanges();
  chosen.addRange(range);
}

mdbar.querySelectorAll('button').forEach((button) => {
  button.onclick = () => {
    const data = button.dataset;
    if (richMode) {
      const [command, value] = data.rich.split(':');
      if (command === 'table') { openTable(); }
      else if (command === 'draw') { openDraw(null); }
      else if (command === 'code') { insertCodeBlock(true); }
      else if (command === 'connect') { openLink(); }
      else if (command === 'local') { insertLocalLink(); }
      else {
        rich.focus();
        const wasCaret = document.getSelection().isCollapsed;
        document.execCommand(command, false, value);
        if (wasCaret && BLOCK_COMMANDS.includes(command)) { caretToLineEnd(); }
      }
      return;
    }
    if (data.wrap) { wrapSelection(data.wrap, data.hint); }
    else if (data.prefix) { prefixLine(data.prefix); }
    else if (data.table) { openTable(); }
    else if (data.draw) { openDraw(null); }
    else if (data.code) { insertCodeBlock(false); }
    else if (data.connect) { openLink(); }
    else if (data.local) { insertLocalLink(); }
    else { typeText(data.snippet); }
  };
});

// 표 만들기: 격자에 바로 입력하고 넣으면 마크다운 표로 들어갑니다.
const tableModal = document.getElementById('table-modal');
const grid = document.getElementById('grid');
let gridRows = 3;
let gridCols = 3;

function readGrid() {
  return [...grid.rows].map((row) => [...row.cells].map((cell) => cell.firstChild.value));
}

function drawGrid() {
  const kept = grid.rows.length ? readGrid() : [];
  grid.innerHTML = '';
  for (let r = 0; r < gridRows; r++) {
    const row = grid.insertRow();
    for (let c = 0; c < gridCols; c++) {
      const input = document.createElement('input');
      input.type = 'text';
      input.value = (kept[r] && kept[r][c]) || '';
      input.placeholder = r === 0 ? '제목' : '';
      row.insertCell().appendChild(input);
    }
  }
}

// 격자에서 화살표로 칸 사이를 오갑니다. 위아래는 바로, 좌우는 글자 끝에 닿으면 넘어갑니다.
function focusCell(row, col, at) {
  const line = grid.rows[row];
  const cell = line && line.cells[col];
  if (!cell) { return false; }
  const input = cell.firstChild;
  input.focus();
  const spot = at === 'end' ? input.value.length : Math.min(at, input.value.length);
  input.setSelectionRange(spot, spot);
  return true;
}

grid.addEventListener('keydown', (e) => {
  const input = e.target;
  if (input.tagName !== 'INPUT' || e.ctrlKey || e.altKey || e.metaKey) { return; }
  const row = input.parentNode.parentNode.rowIndex;
  const col = input.parentNode.cellIndex;
  const at = input.selectionStart;
  const whole = input.selectionStart !== input.selectionEnd;
  let moved = false;
  if (e.key === 'ArrowUp') { moved = focusCell(row - 1, col, at); }
  else if (e.key === 'ArrowDown') { moved = focusCell(row + 1, col, at); }
  else if (e.key === 'ArrowLeft' && !whole && at === 0) { moved = focusCell(row, col - 1, 'end'); }
  else if (e.key === 'ArrowRight' && !whole && at === input.value.length) {
    moved = focusCell(row, col + 1, 0);
  }
  if (moved) { e.preventDefault(); }
});

function openTable() {
  keepRichSpot();
  gridRows = 3;
  gridCols = 3;
  grid.innerHTML = '';
  drawGrid();
  tableModal.hidden = false;
  grid.rows[0].cells[0].firstChild.focus();
}

function closeTable() {
  tableModal.hidden = true;
  backToEditSpot();
}

// 칸 속에는 공백을 남기지 않습니다. 빈 칸은 빈 칸으로 그대로 둡니다.
function tableMarkdown() {
  const data = readGrid().map((row) => row.map((v) => v.trim().replace(/\\|/g, '\\\\|')));
  const lines = ['|' + data[0].join('|') + '|',
                 '|' + data[0].map(() => '---').join('|') + '|'];
  for (const row of data.slice(1)) { lines.push('|' + row.join('|') + '|'); }
  return lines.join('\\n') + '\\n';
}

document.querySelectorAll('[data-grid]').forEach((button) => {
  button.onclick = () => {
    const act = button.dataset.grid;
    if (act === 'row+') { gridRows++; }
    else if (act === 'row-') { gridRows = Math.max(1, gridRows - 1); }
    else if (act === 'col+') { gridCols++; }
    else { gridCols = Math.max(1, gridCols - 1); }
    drawGrid();
  };
});

function tableHtml() {
  const data = readGrid().map((row) => row.map((v) => v.trim()));
  // 빈 칸에는 공백 문자 대신 <br> 을 넣습니다. 공백을 넣으면 칸마다 지워야 할 글자가 남습니다.
  const cell = (v) => v.replace(/&/g, '&amp;').replace(/</g, '&lt;') || '<br>';
  const head = data[0].map((v) => '<th>' + cell(v) + '</th>').join('');
  const body = data.slice(1)
    .map((row) => '<tr>' + row.map((v) => '<td>' + cell(v) + '</td>').join('') + '</tr>')
    .join('');
  return '<table><thead><tr>' + head + '</tr></thead><tbody>' + body + '</tbody></table><p><br></p>';
}

document.getElementById('table-insert').onclick = () => {
  const markdown = tableMarkdown();
  const asHtml = tableHtml();
  closeTable();
  if (richMode) { focusRichSpot(); document.execCommand('insertHTML', false, asHtml); }
  else { typeBlock(markdown); }
};
document.getElementById('table-cancel').onclick = closeTable;
tableModal.onclick = (e) => { if (e.target === tableModal) { closeTable(); } };
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape' && !tableModal.hidden) { closeTable(); }
});

// 그림 그리기: 도형을 SVG 로 그려 파일 하나로 저장하므로 나중에 다시 열어 고칠 수 있습니다.
const NS = 'http://www.w3.org/2000/svg';
const drawModal = document.getElementById('draw-modal');
const canvas = document.getElementById('canvas');
const markers = document.getElementById('markers');
let tool = 'pen';
let color = '#1f2328';
let width = 3;
let shape = null;
let points = [];
let editingImage = null;   // 기존 그림을 고치는 중이면 그 <img>
let picked = [];           // 선택해 둔 도형
let dragFrom = null;       // 도형을 끌고 있는 중이면 시작점
let band = null;           // 빈 곳에서 끌어 고를 때 쓰는 사각형

function drawPoint(e) {
  const box = canvas.getBoundingClientRect();
  return {
    x: Math.round((e.clientX - box.left) / box.width * 800),
    y: Math.round((e.clientY - box.top) / box.height * 500),
  };
}

function shapes() {
  return [...canvas.children].filter(
    (node) => node !== canvas.firstElementChild && node !== markers);
}

function topShape(node) {
  while (node && node.parentNode !== canvas) { node = node.parentNode; }
  return node && node !== canvas.firstElementChild && node !== markers ? node : null;
}

function shift(node) {
  const found = /translate\\(([-\\d.]+)[ ,]([-\\d.]+)\\)/.exec(node.getAttribute('transform') || '');
  return found ? {x: Number(found[1]), y: Number(found[2])} : {x: 0, y: 0};
}

function moveTo(node, x, y) {
  node.setAttribute('transform', `translate(${Math.round(x)} ${Math.round(y)})`);
}

function boxOf(node) {
  const box = node.getBBox();
  const at = shift(node);
  return {x: box.x + at.x, y: box.y + at.y, width: box.width, height: box.height};
}

function showPicked() {
  markers.replaceChildren();
  for (const node of picked) {
    const box = boxOf(node);
    const frame = document.createElementNS(NS, 'rect');
    frame.setAttribute('x', box.x - 5);
    frame.setAttribute('y', box.y - 5);
    frame.setAttribute('width', box.width + 10);
    frame.setAttribute('height', box.height + 10);
    frame.setAttribute('fill', 'none');
    frame.setAttribute('stroke', '#0969da');
    frame.setAttribute('stroke-width', '1.5');
    frame.setAttribute('stroke-dasharray', '6 4');
    frame.setAttribute('pointer-events', 'none');
    markers.appendChild(frame);
  }
}

function pick(nodes) {
  picked = nodes;
  showPicked();
}

function makeShape(kind) {
  const node = document.createElementNS(NS, kind);
  node.setAttribute('fill', 'none');
  node.setAttribute('stroke', color);
  node.setAttribute('stroke-width', width);
  node.setAttribute('stroke-linecap', 'round');
  node.setAttribute('stroke-linejoin', 'round');
  canvas.insertBefore(node, markers);
  return node;
}

canvas.addEventListener('pointerdown', (e) => {
  const at = drawPoint(e);
  if (tool === 'erase') {
    // 맨 아래 흰 배경은 남기고 그 위에 그린 도형만 지웁니다.
    const hit = topShape(e.target);
    if (hit) { hit.remove(); pick(picked.filter((node) => node !== hit)); }
    return;
  }
  if (tool === 'select') {
    canvas.setPointerCapture(e.pointerId);
    const hit = topShape(e.target);
    if (hit) {
      if (e.shiftKey) {
        pick(picked.includes(hit) ? picked.filter((node) => node !== hit) : [...picked, hit]);
      } else if (!picked.includes(hit)) {
        pick([hit]);
      }
      dragFrom = at;
      points = picked.map((node) => shift(node));
    } else {
      if (!e.shiftKey) { pick([]); }
      band = makeShape('rect');
      band.setAttribute('stroke', '#0969da');
      band.setAttribute('stroke-width', '1');
      band.setAttribute('stroke-dasharray', '5 4');
      points = [at];
    }
    return;
  }
  canvas.setPointerCapture(e.pointerId);
  pick([]);
  points = [at];
  if (tool === 'pen') {
    shape = makeShape('path');
    shape.setAttribute('d', `M ${at.x} ${at.y}`);
  } else if (tool === 'rect') {
    shape = makeShape('rect');
  } else if (tool === 'ellipse') {
    shape = makeShape('ellipse');
  } else {
    shape = makeShape('path');
  }
});

canvas.addEventListener('pointermove', (e) => {
  const spot = drawPoint(e);
  if (dragFrom) {
    picked.forEach((node, index) => {
      moveTo(node, points[index].x + spot.x - dragFrom.x, points[index].y + spot.y - dragFrom.y);
    });
    showPicked();
    return;
  }
  if (band) {
    const from = points[0];
    band.setAttribute('x', Math.min(from.x, spot.x));
    band.setAttribute('y', Math.min(from.y, spot.y));
    band.setAttribute('width', Math.abs(spot.x - from.x));
    band.setAttribute('height', Math.abs(spot.y - from.y));
    return;
  }
  if (!shape) { return; }
  const at = spot;
  const from = points[0];
  if (tool === 'pen') {
    points.push(at);
    shape.setAttribute('d', shape.getAttribute('d') + ` L ${at.x} ${at.y}`);
  } else if (tool === 'rect') {
    shape.setAttribute('x', Math.min(from.x, at.x));
    shape.setAttribute('y', Math.min(from.y, at.y));
    shape.setAttribute('width', Math.abs(at.x - from.x));
    shape.setAttribute('height', Math.abs(at.y - from.y));
  } else if (tool === 'ellipse') {
    shape.setAttribute('cx', (from.x + at.x) / 2);
    shape.setAttribute('cy', (from.y + at.y) / 2);
    shape.setAttribute('rx', Math.abs(at.x - from.x) / 2);
    shape.setAttribute('ry', Math.abs(at.y - from.y) / 2);
  } else if (tool === 'line') {
    shape.setAttribute('d', `M ${from.x} ${from.y} L ${at.x} ${at.y}`);
  } else if (tool === 'arrow') {
    const angle = Math.atan2(at.y - from.y, at.x - from.x);
    const head = 8 + width * 2;
    const left = [at.x - head * Math.cos(angle - 0.4), at.y - head * Math.sin(angle - 0.4)];
    const right = [at.x - head * Math.cos(angle + 0.4), at.y - head * Math.sin(angle + 0.4)];
    shape.setAttribute('d', `M ${from.x} ${from.y} L ${at.x} ${at.y}`
      + ` M ${left[0].toFixed(1)} ${left[1].toFixed(1)} L ${at.x} ${at.y}`
      + ` L ${right[0].toFixed(1)} ${right[1].toFixed(1)}`);
  }
});

for (const done of ['pointerup', 'pointerleave', 'pointercancel']) {
  canvas.addEventListener(done, () => {
    if (band) {
      const area = {
        x: Number(band.getAttribute('x') || 0), y: Number(band.getAttribute('y') || 0),
        w: Number(band.getAttribute('width') || 0), h: Number(band.getAttribute('height') || 0),
      };
      band.remove();
      band = null;
      const inside = shapes().filter((node) => {
        const box = boxOf(node);
        return box.x >= area.x && box.y >= area.y
          && box.x + box.width <= area.x + area.w && box.y + box.height <= area.y + area.h;
      });
      pick([...new Set([...picked, ...inside])]);
    }
    dragFrom = null;
    shape = null;
  });
}

document.getElementById('draw-group').onclick = () => {
  if (picked.length < 2) { return; }
  const bundle = document.createElementNS(NS, 'g');
  canvas.insertBefore(bundle, markers);
  picked.forEach((node) => bundle.appendChild(node));
  pick([bundle]);
};

document.getElementById('draw-ungroup').onclick = () => {
  const loose = [];
  for (const node of picked) {
    if (node.tagName !== 'g') { loose.push(node); continue; }
    const at = shift(node);
    while (node.firstElementChild) {
      const child = node.firstElementChild;
      const childAt = shift(child);
      canvas.insertBefore(child, node);
      moveTo(child, childAt.x + at.x, childAt.y + at.y);
      loose.push(child);
    }
    node.remove();
  }
  pick(loose);
};

document.querySelectorAll('[data-tool]').forEach((button) => {
  button.onclick = () => {
    tool = button.dataset.tool;
    canvas.style.cursor = tool === 'select' ? 'default'
      : tool === 'erase' ? 'pointer' : 'crosshair';
    if (tool !== 'select') { pick([]); }
    document.querySelectorAll('[data-tool]').forEach((b) => b.classList.toggle('primary', b === button));
  };
});
document.querySelectorAll('[data-color]').forEach((button) => {
  button.onclick = () => {
    color = button.dataset.color;
    document.querySelectorAll('[data-color]').forEach((b) => b.classList.toggle('on', b === button));
  };
});
document.querySelectorAll('[data-width]').forEach((button) => {
  button.onclick = () => {
    width = Number(button.dataset.width);
    document.querySelectorAll('[data-width]').forEach((b) => b.classList.toggle('primary', b === button));
  };
});

document.getElementById('draw-undo').onclick = () => {
  const drawn = shapes();
  if (drawn.length) {
    const last = drawn[drawn.length - 1];
    last.remove();
    pick(picked.filter((node) => node !== last));
  }
};
document.getElementById('draw-clear').onclick = () => {
  shapes().forEach((node) => node.remove());
  pick([]);
};

async function openDraw(image) {
  keepRichSpot();
  editingImage = image || null;
  shapes().forEach((node) => node.remove());
  pick([]);
  if (image) {
    const res = await fetch(image.getAttribute('src'));
    const text = await res.text();
    const loaded = new DOMParser().parseFromString(text, 'image/svg+xml').documentElement;
    [...loaded.children].slice(1).forEach((node) => canvas.insertBefore(node, markers));
  }
  drawModal.hidden = false;
}

function closeDraw() {
  drawModal.hidden = true;
  editingImage = null;
  backToEditSpot();
}

document.getElementById('draw-insert').onclick = async () => {
  const copy = canvas.cloneNode(true);
  copy.setAttribute('xmlns', NS);
  copy.querySelector('#markers').remove();   // 선택 표시는 저장하지 않습니다
  const svg = new XMLSerializer().serializeToString(copy);
  const stamp = new Date().toISOString().replace(/[-:T]/g, '').slice(0, 14);
  const name = editingImage
    ? decodeURIComponent(editingImage.getAttribute('src').split('/f/')[1])
    : '그림-' + stamp + '.svg';
  status.textContent = '그림 저장 중...';
  const res = await fetch('/upload' + (editingImage ? '?replace=1' : ''), {
    method: 'POST',
    headers: {'X-Filename': encodeURIComponent(name), 'Content-Type': 'image/svg+xml'},
    body: svg,
  });
  if (!res.ok) { status.textContent = '그림을 저장하지 못했습니다.'; return; }
  const saved = await res.json();
  const link = '/f/' + encodeURIComponent(saved.name);
  const wasEditing = editingImage;
  closeDraw();
  if (wasEditing) {
    wasEditing.setAttribute('src', link + '?t=' + Date.now());
    status.textContent = '그림을 고쳤습니다 (저장을 눌러야 반영됩니다)';
    return;
  }
  if (richMode) {
    focusRichSpot();
    document.execCommand('insertHTML', false, '<img src="' + link + '" alt="' + saved.name + '">');
  } else {
    typeText('![' + saved.name + '](' + link + ')\\n');
  }
  status.textContent = saved.name + ' 넣었습니다 (저장을 눌러야 반영됩니다)';
};

document.getElementById('draw-cancel').onclick = closeDraw;
drawModal.onclick = (e) => { if (e.target === drawModal) { closeDraw(); } };
document.addEventListener('keydown', (e) => {
  if (drawModal.hidden) { return; }
  if (e.key === 'Escape') { closeDraw(); }
  else if ((e.key === 'Delete' || e.key === 'Backspace') && picked.length) {
    e.preventDefault();
    picked.forEach((node) => node.remove());
    pick([]);
  }
});

// 표 칸에 채워져 있는 공백(&nbsp;)을 지웁니다. 예전에 만든 표나 다른 곳에서 붙여넣은 표에는
// 빈 칸마다 공백이 들어 있어, 누르고 바로 쓰면 글자 앞에 공백이 남습니다.
function tidyCells() {
  for (const cell of rich.querySelectorAll('td, th')) {
    if (cell.querySelector('img')) { continue; }
    if (!cell.textContent.replace(/[\\s\\u00a0]+/g, '')) {
      if (cell.innerHTML !== '<br>') { cell.innerHTML = '<br>'; }
      continue;
    }
    const texts = [...cell.childNodes].filter((node) => node.nodeType === 3);
    for (const node of texts) { node.data = node.data.replace(/\\u00a0/g, ' '); }
    if (texts.length) {   // 칸 앞뒤에 붙은 공백도 지웁니다
      const first = texts[0];
      const last = texts[texts.length - 1];
      if (first === cell.firstChild) { first.data = first.data.replace(/^\\s+/, ''); }
      if (last === cell.lastChild) { last.data = last.data.replace(/\\s+$/, ''); }
    }
  }
}

// 표 안에서는 화살표로 칸 사이를 오갑니다. 크롬은 표에서 위아래가 엉뚱한 칸으로 튑니다.
function cellHere() {
  const chosen = document.getSelection();
  let node = chosen.anchorNode;
  if (!node || !rich.contains(node)) { return null; }
  if (node.nodeType === 3) { node = node.parentNode; }
  return node.closest ? node.closest('td, th') : null;
}

function atCellEdge(cell, toEnd) {
  const chosen = document.getSelection();
  if (!chosen.isCollapsed || !chosen.rangeCount) { return false; }
  const edge = document.createRange();
  edge.selectNodeContents(cell);
  edge.collapse(!toEnd);
  return edge.compareBoundaryPoints(Range.START_TO_START, chosen.getRangeAt(0)) === 0;
}

function caretInto(node, toEnd) {
  const range = document.createRange();
  range.selectNodeContents(node);
  range.collapse(!toEnd);
  const chosen = document.getSelection();
  chosen.removeAllRanges();
  chosen.addRange(range);
}

function goCell(cell, dr, dc, toEnd) {
  const rows = [...cell.closest('table').rows];
  const line = rows[rows.indexOf(cell.parentNode) + dr];
  const target = line && line.cells[cell.cellIndex + dc];
  if (!target) { return false; }
  caretInto(target, toEnd);
  return true;
}

// 맨 윗줄에서 위로, 맨 아랫줄에서 아래로 누르면 표 밖으로 나갑니다.
// (크롬에 맡기면 표 안에서 옆 칸으로 튑니다.)
function leaveTable(cell, down) {
  const table = cell.closest('table');
  const near = down ? table.nextElementSibling : table.previousElementSibling;
  if (near && near.nodeName !== 'TABLE') { caretInto(near, !down); return true; }
  const fresh = document.createElement('div');
  fresh.appendChild(document.createElement('br'));
  table.parentNode.insertBefore(fresh, down ? table.nextSibling : table);
  caretInto(fresh, false);
  return true;
}

// 링크 글자를 통째로 골라 다시 쓰면 크롬이 링크까지 지웁니다. 링크는 두고 글자만 바꿉니다.
function wholeLinkChosen(range) {
  const holder = (node) => (node && node.nodeType === 3 ? node.parentNode : node);
  const from = holder(range.startContainer);
  const to = holder(range.endContainer);
  const link = from && from.closest ? from.closest('a[data-wiki]') : null;
  if (!link || !to || !to.closest || to.closest('a[data-wiki]') !== link) { return null; }
  const all = document.createRange();
  all.selectNodeContents(link);
  return (all.compareBoundaryPoints(Range.START_TO_START, range) === 0
       && all.compareBoundaryPoints(Range.END_TO_END, range) === 0) ? link : null;
}

// 링크 끝에서 친 글자는 크롬이 링크 밖으로 내보냅니다. 그래서 보이지 않는 글자(폭 없는 공백)를
// 뒤에 붙여 두고 그 앞에서 쓰게 합니다. 이 글자는 저장할 때 지워집니다.
const KEEP = '\\u200b';

function keepLinkAlive(link, first) {
  link.textContent = first + KEEP;
  const range = document.createRange();
  range.setStart(link.firstChild, first.length);
  range.collapse(true);
  const chosen = document.getSelection();
  chosen.removeAllRanges();
  chosen.addRange(range);
}

function chosenWholeLink() {
  const chosen = document.getSelection();
  if (!chosen.rangeCount || chosen.isCollapsed) { return null; }
  return wholeLinkChosen(chosen.getRangeAt(0));
}

rich.addEventListener('beforeinput', (e) => {
  if (e.inputType !== 'insertText' || !e.data) { return; }
  const link = chosenWholeLink();
  if (!link) { return; }
  e.preventDefault();
  keepLinkAlive(link, e.data);
});

// 한글처럼 조합해서 넣는 입력은 막을 수 없습니다. 그래서 조합이 끝난 뒤 그 글자를 다시 링크로
// 감싸 줍니다. 조합을 시작할 때 어느 글로 가는 링크였는지만 기억해 둡니다.
let wasWholeLink = null;

rich.addEventListener('compositionstart', () => {
  const link = chosenWholeLink();
  wasWholeLink = link ? (link.dataset.wiki || '').split('|')[0] : null;
});

rich.addEventListener('compositionend', (e) => {
  const ref = wasWholeLink;
  wasWholeLink = null;
  if (!ref || !e.data) { return; }
  const chosen = document.getSelection();
  if (!chosen.rangeCount) { return; }
  const at = chosen.getRangeAt(0);
  const node = at.startContainer;
  const end = at.startOffset;
  const start = end - e.data.length;
  if (node.nodeType !== 3 || start < 0 || node.data.slice(start, end) !== e.data) { return; }
  const range = document.createRange();
  range.setStart(node, start);
  range.setEnd(node, end);
  const link = document.createElement('a');
  link.setAttribute('data-wiki', ref);
  link.setAttribute('href', '/w/' + encodeURIComponent(ref));
  range.surroundContents(link);
  const after = document.createRange();
  after.setStartAfter(link);
  after.collapse(true);
  chosen.removeAllRanges();
  chosen.addRange(after);
});

// 서식 편집 모드에서 그림을 두 번 누르면 다시 고칠 수 있습니다.
rich.addEventListener('dblclick', (e) => {
  const image = e.target.closest('img');
  if (image && image.getAttribute('src').endsWith('.svg')) { openDraw(image); }
});

// 두 가지 편집 모드: 마크다운 원본을 그대로 고치거나, 꾸며진 화면에서 바로 고칩니다.
const tabText = document.getElementById('tab-text');
const tabRich = document.getElementById('tab-rich');

async function pullFromRich() {
  const done = await post('/tomarkdown', {html: rich.innerHTML});
  if (!done) { return false; }
  editor.value = done.text;
  return true;
}

async function setMode(toRich) {
  if (toRich === richMode) { return; }
  // 모드를 바꾸면 본문을 다시 그리므로 커서 자리는 버리고, 보고 있던 만큼만 다시 내려 줍니다.
  const from = richMode ? rich : editor;
  const room = from.scrollHeight - from.clientHeight;
  const ratio = room > 0 ? from.scrollTop / room : 0;
  if (toRich) {
    const shown = await post('/preview', {text: editor.value});
    if (!shown) { return; }
    rich.innerHTML = shown.html;
    tidyCells();
  } else if (!await pullFromRich()) {
    return;
  }
  richMode = toRich;
  editor.hidden = toRich;
  rich.hidden = !toRich;
  tabText.classList.toggle('primary', !toRich);
  tabRich.classList.toggle('primary', toRich);
  for (const button of mdbar.querySelectorAll('button')) {
    button.disabled = toRich && !button.dataset.rich;
  }
  richSpot = null;                       // 새로 그린 본문에는 옛 자리가 없습니다
  const to = toRich ? rich : editor;
  to.focus();
  to.scrollTop = ratio * (to.scrollHeight - to.clientHeight);
}

tabText.onclick = () => setMode(false);
tabRich.onclick = () => setMode(true);
document.execCommand('styleWithCSS', false, false);
for (const button of mdbar.querySelectorAll('button')) {
  button.disabled = richMode && !button.dataset.rich;   // 처음 열릴 때 상태를 맞춥니다
}

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(false); }
});
for (const box of [titleInput, folderInput]) {
  box.addEventListener('keydown', (e) => {
    // 서식 편집 모드에서는 textarea 가 숨어 있어 focus() 가 통하지 않습니다.
    if (e.key === 'Enter') { e.preventDefault(); backToEditSpot(); }
  });
}

// 마크다운 모드에서 Tab 은 들여쓰기, Enter 는 목록·인용을 이어 줍니다.
const INDENT = '    ';

function lineHere() {
  const start = editor.value.lastIndexOf('\\n', editor.selectionStart - 1) + 1;
  let end = editor.value.indexOf('\\n', editor.selectionStart);
  if (end < 0) { end = editor.value.length; }
  return {
    start: start,
    end: end,
    text: editor.value.slice(start, editor.selectionStart),  // 커서 앞까지
    full: editor.value.slice(start, end),                    // 줄 전체
  };
}

function eraseLine(here) {
  editor.setSelectionRange(here.start, here.end);
  document.execCommand('delete');
}

editor.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') {
    e.preventDefault();
    if (!e.shiftKey) { typeText(INDENT); return; }
    const here = lineHere();
    const spaces = here.text.match(/^ {1,4}/);
    if (spaces) {
      const at = editor.selectionStart;
      editor.setSelectionRange(here.start, here.start + spaces[0].length);
      document.execCommand('delete');
      editor.setSelectionRange(at - spaces[0].length, at - spaces[0].length);
    }
    return;
  }
  if (e.key !== 'Enter' || e.shiftKey || e.ctrlKey || e.metaKey) { return; }
  // 줄 전체로 봅니다. 커서 앞만 보면 표시 바로 뒤에서 누른 Enter 가 빈 항목으로 보입니다.
  const here = lineHere();
  const found = here.full.match(/^(\\s*)([-*+] |\\d+[.)] |> )(.*)$/);
  if (!found) { return; }
  if (editor.selectionStart < here.start + found[1].length + found[2].length) {
    return;   // 표시 안에 커서가 있으면 그냥 줄을 바꿉니다
  }
  e.preventDefault();
  if (!found[3].trim()) {
    eraseLine(here);   // 빈 항목이면 표시를 지우고 목록을 끝냅니다
    typeText('\\n');
    return;
  }
  const numbered = found[2].match(/^(\\d+)([.)] )$/);
  const marker = numbered ? (Number(numbered[1]) + 1) + numbered[2] : found[2];
  typeText('\\n' + found[1] + marker);
});

// 크롬은 인용 안에서 Enter 를 눌러도 인용에서 빠져나오지 못합니다.
// 목록처럼, 빈 인용 줄에서 Enter 를 누르면 인용을 끝내고 보통 줄로 나옵니다.
function leaveQuote() {
  const chosen = document.getSelection();
  let node = chosen.anchorNode;
  if (!node) { return false; }
  if (node.nodeType === 3) { node = node.parentNode; }
  const quote = node.closest('blockquote');
  if (!quote || !rich.contains(quote)) { return false; }
  const line = node.closest(BLOCKS) || quote;
  if (line.textContent.trim() || line.querySelector('img')) { return false; }
  let top = quote;   // 겹쳐 있으면 맨 바깥 인용 밖으로 나옵니다
  for (let up = quote.parentNode; up && up !== rich; up = up.parentNode) {
    if (up.nodeName === 'BLOCKQUOTE') { top = up; }
  }
  const fresh = document.createElement('div');
  fresh.appendChild(document.createElement('br'));
  top.parentNode.insertBefore(fresh, top.nextSibling);
  line.remove();
  if (!top.textContent.trim() && !top.querySelector('img')) { top.remove(); }
  const range = document.createRange();
  range.selectNodeContents(fresh);
  range.collapse(false);
  chosen.removeAllRanges();
  chosen.addRange(range);
  return true;
}

// 서식 편집 모드에서는 Tab 으로 목록 단계를 조절합니다.
rich.addEventListener('keydown', (e) => {
  if (e.key === 'Tab') {
    e.preventDefault();
    document.execCommand(e.shiftKey ? 'outdent' : 'indent');
    return;
  }
  if (e.key === 'Enter' && !e.shiftKey && !e.ctrlKey && !e.metaKey && leaveQuote()) {
    e.preventDefault();
    return;
  }
  if (!e.key.startsWith('Arrow') || e.shiftKey || e.ctrlKey || e.altKey || e.metaKey) { return; }
  const cell = cellHere();
  if (!cell) { return; }
  let moved = false;
  if (e.key === 'ArrowUp') { moved = goCell(cell, -1, 0, true) || leaveTable(cell, false); }
  else if (e.key === 'ArrowDown') { moved = goCell(cell, 1, 0, true) || leaveTable(cell, true); }
  else if (e.key === 'ArrowLeft' && atCellEdge(cell, false)) { moved = goCell(cell, 0, -1, true); }
  else if (e.key === 'ArrowRight' && atCellEdge(cell, true)) { moved = goCell(cell, 0, 1, false); }
  if (moved) { e.preventDefault(); }   // 표 끝이면 그대로 둬서 표 밖으로 나갈 수 있게 합니다
});
for (const box of [editor, rich]) {
  box.addEventListener('paste', (e) => {
    const files = [...e.clipboardData.files];
    if (files.length) { e.preventDefault(); upload(files); return; }
    if (box === rich) {   // 링크 글자를 통째로 골라 붙여넣어도 링크는 남깁니다
      const link = chosenWholeLink();
      const text = e.clipboardData.getData('text/plain');
      if (link && text) { e.preventDefault(); keepLinkAlive(link, text.replace(/\\s+/g, ' ')); return; }
    }
    // 다른 곳에서 붙여넣은 표는 빈 칸이 공백으로 채워져 오므로 붙은 뒤에 지웁니다.
    if (box === rich) { setTimeout(tidyCells, 0); }
  });
  box.addEventListener('dragover', (e) => { e.preventDefault(); box.classList.add('drag'); });
  box.addEventListener('dragleave', () => box.classList.remove('drag'));
  box.addEventListener('drop', (e) => {
    e.preventDefault();
    box.classList.remove('drag');
    if (e.dataTransfer.files.length) { upload(e.dataTransfer.files); }
  });
}
tidyCells();   // 열 때 이미 들어 있는 표의 공백부터 지웁니다
(titleInput.value ? (richMode ? rich : editor) : titleInput).focus();
"""


def draw_modal() -> str:
    tools = "".join(
        f'<button class="btn{" primary" if key == "pen" else ""}" data-tool="{key}">{label}</button>'
        for key, label in DRAW_TOOLS
    )
    colors = "".join(
        f'<button class="swatch{" on" if index == 0 else ""}" data-color="{color}" '
        f'style="background:{color}" title="{color}"></button>'
        for index, color in enumerate(DRAW_COLORS)
    )
    widths = "".join(
        f'<button class="btn{" primary" if size == 3 else ""}" data-width="{size}">{size}</button>'
        for size in (2, 3, 6)
    )
    return (
        '<div class="modal" id="draw-modal" hidden><div class="sheet">'
        "<h2>그림 그리기</h2>"
        f'<div class="drawbar">{tools}</div>'
        f'<div class="drawbar"><span class="meta">색</span>{colors}'
        f'<span class="meta" style="margin-left:8px">굵기</span>{widths}'
        '<span class="spacer"></span>'
        '<button class="btn" id="draw-group">묶기</button>'
        '<button class="btn" id="draw-ungroup">풀기</button>'
        '<button class="btn" id="draw-undo">되돌리기</button>'
        '<button class="btn" id="draw-clear">전체 지우기</button></div>'
        '<svg id="canvas" viewBox="0 0 800 500" width="800" height="500">'
        '<rect x="0" y="0" width="800" height="500" fill="#ffffff"></rect>'
        '<g id="markers"></g></svg>'
        '<div class="toolbar">'
        '<span class="hint">끌어서 그립니다. <b>선택</b> 으로 도형을 눌러 옮기고, '
        '여러 개는 Shift+클릭이나 빈 곳에서 끌어 고른 뒤 <b>묶기</b> 로 하나로 만듭니다. '
        'Delete 로 지웁니다.</span>'
        '<span class="spacer"></span>'
        '<button class="btn primary" id="draw-insert">넣기</button>'
        '<button class="btn" id="draw-cancel">취소</button>'
        "</div></div></div>"
    )


def link_modal(ref: str) -> str:
    """글 연결 창. 목록에서 글을 고르거나, 새 글을 만들어 연결합니다."""
    options = "".join(
        f'<option value="{html.escape(other, quote=True)}">{html.escape(other)}</option>'
        for other in sorted(other for other, _ in list_pages() if other != ref)
    )
    return (
        '<div class="modal" id="link-modal" hidden><div class="sheet">'
        "<h2>글 연결</h2>"
        '<p class="hint">연결할 글을 목록에서 고르고 <b>연결</b> 을 누릅니다. '
        '맨 위 <b>＋ 새 글 만들기</b> 를 고르면 새 글을 만들어 연결합니다. '
        '고른 글자가 있으면 그 글자에 링크가 걸리고, 없으면 글 제목이 그대로 보입니다.</p>'
        '<select id="link-pick">'
        '<option value="+">＋ 새 글 만들기</option>'
        f"{options}</select>"
        '<div id="link-new" hidden>'
        '<p class="hint">만들 글의 제목과 폴더를 정합니다. 내용은 안 써도 되고 나중에 채워도 됩니다.</p>'
        '<input type="text" id="link-title" placeholder="새 글 제목" maxlength="100">'
        '<input type="text" id="link-folder" placeholder="폴더 (비우면 맨 바깥)">'
        '<textarea id="link-text" placeholder="새 글 내용 (선택)" spellcheck="false"></textarea>'
        "</div>"
        '<div class="toolbar"><span class="hint" id="link-label"></span>'
        '<span class="spacer"></span>'
        '<button class="btn primary" id="link-make">연결</button>'
        '<button class="btn" id="link-cancel">취소</button>'
        "</div></div></div>"
    )


def edit_body(ref: str, folder: str = "") -> str:
    exists = page_exists(ref)
    folder = folder_of(ref) or folder
    title = title_of(ref) if ref else ""
    buttons = "".join(f'<button class="btn" {attrs}>{label}</button>' for label, attrs in MD_BUTTONS)
    langs = "".join(f'<option value="{key}">{label}</option>' for key, label in CODE_LANGS)
    buttons += (
        f'<select id="code-lang" title="코드 종류">{langs}</select>'
        '<button class="btn" data-code="1" data-rich="code">코드블록</button>'
    )
    options = "".join(f'<option value="{html.escape(name, quote=True)}">' for name in list_folders())
    cancel_href = f"/w/{urllib.parse.quote(ref)}" if exists else "/"
    return (
        # 저장 단추는 문서 화면과 같이 오른쪽 위에 둡니다.
        '<div class="toolbar">'
        '<span id="status" class="meta"></span>'
        '<span class="spacer"></span>'
        '<label class="btn">파일 첨부<input type="file" id="picker" multiple hidden></label>'
        f'<a class="btn" href="{cancel_href}">취소</a>'
        '<button class="btn" id="save">저장 (Ctrl+S)</button>'
        '<button class="btn primary" id="done">게시</button>'
        "</div>"
        '<div class="titlerow">'
        f'<input type="text" id="folder" list="folders" placeholder="폴더 (선택)" '
        f'value="{html.escape(folder, quote=True)}" maxlength="200">'
        f'<datalist id="folders">{options}</datalist>'
        f'<input type="text" id="title" placeholder="제목을 입력하세요" '
        f'value="{html.escape(title, quote=True)}" maxlength="100">'
        "</div>"
        '<div class="tabs">'
        '<button class="btn" id="tab-text">마크다운</button>'
        '<button class="btn primary" id="tab-rich">서식 편집</button>'
        "</div>"
        f'<div class="mdbar" id="mdbar">{buttons}</div>'
        f'<textarea id="editor" data-original="{html.escape(ref if exists else "", quote=True)}" '
        f'placeholder="본문을 마크다운으로 씁니다." spellcheck="false" hidden>'
        f"{html.escape(read_page(ref))}</textarea>"
        '<div id="rich" contenteditable="true" spellcheck="false">'
        f"{render(read_page(ref), highlight=False)}</div>"
        '<div class="modal" id="table-modal" hidden><div class="sheet">'
        "<h2>표 만들기</h2>"
        '<p class="hint">첫 줄은 제목 칸입니다. <code>Tab</code> 으로 다음 칸으로 넘어갑니다.</p>'
        '<table id="grid"></table>'
        '<div class="toolbar">'
        '<button class="btn" data-grid="row+">행 추가</button>'
        '<button class="btn" data-grid="row-">행 삭제</button>'
        '<button class="btn" data-grid="col+">열 추가</button>'
        '<button class="btn" data-grid="col-">열 삭제</button>'
        '<span class="spacer"></span>'
        '<button class="btn primary" id="table-insert">넣기</button>'
        '<button class="btn" id="table-cancel">취소</button>'
        "</div></div></div>"
        + draw_modal() + link_modal(ref) +
        '<details class="hint"><summary>편집 도움말</summary>'
        '<p><b>마크다운</b> 은 원본을 그대로 고치는 모드, <b>서식 편집</b> 은 '
        '꾸며진 화면에서 바로 고치는 모드입니다. 두 모드는 오갈 때마다 서로 옮겨 적히고, '
        '저장되는 파일은 언제나 마크다운입니다. '
        '인라인 <code>코드</code> 는 마크다운 모드에서 넣어 주세요. 글끼리 연결은 <b>글 연결</b> '
        '하나로 합니다 — 있는 글은 목록에서 고르고, 없는 이름을 쓰면 그 글을 새로 만듭니다.<br>'
        '폴더는 <code>개발/서버</code> 처럼 <code>/</code> 로 여러 단계를 씁니다. '
        '비워 두면 폴더 없이 저장됩니다. 문서 링크는 <code>[[문서 이름]]</code> 또는 '
        '<code>[[폴더/문서 이름]]</code>. 이미지·파일은 드래그해서 놓거나 클립보드에서 '
        '바로 붙여넣을 수 있습니다. 제목이나 폴더를 바꿔 저장하면 문서가 그대로 옮겨집니다.</p>'
        '</details>'
    )


def trash_screen() -> str:
    """휴지통 화면. 지운 것을 되살리거나 완전히 지웁니다."""
    rows = trash_rows()
    if not rows:
        return ("<h1>🗑 휴지통</h1>"
                '<p class="empty">휴지통이 비어 있습니다.<br>'
                "글이나 폴더를 지우면 여기로 옮겨지고, 여기서 되살릴 수 있습니다.</p>")

    cards = []
    for row in rows:
        ref = row.get("ref", "")
        pages = row.get("pages") or []
        folders = row.get("folders") or []
        when = row.get("at", "").replace("T", " ")
        what = "폴더" if row.get("kind") == "folder" else "글"
        deeper = [name for name in pages if name != ref]
        counts = [f"딸린 글 {len(deeper)}개"] if deeper else []
        if folders:
            counts.append(f"하위 폴더 {len(folders)}개")
        note = f' <span class="meta">({", ".join(counts)} 함께)</span>' if counts else ""
        listing = (
            '<details><summary class="meta">함께 지워진 글 보기</summary>'
            + "<ul>" + "".join(f"<li>{html.escape(name)}</li>" for name in sorted(deeper)) + "</ul>"
            "</details>"
        ) if deeper else ""
        cards.append(
            f'<li data-id="{html.escape(row["id"], quote=True)}">'
            '<div class="trash-head">'
            f"<b>{html.escape(ref)}</b> <span class=\"meta\">{what}</span>{note}"
            '<span class="spacer"></span>'
            '<button class="btn" data-act="restore">되살리기</button>'
            '<button class="btn" data-act="drop">완전히 지우기</button>'
            "</div>"
            f'<div class="meta">지운 때: {html.escape(when)}</div>'
            f"{listing}</li>"
        )
    return (
        "<h1>🗑 휴지통</h1>"
        '<p class="hint">지운 글과 폴더가 여기 남아 있습니다. 되살리면 원래 자리로 돌아갑니다. '
        "완전히 지우면 되돌릴 수 없습니다.</p>"
        '<div class="toolbar"><span id="status" class="meta"></span><span class="spacer"></span>'
        '<button class="btn" id="trash-empty">휴지통 비우기</button></div>'
        f'<ul class="trash">{"".join(cards)}</ul>'
    )


TRASH_SCRIPT = """
const trashStatus = document.getElementById('status');

document.querySelectorAll('.trash [data-act]').forEach((button) => {
  button.onclick = async () => {
    const card = button.closest('li');
    const act = button.dataset.act;
    if (act === 'drop' && !confirm('‘' + card.querySelector('b').textContent
        + '’ 를 완전히 지울까요? 되돌릴 수 없습니다.')) { return; }
    const done = await post('/trash/' + (act === 'drop' ? 'drop' : 'restore'), {id: card.dataset.id});
    if (!done) { return; }
    trashStatus.textContent = done.message;
    card.remove();
    if (!document.querySelector('.trash li')) { location.reload(); }
  };
});

document.getElementById('trash-empty').onclick = async () => {
  if (!document.querySelector('.trash li')) { return; }
  if (!confirm('휴지통을 비울까요? 안에 있는 것이 모두 완전히 지워집니다.')) { return; }
  if (await post('/trash/empty', {})) { location.reload(); }
};
"""


SETTINGS_SCRIPT = """
const settingsStatus = document.getElementById('status');

document.getElementById('save').onclick = async () => {
  const done = await post('/settings', {
    name: document.getElementById('name').value,
    home: document.getElementById('home').value,
    editor: document.getElementById('editor').value,
  });
  if (done) { location.href = '/settings'; }
};

document.getElementById('save-data').onclick = async () => {
  const path = document.getElementById('data').value.trim();
  const question = path
    ? '글과 첨부를 다음 폴더에서 씁니다.\\n\\n' + path
      + '\\n\\n그 폴더에 이미 위키 내용이 있으면 그것을 그대로 쓰고, 없으면 지금 내용을 옮깁니다.'
    : '글과 첨부를 프로그램 옆(기본 위치)으로 되돌립니다. 계속할까요?';
  if (!confirm(question)) { return; }
  settingsStatus.textContent = '옮기는 중...';
  const done = await post('/settings/data', {path: path});
  if (done) { alert(done.message); location.href = '/settings'; }
  else { settingsStatus.textContent = ''; }
};
"""


def settings_body() -> str:
    places = "".join(f'<option value="{html.escape(p, quote=True)}">' for p in sync_places())
    synced = DATA != ROOT
    pages = "".join(
        f'<option value="{html.escape(ref, quote=True)}">' for ref, _ in sorted(list_pages())
    )
    chosen = read_config().get("editor", "auto")
    editors = "".join(
        f'<option value="{key}"{" selected" if key == chosen else ""}>{label}</option>'
        for key, label in EDITORS
    )
    return (
        "<h1>설정</h1>"
        "<h2>위키 이름</h2>"
        '<p class="hint">화면 왼쪽 위와 브라우저 탭에 표시됩니다.</p>'
        f'<p><input type="text" id="name" maxlength="40" style="width:320px" '
        f'value="{html.escape(wiki_name(), quote=True)}"></p>'
        "<h2>홈으로 쓸 글</h2>"
        '<p class="hint">왼쪽 위 위키 이름을 눌렀을 때 열리는 글입니다. '
        "비워 두면 문서 목록으로 갑니다.</p>"
        f'<p><input type="text" id="home" list="pagelist" style="width:min(420px,100%)" '
        f'placeholder="예: 가이드" value="{html.escape(read_config().get("home", ""), quote=True)}">'
        f'<datalist id="pagelist">{pages}</datalist></p>'
        "<h2>코드 편집기</h2>"
        '<p class="hint">글에 넣은 코드 위치 링크(<code>파일:줄</code>)를 눌렀을 때 무엇으로 열지 '
        "정합니다. 자동은 Visual Studio 가 켜져 있으면 그쪽, 아니면 VS Code 를 씁니다.</p>"
        f'<p><select id="editor">{editors}</select></p>'
        '<div class="toolbar"><button class="btn primary" id="save">저장</button>'
        '<span id="status" class="meta"></span></div>'
        "<h2>데이터 폴더</h2>"
        '<p class="hint">글과 첨부가 저장되는 곳입니다. OneDrive 같은 동기화 폴더를 지정하면 '
        '다른 기기에서도 같은 내용을 보고 고칠 수 있습니다. 비워 두면 프로그램 옆에 저장합니다.</p>'
        f'<p class="meta">지금 위치: <code>{html.escape(str(DATA))}</code>'
        f'{" (동기화 폴더)" if synced else " (프로그램 옆, 이 기기에만 있음)"}</p>'
        f'<p><input type="text" id="data" list="places" style="width:min(560px,100%)" '
        f'placeholder="비우면 프로그램 옆에 저장합니다" '
        f'value="{html.escape(str(DATA) if synced else "", quote=True)}">'
        f'<datalist id="places">{places}</datalist></p>'
        '<div class="toolbar"><button class="btn" id="save-data">데이터 폴더 바꾸기</button>'
        '<a class="btn" href="/">문서 목록으로</a></div>'
        '<p class="hint">지정한 폴더에 이미 위키 내용이 있으면 <b>그 내용을 그대로</b> 씁니다. '
        '없으면 <b>지금 내용을 그 폴더로 옮깁니다</b>. 다른 기기에서는 같은 동기화 폴더를 '
        '지정하기만 하면 됩니다. 두 기기에서 같은 글을 동시에 고치면 동기화 프로그램이 '
        '충돌 사본을 만들 수 있으니, 한 번에 한 곳에서 쓰는 편이 좋습니다.</p>'
    )


# ---------------------------------------------------------------- 서버

class Handler(http.server.BaseHTTPRequestHandler):
    server_version = "LocalWiki"

    def log_message(self, fmt, *args):
        sys.stderr.write("%s %s\n" % (self.log_date_time_string(), fmt % args))

    def send(self, body: bytes, status: int = 200, content_type: str = "text/html; charset=utf-8"):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # 글을 고치거나 옮긴 뒤 옛 화면이 다시 나오지 않도록 캐시를 쓰지 않습니다.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def redirect(self, where: str):
        self.send_response(303)
        self.send_header("Location", where)
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()

    def send_text(self, status: int, message: str):
        self.send(message.encode("utf-8"), status, "text/plain; charset=utf-8")

    def send_json(self, payload: dict):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send(body, 200, "application/json; charset=utf-8")

    def split_path(self) -> tuple[str, str, dict]:
        parsed = urllib.parse.urlsplit(self.path)
        parts = parsed.path.split("/", 2)
        prefix = parts[1] if len(parts) > 1 else ""
        rest = urllib.parse.unquote(parts[2]) if len(parts) > 2 else ""
        return prefix, rest, urllib.parse.parse_qs(parsed.query)

    def page_number(self, query: dict) -> int:
        try:
            return max(1, int(query.get("page", ["1"])[0]))
        except ValueError:
            return 1

    def cookie(self, want: str) -> str:
        for part in (self.headers.get("Cookie") or "").split(";"):
            key, _, value = part.strip().partition("=")
            if key == want:
                return value
        return ""

    def closed_folders(self) -> set[str]:
        """접어 둔 폴더 목록. 브라우저가 쿠키로 알려 줍니다."""
        raw = self.cookie("closed")
        return {urllib.parse.unquote(name) for name in raw.split("|") if name}

    def side_sort(self) -> str:
        chosen = self.cookie("sideSort")
        return chosen if chosen in dict(SORTS) else "order"

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        prefix, rest, query = self.split_path()
        name = wiki_name()
        closed = self.closed_folders()
        side = self.side_sort()

        if prefix == "":
            # 글 목록은 왼쪽 목록이 대신하므로, 맨 앞 주소는 홈으로 정한 글을 엽니다.
            if home_page():
                self.redirect("/w/" + urllib.parse.quote(home_page()))
                return
            self.send(shell(name, start_body(), closed=closed, side_sort=side))
        elif prefix == "new":
            folder = normalize_ref(query.get("folder", [""])[0])
            self.send(shell(
                f"새 글 - {name}", edit_body("", folder), EDITOR_SCRIPT,
                closed=closed, side_sort=side, body_class="editing",
            ))
        elif prefix == "search":
            keyword = query.get("q", [""])[0].strip()
            self.send(shell(
                f"검색 - {name}", search_body(keyword, self.page_number(query)), LIST_SCRIPT,
                query=keyword, closed=closed, side_sort=side,
            ))
        elif prefix == "settings":
            self.send(shell(f"설정 - {name}", settings_body(), SETTINGS_SCRIPT, closed=closed, side_sort=side))
        elif prefix == "trash":
            self.send(shell(f"휴지통 - {name}", trash_screen(), TRASH_SCRIPT, closed=closed, side_sort=side))
        elif prefix == "tree":
            folder = normalize_ref(query.get("folder", [""])[0])
            rows = sidebar_rows(folder, folder.count("/") + 1, "", "", closed - {folder}, side)
            self.send("".join(rows).encode("utf-8"))
        elif prefix in ("w", "e"):
            ref = normalize_ref(rest)
            if not is_valid_ref(ref):
                self.send_text(400, "잘못된 문서 이름입니다.")
            elif prefix == "w":
                ref = resolve_ref(ref)
                self.send(shell(
                    f"{title_of(ref)} - {name}", view_body(ref), VIEW_SCRIPT,
                    current_ref=ref, closed=closed, side_sort=side,
                ))
            else:
                self.send(shell(
                    f"{title_of(ref)} 편집", edit_body(ref), EDITOR_SCRIPT,
                    current_ref=ref, closed=closed, side_sort=side, body_class="editing",
                ))
        elif prefix == "f":
            self.serve_file(rest)
        else:
            self.send_text(404, "없는 주소입니다.")

    def do_POST(self):
        prefix, rest, _ = self.split_path()

        if prefix == "save":
            self.save_page()
        elif prefix == "move":
            self.move_page()
        elif prefix == "delete" and rest == "plan":
            self.drop_plan()
        elif prefix == "delete":
            self.remove_page()
        elif prefix == "trash":
            self.change_trash(rest)
        elif prefix == "open":
            # 다른 사이트가 몰래 파일을 실행시키지 못하도록 요청이 이 위키에서 왔는지 봅니다.
            origin = self.headers.get("Origin") or self.headers.get("Referer") or ""
            if origin and not re.match(r"^https?://(localhost|127\.0\.0\.1|\[::1\])[:/]", origin):
                self.send_text(403, "이 위키에서 온 요청이 아닙니다.")
                return
            data = self.read_json()
            try:
                line = int(data.get("line") or 0)
            except ValueError:
                line = 0
            self.reply(open_local(data.get("path", ""), line, read_config().get("editor", "auto")),
                       data.get("path", ""))
        elif prefix == "note":
            data = self.read_json()
            ref = normalize_ref(data.get("ref", ""))
            note = data.get("note", "").strip()
            if not page_exists(ref):
                self.send_text(400, "없는 문서입니다.")
            elif rest == "remove":
                self.reply(change_note(ref, data.get("quote", ""), None), ref)
            elif not note:
                self.send_text(400, "메모 내용을 적어 주세요.")
            elif rest == "edit":
                self.reply(change_note(ref, data.get("quote", ""), note), ref)
            else:
                self.reply(add_note(ref, data.get("quote", ""), note), ref)
        elif prefix == "comment":
            data = self.read_json()
            ref = normalize_ref(data.get("ref", ""))
            text = data.get("text", "").strip()
            if not page_exists(ref):
                self.send_text(400, "없는 문서입니다.")
            elif rest == "remove":
                self.reply(change_comment(ref, data.get("at", ""), None), ref)
            elif not text:
                self.send_text(400, "댓글 내용을 적어 주세요.")
            elif rest == "edit":
                self.reply(change_comment(ref, data.get("at", ""), text), ref)
            else:
                self.reply(add_comment(ref, text), ref)
        elif prefix == "order":
            self.save_order()
        elif prefix == "preview":
            self.send_json({"html": render(self.read_json().get("text", ""), highlight=False)})
        elif prefix == "tomarkdown":
            self.send_json({"text": to_markdown(self.read_json().get("html", ""))})
        elif prefix == "folder":
            self.change_folder(rest)
        elif prefix == "settings" and rest == "data":
            self.change_data_dir()
        elif prefix == "settings":
            data = self.read_json()
            name = data.get("name", "").strip()
            home = normalize_ref(data.get("home", ""))
            if not name:
                self.send_text(400, "위키 이름을 입력해 주세요.")
            elif home and not page_exists(home):
                self.send_text(400, f"‘{home}’ 글이 없습니다. 있는 글 이름을 넣어 주세요.")
            else:
                editor = data.get("editor", "auto")
                write_config({
                    "name": name[:40], "home": home,
                    "editor": editor if editor in dict(EDITORS) else "auto",
                })
                self.send_json({"name": wiki_name(), "home": home})
        elif prefix == "upload":
            self.save_upload()
        else:
            self.send_text(404, "없는 주소입니다.")

    def save_page(self):
        data = self.read_json()
        title = data.get("title", "").strip()
        folder = normalize_ref(data.get("folder", ""))
        original = normalize_ref(data.get("original", ""))
        if not is_valid_name(title):
            self.send_text(400, r'제목에 \ / : * ? " < > | 는 쓸 수 없습니다.')
            return
        if folder and not is_valid_ref(folder):
            self.send_text(400, r'폴더 이름에 \ : * ? " < > | 는 쓸 수 없습니다.')
            return
        ref = f"{folder}/{title}" if folder else title
        if ref != original and page_exists(ref):
            self.send_text(400, f"‘{ref}’ 문서가 이미 있습니다. 다른 제목이나 폴더를 써 주세요.")
            return
        write_page(ref, data.get("text", ""))
        if original and original != ref:
            if not move_children(original, ref):
                self.send_text(400, f"‘{ref}’ 아래에 이미 다른 글이 있어 옮기지 못했습니다.")
                return
            delete_page(original)
            move_comments(original, ref)
            relink(original, ref)
        self.send_json({"ref": ref})

    def move_page(self):
        """글을 다른 폴더로 옮기거나 제목을 바꿉니다. 준 값만 바뀝니다."""
        data = self.read_json()
        ref = normalize_ref(data.get("ref", ""))
        if not page_exists(ref):
            self.send_text(400, "없는 문서입니다.")
            return
        folder = normalize_ref(data["folder"]) if "folder" in data else folder_of(ref)
        title = data.get("title", "").strip() or title_of(ref)
        if folder and not is_valid_ref(folder):
            self.send_text(400, r'폴더 이름에 \ : * ? " < > | 는 쓸 수 없습니다.')
            return
        if not is_valid_name(title):
            self.send_text(400, r'제목에 \ / : * ? " < > | 는 쓸 수 없습니다.')
            return
        new_ref = f"{folder}/{title}" if folder else title
        if new_ref != ref:
            if page_exists(new_ref):
                self.send_text(400, f"‘{new_ref}’ 문서가 이미 있습니다. 다른 폴더를 골라 주세요.")
                return
            write_page(new_ref, read_page(ref))
            if not move_children(ref, new_ref):
                page_path(new_ref).unlink(missing_ok=True)
                self.send_text(400, f"‘{new_ref}’ 아래에 이미 다른 글이 있어 옮기지 못했습니다.")
                return
            delete_page(ref)
            move_comments(ref, new_ref)
            relink(ref, new_ref)
        self.send_json({"ref": new_ref})

    def remove_page(self):
        """글을 아래 딸린 글까지 함께 휴지통으로 보냅니다."""
        ref = normalize_ref(self.read_json().get("ref", ""))
        if not page_exists(ref):
            self.send_text(400, "없는 문서입니다.")
            return
        done, message = to_trash(ref, "page")
        if not done:
            self.send_text(400, message)
            return
        self.send_json({"folder": folder_of(ref), "message": message})

    def drop_plan(self):
        """지우기 전에 무엇이 함께 지워지는지 알려 줍니다."""
        data = self.read_json()
        folder = normalize_ref(data.get("folder", ""))
        ref = folder or normalize_ref(data.get("ref", ""))
        if not ref:
            self.send_text(400, "무엇을 지울지 알 수 없습니다.")
            return
        if folder:
            if not folder_path(folder).is_dir():
                self.send_text(400, f"‘{folder}’ 폴더가 없습니다.")
                return
        elif not page_exists(ref):
            self.send_text(400, "없는 문서입니다.")
            return
        self.send_json({
            "ref": ref, "kind": "folder" if folder else "page",
            "pages": pages_under(ref), "folders": folders_under(ref),
        })

    def change_trash(self, act: str):
        data = self.read_json() if act != "empty" else {}
        entry = str(data.get("id", ""))
        if act == "restore":
            self.reply(from_trash(entry), "")
        elif act == "drop":
            self.reply(drop_trash(entry), "")
        elif act == "empty":
            self.reply(empty_trash(), "")
        else:
            self.send_text(404, "없는 주소입니다.")

    def change_data_dir(self):
        raw = self.read_json().get("path", "").strip()
        target = Path(raw).expanduser() if raw else ROOT
        if raw and not target.is_absolute():
            self.send_text(400, "폴더는 전체 경로로 적어 주세요. 예: C:\\Users\\나\\OneDrive\\wiki")
            return
        moved, message = move_data_to(target)
        if moved:
            self.send_json({"data": str(DATA), "message": message})
        else:
            self.send_text(400, message)

    def save_order(self):
        data = self.read_json()
        folder = normalize_ref(data.get("folder", ""))
        if not folder_path(folder).is_dir():
            self.send_text(400, "없는 폴더입니다.")
            return
        items = []
        for name in data.get("names", []):
            is_folder = name.endswith("/")
            items.append((name[:-1] if is_folder else name, is_folder))
        write_order(folder, items)
        self.send_json({"folder": folder, "count": len(items)})

    def change_folder(self, action: str):
        data = self.read_json()
        folder = normalize_ref(data.get("folder", ""))
        to = normalize_ref(data.get("to", ""))

        if action == "create":
            if not is_valid_ref(folder):
                self.send_text(400, r'폴더 이름에 \ : * ? " < > | 는 쓸 수 없습니다.')
                return
            self.reply(create_folder(folder), folder)
            return

        if folder not in list_folders():
            self.send_text(400, "없는 폴더입니다.")
        elif action == "delete":
            self.reply(delete_folder(folder), "")
        elif action != "rename":
            self.send_text(404, "없는 주소입니다.")
        elif not is_valid_ref(to):
            self.send_text(400, r'폴더 이름에 \ : * ? " < > | 는 쓸 수 없습니다.')
        elif to == folder or to.startswith(folder + "/"):
            self.send_text(400, "폴더를 자기 자신 아래로 옮길 수는 없습니다.")
        else:
            self.reply(rename_folder(folder, to), to)

    def reply(self, result: tuple[bool, str], folder: str):
        done, message = result
        if done:
            self.send_json({"folder": folder, "message": message})
        else:
            self.send_text(400, message)

    def save_upload(self):
        length = int(self.headers.get("Content-Length", 0))
        name = urllib.parse.unquote(self.headers.get("X-Filename", "file"))
        _, _, query = self.split_path()
        saved = store_upload(name, self.rfile.read(length), replace="replace" in query)
        link = "/f/" + urllib.parse.quote(saved)
        is_image = (mimetypes.guess_type(saved)[0] or "").startswith("image/")
        snippet = f"![{saved}]({link})" if is_image else f"[{saved}]({link})"
        self.send_json({"name": saved, "markdown": snippet})

    def serve_file(self, name: str):
        target = (FILES / Path(name).name).resolve()
        if not target.is_file() or FILES.resolve() not in target.parents:
            self.send_text(404, "없는 파일입니다.")
            return
        content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send(target.read_bytes(), 200, content_type)


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class Server6(Server):
    # localhost 는 ::1(IPv6)로 먼저 풀리는 일이 많습니다. 한쪽만 듣고 있으면 브라우저가
    # 실패한 뒤 다시 붙느라 요청마다 몇 초씩 버리므로, 양쪽을 모두 듣습니다.
    address_family = socket.AF_INET6


def main():
    # pythonw 처럼 콘솔 없이 띄우면 출력 통로가 없어 로그를 쓰다가 죽습니다.
    if sys.stdout is None or sys.stderr is None:
        sys.stdout = sys.stderr = open(os.devnull, "w", encoding="utf-8")

    args = sys.argv[1:]
    data = ""
    if "--data" in args:
        at = args.index("--data")
        data = args[at + 1] if at + 1 < len(args) else ""
        del args[at:at + 2]
    port = int(args[0]) if args else DEFAULT_PORT

    use_data_dir(Path(data).expanduser() if data else saved_data_dir())
    if not list_pages():  # 글이 하나도 없을 때만 안내글을 놓아 둡니다
        write_page(HOME, WELCOME)

    url = f"http://localhost:{port}/"
    print(f"위키 저장 위치: {DATA}")
    print(f"주소: {url}   (종료: Ctrl+C)")

    servers = []
    for host, kind in (("127.0.0.1", Server), ("::1", Server6)):
        try:
            servers.append(kind((host, port), Handler))
        except OSError as error:
            print(f"{host} 는 듣지 못합니다: {error}")
    if not servers:
        raise SystemExit(f"{port} 포트를 열 수 없습니다.")

    for httpd in servers:
        threading.Thread(target=httpd.serve_forever, daemon=True).start()
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    try:
        run_tray(url)
    except KeyboardInterrupt:
        print("\n종료합니다.")
    finally:
        for httpd in servers:
            httpd.shutdown()


def run_tray(url: str) -> None:
    """트레이(작업 표시줄 오른쪽 아래)에 아이콘을 띄웁니다. 없으면 그냥 계속 돕니다."""
    try:
        import pystray
        from PIL import Image, ImageDraw
    except ImportError:
        threading.Event().wait()   # 트레이를 못 쓰면 Ctrl+C 로 끕니다
        return

    image = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    pen = ImageDraw.Draw(image)
    pen.rounded_rectangle((6, 8, 58, 56), radius=8, fill=(9, 105, 218))
    pen.rectangle((30, 8, 34, 56), fill=(255, 255, 255))
    for line in range(3):
        pen.rectangle((12, 18 + line * 10, 27, 21 + line * 10), fill=(255, 255, 255))
        pen.rectangle((37, 18 + line * 10, 52, 21 + line * 10), fill=(255, 255, 255))

    icon = pystray.Icon(
        "wiki", image, f"{wiki_name()} — {url}",
        menu=pystray.Menu(
            pystray.MenuItem("위키 열기", lambda: webbrowser.open(url), default=True),
            pystray.MenuItem("저장 폴더 열기", lambda: os.startfile(DATA)),
            pystray.MenuItem("끝내기", lambda tray: tray.stop()),
        ),
    )
    icon.run()


WELCOME = """로컬 위키에 오신 것을 환영합니다. 이 문서도 편집 버튼으로 자유롭게 고칠 수 있습니다.

## 쓰는 법

- **새 글 쓰기** 를 누르면 폴더·제목·본문을 바로 쓸 수 있습니다.
- 폴더는 `개발/서버` 처럼 `/` 로 여러 단계를 만들 수 있고, 비워 두면 폴더 없이 저장됩니다.
- 왼쪽 목록에서 폴더와 문서를 한눈에 보고 옮겨 다닐 수 있습니다. 왼쪽 위 `☰` 로 목록을
  접었다 펼 수 있고, 접은 상태는 다음에 열 때도 그대로 유지됩니다.
- 왼쪽 목록에서는 문서와 폴더를 끌어다 놓아 옮길 수 있습니다. 폴더 **가운데**에 놓으면 그
  안으로 들어가고, 줄의 **위아래 가장자리**에 놓으면 그 자리로 순서가 바뀝니다. 목록 빈 곳에
  놓으면 맨 바깥으로 나옵니다.
- 정한 순서는 각 폴더의 `.order` 파일에 저장되어 왼쪽 목록과 문서 목록에 그대로 쓰입니다.
- 왼쪽 목록의 폴더 줄에 마우스를 올리면 `＋`(하위 폴더 추가), `✎`(이름·위치 바꾸기),
  `✕`(삭제) 단추가 나옵니다. 목록 맨 위 **＋ 폴더** 는 맨 바깥에 폴더를 만듭니다.
- 폴더 이름을 바꾸면 그 안의 문서와 하위 폴더가 통째로 따라 옮겨지고, 다른 글에 걸린 링크도
  함께 고쳐집니다.
- 이미 쓴 글은 문서 화면 오른쪽 위 **글 수정** 으로 고치고, **폴더 이동** 으로 다른 폴더로
  옮기고, **삭제** 로 지웁니다. 편집 화면에서 제목이나 폴더를 바꿔 저장해도 문서가 그대로
  옮겨집니다.
- 지운 글과 폴더는 **휴지통**(오른쪽 위 `🗑`)으로 갑니다. 아래에 딸린 글이 있으면 함께
  지워지고, 지우기 전에 무엇이 함께 지워지는지 팝업으로 알려 줍니다. 휴지통에서 **되살리기**
  를 누르면 원래 자리로 돌아옵니다. **완전히 지우기** 는 되돌릴 수 없습니다.
- 글끼리 연결은 편집 화면의 **글 연결** 단추 하나로 합니다. 이미 있는 글은 목록에서 골라 링크만
  걸고, 목록에 없는 이름을 쓰면 그 글을 새로 만들어 링크를 겁니다. 글자를 골라 두고 누르면 그
  글자에 링크가 걸립니다. 직접 쓰려면 `[[문서 이름]]`, `[[폴더/문서 이름]]`, `[[문서|보일 글자]]`
  형식도 그대로 됩니다. 아직 없는 문서는 빨간 링크로 보이고, 누르면 바로 만들어집니다.
- 링크가 걸린 글자도 그대로 고칠 수 있습니다. 글자만 바뀌고 가리키는 글은 그대로 남습니다.
- 외부 주소는 `[이름](https://example.com)` 형식의 보통 마크다운으로 씁니다.
- **내 PC 파일** 단추로 내 컴퓨터의 파일이나 코드 위치(`파일:줄번호`)로 가는 링크를 넣습니다.
- 글을 읽다가 글자를 고르면 **📝 메모 붙이기** 가 나옵니다. 메모를 붙인 자리는 노랗게 표시되고,
  눌러서 보고 고칠 수 있습니다.
- 글 맨 아래에는 **댓글** 을 남길 수 있습니다. 마크다운을 그대로 쓸 수 있고 `Ctrl+Enter` 로
  등록됩니다. 올린 댓글은 `✎` 로 고치고 `✕` 로 지웁니다. 글을 옮겨도 댓글은 따라갑니다.
- 목록은 **목록**·**숫자 목록** 단추로 넣고, Enter 로 다음 항목이 이어집니다. 빈 항목에서 Enter
  를 누르면 목록이 끝납니다. 인용도 같습니다.
- 표 안에서는 화살표로 칸 사이를 오갑니다. 위아래는 바로, 좌우는 글자 끝에 닿으면 옆 칸으로
  넘어가고, 맨 윗줄·맨 아랫줄에서 더 누르면 표 밖으로 나옵니다.
- Enter 를 여러 번 누르면 누른 만큼 줄이 띄워집니다.
- 편집 화면에 이미지나 파일을 드래그해서 놓거나, 스크린샷을 그대로 붙여넣으면
  `files/` 폴더에 저장되고 본문에 링크가 삽입됩니다.
- **그림** 단추로 자유선·직선·사각형·타원·화살표를 그려 넣을 수 있습니다. 그림은 SVG 한 장으로
  저장되고, 서식 편집 모드에서 그림을 두 번 누르면 다시 열어 고칠 수 있습니다.
- 오른쪽 위 검색창에서 제목과 본문을 함께 찾습니다.
- 위키 이름과 **데이터 폴더**는 오른쪽 위 ⚙ 에서 바꿉니다. 데이터 폴더를 OneDrive 같은
  동기화 폴더로 지정하면 다른 기기에서도 같은 내용을 보고 고칠 수 있습니다.
- 편집 화면 위쪽에서 **마크다운**(원본을 그대로 고치기)과 **서식 편집**(꾸며진 화면에서
  바로 고치기) 두 모드를 오갈 수 있습니다. 어느 쪽에서 고쳐도 파일은 마크다운으로 저장됩니다.
- 저장 단추는 편집 화면 오른쪽 위에 있습니다. `Ctrl+S` 로도 저장되고, **게시** 는 저장한 뒤
  문서 화면으로 나갑니다.

## 시작하기

- [[메모]] — 링크를 눌러 첫 문서를 만들어 보세요.
"""


if __name__ == "__main__":
    main()
