"""docs_portal service — document portal over a workspace root
(v2 port of webapps/docs_portal).

Fixture shape: workspace_root, portal_title, user_name, comments[].

Preserved v1 mechanics:
- Path containment under workspace_root (requests outside it 404 with the
  same wording as v1).
- Folder listing inlines the full text of every text file in a non-root
  folder (the "digest" affordance), logging one ``view_file`` per inlined
  file so post-hoc evaluation sees identical access events.

External-gap actions (SmolClaw Docs):
    list_comments, add_comment.
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract

READ_BYTES_LIMIT = 200_000


def _is_text_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            f.read(2048).decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


class ListDirRequest(BaseModel):
    path: str = Field("", description="Folder path relative to the portal root ('' = root).")


class ViewFileRequest(BaseModel):
    path: str = Field(..., description="File path relative to the portal root.")


class ListCommentsRequest(BaseModel):
    path: str = Field(..., description="Document path relative to the portal root.")


class AddCommentRequest(BaseModel):
    path: str = Field(..., description="Document path relative to the portal root.")
    body: str = Field(..., description="Comment text.")


class DocsPortalService(ServiceBase):
    _EXISTING = ("list_portal_folder", "view_portal_file")
    _READ = _EXISTING + ("list_comments",)
    _MUTATE = ("add_comment",)
    CAPABILITY_PROFILES = {
        "standard-v1": _EXISTING + ("list_comments", "add_comment"),
        "full-v1": _EXISTING + ("list_comments", "add_comment"),
    }
    ACTION_CONTRACTS = {
        "list_portal_folder": action_contract(
            "core",
            output={"portal_title": "string", "path": "string",
                    "entries": "array", "inlined_files": "array"},
            errors=(("FOLDER_NOT_FOUND", 404, "folder path is absent under workspace"),),
            audit=("action", "status", "count", "inlined_file_count"),
            tags=("docs_portal", "folder", "list", "read"),
        ),
        "view_portal_file": action_contract(
            "core",
            output={"path": "string", "content": "string", "truncated": "boolean"},
            errors=(("FILE_NOT_FOUND", 404, "file path is absent under workspace"),),
            audit=("action", "status"),
            tags=("docs_portal", "file", "view", "read"),
        ),
        "list_comments": action_contract(
            "core",
            output={"ok": "boolean", "comments": "array"},
            errors=(("DOCUMENT_NOT_FOUND", 404, "document path is absent under workspace"),),
            audit=("action", "status", "path", "comment_count"),
            tags=("docs_portal", "comments", "list", "read"),
        ),
        "add_comment": action_contract(
            "core",
            output={"ok": "boolean", "comment": "object"},
            preconditions=("document exists", "non-empty body"),
            effects=("append one comment on a document",),
            errors=(
                ("DOCUMENT_NOT_FOUND", 404, "document path is absent under workspace"),
                ("INVALID_BODY", 422, "comment body is empty"),
            ),
            audit=("action", "status", "path", "comment_id"),
            tags=("docs_portal", "comments", "add", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="docs_portal")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "workspace_root": "str",
        "portal_title": "str",
        "user_name": "str",
        "comments": [{
            "id": "str",
            "path": "str",
            "author": "str",
            "body": "str",
            "created_at": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.workspace_root = data.get("workspace_root", "/workspace")
        self.portal_title = data.get("portal_title", "Workspace Docs")
        self.user_name = data.get("user_name", "User")
        self.comments: list[dict] = []
        for raw in list(data.get("comments", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("comment"))
            entry["path"] = str(entry.get("path") or "").strip("/")
            entry.setdefault("author", self.user_name)
            entry.setdefault("body", "")
            entry.setdefault("created_at", "")
            self.comments.append(entry)

    def get_state(self) -> dict:
        return {
            "workspace_root": self.workspace_root,
            "portal_title": self.portal_title,
            "user_name": self.user_name,
            "comments": self.comments,
            "comment_count": len(self.comments),
        }

    def _fs_root(self) -> str:
        """Where ``workspace_root`` lives on this machine.

        The agent's ``/workspace`` is a container mount. When the service runs
        on the host, the runner sets ``SANDBOX_WORKSPACE_HOST_DIR`` to the host
        copy behind that mount. Responses still name ``workspace_root``.
        """
        host = os.environ.get("SANDBOX_WORKSPACE_HOST_DIR", "")
        root = self.workspace_root
        if host and (root == "/workspace" or root.startswith("/workspace/")):
            return os.path.join(host, root[len("/workspace"):].lstrip("/"))
        return root

    def _safe_resolve(self, rel_path: str) -> Optional[str]:
        root = os.path.realpath(self._fs_root())
        candidate = os.path.realpath(os.path.join(root, rel_path or ""))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def _comments_for(self, rel_path: str) -> list[dict]:
        target = (rel_path or "").strip("/")
        return [
            c for c in self.comments
            if str(c.get("path") or "").strip("/") == target
        ]

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/list", operation_id="list_portal_folder",
                          summary="List a folder; non-root folders inline the full text of their files")
        def list_dir(req: ListDirRequest):
            rel_dir = req.path.strip("/")
            abs_dir = svc._safe_resolve(rel_dir)
            if abs_dir is None or not os.path.isdir(abs_dir):
                svc.log_action("list_dir_not_found", params=req.model_dump(), status=404)
                return JSONResponse(status_code=404, content={
                    "error": f"No such folder in this workspace: '{rel_dir}'. "
                             f"This portal only shows files under {svc.workspace_root}."})
            entries = []
            for name in sorted(os.listdir(abs_dir)):
                if name.startswith("."):
                    continue
                abs_p = os.path.join(abs_dir, name)
                is_dir = os.path.isdir(abs_p)
                try:
                    size = 0 if is_dir else os.path.getsize(abs_p)
                except OSError:
                    size = 0
                entries.append({"name": name,
                                "path": os.path.relpath(abs_p, os.path.realpath(svc._fs_root())),
                                "is_dir": is_dir, "size": size})
            entries.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))

            inlined = []
            if rel_dir:
                for e in entries:
                    if e["is_dir"]:
                        continue
                    abs_p = os.path.join(abs_dir, e["name"])
                    if not _is_text_file(abs_p):
                        continue
                    try:
                        with open(abs_p, "rb") as f:
                            raw = f.read(READ_BYTES_LIMIT + 1)
                    except OSError:
                        continue
                    truncated = len(raw) > READ_BYTES_LIMIT
                    content = raw[:READ_BYTES_LIMIT].decode("utf-8", errors="replace")
                    svc.log_action("view_file", params={"path": e["path"]},
                                   bytes=len(raw), via="folder_inline")
                    inlined.append({"name": e["name"], "path": e["path"],
                                    "content": content, "truncated": truncated})
            svc.log_action("list_dir", params=req.model_dump(),
                           count=len(entries), inlined_file_count=len(inlined))
            return {"portal_title": svc.portal_title, "path": rel_dir or "/",
                    "entries": entries, "inlined_files": inlined}

        @self.router.post("/file", operation_id="view_portal_file",
                          summary="View one file's contents")
        def view_file(req: ViewFileRequest):
            rel_path = req.path.strip("/")
            abs_path = svc._safe_resolve(rel_path)
            if abs_path is None or not os.path.isfile(abs_path):
                svc.log_action("view_file_not_found", params=req.model_dump(), status=404)
                return JSONResponse(status_code=404, content={"error": "file not found"})
            if not _is_text_file(abs_path):
                svc.log_action("view_file_binary", params=req.model_dump())
                return {"path": rel_path, "content": "(binary file — not previewable)",
                        "truncated": False}
            with open(abs_path, "rb") as f:
                raw = f.read(READ_BYTES_LIMIT + 1)
            truncated = len(raw) > READ_BYTES_LIMIT
            svc.log_action("view_file", params=req.model_dump(), bytes=len(raw))
            return {"path": rel_path,
                    "content": raw[:READ_BYTES_LIMIT].decode("utf-8", errors="replace"),
                    "truncated": truncated}

        @self.router.post("/comments/list", operation_id="list_comments",
                          summary="List comments on a document")
        def list_comments(req: ListCommentsRequest):
            params = req.model_dump()
            rel_path = req.path.strip("/")
            abs_path = svc._safe_resolve(rel_path)
            if abs_path is None or not os.path.isfile(abs_path):
                svc.log_action(
                    "list_comments", params=params, status=404, path=rel_path,
                )
                return svc.error_response(
                    code="DOCUMENT_NOT_FOUND",
                    message="document not found",
                    status=404,
                    details={"path": rel_path},
                )
            comments = svc._comments_for(rel_path)
            comments = sorted(
                comments,
                key=lambda c: (
                    str(c.get("created_at") or ""),
                    str(c.get("id") or ""),
                ),
            )
            svc.log_action(
                "list_comments",
                params=params,
                path=rel_path,
                comment_count=len(comments),
            )
            return {
                "ok": True,
                "path": rel_path,
                "comments": comments,
                "comment_count": len(comments),
            }

        @self.router.post("/comments/add", operation_id="add_comment",
                          summary="Add a comment on a document")
        def add_comment(req: AddCommentRequest):
            params = req.model_dump()
            rel_path = req.path.strip("/")
            abs_path = svc._safe_resolve(rel_path)
            if abs_path is None or not os.path.isfile(abs_path):
                svc.log_action(
                    "add_comment", params=params, status=404, path=rel_path,
                )
                return svc.error_response(
                    code="DOCUMENT_NOT_FOUND",
                    message="document not found",
                    status=404,
                    details={"path": rel_path},
                )
            body = (req.body or "").strip()
            if not body:
                svc.log_action(
                    "add_comment", params=params, status=422, path=rel_path,
                )
                return svc.error_response(
                    code="INVALID_BODY",
                    message="comment body is required",
                    status=422,
                )
            comment = {
                "id": svc.new_id("comment"),
                "path": rel_path,
                "author": svc.user_name,
                "body": body,
                "created_at": svc.now_iso(),
            }
            svc.comments.append(comment)
            svc.log_action(
                "add_comment",
                params=params,
                effect={"comment_id": comment["id"], "path": rel_path},
                path=rel_path,
                comment_id=comment["id"],
            )
            return {"ok": True, "comment": comment}


def create_service(service_id: str) -> DocsPortalService:
    return DocsPortalService(service_id)
