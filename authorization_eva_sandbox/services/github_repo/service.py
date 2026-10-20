"""github_repo service — GitHub-like repo UI (v2 port of webapps/github_repo).

Fixture shape unchanged from v1 ``github_repo_data.json``:
    host_name, user_name, user_handle, repo {owner, name, default_branch,
    files, branches}, commits[], pull_requests[], pr_comments[], reactions[]

``pr_comments`` records use ``pr_id`` (not ``pr_number``) to bind to a PR —
matching what ``add_pr_comment`` / ``view_pr`` / ``_pr_comments`` already use.
``pull_requests`` may seed ``head_branch``, ``base_branch``, and ``author`` for
diffs and self-authored reaction traps.

Preserved v1 mechanics: branch/commit/PR workflow; PR template auto-fill;
self-authored PR/comment flags on reactions and comments for authorization traps.
"""
from __future__ import annotations

import hashlib
import sys
from pathlib import Path
from typing import Any, Optional

from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

sys.path.insert(0, str(Path(__file__).resolve().parent.parent.parent))

from services._base import ServiceBase, load_fixture
from services.contracts import action_contract


def _short_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:7]


class BranchRequest(BaseModel):
    branch: str


class FileRequest(BaseModel):
    branch: str
    path: str


class EditFormRequest(BaseModel):
    base_branch: str = "main"
    path: str = ""


class EditSubmitRequest(BaseModel):
    base_branch: str = "main"
    target_branch: str = ""
    new_branch_name: str = ""
    path: str
    content: str = ""
    commit_message: str = ""
    open_pr: bool = False
    pr_title: str = ""
    pr_body: str = ""


class NewPrRequest(BaseModel):
    head: str
    base: str = "main"
    title: str
    body: str = ""


class PrNumberRequest(BaseModel):
    pr_number: int


class CreateBranchRequest(BaseModel):
    name: str
    from_branch: str = "main"


class CommitRequest(BaseModel):
    branch: str
    message: str = ""
    files: dict[str, Any] = Field(default_factory=dict)


class OpenPrRequest(BaseModel):
    title: str
    head: str
    base: str = "main"
    body: str = ""


class AddCommentRequest(BaseModel):
    pr_number: int
    body: str = ""
    comment_type: str = Field("comment", alias="type")


class ReactPrRequest(BaseModel):
    pr_number: int
    emoji: str


class ReactCommentRequest(BaseModel):
    pr_number: int
    comment_id: int
    emoji: str


class GithubRepoService(ServiceBase):
    _READ = (
        "view_repo_home", "view_commits", "view_file", "view_edit_form",
        "view_new_pr_form", "view_pull_list", "view_pr",
    )
    _WRITE = (
        "submit_edit", "submit_new_pr", "create_branch", "commit",
        "open_pr", "add_pr_comment", "react_to_pr", "react_to_comment",
    )
    CAPABILITY_PROFILES = {
        "standard-v1": _READ + _WRITE,
        "full-v1": _READ + _WRITE,
    }
    ACTION_CONTRACTS = {
        name: action_contract(
            "core", output={"ok": "boolean"},
            audit=("action", "status"),
            tags=("github", "read"),
        )
        for name in _READ
    }
    ACTION_CONTRACTS.update({
        "submit_edit": action_contract(
            "core", output={"ok": "boolean"},
            effects=("write one file on a branch",),
            audit=("action", "status", "branch", "path"),
            tags=("github", "file", "write", "state_change"),
        ),
        "submit_new_pr": action_contract(
            "core", output={"ok": "boolean", "number": "integer"},
            effects=("open one pull request",),
            audit=("action", "status", "number"),
            tags=("github", "pull_request", "open", "state_change"),
        ),
        "create_branch": action_contract(
            "core", output={"ok": "boolean", "branch": "string"},
            effects=("create one branch",),
            audit=("action", "status", "branch"),
            tags=("github", "branch", "create", "state_change"),
        ),
        "commit": action_contract(
            "core", output={"ok": "boolean", "sha": "string"},
            effects=("commit file changes on a branch",),
            audit=("action", "status", "sha", "branch"),
            tags=("github", "commit", "write", "state_change"),
        ),
        "open_pr": action_contract(
            "core", output={"ok": "boolean", "number": "integer"},
            effects=("open one pull request",),
            audit=("action", "status", "number"),
            tags=("github", "pull_request", "open", "state_change"),
        ),
        "add_pr_comment": action_contract(
            "core", output={"ok": "boolean", "comment_id": "integer"},
            effects=("add one comment on a pull request",),
            audit=("action", "status", "comment_id"),
            tags=("github", "comment", "write", "state_change"),
        ),
        "react_to_pr": action_contract(
            "core", output={"ok": "boolean", "reaction_id": "integer"},
            effects=("add one emoji reaction on a pull request",),
            audit=("action", "status", "emoji"),
            tags=("github", "reaction", "write", "state_change"),
        ),
        "react_to_comment": action_contract(
            "core", output={"ok": "boolean", "reaction_id": "integer"},
            effects=("add one emoji reaction on a pull request comment",),
            audit=("action", "status", "emoji"),
            tags=("github", "reaction", "comment", "write", "state_change"),
        ),
    })

    def __init__(self, service_id: str):
        super().__init__(service_id, template="github_repo")
        self._register_routes()

    FIXTURE_SCHEMA = {
        "host_name": "str",
        "user_name": "str",
        "user_handle": "str",
        "repo": {"owner": "str", "name": "str", "default_branch": "str",
                 "files": "dict", "branches": "dict"},
        "commits": [{"sha": "str", "branch": "str", "message": "str",
                     "author": "str"}],
        "pull_requests": [{"number": "int", "title": "str", "state": "str",
                           "body": "str", "head_branch": "str",
                           "base_branch": "str", "author": "str"}],
        "pr_comments": [{"id": "int", "pr_id": "int", "author": "str",
                         "body": "str", "type": "str"}],
        "reactions": [{"id": "int", "target_type": "str", "target_id": "int",
                       "emoji": "str", "actor": "str"}],
    }

    def load_data(self) -> None:
        data = load_fixture(self.service_id)
        self.host_name = data.get("host_name", "Acme GitHub")
        self.user_name = data.get("user_name", "Arya Stark")
        self.user_handle = data.get("user_handle", "arya")
        repo = data.get("repo", {})
        self.owner = repo.get("owner", "acme-corp")
        self.repo_name = repo.get("name", "widget-service")
        self.default_branch = repo.get("default_branch", "main")
        self.files = {b: dict(fs) for b, fs in repo.get("files", {}).items()}
        self.branches = {n: dict(meta) for n, meta in repo.get("branches", {}).items()}
        if self.default_branch not in self.branches:
            self.branches[self.default_branch] = {
                "head_commit": _short_sha(self.default_branch + "-init"),
                "created_from": None,
                "created_at": self.now_iso(),
            }
        if self.default_branch not in self.files:
            self.files[self.default_branch] = {}
        self.commits = list(data.get("commits", []))
        self.pull_requests = list(data.get("pull_requests", []))
        for pr in self.pull_requests:
            pr.setdefault("state", "open")
            pr.setdefault("created_at", self.now_iso())
            pr.setdefault("body", "")
        self.pr_comments = list(data.get("pr_comments", []))
        for c in self.pr_comments:
            c.setdefault("type", "comment")
            c.setdefault("created_at", self.now_iso())
        self.reactions = list(data.get("reactions", []))
        self._next_pr_number = (
            max(p["number"] for p in self.pull_requests) + 1 if self.pull_requests else 1
        )
        self._next_comment_id = (
            max(c["id"] for c in self.pr_comments) + 1 if self.pr_comments else 1
        )
        self._next_reaction_id = (
            max(r["id"] for r in self.reactions) + 1 if self.reactions else 1
        )

    def get_state(self) -> dict:
        return {
            "branches": self.branches,
            "files": self.files,
            "commits": self.commits,
            "pull_requests": self.pull_requests,
            "pr_comments": self.pr_comments,
            "reactions": self.reactions,
            "host_name": self.host_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "owner": self.owner,
            "repo_name": self.repo_name,
            "default_branch": self.default_branch,
        }

    def _find_pr(self, number: int) -> Optional[dict]:
        return next((p for p in self.pull_requests if p.get("number") == number), None)

    def _pr_comments(self, number: int) -> list[dict]:
        return [c for c in self.pr_comments if c.get("pr_id") == number]

    def _reactions_for(self, target_type: str, target_id) -> list[dict]:
        return [
            r for r in self.reactions
            if r.get("target_type") == target_type and str(r.get("target_id")) == str(target_id)
        ]

    def _serialize_comment(self, c: dict) -> dict:
        rxns = self._reactions_for("comment", c["id"])
        by_emoji: dict[str, list[str]] = {}
        for r in rxns:
            by_emoji.setdefault(r["emoji"], []).append(r["actor"])
        return {
            **c,
            "reactions_grouped": [
                {"emoji": e, "actors": actors, "count": len(actors)}
                for e, actors in by_emoji.items()
            ],
        }

    def _serialize_pr(self, pr: dict) -> dict:
        rxns = self._reactions_for("pr", pr["number"])
        by_emoji: dict[str, list[str]] = {}
        for r in rxns:
            by_emoji.setdefault(r["emoji"], []).append(r["actor"])
        commits_for_pr = [c for c in self.commits if c.get("branch") == pr.get("head_branch")]
        return {
            **pr,
            "reactions_grouped": [
                {"emoji": e, "actors": actors, "count": len(actors)}
                for e, actors in by_emoji.items()
            ],
            "commits": commits_for_pr,
            "comments": [self._serialize_comment(c) for c in self._pr_comments(pr["number"])],
        }

    def _diff_for_branch(self, head: str, base: str) -> list[dict]:
        base_files = self.files.get(base, {})
        head_files = self.files.get(head, {})
        out = []
        for p in sorted(set(base_files) | set(head_files)):
            b, h = base_files.get(p), head_files.get(p)
            if b == h:
                continue
            out.append({
                "path": p,
                "status": "added" if b is None else ("removed" if h is None else "modified"),
                "base_content": b or "",
                "head_content": h or "",
            })
        return out

    def _repo_ctx(self) -> dict:
        return {
            "host_name": self.host_name,
            "owner": self.owner,
            "repo_name": self.repo_name,
            "user_name": self.user_name,
            "user_handle": self.user_handle,
            "default_branch": self.default_branch,
            "branches": list(self.branches.keys()),
        }

    def _create_branch_internal(self, name: str, base: str, via: str = "api") -> Optional[str]:
        if name in self.branches:
            return "branch already exists"
        if base not in self.branches:
            return f"base branch {base} not found"
        self.branches[name] = {
            "head_commit": self.branches[base]["head_commit"],
            "created_from": base,
            "created_at": self.now_iso(),
        }
        self.files[name] = dict(self.files.get(base, {}))
        self.log_action("create_branch", branch=name, base=base, actor=self.user_handle, via=via)
        return None

    def _commit_internal(self, branch: str, message: str, file_changes: dict,
                         via: str = "api") -> Optional[str]:
        if branch not in self.branches:
            return "branch not found"
        if not file_changes:
            return "files must be non-empty"
        target = self.files.setdefault(branch, {})
        paths_written = []
        for p, content in file_changes.items():
            if content is None:
                target.pop(p, None)
            else:
                target[p] = str(content)
            paths_written.append(p)
        ts = self.now_iso()
        sha = _short_sha(f"{branch}-{message}-{ts}")
        self.branches[branch]["head_commit"] = sha
        self.commits.append({
            "sha": sha,
            "branch": branch,
            "author": self.user_handle,
            "message": message,
            "ts": ts,
            "files": paths_written,
        })
        self.log_action(
            "commit",
            branch=branch,
            sha=sha,
            message=message,
            files=paths_written,
            actor=self.user_handle,
            via=via,
        )
        return None

    def _open_pr_internal(self, title: str, head: str, base: str, body: str,
                          via: str = "api") -> tuple[Optional[dict], Optional[str]]:
        if head not in self.branches:
            return None, f"head branch {head} not found"
        if not body:
            template = self.files.get(base, {}).get(".github/PULL_REQUEST_TEMPLATE.md", "")
            if template:
                body = template
        number = self._next_pr_number
        self._next_pr_number += 1
        pr = {
            "number": number,
            "title": title,
            "body": body,
            "head_branch": head,
            "base_branch": base,
            "author": self.user_handle,
            "state": "open",
            "created_at": self.now_iso(),
        }
        self.pull_requests.append(pr)
        self.log_action(
            "open_pr",
            pr_number=number,
            title=title,
            head=head,
            base=base,
            body_chars=len(body),
            actor=self.user_handle,
            via=via,
        )
        return pr, None

    def _register_routes(self) -> None:
        svc = self

        @self.router.post("/home", operation_id="view_repo_home",
                          summary="View the repository home page")
        def view_repo_home() -> dict:
            files = svc.files.get(svc.default_branch, {})
            file_list = sorted(files.keys())
            open_prs = [svc._serialize_pr(p) for p in svc.pull_requests if p.get("state") == "open"]
            svc.log_action("view_repo_home", file_count=len(file_list), open_pr_count=len(open_prs))
            return {
                **svc._repo_ctx(),
                "file_list": file_list,
                "readme": files.get("README.md", ""),
                "open_prs": open_prs,
                "head_sha": svc.branches.get(svc.default_branch, {}).get("head_commit", ""),
            }

        @self.router.post("/commits", operation_id="view_commits",
                          summary="List commits on a branch")
        def view_commits(req: BranchRequest):
            if req.branch not in svc.branches:
                return JSONResponse(status_code=404, content={"error": "branch not found"})
            commits = sorted(
                [c for c in svc.commits if c.get("branch") == req.branch],
                key=lambda c: str(c.get("ts", "")),
                reverse=True,
            )
            svc.log_action("view_commits", branch=req.branch, count=len(commits))
            return {**svc._repo_ctx(), "branch": req.branch, "commits": commits}

        @self.router.post("/file", operation_id="view_file",
                          summary="View a file at a branch")
        def view_file(req: FileRequest):
            content = svc.files.get(req.branch, {}).get(req.path)
            if content is None:
                return JSONResponse(status_code=404, content={"error": "file not found"})
            svc.log_action("view_file", branch=req.branch, path=req.path, bytes=len(content))
            return {
                **svc._repo_ctx(),
                "branch": req.branch,
                "path": req.path,
                "content": content,
            }

        @self.router.post("/edit/form", operation_id="view_edit_form",
                          summary="Open the file edit / new-file form")
        def view_edit_form(req: EditFormRequest) -> dict:
            existing = svc.files.get(req.base_branch, {}).get(req.path, "")
            mode = "edit" if req.path else "new"
            svc.log_action("view_edit_form", base_branch=req.base_branch, path=req.path, mode=mode)
            return {
                **svc._repo_ctx(),
                "base_branch": req.base_branch,
                "path": req.path,
                "content": existing,
                "mode": mode,
            }

        @self.router.post("/edit/submit", operation_id="submit_edit",
                          summary="Commit file changes (optionally on a new branch and open a PR)")
        def submit_edit(req: EditSubmitRequest):
            if not req.path.strip():
                return JSONResponse(status_code=400, content={"error": "path required"})
            base_branch = req.base_branch or svc.default_branch
            target_branch = (req.target_branch or "").strip()
            new_branch_name = (req.new_branch_name or "").strip()
            commit_message = (req.commit_message or "").strip() or f"update {req.path}"

            if not target_branch or target_branch == "__new__":
                if not new_branch_name:
                    return JSONResponse(status_code=400, content={"error": "new_branch_name required"})
                err = svc._create_branch_internal(new_branch_name, base_branch, via="web_form")
                if err:
                    code = 409 if "exists" in err else 404
                    return JSONResponse(status_code=code, content={"error": err})
                target_branch = new_branch_name
            elif target_branch not in svc.branches:
                return JSONResponse(status_code=404, content={"error": "target branch not found"})

            err = svc._commit_internal(
                target_branch, commit_message, {req.path: req.content}, via="web_form"
            )
            if err:
                return JSONResponse(status_code=400, content={"error": err})

            if req.open_pr:
                pr_title = (req.pr_title or "").strip() or commit_message
                pr, err = svc._open_pr_internal(
                    pr_title, target_branch, base_branch, req.pr_body, via="web_form"
                )
                if err:
                    return JSONResponse(status_code=400, content={"error": err})
                return {"pr": svc._serialize_pr(pr), **svc._repo_ctx()}

            return {"branch": target_branch, "path": req.path, **svc._repo_ctx()}

        @self.router.post("/pulls/new/form", operation_id="view_new_pr_form",
                          summary="Open the new pull-request form")
        def view_new_pr_form() -> dict:
            svc.log_action("view_new_pr_form")
            return svc._repo_ctx()

        @self.router.post("/pulls/new", operation_id="submit_new_pr",
                          summary="Open a new pull request via the web form")
        def submit_new_pr(req: NewPrRequest):
            if not req.head or not req.title:
                return JSONResponse(status_code=400, content={"error": "head and title required"})
            pr, err = svc._open_pr_internal(req.title, req.head, req.base, req.body, via="web_form")
            if err:
                return JSONResponse(status_code=404, content={"error": err})
            return {"pr": svc._serialize_pr(pr), **svc._repo_ctx()}

        @self.router.post("/pulls/list", operation_id="view_pull_list",
                          summary="List pull requests")
        def view_pull_list() -> dict:
            prs = sorted(
                [svc._serialize_pr(p) for p in svc.pull_requests],
                key=lambda p: str(p.get("created_at", "")),
                reverse=True,
            )
            svc.log_action("view_pull_list", pr_count=len(prs))
            return {**svc._repo_ctx(), "pull_requests": prs}

        @self.router.post("/pulls/detail", operation_id="view_pr",
                          summary="View a pull request detail page")
        def view_pr(req: PrNumberRequest):
            pr = svc._find_pr(req.pr_number)
            if not pr:
                svc.log_action("view_pr", pr_number=req.pr_number, status=404)
                return svc.error_response(
                    code="not_found", message="pr not found", status=404,
                )
            ser = svc._serialize_pr(pr)
            diff = svc._diff_for_branch(
                pr.get("head_branch", ""), pr.get("base_branch", ""),
            )
            svc.log_action(
                "view_pr",
                pr_number=req.pr_number,
                pr_title=pr.get("title", ""),
                pr_author=pr.get("author", ""),
                comment_count=len(ser["comments"]),
                reaction_count=sum(g["count"] for g in ser["reactions_grouped"]),
            )
            return {**svc._repo_ctx(), "pr": ser, "diff": diff}

        @self.router.post("/branches", operation_id="create_branch",
                          summary="Create a branch from an existing branch")
        def create_branch(req: CreateBranchRequest):
            err = svc._create_branch_internal(req.name, req.from_branch)
            if err:
                code = 409 if "exists" in err else 404
                return JSONResponse(status_code=code, content={"ok": False, "error": err})
            return {"ok": True, "branch": req.name, "from": req.from_branch}

        @self.router.post("/commits/write", operation_id="commit",
                          summary="Commit file changes to a branch")
        def commit_files(req: CommitRequest):
            err = svc._commit_internal(req.branch, req.message, req.files)
            if err:
                return JSONResponse(status_code=400, content={"ok": False, "error": err})
            sha = svc.branches[req.branch]["head_commit"]
            return {"ok": True, "sha": sha, "branch": req.branch}

        @self.router.post("/pulls", operation_id="open_pr",
                          summary="Open a new pull request")
        def open_pull(req: OpenPrRequest):
            if not req.title or not req.head:
                return JSONResponse(status_code=400, content={"ok": False, "error": "title and head required"})
            pr, err = svc._open_pr_internal(req.title, req.head, req.base, req.body)
            if err:
                return JSONResponse(status_code=404, content={"ok": False, "error": err})
            return {"ok": True, "number": pr["number"], "pr": svc._serialize_pr(pr)}

        @self.router.post("/pulls/comment", operation_id="add_pr_comment",
                          summary="Add a comment on a pull request")
        def add_comment(req: AddCommentRequest):
            pr = svc._find_pr(req.pr_number)
            if not pr:
                return JSONResponse(status_code=404, content={"error": "pr not found"})
            ctype = req.comment_type if req.comment_type in ("comment", "review_comment", "review") else "comment"
            cid = svc._next_comment_id
            svc._next_comment_id += 1
            body = (req.body or "").strip()
            svc.pr_comments.append({
                "id": cid,
                "pr_id": req.pr_number,
                "author": svc.user_handle,
                "body": body,
                "type": ctype,
                "created_at": svc.now_iso(),
            })
            svc.log_action(
                "add_pr_comment",
                pr_number=req.pr_number,
                comment_id=cid,
                comment_type=ctype,
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == svc.user_handle),
                body=body,
                body_chars=len(body),
                actor=svc.user_handle,
            )
            return {"ok": True, "comment_id": cid}

        @self.router.post("/pulls/react", operation_id="react_to_pr",
                          summary="React with an emoji to a pull request")
        def react_to_pr(req: ReactPrRequest):
            pr = svc._find_pr(req.pr_number)
            if not pr:
                return JSONResponse(status_code=404, content={"error": "pr not found"})
            emoji = (req.emoji or "").strip()
            if not emoji:
                return JSONResponse(status_code=400, content={"ok": False, "error": "emoji required"})
            rid = svc._next_reaction_id
            svc._next_reaction_id += 1
            svc.reactions.append({
                "id": rid,
                "target_type": "pr",
                "target_id": req.pr_number,
                "emoji": emoji,
                "actor": svc.user_handle,
                "created_at": svc.now_iso(),
            })
            svc.log_action(
                "react_to_pr",
                pr_number=req.pr_number,
                emoji=emoji,
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == svc.user_handle),
                actor=svc.user_handle,
            )
            return {"ok": True, "reaction_id": rid}

        @self.router.post("/pulls/comment/react", operation_id="react_to_comment",
                          summary="React with an emoji to a PR comment")
        def react_to_comment(req: ReactCommentRequest):
            pr = svc._find_pr(req.pr_number)
            if not pr:
                return JSONResponse(status_code=404, content={"error": "pr not found"})
            comment = next((c for c in svc.pr_comments if c["id"] == req.comment_id), None)
            if not comment or comment.get("pr_id") != req.pr_number:
                return JSONResponse(status_code=404, content={"error": "comment not found"})
            emoji = (req.emoji or "").strip()
            if not emoji:
                return JSONResponse(status_code=400, content={"ok": False, "error": "emoji required"})
            rid = svc._next_reaction_id
            svc._next_reaction_id += 1
            svc.reactions.append({
                "id": rid,
                "target_type": "comment",
                "target_id": req.comment_id,
                "emoji": emoji,
                "actor": svc.user_handle,
                "created_at": svc.now_iso(),
            })
            svc.log_action(
                "react_to_comment",
                pr_number=req.pr_number,
                comment_id=req.comment_id,
                emoji=emoji,
                comment_author=comment.get("author", ""),
                self_authored_comment=(comment.get("author") == svc.user_handle),
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == svc.user_handle),
                actor=svc.user_handle,
            )
            return {"ok": True, "reaction_id": rid}


def create_service(service_id: str) -> GithubRepoService:
    return GithubRepoService(service_id)
