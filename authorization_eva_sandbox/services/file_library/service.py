"""file_library service — shared file library with access grants.

Box-shaped fixture-backed library (not a Box API copy):
    list_files, get_file, upload_file, share_file

Deterministic fixture state; no network, OAuth, wall-clock, or LLM.
"""
from __future__ import annotations

import sys
from pathlib import Path
from typing import Optional

from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


class ListFilesRequest(BaseModel):
    owner_id: str = Field(
        "",
        description="Optional owner filter; empty lists all files.",
    )


class FileIdRequest(BaseModel):
    file_id: str = Field(..., description="File id from list_files / upload_file.")


class UploadFileRequest(BaseModel):
    name: str = Field(..., description="File display name.")
    content: str = Field("", description="File text content.")


class ShareFileRequest(BaseModel):
    file_id: str = Field(..., description="File id to grant access on.")
    user_id: str = Field(..., description="User id to grant access to.")


class FileLibraryService(ServiceBase):
    _READ = ("list_files", "get_file")
    _MUTATE = ("upload_file", "share_file")
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _MUTATE,
        "full-v1": _READ + _MUTATE,
    }
    ACTION_CONTRACTS = {
        "list_files": action_contract(
            "core", output={"ok": "boolean", "files": "array"},
            audit=("action", "status"),
            tags=("file_library", "files", "read"),
        ),
        "get_file": action_contract(
            "core", output={"ok": "boolean", "file": "object"},
            errors=(("FILE_NOT_FOUND", 404, "file id is absent"),),
            audit=("action", "status", "file_id"),
            tags=("file_library", "files", "read"),
        ),
        "upload_file": action_contract(
            "core", output={"ok": "boolean", "file": "object"},
            effects=("append one file owned by the current user",),
            audit=("action", "status", "file_id"),
            tags=("file_library", "files", "upload", "state_change"),
        ),
        "share_file": action_contract(
            "core", output={"ok": "boolean", "file": "object"},
            preconditions=("file and user exist",),
            effects=("grant one user access on a file",),
            errors=(
                ("FILE_NOT_FOUND", 404, "file id is absent"),
                ("USER_NOT_FOUND", 404, "user id is absent"),
            ),
            audit=("action", "status", "file_id", "user_id"),
            tags=("file_library", "files", "share", "state_change"),
        ),
    }

    def __init__(self, service_id: str):
        super().__init__(service_id, template="file_library")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "library_name": "str",
        "current_user_id": "str",
        "users": [{
            "id": "str",
            "name": "str",
            "email": "str",
        }],
        "files": [{
            "id": "str",
            "name": "str",
            "content": "str",
            "owner_id": "str",
            "shared_with": "list",
            "created_at": "str",
        }],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.library_name = data.get("library_name", "File Library")
        self.current_user_id = str(data.get("current_user_id") or "")
        self.users: list[dict] = []
        for raw in list(data.get("users", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("user"))
            entry.setdefault("name", "")
            entry.setdefault("email", "")
            self.users.append(entry)
        if not self.current_user_id and self.users:
            self.current_user_id = str(self.users[0].get("id", ""))
        self.files: list[dict] = []
        for raw in list(data.get("files", [])):
            entry = dict(raw)
            entry.setdefault("id", self.new_id("file"))
            entry.setdefault("name", "")
            entry.setdefault("content", "")
            entry.setdefault("owner_id", self.current_user_id)
            entry.setdefault("shared_with", [])
            entry.setdefault("created_at", "")
            entry["shared_with"] = [
                str(u) for u in list(entry.get("shared_with") or [])
            ]
            self.files.append(entry)

    def get_state(self) -> dict:
        return {
            "library_name": self.library_name,
            "current_user_id": self.current_user_id,
            "users": self.users,
            "files": self.files,
            "file_count": len(self.files),
        }

    def _find_user(self, user_id: str) -> Optional[dict]:
        return next(
            (u for u in self.users if str(u.get("id")) == str(user_id)),
            None,
        )

    def _find_file(self, file_id: str) -> Optional[dict]:
        return next(
            (f for f in self.files if str(f.get("id")) == str(file_id)),
            None,
        )

    def _ordered_files(self, owner_id: str = "") -> list[dict]:
        items = list(self.files)
        filter_owner = (owner_id or "").strip()
        if filter_owner:
            items = [
                f for f in items
                if str(f.get("owner_id") or "") == filter_owner
            ]
        return sorted(
            items,
            key=lambda f: (str(f.get("name") or ""), str(f.get("id") or "")),
        )

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/files/list", operation_id="list_files",
                          summary="List files in the shared library")
        def list_files(req: ListFilesRequest) -> dict:
            params = req.model_dump()
            files = svc._ordered_files(req.owner_id)
            svc.log_action(
                "list_files",
                params=params,
                file_count=len(files),
            )
            return {
                "ok": True,
                "library_name": svc.library_name,
                "current_user_id": svc.current_user_id,
                "files": files,
                "file_count": len(files),
            }

        @self.router.post("/files/get", operation_id="get_file",
                          summary="Get one file by id")
        def get_file(req: FileIdRequest):
            params = req.model_dump()
            file_rec = svc._find_file(req.file_id)
            if not file_rec:
                svc.log_action(
                    "get_file", params=params, status=404, file_id=req.file_id,
                )
                return svc.error_response(
                    code="FILE_NOT_FOUND",
                    message="file not found",
                    status=404,
                    details={"file_id": req.file_id},
                )
            svc.log_action("get_file", params=params, file_id=req.file_id)
            return {"ok": True, "file": file_rec}

        @self.router.post("/files/upload", operation_id="upload_file",
                          summary="Upload a new file owned by the current user")
        def upload_file(req: UploadFileRequest):
            params = req.model_dump()
            name = (req.name or "").strip()
            if not name:
                svc.log_action("upload_file", params=params, status=422)
                return svc.error_response(
                    code="NAME_REQUIRED",
                    message="file name is required",
                    status=422,
                )
            now = svc.now_iso()
            file_rec = {
                "id": svc.new_id("file"),
                "name": name,
                "content": req.content or "",
                "owner_id": svc.current_user_id,
                "shared_with": [],
                "created_at": now,
            }
            svc.files.append(file_rec)
            svc.log_action(
                "upload_file",
                params=params,
                effect={"file_id": file_rec["id"]},
                file_id=file_rec["id"],
            )
            return {"ok": True, "file": file_rec}

        @self.router.post("/files/share", operation_id="share_file",
                          summary="Grant a user access to a file")
        def share_file(req: ShareFileRequest):
            params = req.model_dump()
            file_rec = svc._find_file(req.file_id)
            if not file_rec:
                svc.log_action(
                    "share_file",
                    params=params, status=404, file_id=req.file_id,
                    user_id=req.user_id,
                )
                return svc.error_response(
                    code="FILE_NOT_FOUND",
                    message="file not found",
                    status=404,
                    details={"file_id": req.file_id},
                )
            user = svc._find_user(req.user_id)
            if not user:
                svc.log_action(
                    "share_file",
                    params=params, status=404, file_id=req.file_id,
                    user_id=req.user_id,
                )
                return svc.error_response(
                    code="USER_NOT_FOUND",
                    message="user not found",
                    status=404,
                    details={"user_id": req.user_id},
                )
            granted = str(req.user_id)
            shared = list(file_rec.get("shared_with") or [])
            already = granted in shared
            if not already:
                shared.append(granted)
                file_rec["shared_with"] = shared
            svc.log_action(
                "share_file",
                params=params,
                effect={
                    "file_id": req.file_id,
                    "user_id": granted,
                    "already_shared": already,
                },
                file_id=req.file_id,
                user_id=granted,
                no_op=already,
            )
            return {"ok": True, "file": file_rec}


def create_service(service_id: str) -> FileLibraryService:
    return FileLibraryService(service_id)
