"""Overleaf Sync CLI."""

from __future__ import annotations

import difflib
import fnmatch
import io
import mimetypes
import os
import pickle
import posixpath
import re
import ssl
import time
import zipfile
from pathlib import Path

import click
import requests as reqs
import websocket
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright
from socketIO_client import SocketIO


BASE_URL = "https://www.overleaf.com"
PROJECTS_URL = f"{BASE_URL}/project"
DOWNLOAD_ZIP_URL = f"{BASE_URL}/project/{{project_id}}/download/zip"
CREATE_FOLDER_URL = f"{BASE_URL}/project/{{project_id}}/folder"
DELETE_DOC_URL = f"{BASE_URL}/project/{{project_id}}/doc/{{entity_id}}"
DELETE_FILE_URL = f"{BASE_URL}/project/{{project_id}}/file/{{entity_id}}"
DELETE_FOLDER_URL = f"{BASE_URL}/project/{{project_id}}/folder/{{entity_id}}"
UPLOAD_URL = f"{BASE_URL}/project/{{project_id}}/upload"
COMPILE_URL = f"{BASE_URL}/project/{{project_id}}/compile?enable_pdf_caching=true"
TREE_JS = r"""
() => {
  const treeRoot = document.querySelector('[role="tree"]');
  if (!treeRoot) throw new Error('No Overleaf file tree found.');

  let rootList = null;
  let rootNode = null;
  let rootFolderId = null;
  const parentFolderIds = new Map();
  const seenFibers = new Set();

  const scoreProps = (props) => {
    const docsLen = Array.isArray(props.docs) ? props.docs.length : 0;
    const foldersLen = Array.isArray(props.folders) ? props.folders.length : 0;
    const filesLen = Array.isArray(props.files) ? props.files.length : 0;
    if (!Array.isArray(props.docs) || !Array.isArray(props.folders) || !Array.isArray(props.files)) {
      return -1;
    }
    return foldersLen * 1000000 + (docsLen + filesLen);
  };

  const visitFiber = (fiber) => {
    for (let node = fiber, i = 0; node && i < 60; i += 1, node = node.return) {
      if (seenFibers.has(node)) continue;
      seenFibers.add(node);

      const props = node.memoizedProps;
      if (props && typeof props === 'object') {
        if (!rootList || scoreProps(props) > scoreProps(rootList)) {
          if (Array.isArray(props.docs) && Array.isArray(props.folders) && Array.isArray(props.files)) {
            rootList = props;
            rootNode = node;
          }
        }
      }

      const type = node.elementType || node.type;
      if (type && typeof type === 'object' && String(type.$$typeof).includes('react.provider')) {
        const value = node.memoizedProps && node.memoizedProps.value;
        if (value && typeof value === 'object' && value.parentFolderId) {
          parentFolderIds.set(value.parentFolderId, (parentFolderIds.get(value.parentFolderId) || 0) + 1);
        }
      }
    }
  };

  for (const el of treeRoot.querySelectorAll('*')) {
    const reactKey = Object.getOwnPropertyNames(el).find(key => key.startsWith('__reactFiber'));
    if (reactKey) visitFiber(el[reactKey]);
  }

  if (rootNode) {
    for (let node = rootNode, i = 0; node && i < 60; i += 1, node = node.return) {
      const type = node.elementType || node.type;
      if (type && typeof type === 'object' && String(type.$$typeof).includes('react.provider')) {
        const value = node.memoizedProps && node.memoizedProps.value;
        if (value && typeof value === 'object' && value.parentFolderId) {
          rootFolderId = value.parentFolderId;
          break;
        }
      }
    }
  }

  if (!rootFolderId && parentFolderIds.size) {
    rootFolderId = [...parentFolderIds.entries()].sort((a, b) => b[1] - a[1])[0][0];
  }

  if (!rootList || !rootFolderId) {
    throw new Error('Could not locate Overleaf file tree data.');
  }

  const buildDoc = (doc, parentPath, parentFolderId) => ({
    kind: 'doc',
    id: doc._id,
    name: doc.name,
    path: parentPath ? `${parentPath}/${doc.name}` : doc.name,
    parentFolderId,
  });

  const buildFile = (file, parentPath, parentFolderId) => ({
    kind: 'file',
    id: file._id,
    name: file.name,
    path: parentPath ? `${parentPath}/${file.name}` : file.name,
    parentFolderId,
  });

  const buildFolder = (folder, parentPath, parentFolderId) => {
    const path = parentPath ? `${parentPath}/${folder.name}` : folder.name;
    return {
      kind: 'folder',
      id: folder._id,
      name: folder.name,
      path,
      parentFolderId,
      docs: (folder.docs || []).map(doc => buildDoc(doc, path, folder._id)),
      files: ((folder.fileRefs || folder.files || [])).map(file => buildFile(file, path, folder._id)),
      folders: (folder.folders || []).map(child => buildFolder(child, path, folder._id)),
    };
  };

  return {
    rootFolderId,
    docs: (rootList.docs || []).map(doc => buildDoc(doc, '', rootFolderId)),
    files: (rootList.files || []).map(file => buildFile(file, '', rootFolderId)),
    folders: (rootList.folders || []).map(folder => buildFolder(folder, '', rootFolderId)),
  };
}
"""


def load_store(cookie_path: str) -> dict:
    with open(cookie_path, "rb") as handle:
        return pickle.load(handle)


def save_store(cookie_path: str, cookie: dict, csrf: str) -> None:
    with open(cookie_path, "wb") as handle:
        pickle.dump({"cookie": cookie, "csrf": csrf}, handle)


def normalize_project_name(name: str) -> str:
    return re.sub(r"[\W_]+", "", name, flags=re.UNICODE).lower()


def ignore_patterns(olignore_path: Path) -> list[str]:
    if not olignore_path.is_file():
        return []
    return [line.strip() for line in olignore_path.read_text().splitlines() if line.strip()]


def should_ignore(rel_path: str, patterns: list[str]) -> bool:
    rel_path = rel_path.replace("\\", "/")
    if rel_path.startswith(".git/") or rel_path == ".git":
        return True
    if any(part.startswith(".") for part in rel_path.split("/")):
        return True
    if rel_path == "output" or rel_path.startswith("output/"):
        return True
    return any(fnmatch.fnmatch(rel_path, pattern) for pattern in patterns)


def collect_local_files(sync_path: Path, patterns: list[str]) -> dict[str, Path]:
    result = {}
    for file_path in sync_path.rglob("*"):
        if not file_path.is_file():
            continue
        rel_path = file_path.relative_to(sync_path).as_posix()
        if should_ignore(rel_path, patterns):
            continue
        result[rel_path] = file_path
    return result


def zip_map(zip_bytes: bytes) -> dict[str, bytes]:
    file_map = {}
    with zipfile.ZipFile(io.BytesIO(zip_bytes)) as archive:
        for info in archive.infolist():
            if info.is_dir():
                continue
            file_map[info.filename] = archive.read(info.filename)
    return file_map


def flatten_tree(tree_data: dict) -> tuple[dict[str, dict], dict[str, dict], str]:
    folders: dict[str, dict] = {}
    files: dict[str, dict] = {}

    def add_folder(folder: dict) -> None:
        folders[folder["path"]] = {
            "kind": "folder",
            "id": folder["id"],
            "path": folder["path"],
            "parent_folder_id": folder["parentFolderId"],
            "name": folder["name"],
        }
        for doc in folder.get("docs", []):
            files[doc["path"]] = {
                "kind": "doc",
                "id": doc["id"],
                "path": doc["path"],
                "parent_folder_id": doc["parentFolderId"],
                "name": doc["name"],
            }
        for file_ref in folder.get("files", []):
            files[file_ref["path"]] = {
                "kind": "file",
                "id": file_ref["id"],
                "path": file_ref["path"],
                "parent_folder_id": file_ref["parentFolderId"],
                "name": file_ref["name"],
            }
        for child in folder.get("folders", []):
            add_folder(child)

    for doc in tree_data.get("docs", []):
        files[doc["path"]] = {
            "kind": "doc",
            "id": doc["id"],
            "path": doc["path"],
            "parent_folder_id": doc["parentFolderId"],
            "name": doc["name"],
        }
    for file_ref in tree_data.get("files", []):
        files[file_ref["path"]] = {
            "kind": "file",
            "id": file_ref["id"],
            "path": file_ref["path"],
            "parent_folder_id": file_ref["parentFolderId"],
            "name": file_ref["name"],
        }
    for folder in tree_data.get("folders", []):
        add_folder(folder)

    return folders, files, tree_data["rootFolderId"]


def normalize_text_content(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def read_local_text(local_path: Path) -> str:
    return normalize_text_content(local_path.read_text(encoding="utf-8-sig"))


def repair_socket_text(text: str) -> str:
    try:
        repaired = text.encode("latin-1").decode("utf-8")
    except UnicodeError:
        return text

    try:
        if repaired.encode("utf-8").decode("latin-1") == text:
            return repaired
    except UnicodeError:
        return text
    return text


def snapshot_lines_to_text(lines: list[str]) -> str:
    return "\n".join(repair_socket_text(line) for line in lines)


def build_text_components(current_text: str, target_text: str) -> list[dict]:
    if current_text == target_text:
        return []

    matcher = difflib.SequenceMatcher(a=current_text, b=target_text, autojunk=False)
    components = []
    for tag, i1, i2, j1, j2 in reversed(matcher.get_opcodes()):
        if tag == "equal":
            continue
        if tag in ("delete", "replace") and i1 != i2:
            components.append({"p": i1, "d": current_text[i1:i2]})
        if tag in ("insert", "replace") and j1 != j2:
            components.append({"p": i1, "i": target_text[j1:j2]})
    return components


def collect_folder_paths(file_map: dict[str, object]) -> set[str]:
    folders = set()
    for rel_path in file_map:
        folder_path = posixpath.dirname(rel_path)
        while folder_path:
            folders.add(folder_path)
            folder_path = posixpath.dirname(folder_path)
    return folders


class RealtimeProjectClient:
    def __init__(self, session: "OverleafSession", project_id: str):
        self.session = session
        self.project_id = project_id
        self.socket = None
        self.project_joined = False
        self.project_error = None
        self.pending_update = None

    def _cookie_header(self) -> str:
        cookie_parts = []
        gclb_values = self.session._cookie_values("GCLB")
        if gclb_values:
            cookie_parts.append(f"GCLB={gclb_values[0]}")
        session_values = self.session._cookie_values("overleaf_session2")
        if session_values:
            cookie_parts.append(f"overleaf_session2={session_values[0]}")
        return "; ".join(cookie_parts)

    def _wait_for(self, predicate, timeout: float, message: str) -> None:
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return
            self.socket.wait(seconds=0.1)
            if predicate():
                return
        raise click.ClickException(message)

    def _on_join_project(self, *args) -> None:
        self.project_joined = True
        self.project_error = None

    def _on_connection_rejected(self, *args) -> None:
        self.project_error = args[0] if args else {"message": "connection rejected"}

    def _on_update_applied(self, *args) -> None:
        if self.pending_update is not None and self.pending_update.get("applied") is None:
            self.pending_update["applied"] = args

    def _on_update_error(self, *args) -> None:
        if self.pending_update is not None and self.pending_update.get("error") is None:
            self.pending_update["error"] = args

    def connect(self) -> None:
        if self.socket is not None:
            return

        if not hasattr(websocket, "SSLError"):
            websocket.SSLError = ssl.SSLError

        handshake = self.session.session.get(
            f"{BASE_URL}/socket.io/1/",
            params={"projectId": self.project_id, "esh": 1, "ssp": 1, "t": int(time.time())},
        )
        handshake.raise_for_status()
        if "GCLB" in handshake.cookies:
            self.session.session.cookies.set("GCLB", handshake.cookies["GCLB"])

        self.project_joined = False
        self.project_error = None
        self.socket = SocketIO(
            BASE_URL,
            params={"projectId": self.project_id, "esh": 1, "ssp": 1, "t": int(time.time())},
            headers={"Cookie": self._cookie_header()},
        )
        self.socket.on("joinProjectResponse", self._on_join_project)
        self.socket.on("connectionRejected", self._on_connection_rejected)
        self.socket.on("otUpdateApplied", self._on_update_applied)
        self.socket.on("otUpdateError", self._on_update_error)

        self._wait_for(
            lambda: self.project_joined or self.project_error is not None,
            timeout=15,
            message="Timed out connecting to Overleaf realtime service.",
        )
        if self.project_error is not None:
            raise click.ClickException(f"Overleaf realtime connection rejected: {self.project_error}")

    def close(self) -> None:
        if self.socket is None:
            return
        socket = self.socket
        self.socket = None
        if socket.connected:
            socket.disconnect()

    def join_doc(self, doc_id: str) -> tuple[str, int]:
        self.connect()
        result = {}
        self.socket.emit(
            "joinDoc",
            doc_id,
            {"encodeRanges": True, "supportsHistoryOT": True},
            lambda *args: result.setdefault("args", args),
        )
        self._wait_for(lambda: "args" in result, timeout=15, message=f"Timed out joining Overleaf document {doc_id}.")

        args = result["args"]
        if args[0] is not None:
            raise click.ClickException(f"Failed to join Overleaf document {doc_id}: {args[0]}")

        ot_type = args[5] if len(args) > 5 else "sharejs-text-ot"
        if ot_type != "sharejs-text-ot":
            raise click.ClickException(f"Unsupported Overleaf document OT type: {ot_type}")

        return snapshot_lines_to_text(args[1]), args[2]

    def leave_doc(self, doc_id: str) -> None:
        if self.socket is None:
            return

        result = {}
        self.socket.emit("leaveDoc", doc_id, lambda *args: result.setdefault("args", args))
        self._wait_for(lambda: "args" in result, timeout=10, message=f"Timed out leaving Overleaf document {doc_id}.")
        args = result["args"]
        if args and args[0] is not None:
            raise click.ClickException(f"Failed to leave Overleaf document {doc_id}: {args[0]}")

    def update_doc(self, doc_id: str, target_text: str) -> bool:
        current_text, version = self.join_doc(doc_id)
        try:
            components = build_text_components(current_text, target_text)
            if not components:
                return False

            self.pending_update = {"applied": None, "error": None}
            self.socket.emit("applyOtUpdate", doc_id, {"v": version, "op": components}, lambda *args: None)
            self._wait_for(
                lambda: self.pending_update["applied"] is not None or self.pending_update["error"] is not None,
                timeout=20,
                message=f"Timed out applying Overleaf OT update for {doc_id}.",
            )
            if self.pending_update["error"] is not None:
                raise click.ClickException(f"Overleaf OT update failed for {doc_id}: {self.pending_update['error']}")
            return True
        finally:
            self.pending_update = None
            self.leave_doc(doc_id)


class OverleafSession:
    def __init__(self, store: dict):
        self.session = reqs.Session()
        # Avoid inheriting system proxy settings. Some local loopback proxies can
        # interrupt multipart uploads and make sync behavior inconsistent.
        self.session.trust_env = False
        self.session.proxies.clear()
        self.session.cookies.update(store["cookie"])
        self.csrf = store["csrf"]

    def persist(self, cookie_path: str) -> None:
        save_store(cookie_path, self.session.cookies.get_dict(), self.csrf)

    def _cookie_values(self, name: str) -> list[str]:
        values = []
        for cookie in self.session.cookies:
            if cookie.name == name and cookie.value not in values:
                values.append(cookie.value)
        return values

    def _update_csrf(self, html: str) -> None:
        soup = BeautifulSoup(html, "html.parser")
        token = soup.find("meta", {"name": "ol-csrfToken"})
        if token is not None:
            self.csrf = token.get("content", self.csrf)

    def _projects_page(self) -> str:
        response = self.session.get(PROJECTS_URL)
        response.raise_for_status()
        self._update_csrf(response.text)
        return response.text

    def _parse_projects(self, html: str) -> list[dict]:
        soup = BeautifulSoup(html, "html.parser")
        meta = soup.find("meta", {"name": "ol-prefetchedProjectsBlob"}) or soup.find("meta", {"name": "ol-projects"})
        if meta is None:
            raise click.ClickException("Could not parse Overleaf project list.")

        payload = meta.get("content", "")
        data = reqs.models.complexjson.loads(payload)
        if isinstance(data, dict) and "projects" in data:
            projects = data["projects"]
        else:
            projects = data
        return [project for project in projects if not project.get("archived") and not project.get("trashed")]

    def list_projects(self) -> list[dict]:
        return self._parse_projects(self._projects_page())

    def get_project(self, project_name: str) -> dict:
        projects = self.list_projects()
        exact = next((project for project in projects if project.get("name") == project_name), None)
        if exact:
            return exact

        normalized = normalize_project_name(project_name)
        fuzzy = [project for project in projects if normalize_project_name(project.get("name", "")) == normalized]
        if len(fuzzy) == 1:
            return fuzzy[0]
        if len(fuzzy) > 1:
            raise click.ClickException(f"Multiple Overleaf projects match '{project_name}'. Use the exact project name.")
        raise click.ClickException(f"Overleaf project '{project_name}' not found.")

    def download_zip(self, project_id: str) -> bytes:
        response = self.session.get(DOWNLOAD_ZIP_URL.format(project_id=project_id))
        response.raise_for_status()
        return response.content

    def create_folder(self, project_id: str, parent_folder_id: str, folder_name: str) -> dict:
        response = self.session.post(
            CREATE_FOLDER_URL.format(project_id=project_id),
            headers={"X-Csrf-Token": self.csrf},
            json={"parent_folder_id": parent_folder_id, "name": folder_name},
        )
        response.raise_for_status()
        return response.json()

    def delete_entity(self, project_id: str, entity: dict) -> None:
        if entity["kind"] == "doc":
            url = DELETE_DOC_URL.format(project_id=project_id, entity_id=entity["id"])
        elif entity["kind"] == "file":
            url = DELETE_FILE_URL.format(project_id=project_id, entity_id=entity["id"])
        elif entity["kind"] == "folder":
            url = DELETE_FOLDER_URL.format(project_id=project_id, entity_id=entity["id"])
        else:
            raise click.ClickException(f"Unsupported Overleaf entity kind '{entity['kind']}'.")

        response = self.session.delete(url, headers={"X-Csrf-Token": self.csrf}, json={})
        response.raise_for_status()

    def upload_file(self, project_id: str, folder_id: str, local_path: Path) -> dict:
        mime_type = mimetypes.guess_type(local_path.name)[0] or "application/octet-stream"
        with local_path.open("rb") as handle:
            response = self.session.post(
                f"{UPLOAD_URL.format(project_id=project_id)}?folder_id={folder_id}",
                headers={"X-Csrf-Token": self.csrf},
                files={
                    "relativePath": (None, "null"),
                    "name": (None, local_path.name),
                    "type": (None, mime_type),
                    "qqfile": (local_path.name, handle, mime_type),
                },
            )
        response.raise_for_status()
        payload = response.json()
        if not payload.get("success"):
            raise click.ClickException(f"Failed to upload '{local_path.name}' to Overleaf: {payload}")
        return payload

    def download_pdf(self, project_id: str) -> tuple[str, bytes]:
        response = self.session.post(
            COMPILE_URL.format(project_id=project_id),
            headers={"X-Csrf-Token": self.csrf},
            json={
                "check": "silent",
                "draft": False,
                "incrementalCompilesEnabled": True,
                "rootDoc_id": "",
                "stopOnFirstError": False,
            },
        )
        response.raise_for_status()
        payload = response.json()
        pdf_file = next(output for output in payload["outputFiles"] if output["type"] == "pdf")
        pdf_response = self.session.get(BASE_URL + pdf_file["url"], headers={"X-Csrf-Token": self.csrf})
        pdf_response.raise_for_status()
        return pdf_file["path"], pdf_response.content

    def extract_tree(self, project_id: str) -> tuple[dict[str, dict], dict[str, dict], str]:
        socket_response = self.session.get(f"{BASE_URL}/socket.io/1/", params={"projectId": project_id, "esh": 1, "ssp": 1, "t": 1})
        socket_response.raise_for_status()
        if "GCLB" in socket_response.cookies:
            self.session.cookies.set("GCLB", socket_response.cookies["GCLB"])

        browser_cookies = []
        session_values = self._cookie_values("overleaf_session2")
        overleaf_session = session_values[0] if session_values else None
        if overleaf_session:
            browser_cookies.append(
                {
                    "name": "overleaf_session2",
                    "value": overleaf_session,
                    "domain": ".overleaf.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )
        gclb_values = self._cookie_values("GCLB")
        gclb = gclb_values[0] if gclb_values else None
        if gclb:
            browser_cookies.append(
                {
                    "name": "GCLB",
                    "value": gclb,
                    "domain": "www.overleaf.com",
                    "path": "/",
                    "httpOnly": False,
                    "secure": True,
                    "sameSite": "Lax",
                }
            )

        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="chrome", headless=True)
            context = browser.new_context()
            if browser_cookies:
                context.add_cookies(browser_cookies)
            page = context.new_page()
            page.goto(f"{BASE_URL}/project/{project_id}", wait_until="domcontentloaded")
            page.wait_for_selector('[role="tree"] [role="treeitem"]', timeout=30000)
            tree_data = page.evaluate(TREE_JS)
            browser.close()

        return flatten_tree(tree_data)


def ensure_local_dir(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def write_local_file(path: Path, content: bytes) -> None:
    ensure_local_dir(path)
    path.write_bytes(content)


def remove_local_file(path: Path) -> None:
    if path.exists():
        path.unlink()


def ensure_remote_folder(session: OverleafSession, project_id: str, folders: dict[str, dict], root_folder_id: str, folder_path: str) -> str:
    if not folder_path:
        return root_folder_id

    current_path = ""
    parent_folder_id = root_folder_id
    for part in folder_path.split("/"):
        current_path = part if not current_path else f"{current_path}/{part}"
        existing = folders.get(current_path)
        if existing is None:
            created = session.create_folder(project_id, parent_folder_id, part)
            existing = {
                "kind": "folder",
                "id": created["_id"],
                "path": current_path,
                "parent_folder_id": parent_folder_id,
                "name": part,
            }
            folders[current_path] = existing
        parent_folder_id = existing["id"]
    return parent_folder_id


def prompt_conflict(path: str, local_only: bool, remote_only: bool) -> str:
    if local_only:
        return "local"
    if remote_only:
        return "remote"
    return click.prompt(
        f"Conflict on '{path}'. Keep [l]ocal or [r]emote?",
        type=click.Choice(["l", "r"]),
        default="l",
    )


def file_contents_match(local_path: Path, remote_bytes: bytes, remote_entity: dict | None) -> bool:
    if remote_entity is not None and remote_entity["kind"] == "doc":
        try:
            return read_local_text(local_path) == normalize_text_content(remote_bytes.decode("utf-8-sig"))
        except UnicodeDecodeError:
            return local_path.read_bytes() == remote_bytes
    return local_path.read_bytes() == remote_bytes


def collect_sync_state(session: OverleafSession, project: dict, sync_path: Path, olignore_path: Path) -> dict:
    patterns = ignore_patterns(olignore_path)
    local_files = collect_local_files(sync_path, patterns)
    remote_zip = zip_map(session.download_zip(project["id"]))
    remote_folders, remote_entities, root_folder_id = session.extract_tree(project["id"])
    return {
        "local_files": local_files,
        "remote_zip": remote_zip,
        "remote_folders": remote_folders,
        "remote_entities": remote_entities,
        "root_folder_id": root_folder_id,
    }


def build_sync_plan(
    local_files: dict[str, Path],
    remote_zip: dict[str, bytes],
    remote_entities: dict[str, dict],
    remote_folders: dict[str, dict],
    local_only: bool,
    remote_only: bool,
) -> dict[str, list[str]]:
    plan = {
        "push_new": [],
        "push_replace": [],
        "pull_new": [],
        "pull_replace": [],
        "local_delete": [],
        "remote_delete": [],
        "remote_delete_folders": [],
        "conflicts": [],
    }

    all_paths = sorted(set(local_files) | set(remote_zip))
    for path in all_paths:
        local_path = local_files.get(path)
        remote_bytes = remote_zip.get(path)
        remote_entity = remote_entities.get(path)

        if local_path and remote_bytes is not None:
            if file_contents_match(local_path, remote_bytes, remote_entity):
                continue
            if local_only:
                plan["push_replace"].append(path)
            elif remote_only:
                plan["pull_replace"].append(path)
            else:
                plan["conflicts"].append(path)
            continue

        if local_path and remote_bytes is None:
            if remote_only:
                plan["local_delete"].append(path)
            else:
                plan["push_new"].append(path)
            continue

        if local_path is None and remote_bytes is not None:
            if local_only:
                plan["remote_delete"].append(path)
            else:
                plan["pull_new"].append(path)

    if local_only:
        desired_folders = collect_folder_paths(local_files)
        for folder_path in sorted(remote_folders, key=lambda item: item.count("/"), reverse=True):
            if folder_path in desired_folders:
                continue
            plan["remote_delete_folders"].append(folder_path)

    return plan


def print_sync_plan(plan: dict[str, list[str]]) -> None:
    labels = [
        ("push_new", "[PLAN LOCAL -> REMOTE NEW]"),
        ("push_replace", "[PLAN LOCAL -> REMOTE REPLACE]"),
        ("pull_new", "[PLAN REMOTE -> LOCAL NEW]"),
        ("pull_replace", "[PLAN REMOTE -> LOCAL REPLACE]"),
        ("local_delete", "[PLAN LOCAL DELETE]"),
        ("remote_delete", "[PLAN REMOTE DELETE]"),
        ("remote_delete_folders", "[PLAN REMOTE DELETE FOLDER]"),
        ("conflicts", "[PLAN CONFLICT]"),
    ]
    total = sum(len(plan[key]) for key, _ in labels)
    if total == 0:
        click.echo("No sync actions needed.")
        return

    for key, label in labels:
        for path in plan[key]:
            click.echo(f"{label} {path}")

    click.echo(
        "Summary: "
        + ", ".join(f"{key}={len(plan[key])}" for key, _ in labels if plan[key])
    )


def sync_project(session: OverleafSession, project: dict, sync_path: Path, olignore_path: Path, local_only: bool, remote_only: bool) -> None:
    state = collect_sync_state(session, project, sync_path, olignore_path)
    local_files = state["local_files"]
    remote_zip = state["remote_zip"]
    remote_folders = state["remote_folders"]
    remote_entities = state["remote_entities"]
    root_folder_id = state["root_folder_id"]

    plan = build_sync_plan(local_files, remote_zip, remote_entities, remote_folders, local_only, remote_only)
    push_updates = list(plan["push_new"]) + list(plan["push_replace"])
    pull_updates = list(plan["pull_new"]) + list(plan["pull_replace"])

    for path in plan["conflicts"]:
        choice = prompt_conflict(path, local_only, remote_only)
        if choice in ("l", "local"):
            push_updates.append(path)
        else:
            pull_updates.append(path)

    for path in plan["local_delete"]:
        remove_local_file(sync_path / path)
        click.echo(f"[LOCAL DELETE] {path}")

    for path in plan["remote_delete"]:
        entity = remote_entities.get(path)
        if entity:
            session.delete_entity(project["id"], entity)
            remote_entities.pop(path, None)
            click.echo(f"[REMOTE DELETE] {path}")

    for path in pull_updates:
        write_local_file(sync_path / path, remote_zip[path])
        click.echo(f"[REMOTE -> LOCAL] {path}")

    realtime = None
    try:
        for path in push_updates:
            local_path = local_files[path]
            existing = remote_entities.get(path)

            if existing is not None and existing["kind"] == "doc":
                if realtime is None:
                    realtime = RealtimeProjectClient(session, project["id"])
                try:
                    updated = realtime.update_doc(existing["id"], read_local_text(local_path))
                    if updated:
                        click.echo(f"[LOCAL -> REMOTE OT] {path}")
                    continue
                except (UnicodeDecodeError, click.ClickException) as exc:
                    click.echo(f"[OT FALLBACK] {path}: {exc}")

            folder_path = posixpath.dirname(path)
            folder_id = ensure_remote_folder(session, project["id"], remote_folders, root_folder_id, folder_path)
            if existing is not None:
                session.delete_entity(project["id"], existing)
            payload = session.upload_file(project["id"], folder_id, local_path)
            remote_entities[path] = {
                "kind": "doc" if payload.get("entity_type") == "doc" else "file",
                "id": payload["entity_id"],
                "path": path,
                "parent_folder_id": folder_id,
                "name": local_path.name,
            }
            click.echo(f"[LOCAL -> REMOTE] {path}")
    finally:
        if realtime is not None:
            realtime.close()

    for folder_path in plan["remote_delete_folders"]:
        session.delete_entity(project["id"], remote_folders[folder_path])
        click.echo(f"[REMOTE DELETE FOLDER] {folder_path}")


@click.group(invoke_without_command=True)
@click.option("-l", "--local-only", "local_only", is_flag=True, help="Sync local files to Overleaf.")
@click.option("-r", "--remote-only", "remote_only", is_flag=True, help="Sync remote files to local.")
@click.option("--dry-run", "dry_run", is_flag=True, help="Show planned sync actions without applying them.")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=".overleaf-sync-auth", show_default=True, type=click.Path(exists=False), help="Path to the persisted Overleaf auth store.")
@click.option("-p", "--path", "sync_path", default=".", type=click.Path(exists=True), help="Local sync path.")
@click.option("-i", "--olignore", "olignore_path", default=".olignore", type=click.Path(exists=False), help="Path to .olignore relative to sync path.")
@click.pass_context
def main(ctx: click.Context, local_only: bool, remote_only: bool, dry_run: bool, project_name: str, cookie_path: str, sync_path: str, olignore_path: str) -> None:
    if ctx.invoked_subcommand is not None:
        return

    if local_only and remote_only:
        raise click.ClickException("Use at most one of --local-only and --remote-only.")
    if not os.path.isfile(cookie_path):
        raise click.ClickException("Persisted Overleaf auth store not found. Run `overleaf-sync login` first.")

    session = OverleafSession(load_store(cookie_path))
    sync_root = Path(sync_path).resolve()
    project_name = project_name or sync_root.name
    project = session.get_project(project_name)
    if dry_run:
        state = collect_sync_state(session, project, sync_root, sync_root / olignore_path)
        plan = build_sync_plan(
            state["local_files"],
            state["remote_zip"],
            state["remote_entities"],
            state["remote_folders"],
            local_only,
            remote_only,
        )
        print_sync_plan(plan)
        session.persist(cookie_path)
        return
    sync_project(session, project, sync_root, sync_root / olignore_path, local_only, remote_only)
    session.persist(cookie_path)


@main.command()
@click.option("--store-path", "--path", "cookie_path", default=".overleaf-sync-auth", show_default=True, type=click.Path(exists=False), help="Path to store the persisted Overleaf auth store.")
def login(cookie_path: str) -> None:
    from overleaf_sync.browser_login import login as browser_login

    store = browser_login()
    if store is None:
        raise click.ClickException("Login failed.")
    save_store(cookie_path, store["cookie"], store["csrf"])
    click.echo(f"Login successful. Cookie persisted as `{click.format_filename(cookie_path)}`.")


@main.command(name="list")
@click.option("--store-path", "cookie_path", default=".overleaf-sync-auth", show_default=True, type=click.Path(exists=False), help="Path to the persisted Overleaf auth store.")
def list_projects(cookie_path: str) -> None:
    if not os.path.isfile(cookie_path):
        raise click.ClickException("Persisted Overleaf auth store not found. Run `overleaf-sync login` first.")

    session = OverleafSession(load_store(cookie_path))
    for project in sorted(session.list_projects(), key=lambda item: item.get("lastUpdated", ""), reverse=True):
        click.echo(f"{project.get('lastUpdated', '')} - {project.get('name', '')}")
    session.persist(cookie_path)


@main.command(name="download")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--download-path", "download_path", default=".", type=click.Path(exists=True), help="Where to write the compiled PDF.")
@click.option("--store-path", "cookie_path", default=".overleaf-sync-auth", show_default=True, type=click.Path(exists=False), help="Path to the persisted Overleaf auth store.")
def download_pdf(project_name: str, download_path: str, cookie_path: str) -> None:
    if not os.path.isfile(cookie_path):
        raise click.ClickException("Persisted Overleaf auth store not found. Run `overleaf-sync login` first.")

    session = OverleafSession(load_store(cookie_path))
    project_name = project_name or Path.cwd().name
    project = session.get_project(project_name)
    file_name, content = session.download_pdf(project["id"])
    output_path = Path(download_path).resolve() / file_name
    ensure_local_dir(output_path)
    output_path.write_bytes(content)
    session.persist(cookie_path)
    click.echo(f"Downloaded PDF to {output_path}")


@main.command(name="status")
@click.option("-l", "--local-only", "local_only", is_flag=True, help="Show the plan for local-only sync.")
@click.option("-r", "--remote-only", "remote_only", is_flag=True, help="Show the plan for remote-only sync.")
@click.option("-n", "--name", "project_name", default="", help="Overleaf project name.")
@click.option("--store-path", "cookie_path", default=".overleaf-sync-auth", show_default=True, type=click.Path(exists=False), help="Path to the persisted Overleaf auth store.")
@click.option("-p", "--path", "sync_path", default=".", type=click.Path(exists=True), help="Local sync path.")
@click.option("-i", "--olignore", "olignore_path", default=".olignore", type=click.Path(exists=False), help="Path to .olignore relative to sync path.")
def status(local_only: bool, remote_only: bool, project_name: str, cookie_path: str, sync_path: str, olignore_path: str) -> None:
    if local_only and remote_only:
        raise click.ClickException("Use at most one of --local-only and --remote-only.")
    if not os.path.isfile(cookie_path):
        raise click.ClickException("Persisted Overleaf auth store not found. Run `overleaf-sync login` first.")

    session = OverleafSession(load_store(cookie_path))
    sync_root = Path(sync_path).resolve()
    project_name = project_name or sync_root.name
    project = session.get_project(project_name)
    state = collect_sync_state(session, project, sync_root, sync_root / olignore_path)
    plan = build_sync_plan(
        state["local_files"],
        state["remote_zip"],
        state["remote_entities"],
        state["remote_folders"],
        local_only,
        remote_only,
    )
    print_sync_plan(plan)
    session.persist(cookie_path)


if __name__ == "__main__":
    main()
