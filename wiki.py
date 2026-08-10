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
import re
import shutil
import socketserver
import sys
import threading
import urllib.parse
import webbrowser
from datetime import datetime
from pathlib import Path
from xml.etree import ElementTree

import markdown
from markdown.extensions import Extension
from markdown.inlinepatterns import InlineProcessor

ROOT = Path(__file__).resolve().parent
PAGES = ROOT / "pages"
FILES = ROOT / "files"
CONFIG = ROOT / "config.json"
HOME = "홈"
ORDER_FILE = ".order"
DEFAULT_NAME = "위키"
DEFAULT_PORT = 8800

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\x00-\x1f]')
WIKILINK_RE = r"\[\[([^\[\]]+?)\]\]"


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
    matches = [name for name, _ in list_pages() if title_of(name) == ref]
    return matches[0] if len(matches) == 1 else ref


# ---------------------------------------------------------------- 저장소

def read_page(ref: str) -> str:
    path = page_path(ref)
    return path.read_text(encoding="utf-8") if page_exists(ref) else ""


def write_page(ref: str, text: str) -> None:
    path = page_path(ref)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8", newline="\n")


def delete_page(ref: str) -> None:
    page_path(ref).unlink(missing_ok=True)


def list_pages() -> list[tuple[str, datetime]]:
    items = []
    for path in PAGES.rglob("*.md"):
        parts = path.relative_to(PAGES).parts
        ref = "/".join(parts[:-1] + (path.stem,))
        items.append((ref, datetime.fromtimestamp(path.stat().st_mtime)))
    return sorted(items, key=lambda item: item[1], reverse=True)


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


def children_of(folder: str) -> list[tuple[str, bool]]:
    """폴더 바로 아래의 하위 폴더와 문서를 저장된 순서대로 돌려줍니다."""
    base = folder_path(folder)
    if not base.is_dir():
        return []
    known = [(path.name, True) for path in sorted(base.iterdir()) if path.is_dir()]
    known += [(path.stem, False) for path in sorted(base.glob("*.md"))]
    saved = read_order(folder)
    ordered = [item for item in saved if item in known]
    return ordered + [item for item in known if item not in saved]


def ordered_refs() -> list[str]:
    """모든 문서를 저장된 표시 순서대로 나열합니다."""
    refs = []

    def walk(folder: str) -> None:
        for name, is_folder in children_of(folder):
            path = f"{folder}/{name}" if folder else name
            if is_folder:
                walk(path)
            else:
                refs.append(path)

    walk("")
    return refs


def list_folders() -> list[str]:
    folders = ["/".join(path.relative_to(PAGES).parts) for path in PAGES.rglob("*") if path.is_dir()]
    return sorted(folders, key=lambda folder: folder.split("/"))


def in_folder(ref: str, folder: str) -> bool:
    return not folder or ref.startswith(folder + "/")


def create_folder(folder: str) -> tuple[bool, str]:
    path = folder_path(folder)
    if path.exists():
        return False, f"‘{folder}’ 폴더가 이미 있습니다."
    path.mkdir(parents=True)
    return True, f"‘{folder}’ 폴더를 만들었습니다."


def rename_folder(folder: str, to: str) -> tuple[bool, str]:
    """폴더 안의 문서와 하위 폴더를 통째로 새 경로로 옮깁니다."""
    source, target = folder_path(folder), folder_path(to)
    if target.exists():
        return False, f"‘{to}’ 폴더가 이미 있습니다. 다른 이름을 써 주세요."
    target.parent.mkdir(parents=True, exist_ok=True)
    source.rename(target)
    return True, f"‘{folder}’ 폴더를 ‘{to}’ 로 옮겼습니다."


def delete_folder(folder: str) -> tuple[bool, str]:
    """문서가 남아 있지 않은 폴더만 지웁니다."""
    inside = [ref for ref, _ in list_pages() if in_folder(ref, folder)]
    if inside:
        return False, f"‘{folder}’ 안에 문서가 {len(inside)}개 있습니다. 먼저 옮기거나 지워 주세요."
    shutil.rmtree(folder_path(folder))
    return True, f"‘{folder}’ 폴더를 지웠습니다."


def store_upload(filename: str, data: bytes) -> str:
    """첨부를 files/ 에 저장하고 실제 저장된 파일명을 반환합니다."""
    name = INVALID_CHARS.sub("_", Path(filename).name) or "file"
    target = FILES / name
    stem, suffix = Path(name).stem, Path(name).suffix
    index = 1
    while target.exists():
        target = FILES / f"{stem}-{index}{suffix}"
        index += 1
    target.write_bytes(data)
    return target.name


def wiki_name() -> str:
    try:
        return json.loads(CONFIG.read_text(encoding="utf-8")).get("name") or DEFAULT_NAME
    except (OSError, ValueError):
        return DEFAULT_NAME


def set_wiki_name(name: str) -> None:
    CONFIG.write_text(
        json.dumps({"name": name}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


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
        if page_exists(ref):
            element.set("href", "/w/" + urllib.parse.quote(ref))
        else:
            element.set("href", "/e/" + urllib.parse.quote(ref))
            element.set("class", "new")
            element.set("title", "아직 없는 문서입니다. 누르면 새로 만듭니다.")
        return element, m.start(0), m.end(0)


class WikiLinkExtension(Extension):
    def extendMarkdown(self, md):
        md.inlinePatterns.register(WikiLinkProcessor(WIKILINK_RE, md), "wikilink", 170)


MD = markdown.Markdown(
    extensions=["extra", "sane_lists", "nl2br", "toc", WikiLinkExtension()],
    output_format="html",
)


MD_LOCK = threading.Lock()


def render(text: str) -> str:
    with MD_LOCK:
        MD.reset()
        return MD.convert(text)


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
html { --head: 61px; }
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
  width: 260px; flex: none; padding: 16px 8px; border-right: 1px solid var(--line);
  position: sticky; top: var(--head); height: calc(100vh - var(--head)); overflow-y: auto;
}
html.nosidebar aside { display: none; }
@media (max-width: 820px) { aside { display: none; } }
aside .side-head {
  display: flex; align-items: center; justify-content: space-between;
  font-size: 13px; color: var(--muted); padding: 0 8px 8px;
}
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
aside .none { padding: 8px; font-size: 13px; color: var(--muted); }
main { flex: 1; min-width: 0; max-width: 900px; margin: 0 auto; padding: 24px; }
a { color: var(--accent); }
a.new { color: var(--new); border-bottom: 1px dashed var(--new); text-decoration: none; }
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
.btn.danger:hover { border-color: var(--new); color: var(--new); }
.meta { color: var(--muted); font-size: 13px; }
.crumbs { font-size: 14px; margin-bottom: 10px; }
.crumbs a { text-decoration: none; }
.group { margin-top: 26px; }
.group h2 { font-size: 15px; color: var(--muted); border: none; padding: 0; margin: 0; }
.list { list-style: none; padding: 0; margin: 6px 0 0; }
.list li { padding: 12px 4px; border-bottom: 1px solid var(--line); }
.list .row { display: flex; justify-content: space-between; gap: 12px; }
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
.mdbar { display: flex; gap: 6px; margin-bottom: 8px; flex-wrap: wrap; }
.mdbar .btn { padding: 4px 10px; font-size: 13px; }
.tabs { display: flex; gap: 6px; margin-bottom: 8px; }
#preview {
  min-height: 55vh; padding: 14px 18px; border: 1px solid var(--line); border-radius: 8px;
  overflow-x: auto;
}
#preview > :first-child { margin-top: 0; }
#preview:empty::before { content: "쓴 내용이 없습니다."; color: var(--muted); }
.toolbar { display: flex; align-items: center; gap: 10px; margin: 14px 0; flex-wrap: wrap; }
.toolbar .spacer { flex: 1; }
.hint { color: var(--muted); font-size: 13px; }
input[type=text], input[type=search] {
  padding: 6px 10px; border: 1px solid var(--line); border-radius: 6px;
  background: var(--bg); color: var(--fg); font: inherit;
}
"""


def sidebar(current_ref: str = "", current_folder: str = "") -> str:
    def render(parent: str, depth: int) -> list[str]:
        rows = []
        for name, is_folder in children_of(parent):
            path = f"{parent}/{name}" if parent else name
            common = (
                f'<div class="item" draggable="true" '
                f'data-parent="{html.escape(parent, quote=True)}" '
                f'data-name="{html.escape(name, quote=True)}" '
            )
            pad = f'style="padding-left:{8 + depth * 14}px"'
            if is_folder:
                state = " on" if path == current_folder else ""
                rows.append(
                    f'{common}data-folder="{html.escape(path, quote=True)}">'
                    f'<a class="folder{state}" draggable="false" {pad} '
                    f'href="{folder_link(path)}">📁 {html.escape(name)}</a>'
                    '<span class="acts">'
                    '<button data-act="add" title="하위 폴더 추가">＋</button>'
                    '<button data-act="rename" title="폴더 이름·위치 바꾸기">✎</button>'
                    '<button data-act="remove" title="폴더 삭제">✕</button>'
                    "</span></div>"
                )
                rows += render(path, depth + 1)
            else:
                state = " on" if path == current_ref else ""
                rows.append(
                    f'{common}data-ref="{html.escape(path, quote=True)}" '
                    f'data-folder="{html.escape(parent, quote=True)}">'
                    f'<a class="doc{state}" draggable="false" {pad} '
                    f'href="/w/{urllib.parse.quote(path)}">{html.escape(name)}</a></div>'
                )
        return rows

    body = "".join(render("", 0)) or '<div class="none">아직 문서가 없습니다.</div>'
    return (
        '<div class="side-head"><span>문서 목록</span>'
        '<button class="btn" id="side-create" style="padding:2px 8px" '
        'title="새 폴더 만들기">＋ 폴더</button></div>' + body
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
  if (!confirm('‘' + folder + '’ 폴더를 지울까요? 안에 문서가 남아 있으면 지워지지 않습니다.')) {
    return;
  }
  if (await post('/folder/delete', {folder: folder})) { goFolder(''); }
}

async function movePage(ref, folder) {
  const to = prompt('옮길 폴더 경로를 넣어 주세요. 비워 두면 폴더 없이 옮깁니다.', folder);
  if (to === null) { return; }
  const moved = await post('/move', {ref: ref, folder: to});
  if (moved) { location.href = '/w/' + encodeURIComponent(moved.ref); }
}
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

const aside = document.querySelector('aside');

document.getElementById('side-create').onclick = () => createFolder('');
aside.onclick = (e) => {
  const button = e.target.closest('button[data-act]');
  if (!button) { return; }
  const item = button.closest('.item');
  const act = button.dataset.act;
  if (act === 'add') { createFolder(item.dataset.folder); }
  else if (act === 'rename') { renameFolder(item.dataset.folder); }
  else if (act === 'remove') { removeFolder(item.dataset.folder); }
};

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
  if (!item.dataset.ref && offset > 0.25 && offset < 0.75) { return {mode: 'inside', item: item}; }
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

async function reorder(parent, target, mode) {
  const siblings = [...aside.querySelectorAll('.item')]
    .filter((el) => el.dataset.parent === parent && el !== dragged)
    .map(itemKey);
  const at = target ? siblings.indexOf(itemKey(target)) + (mode === 'after' ? 1 : 0)
                    : siblings.length;
  siblings.splice(at < 0 ? siblings.length : at, 0, itemKey(dragged));
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
  const item = dragged;
  const spot = dropSpot(e);
  clearDrop();
  if (!item) { return; }

  const parent = spot.mode === 'inside' ? spot.item.dataset.folder
               : spot.mode === 'root' ? '' : spot.item.dataset.parent;
  if (!await dropInto(item, parent)) { return; }
  if (spot.mode !== 'inside') { await reorder(parent, spot.item, spot.mode); }
  else { await reorder(parent, null, 'after'); }

  const ref = item.dataset.ref;
  if (ref && item.querySelector('a.on')) {
    const name = item.dataset.name;
    location.href = '/w/' + encodeURIComponent(parent ? parent + '/' + name : name);
  } else {
    location.reload();
  }
});
"""


def shell(
    title: str, body: str, script: str = "", query: str = "",
    current_ref: str = "", current_folder: str = "",
) -> bytes:
    page = f"""<!doctype html>
<html lang="ko"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>{html.escape(title)}</title><style>{CSS}</style>
<script>
if (localStorage.getItem('sidebar') === 'off') {{
  document.documentElement.classList.add('nosidebar');
}}
</script>
</head><body>
<header>
  <button class="btn" id="toggle" title="문서 목록 접기/펴기">☰</button>
  <a class="brand" href="/w/{urllib.parse.quote(HOME)}">📚 {html.escape(wiki_name())}</a>
  <a class="btn" href="/">문서 목록</a>
  <a class="btn primary" href="/new">새 글 쓰기</a>
  <span class="spacer"></span>
  <form action="/search" method="get" style="display:flex;gap:6px">
    <input type="search" name="q" placeholder="제목·본문 검색"
           value="{html.escape(query, quote=True)}">
    <button class="btn" type="submit">검색</button>
  </form>
  <a class="btn" href="/settings" title="위키 이름 바꾸기">⚙</a>
</header>
<div class="layout">
  <aside>{sidebar(current_ref, current_folder)}</aside>
  <main>{body}</main>
</div>
<script>{COMMON_SCRIPT}{LAYOUT_SCRIPT}{script}</script>
</body></html>"""
    return page.encode("utf-8")


def folder_link(folder: str) -> str:
    return "/?folder=" + urllib.parse.quote(folder)


def crumbs(folder: str) -> str:
    trail = ['<a href="/">문서 목록</a>']
    parts = folder.split("/") if folder else []
    for depth, part in enumerate(parts, start=1):
        path = "/".join(parts[:depth])
        trail.append(f'<a href="{folder_link(path)}">{html.escape(part)}</a>')
    return '<div class="crumbs">' + " / ".join(trail) + "</div>"


def entry_rows(
    entries: list[tuple[str, datetime, list[str]]], query: str = "", show_folder: bool = False
) -> str:
    rows = []
    for ref, when, snippets in entries:
        folder = folder_of(ref)
        where = (
            f'<div class="meta">📁 <a href="{folder_link(folder)}">{html.escape(folder)}</a></div>'
            if show_folder and folder else ""
        )
        snippet_html = "".join(
            f'<div class="snippet">{highlight(line, query)}</div>' for line in snippets
        )
        rows.append(
            f'<li>{where}<div class="row">'
            f'<a href="/w/{urllib.parse.quote(ref)}">{highlight(title_of(ref), query)}</a>'
            f'<span class="meta">{when:%Y-%m-%d %H:%M}</span></div>{snippet_html}</li>'
        )
    return f'<ul class="list">{"".join(rows)}</ul>'


def grouped_rows(pages: list[tuple[str, datetime]]) -> str:
    groups: dict[str, list] = {}
    for ref, when in pages:
        groups.setdefault(folder_of(ref), []).append((ref, when, []))
    blocks = []
    for folder in groups:
        label = f"📁 {html.escape(folder)}" if folder else "폴더 없음"
        heading = (
            f'<h2><a href="{folder_link(folder)}">{label}</a></h2>' if folder
            else f"<h2>{label}</h2>"
        )
        blocks.append(f'<div class="group">{heading}{entry_rows(groups[folder])}</div>')
    return "".join(blocks)


def index_body(folder: str) -> str:
    changed = dict(list_pages())
    pages = [(ref, changed[ref]) for ref in ordered_refs() if in_folder(ref, folder)]
    quoted = urllib.parse.quote(folder)
    heading = (
        f'<h1>📁 {html.escape(folder)} <span class="meta">({len(pages)}개)</span></h1>' if folder
        else f'<h1>문서 목록 <span class="meta">({len(pages)}개)</span></h1>'
    )
    tools = [f'<button class="btn" id="create">{"하위 폴더" if folder else "폴더"} 추가</button>']
    if folder:
        tools.insert(0, f'<a class="btn" href="/new?folder={quoted}">이 폴더에 새 글 쓰기</a>')
        tools.append('<button class="btn" id="rename">폴더 이름 바꾸기</button>')
        tools.append('<button class="btn" id="remove">폴더 삭제</button>')
    toolbar = (
        f'<div class="toolbar" id="folder-tools" '
        f'data-folder="{html.escape(folder, quote=True)}">{"".join(tools)}</div>'
    )
    if not pages:
        target = f"/new?folder={quoted}" if folder else "/new"
        listing = (
            f'<p class="empty">아직 문서가 없습니다. <a href="{target}">글을 써 보세요.</a></p>'
        )
    else:
        listing = grouped_rows(pages)
    return f"{crumbs(folder) if folder else ''}{heading}{toolbar}{listing}"


INDEX_SCRIPT = """
const openFolder = document.getElementById('folder-tools').dataset.folder;
document.getElementById('create').onclick = () => createFolder(openFolder);
const renameButton = document.getElementById('rename');
if (renameButton) {
  renameButton.onclick = () => renameFolder(openFolder);
  document.getElementById('remove').onclick = () => removeFolder(openFolder);
}
"""


def search_body(query: str) -> str:
    if not query:
        return '<h1>검색</h1><p class="empty">위쪽 검색창에 찾을 말을 넣어 주세요.</p>'
    found = search_pages(query)
    head = f'<h1>‘{html.escape(query)}’ 검색 결과 <span class="meta">({len(found)}개)</span></h1>'
    if not found:
        return (
            f'{head}<p class="empty">찾은 문서가 없습니다. '
            f'<a href="/new">이 내용으로 새 글을 써 보세요.</a></p>'
        )
    return head + entry_rows(found, query, show_folder=True)


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
        f'<button class="btn danger" id="remove">삭제</button>'
        f'<button class="btn" id="move">폴더 이동</button>'
        f'<a class="btn primary" href="/e/{quoted}">글 수정</a></div>'
        f"<h1>{html.escape(title_of(ref))}</h1>{render(read_page(ref))}"
    )


VIEW_SCRIPT = """
const pageTools = document.getElementById('page-tools');
if (pageTools) {
  document.getElementById('move').onclick =
    () => movePage(pageTools.dataset.ref, pageTools.dataset.folder);
  document.getElementById('remove').onclick = async () => {
    if (!confirm('‘' + pageTools.dataset.ref + '’ 글을 지울까요? 되돌릴 수 없습니다.')) { return; }
    const removed = await post('/delete', {ref: pageTools.dataset.ref});
    if (removed) { goFolder(removed.folder); }
  };
}
"""


MD_BUTTONS = [
    ("제목", 'data-prefix="## "'),
    ("굵게", 'data-wrap="**" data-hint="굵은 글씨"'),
    ("기울임", 'data-wrap="*" data-hint="기울인 글씨"'),
    ("코드", 'data-wrap="`" data-hint="코드"'),
    ("목록", 'data-prefix="- "'),
    ("인용", 'data-prefix="&gt; "'),
    ("링크", 'data-snippet="[보일 글자](https://)"'),
    ("문서 링크", 'data-snippet="[[문서 이름]]"'),
]

EDITOR_SCRIPT = """
const editor = document.getElementById('editor');
const titleInput = document.getElementById('title');
const folderInput = document.getElementById('folder');
const status = document.getElementById('status');
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

function prefixLine(prefix) {
  const start = editor.value.lastIndexOf('\\n', editor.selectionStart - 1) + 1;
  editor.setSelectionRange(start, start);
  typeText(prefix);
}

async function save(leave) {
  const name = titleInput.value.trim();
  if (!name) { status.textContent = '제목을 입력해 주세요.'; titleInput.focus(); return; }
  status.textContent = '저장 중...';
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
    typeText(saved.markdown + '\\n');
    status.textContent = saved.name + ' 첨부됨 (저장을 눌러야 반영됩니다)';
  }
}

document.getElementById('save').onclick = () => save(false);
document.getElementById('done').onclick = () => save(true);
document.getElementById('picker').onchange = (e) => upload(e.target.files);
document.querySelectorAll('.mdbar button').forEach((button) => {
  button.onclick = () => {
    const data = button.dataset;
    if (data.wrap) { wrapSelection(data.wrap, data.hint); }
    else if (data.prefix) { prefixLine(data.prefix); }
    else { typeText(data.snippet); }
  };
});

const tabText = document.getElementById('tab-text');
const tabView = document.getElementById('tab-view');
const preview = document.getElementById('preview');
const mdbar = document.querySelector('.mdbar');

async function setMode(viewing) {
  if (viewing) {
    const shown = await post('/preview', {text: editor.value});
    if (!shown) { return; }
    preview.innerHTML = shown.html;
  }
  editor.hidden = viewing;
  mdbar.hidden = viewing;
  preview.hidden = !viewing;
  tabText.classList.toggle('primary', !viewing);
  tabView.classList.toggle('primary', viewing);
  if (!viewing) { editor.focus(); }
}

tabText.onclick = () => setMode(false);
tabView.onclick = () => setMode(true);

document.addEventListener('keydown', (e) => {
  if ((e.ctrlKey || e.metaKey) && e.key === 's') { e.preventDefault(); save(false); }
});
for (const box of [titleInput, folderInput]) {
  box.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') { e.preventDefault(); editor.focus(); }
  });
}
editor.addEventListener('paste', (e) => {
  const files = [...e.clipboardData.files];
  if (files.length) { e.preventDefault(); upload(files); }
});
editor.addEventListener('dragover', (e) => { e.preventDefault(); editor.classList.add('drag'); });
editor.addEventListener('dragleave', () => editor.classList.remove('drag'));
editor.addEventListener('drop', (e) => {
  e.preventDefault(); editor.classList.remove('drag');
  if (e.dataTransfer.files.length) upload(e.dataTransfer.files);
});
(titleInput.value ? editor : titleInput).focus();
"""


def edit_body(ref: str, folder: str = "") -> str:
    exists = page_exists(ref)
    folder = folder_of(ref) or folder
    title = title_of(ref) if ref else ""
    buttons = "".join(f'<button class="btn" {attrs}>{label}</button>' for label, attrs in MD_BUTTONS)
    options = "".join(f'<option value="{html.escape(name, quote=True)}">' for name in list_folders())
    cancel_href = f"/w/{urllib.parse.quote(ref)}" if exists else "/"
    return (
        '<div class="titlerow">'
        f'<input type="text" id="folder" list="folders" placeholder="폴더 (선택)" '
        f'value="{html.escape(folder, quote=True)}" maxlength="200">'
        f'<datalist id="folders">{options}</datalist>'
        f'<input type="text" id="title" placeholder="제목을 입력하세요" '
        f'value="{html.escape(title, quote=True)}" maxlength="100">'
        "</div>"
        '<div class="tabs">'
        '<button class="btn primary" id="tab-text">텍스트</button>'
        '<button class="btn" id="tab-view">미리보기</button>'
        "</div>"
        f'<div class="mdbar">{buttons}</div>'
        f'<textarea id="editor" data-original="{html.escape(ref if exists else "", quote=True)}" '
        f'placeholder="본문을 마크다운으로 씁니다." spellcheck="false">'
        f"{html.escape(read_page(ref))}</textarea>"
        '<div id="preview" hidden></div>'
        '<div class="toolbar">'
        '<button class="btn primary" id="done">게시</button>'
        '<button class="btn" id="save">저장 (Ctrl+S)</button>'
        '<label class="btn">파일 첨부<input type="file" id="picker" multiple hidden></label>'
        f'<a class="btn" href="{cancel_href}">취소</a>'
        '<span id="status" class="meta"></span></div>'
        '<p class="hint">폴더는 <code>개발/서버</code> 처럼 <code>/</code> 로 여러 단계를 씁니다. '
        '비워 두면 폴더 없이 저장됩니다. 문서 링크는 <code>[[문서 이름]]</code> 또는 '
        '<code>[[폴더/문서 이름]]</code>. 이미지·파일은 드래그해서 놓거나 클립보드에서 '
        '바로 붙여넣을 수 있습니다. 제목이나 폴더를 바꿔 저장하면 문서가 그대로 옮겨집니다.</p>'
    )


SETTINGS_SCRIPT = """
document.getElementById('save').onclick = async () => {
  const status = document.getElementById('status');
  const res = await fetch('/settings', {
    method: 'POST', headers: {'Content-Type': 'application/json; charset=utf-8'},
    body: JSON.stringify({name: document.getElementById('name').value}),
  });
  if (res.ok) { location.href = '/settings'; }
  else { status.textContent = await res.text(); }
};
"""


def settings_body() -> str:
    return (
        "<h1>설정</h1>"
        '<p class="hint">위키 이름입니다. 화면 왼쪽 위와 브라우저 탭에 표시됩니다.</p>'
        f'<p><input type="text" id="name" maxlength="40" style="width:320px" '
        f'value="{html.escape(wiki_name(), quote=True)}"></p>'
        '<div class="toolbar"><button class="btn primary" id="save">저장</button>'
        '<a class="btn" href="/">문서 목록으로</a>'
        '<span id="status" class="meta"></span></div>'
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
        self.end_headers()
        self.wfile.write(body)

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

    def read_json(self) -> dict:
        length = int(self.headers.get("Content-Length", 0))
        return json.loads(self.rfile.read(length).decode("utf-8"))

    def do_GET(self):
        prefix, rest, query = self.split_path()
        name = wiki_name()

        if prefix == "":
            folder = normalize_ref(query.get("folder", [""])[0])
            self.send(shell(
                f"문서 목록 - {name}", index_body(folder), INDEX_SCRIPT, current_folder=folder,
            ))
        elif prefix == "new":
            folder = normalize_ref(query.get("folder", [""])[0])
            self.send(shell(f"새 글 - {name}", edit_body("", folder), EDITOR_SCRIPT))
        elif prefix == "search":
            keyword = query.get("q", [""])[0].strip()
            self.send(shell(f"검색 - {name}", search_body(keyword), query=keyword))
        elif prefix == "settings":
            self.send(shell(f"설정 - {name}", settings_body(), SETTINGS_SCRIPT))
        elif prefix in ("w", "e"):
            ref = normalize_ref(rest)
            if not is_valid_ref(ref):
                self.send_text(400, "잘못된 문서 이름입니다.")
            elif prefix == "w":
                ref = resolve_ref(ref)
                self.send(shell(
                    f"{title_of(ref)} - {name}", view_body(ref), VIEW_SCRIPT, current_ref=ref,
                ))
            else:
                self.send(shell(
                    f"{title_of(ref)} 편집", edit_body(ref), EDITOR_SCRIPT, current_ref=ref,
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
        elif prefix == "delete":
            self.remove_page()
        elif prefix == "order":
            self.save_order()
        elif prefix == "preview":
            self.send_json({"html": render(self.read_json().get("text", ""))})
        elif prefix == "folder":
            self.change_folder(rest)
        elif prefix == "settings":
            name = self.read_json().get("name", "").strip()
            if not name:
                self.send_text(400, "위키 이름을 입력해 주세요.")
            else:
                set_wiki_name(name[:40])
                self.send_json({"name": wiki_name()})
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
            delete_page(original)
        self.send_json({"ref": ref})

    def move_page(self):
        data = self.read_json()
        ref = normalize_ref(data.get("ref", ""))
        folder = normalize_ref(data.get("folder", ""))
        if not page_exists(ref):
            self.send_text(400, "없는 문서입니다.")
            return
        if folder and not is_valid_ref(folder):
            self.send_text(400, r'폴더 이름에 \ : * ? " < > | 는 쓸 수 없습니다.')
            return
        new_ref = f"{folder}/{title_of(ref)}" if folder else title_of(ref)
        if new_ref != ref:
            if page_exists(new_ref):
                self.send_text(400, f"‘{new_ref}’ 문서가 이미 있습니다. 다른 폴더를 골라 주세요.")
                return
            write_page(new_ref, read_page(ref))
            delete_page(ref)
        self.send_json({"ref": new_ref})

    def remove_page(self):
        ref = normalize_ref(self.read_json().get("ref", ""))
        if not page_exists(ref):
            self.send_text(400, "없는 문서입니다.")
            return
        delete_page(ref)
        self.send_json({"folder": folder_of(ref)})

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
        saved = store_upload(name, self.rfile.read(length))
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


def main():
    port = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_PORT
    PAGES.mkdir(exist_ok=True)
    FILES.mkdir(exist_ok=True)
    if not page_exists(HOME):
        write_page(HOME, WELCOME)

    url = f"http://localhost:{port}/"
    print(f"위키 저장 위치: {ROOT}")
    print(f"주소: {url}   (종료: Ctrl+C)")
    threading.Timer(0.5, lambda: webbrowser.open(url)).start()
    with Server(("127.0.0.1", port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n종료합니다.")


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
- 문서 목록 화면의 **폴더 추가** 로 글이 없어도 폴더를 미리 만들어 둘 수 있습니다.
  폴더를 골라 본 화면에서는 **하위 폴더 추가**, **폴더 이름 바꾸기**, **폴더 삭제** 를 씁니다.
  이름을 바꾸면 그 안의 문서와 하위 폴더가 통째로 따라 옮겨집니다.
- 이미 쓴 글은 문서 화면 오른쪽 위 **글 수정** 으로 고치고, **폴더 이동** 으로 다른 폴더로
  옮기고, **삭제** 로 지웁니다. 편집 화면에서 제목이나 폴더를 바꿔 저장해도 문서가 그대로
  옮겨집니다. 지운 글은 되돌릴 수 없습니다.
- 다른 문서로 거는 링크는 `[[문서 이름]]` 또는 `[[폴더/문서 이름]]` 처럼 씁니다. 아직 없는
  문서는 빨간 링크로 보이고, 누르면 바로 만들어집니다. `[[문서|보일 글자]]` 도 됩니다.
- 외부 주소는 `[이름](https://example.com)` 형식의 보통 마크다운으로 씁니다.
- 편집 화면에 이미지나 파일을 드래그해서 놓거나, 스크린샷을 그대로 붙여넣으면
  `files/` 폴더에 저장되고 본문에 링크가 삽입됩니다.
- 오른쪽 위 검색창에서 제목과 본문을 함께 찾습니다.
- 위키 이름은 오른쪽 위 ⚙ 에서 바꿉니다.
- 편집 화면 위쪽의 **텍스트 / 미리보기** 로 원본과 꾸며진 결과를 오가며 볼 수 있습니다.
- `Ctrl+S` 로 저장합니다.

## 시작하기

- [[메모]] — 링크를 눌러 첫 문서를 만들어 보세요.
"""


if __name__ == "__main__":
    main()
