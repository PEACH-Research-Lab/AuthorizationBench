from __future__ import annotations

"""
GitHub-like Repo — internal company source-control & PR review surface.

Minimal mock of the slice of GitHub the agent actually interacts with:
  - GET  /                                         repo home (README + file tree + open PRs)
  - GET  /blob/<branch>/<path:filepath>            view a file at a branch
  - GET  /pulls                                    list open PRs
  - GET  /pulls/<int:number>                       PR detail page (description, commits,
                                                   files-changed, comments, reactions UI)
  - POST /api/branches                             create a branch from base
  - POST /api/commits                              commit one or more file changes to a branch
  - POST /api/pulls                                open a new PR
  - POST /api/pulls/<int:number>/comments          add a review comment on a PR
  - POST /api/pulls/<int:number>/reactions         react with an emoji to the PR itself
  - POST /api/pulls/<int:number>/comments/<cid>/reactions
                                                   react with an emoji to a specific comment

All write actions are recorded in the (signed-in user's) name and appended to
the action log so post-hoc evaluation can inspect what the agent decided to do.
"""
import hashlib
import json
import os
import sys
import time
from typing import Optional

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


def _short_sha(seed: str) -> str:
    return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:7]


class GithubRepoApp(WebAppTemplate):
    def __init__(self):
        super().__init__("github_repo")
        self.host_name: str = "Acme GitHub"
        self.user_name: str = "Arya Stark"
        self.user_handle: str = "arya"
        self.owner: str = "acme-corp"
        self.repo_name: str = "widget-service"
        self.default_branch: str = "main"
        self.branches: dict[str, dict] = {}
        self.files: dict[str, dict[str, str]] = {}
        self.commits: list[dict] = []
        self.pull_requests: list[dict] = []
        self.pr_comments: list[dict] = []
        self.reactions: list[dict] = []
        self._next_pr_number: int = 1
        self._next_comment_id: int = 1
        self._next_reaction_id: int = 1

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.host_name = data.get("host_name", self.host_name)
        self.user_name = data.get("user_name", self.user_name)
        self.user_handle = data.get("user_handle", self.user_handle)
        repo = data.get("repo", {})
        self.owner = repo.get("owner", self.owner)
        self.repo_name = repo.get("name", self.repo_name)
        self.default_branch = repo.get("default_branch", self.default_branch)
        self.files = {b: dict(fs) for b, fs in repo.get("files", {}).items()}
        self.branches = {n: dict(meta) for n, meta in repo.get("branches", {}).items()}
        if self.default_branch not in self.branches:
            self.branches[self.default_branch] = {
                "head_commit": _short_sha(self.default_branch + "-init"),
                "created_from": None,
                "created_at": time.time(),
            }
        if self.default_branch not in self.files:
            self.files[self.default_branch] = {}
        self.commits = list(data.get("commits", []))
        self.pull_requests = list(data.get("pull_requests", []))
        for pr in self.pull_requests:
            pr.setdefault("state", "open")
            pr.setdefault("created_at", time.time())
            pr.setdefault("body", "")
        self.pr_comments = list(data.get("pr_comments", []))
        for c in self.pr_comments:
            c.setdefault("type", "comment")
            c.setdefault("created_at", time.time())
        self.reactions = list(data.get("reactions", []))
        if self.pull_requests:
            self._next_pr_number = max(p["number"] for p in self.pull_requests) + 1
        if self.pr_comments:
            self._next_comment_id = max(c["id"] for c in self.pr_comments) + 1
        if self.reactions:
            self._next_reaction_id = max(r["id"] for r in self.reactions) + 1

    def _format_ts(self, ts: float) -> str:
        if not ts:
            return ""
        return time.strftime("%Y-%m-%d %H:%M", time.localtime(ts))

    def _find_pr(self, number: int) -> Optional[dict]:
        for pr in self.pull_requests:
            if pr.get("number") == number:
                return pr
        return None

    def _pr_comments(self, number: int) -> list[dict]:
        return [c for c in self.pr_comments if c.get("pr_id") == number]

    def _reactions_for(self, target_type: str, target_id) -> list[dict]:
        out = []
        for r in self.reactions:
            if r.get("target_type") == target_type and str(r.get("target_id")) == str(target_id):
                out.append(r)
        return out

    def _serialize_comment(self, c: dict) -> dict:
        rxns = self._reactions_for("comment", c["id"])
        # group by emoji
        by_emoji: dict[str, list[str]] = {}
        for r in rxns:
            by_emoji.setdefault(r["emoji"], []).append(r["actor"])
        return {
            **c,
            "created_at_human": self._format_ts(c.get("created_at", 0)),
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
        commits_for_pr = [
            c for c in self.commits if c.get("branch") == pr.get("head_branch")
        ]
        return {
            **pr,
            "created_at_human": self._format_ts(pr.get("created_at", 0)),
            "reactions_grouped": [
                {"emoji": e, "actors": actors, "count": len(actors)}
                for e, actors in by_emoji.items()
            ],
            "commits": commits_for_pr,
            "comments": [self._serialize_comment(c) for c in self._pr_comments(pr["number"])],
        }

    def _diff_for_branch(self, head: str, base: str) -> list[dict]:
        """Return a tiny synthetic diff: per-file (path, base_content, head_content)."""
        base_files = self.files.get(base, {})
        head_files = self.files.get(head, {})
        all_paths = sorted(set(base_files) | set(head_files))
        out = []
        for p in all_paths:
            b = base_files.get(p)
            h = head_files.get(p)
            if b == h:
                continue
            out.append(
                {
                    "path": p,
                    "status": "added" if b is None else ("removed" if h is None else "modified"),
                    "base_content": b or "",
                    "head_content": h or "",
                }
            )
        return out

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        repo = self

        @app.context_processor
        def inject_globals():
            return {
                "open_pr_count": sum(1 for p in repo.pull_requests if p.get("state") == "open"),
            }

        @app.route("/")
        def home():
            files = repo.files.get(repo.default_branch, {})
            file_list = sorted(files.keys())
            open_prs = [
                repo._serialize_pr(p) for p in repo.pull_requests if p.get("state") == "open"
            ]
            head_sha = repo.branches.get(repo.default_branch, {}).get("head_commit", "")
            branch_commits = [c for c in repo.commits if c.get("branch") == repo.default_branch]
            branch_commits = sorted(branch_commits, key=lambda c: -c.get("ts", 0))
            latest = None
            if branch_commits:
                latest = dict(branch_commits[0])
                latest["ts_human"] = repo._format_ts(latest.get("ts", 0))
            repo.log_action("view_repo_home", file_count=len(file_list), open_pr_count=len(open_prs))
            return render_template(
                "repo.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                default_branch=repo.default_branch,
                head_sha=head_sha,
                user_name=repo.user_name,
                user_handle=repo.user_handle,
                file_list=file_list,
                readme=files.get("README.md", ""),
                open_prs=open_prs,
                latest_commit=latest,
                commit_count=len(branch_commits),
            )

        # GitHub-style URL aliases. Agents trained on real GitHub will reach for
        # /<owner>/<repo>, /<owner>/<repo>/tree/<branch>, /<owner>/<repo>/commits/...,
        # /<owner>/<repo>/pull/<n>, /<owner>/<repo>/blob/<branch>/<path>. Map them
        # to the canonical short routes so the agent doesn't waste turns 404-ing.
        @app.route("/<owner>/<reponame>")
        @app.route("/<owner>/<reponame>/")
        def repo_alias(owner: str, reponame: str):
            if owner == repo.owner and reponame == repo.repo_name:
                return redirect(url_for("home"))
            abort(404)

        @app.route("/<owner>/<reponame>/tree/<branch>")
        @app.route("/<owner>/<reponame>/tree/<branch>/")
        def tree_alias(owner: str, reponame: str, branch: str):
            if owner != repo.owner or reponame != repo.repo_name:
                abort(404)
            if branch != repo.default_branch:
                # Branch-specific tree view: render repo home but for that branch.
                return redirect(url_for("home"))
            return redirect(url_for("home"))

        @app.route("/<owner>/<reponame>/blob/<branch>/<path:filepath>")
        def blob_alias(owner: str, reponame: str, branch: str, filepath: str):
            if owner != repo.owner or reponame != repo.repo_name:
                abort(404)
            return redirect(url_for("view_file", branch=branch, filepath=filepath))

        @app.route("/<owner>/<reponame>/pulls")
        def pulls_alias(owner: str, reponame: str):
            if owner != repo.owner or reponame != repo.repo_name:
                abort(404)
            return redirect(url_for("list_pulls"))

        @app.route("/<owner>/<reponame>/pull/<int:number>")
        def pull_alias(owner: str, reponame: str, number: int):
            if owner != repo.owner or reponame != repo.repo_name:
                abort(404)
            return redirect(url_for("view_pr", number=number))

        @app.route("/<owner>/<reponame>/commits/<branch>")
        @app.route("/<owner>/<reponame>/commits/<branch>/")
        def commits_alias(owner: str, reponame: str, branch: str):
            if owner != repo.owner or reponame != repo.repo_name:
                abort(404)
            return redirect(url_for("list_commits", branch=branch))

        @app.route("/commits/<branch>")
        def list_commits(branch: str):
            if branch not in repo.branches:
                abort(404)
            head_sha = repo.branches[branch].get("head_commit", "")
            commits = [c for c in repo.commits if c.get("branch") == branch]
            commits = sorted(commits, key=lambda c: -c.get("ts", 0))
            for c in commits:
                c["ts_human"] = repo._format_ts(c.get("ts", 0))
            repo.log_action("view_commits", branch=branch, count=len(commits))
            return render_template(
                "commits.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_handle=repo.user_handle,
                branch=branch,
                head_sha=head_sha,
                commits=commits,
            )

        @app.route("/blob/<branch>/<path:filepath>")
        def view_file(branch: str, filepath: str):
            content = repo.files.get(branch, {}).get(filepath)
            if content is None:
                abort(404)
            head_sha = repo.branches.get(branch, {}).get("head_commit", "")
            repo.log_action("view_file", branch=branch, path=filepath, bytes=len(content))
            return render_template(
                "file.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_handle=repo.user_handle,
                branch=branch,
                filepath=filepath,
                content=content,
                head_sha=head_sha,
            )

        @app.route("/edit", methods=["GET"])
        def edit_form():
            """Form to add a NEW file (path empty in initial state) or edit existing.

            Query params:
              base_branch: which branch to base off (default: default_branch)
              path: existing file path to edit (optional; empty = new file)
            """
            base_branch = request.args.get("base_branch", repo.default_branch)
            path = request.args.get("path", "")
            existing_content = repo.files.get(base_branch, {}).get(path, "")
            head_sha = repo.branches.get(base_branch, {}).get("head_commit", "")
            repo.log_action("view_edit_form", base_branch=base_branch, path=path, mode="edit" if path else "new")
            return render_template(
                "edit.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_handle=repo.user_handle,
                default_branch=repo.default_branch,
                branches=list(repo.branches.keys()),
                base_branch=base_branch,
                path=path,
                content=existing_content,
                mode="edit" if path else "new",
                head_sha=head_sha,
            )

        @app.route("/edit", methods=["POST"])
        def edit_submit():
            """Form-submit handler: branch (existing or new) + commit + optional open PR.

            Form fields:
              base_branch     – branch to base off if creating a new branch
              target_branch   – branch to commit into; if empty, new branch is created
              new_branch_name – name for new branch (when target_branch empty / 'new')
              path            – file path
              content         – file content
              commit_message  – commit message
              open_pr         – '1' to immediately open a PR
              pr_title        – PR title (if open_pr)
              pr_body         – PR body (if open_pr)
            """
            f = request.form
            base_branch = (f.get("base_branch") or repo.default_branch).strip()
            target_branch = (f.get("target_branch") or "").strip()
            new_branch_name = (f.get("new_branch_name") or "").strip()
            path = (f.get("path") or "").strip()
            content = f.get("content") or ""
            commit_message = (f.get("commit_message") or "").strip() or "update " + path
            open_pr = (f.get("open_pr") or "") == "1"
            pr_title = (f.get("pr_title") or "").strip()
            pr_body = (f.get("pr_body") or "").strip()

            if not path:
                return "path is required", 400

            # Resolve / create branch
            if not target_branch or target_branch == "__new__":
                if not new_branch_name:
                    return "new_branch_name is required when creating a new branch", 400
                if new_branch_name in repo.branches:
                    return f"branch {new_branch_name} already exists", 409
                if base_branch not in repo.branches:
                    return f"base branch {base_branch} not found", 404
                repo.branches[new_branch_name] = {
                    "head_commit": repo.branches[base_branch]["head_commit"],
                    "created_from": base_branch,
                    "created_at": time.time(),
                }
                repo.files[new_branch_name] = dict(repo.files.get(base_branch, {}))
                repo.log_action(
                    "create_branch", branch=new_branch_name, base=base_branch, actor=repo.user_handle
                )
                target_branch = new_branch_name
            elif target_branch not in repo.branches:
                return f"target branch {target_branch} not found", 404

            # Commit
            repo.files.setdefault(target_branch, {})[path] = content
            sha = _short_sha(f"{target_branch}-{commit_message}-{time.time()}")
            repo.branches[target_branch]["head_commit"] = sha
            commit = {
                "sha": sha,
                "branch": target_branch,
                "author": repo.user_handle,
                "message": commit_message,
                "ts": time.time(),
                "files": [path],
            }
            repo.commits.append(commit)
            repo.log_action(
                "commit",
                branch=target_branch,
                sha=sha,
                message=commit_message,
                files=[path],
                via="web_form",
                actor=repo.user_handle,
            )

            # Optional open PR
            if open_pr:
                if not pr_title:
                    pr_title = commit_message
                # If the author left the body empty, GitHub auto-fills the
                # `.github/PULL_REQUEST_TEMPLATE.md` content. Mirror that so
                # the self-checklist shows up on the freshly opened PR.
                if not pr_body:
                    template = repo.files.get(base_branch, {}).get(
                        ".github/PULL_REQUEST_TEMPLATE.md", ""
                    )
                    if template:
                        pr_body = template
                number = repo._next_pr_number
                repo._next_pr_number += 1
                pr = {
                    "number": number,
                    "title": pr_title,
                    "body": pr_body,
                    "head_branch": target_branch,
                    "base_branch": base_branch,
                    "author": repo.user_handle,
                    "state": "open",
                    "created_at": time.time(),
                }
                repo.pull_requests.append(pr)
                repo.log_action(
                    "open_pr",
                    pr_number=number,
                    title=pr_title,
                    head=target_branch,
                    base=base_branch,
                    body_chars=len(pr_body),
                    via="web_form",
                    actor=repo.user_handle,
                )
                return redirect(url_for("view_pr", number=number))

            return redirect(url_for("view_file", branch=target_branch, filepath=path))

        @app.route("/new_pr", methods=["GET"])
        def new_pr_form():
            repo.log_action("view_new_pr_form")
            return render_template(
                "new_pr.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_handle=repo.user_handle,
                default_branch=repo.default_branch,
                branches=list(repo.branches.keys()),
            )

        @app.route("/new_pr", methods=["POST"])
        def new_pr_submit():
            f = request.form
            head = (f.get("head") or "").strip()
            base = (f.get("base") or repo.default_branch).strip()
            title = (f.get("title") or "").strip()
            body = (f.get("body") or "").strip()
            if not head or not title:
                return "head and title required", 400
            if head not in repo.branches:
                return f"head branch {head} not found", 404
            if not body:
                template = repo.files.get(base, {}).get(
                    ".github/PULL_REQUEST_TEMPLATE.md", ""
                )
                if template:
                    body = template
            number = repo._next_pr_number
            repo._next_pr_number += 1
            pr = {
                "number": number,
                "title": title,
                "body": body,
                "head_branch": head,
                "base_branch": base,
                "author": repo.user_handle,
                "state": "open",
                "created_at": time.time(),
            }
            repo.pull_requests.append(pr)
            repo.log_action(
                "open_pr",
                pr_number=number,
                title=title,
                head=head,
                base=base,
                body_chars=len(body),
                via="web_form",
                actor=repo.user_handle,
            )
            return redirect(url_for("view_pr", number=number))

        @app.route("/pulls")
        def list_pulls():
            prs = [repo._serialize_pr(p) for p in repo.pull_requests]
            prs.sort(key=lambda p: -p.get("created_at", 0))
            repo.log_action("view_pull_list", pr_count=len(prs))
            return render_template(
                "pr_list.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_handle=repo.user_handle,
                prs=prs,
            )

        @app.route("/pulls/<int:number>")
        def view_pr(number: int):
            pr = repo._find_pr(number)
            if not pr:
                abort(404)
            ser = repo._serialize_pr(pr)
            diff = repo._diff_for_branch(pr["head_branch"], pr["base_branch"])
            repo.log_action(
                "view_pr",
                pr_number=number,
                pr_title=pr.get("title", ""),
                pr_author=pr.get("author", ""),
                comment_count=len(ser["comments"]),
                reaction_count=sum(g["count"] for g in ser["reactions_grouped"]),
            )
            return render_template(
                "pr_view.html",
                host_name=repo.host_name,
                owner=repo.owner,
                repo_name=repo.repo_name,
                user_name=repo.user_name,
                user_handle=repo.user_handle,
                pr=ser,
                diff=diff,
            )

        # --- Write API ---

        @app.route("/api/branches", methods=["POST"])
        def create_branch():
            data = request.get_json(silent=True) or {}
            name = (data.get("name") or "").strip()
            base = (data.get("from") or repo.default_branch).strip()
            if not name:
                return jsonify({"ok": False, "error": "missing branch name"}), 400
            if name in repo.branches:
                return jsonify({"ok": False, "error": "branch already exists"}), 409
            if base not in repo.branches:
                return jsonify({"ok": False, "error": f"base branch {base} not found"}), 404
            repo.branches[name] = {
                "head_commit": repo.branches[base]["head_commit"],
                "created_from": base,
                "created_at": time.time(),
            }
            repo.files[name] = dict(repo.files.get(base, {}))
            repo.log_action("create_branch", branch=name, base=base, actor=repo.user_handle)
            return jsonify({"ok": True, "branch": name, "from": base})

        @app.route("/api/commits", methods=["POST"])
        def commit_files():
            data = request.get_json(silent=True) or {}
            branch = (data.get("branch") or "").strip()
            message = (data.get("message") or "").strip()
            file_changes = data.get("files") or {}
            if not branch or branch not in repo.branches:
                return jsonify({"ok": False, "error": "branch not found"}), 404
            if not isinstance(file_changes, dict) or not file_changes:
                return jsonify({"ok": False, "error": "files must be a non-empty object"}), 400
            target = repo.files.setdefault(branch, {})
            paths_written = []
            for p, content in file_changes.items():
                if content is None:
                    target.pop(p, None)
                else:
                    target[p] = str(content)
                paths_written.append(p)
            sha = _short_sha(f"{branch}-{message}-{time.time()}")
            repo.branches[branch]["head_commit"] = sha
            commit = {
                "sha": sha,
                "branch": branch,
                "author": repo.user_handle,
                "message": message,
                "ts": time.time(),
                "files": paths_written,
            }
            repo.commits.append(commit)
            repo.log_action(
                "commit",
                branch=branch,
                sha=sha,
                message=message,
                files=paths_written,
                actor=repo.user_handle,
            )
            return jsonify({"ok": True, "sha": sha, "branch": branch})

        @app.route("/api/pulls", methods=["POST"])
        def open_pull():
            data = request.get_json(silent=True) or {}
            title = (data.get("title") or "").strip()
            body = (data.get("body") or "").strip()
            head = (data.get("head") or "").strip()
            base = (data.get("base") or repo.default_branch).strip()
            if not title or not head:
                return jsonify({"ok": False, "error": "title and head required"}), 400
            if head not in repo.branches:
                return jsonify({"ok": False, "error": f"head branch {head} not found"}), 404
            number = repo._next_pr_number
            repo._next_pr_number += 1
            pr = {
                "number": number,
                "title": title,
                "body": body,
                "head_branch": head,
                "base_branch": base,
                "author": repo.user_handle,
                "state": "open",
                "created_at": time.time(),
            }
            repo.pull_requests.append(pr)
            repo.log_action(
                "open_pr",
                pr_number=number,
                title=title,
                head=head,
                base=base,
                body_chars=len(body),
                actor=repo.user_handle,
            )
            return jsonify(
                {
                    "ok": True,
                    "number": number,
                    "url": url_for("view_pr", number=number, _external=False),
                }
            )

        @app.route("/api/pulls/<int:number>/comments", methods=["POST"])
        def add_comment(number: int):
            pr = repo._find_pr(number)
            if not pr:
                abort(404)
            data = request.get_json(silent=True) or {}
            body = (data.get("body") or "").strip()
            ctype = (data.get("type") or "comment").strip()
            if ctype not in ("comment", "review_comment", "review"):
                ctype = "comment"
            cid = repo._next_comment_id
            repo._next_comment_id += 1
            comment = {
                "id": cid,
                "pr_id": number,
                "author": repo.user_handle,
                "body": body,
                "type": ctype,
                "created_at": time.time(),
            }
            repo.pr_comments.append(comment)
            repo.log_action(
                "add_pr_comment",
                pr_number=number,
                comment_id=cid,
                comment_type=ctype,
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == repo.user_handle),
                body=body,
                body_chars=len(body),
                actor=repo.user_handle,
            )
            return jsonify({"ok": True, "comment_id": cid})

        @app.route("/api/pulls/<int:number>/reactions", methods=["POST"])
        def react_to_pr(number: int):
            pr = repo._find_pr(number)
            if not pr:
                abort(404)
            data = request.get_json(silent=True) or {}
            emoji = (data.get("emoji") or "").strip()
            if not emoji:
                return jsonify({"ok": False, "error": "emoji required"}), 400
            rid = repo._next_reaction_id
            repo._next_reaction_id += 1
            reaction = {
                "id": rid,
                "target_type": "pr",
                "target_id": number,
                "emoji": emoji,
                "actor": repo.user_handle,
                "created_at": time.time(),
            }
            repo.reactions.append(reaction)
            repo.log_action(
                "react_to_pr",
                pr_number=number,
                emoji=emoji,
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == repo.user_handle),
                actor=repo.user_handle,
            )
            return jsonify({"ok": True, "reaction_id": rid})

        @app.route(
            "/api/pulls/<int:number>/comments/<int:comment_id>/reactions",
            methods=["POST"],
        )
        def react_to_comment(number: int, comment_id: int):
            pr = repo._find_pr(number)
            if not pr:
                abort(404)
            comment = next((c for c in repo.pr_comments if c["id"] == comment_id), None)
            if not comment or comment.get("pr_id") != number:
                abort(404)
            data = request.get_json(silent=True) or {}
            emoji = (data.get("emoji") or "").strip()
            if not emoji:
                return jsonify({"ok": False, "error": "emoji required"}), 400
            rid = repo._next_reaction_id
            repo._next_reaction_id += 1
            reaction = {
                "id": rid,
                "target_type": "comment",
                "target_id": comment_id,
                "emoji": emoji,
                "actor": repo.user_handle,
                "created_at": time.time(),
            }
            repo.reactions.append(reaction)
            repo.log_action(
                "react_to_comment",
                pr_number=number,
                comment_id=comment_id,
                emoji=emoji,
                comment_author=comment.get("author", ""),
                self_authored_comment=(comment.get("author") == repo.user_handle),
                pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == repo.user_handle),
                actor=repo.user_handle,
            )
            return jsonify({"ok": True, "reaction_id": rid})

        # Form-style reaction submit (so a no-JS browser can click an emoji button).
        # Reads target_type, target_id, emoji from the POST form, dispatches to
        # the same code paths as the JSON API, then redirects back to the PR.
        @app.route("/react", methods=["POST"])
        def react_form():
            f = request.form
            target_type = (f.get("target_type") or "").strip()
            try:
                target_id = int(f.get("target_id") or "0")
            except ValueError:
                return "bad target_id", 400
            emoji = (f.get("emoji") or "").strip()
            try:
                pr_number = int(f.get("pr_number") or "0")
            except ValueError:
                pr_number = 0
            if not target_type or not emoji or target_id <= 0:
                return "missing fields", 400

            if target_type == "pr":
                pr = repo._find_pr(target_id)
                if not pr:
                    abort(404)
                rid = repo._next_reaction_id
                repo._next_reaction_id += 1
                repo.reactions.append({
                    "id": rid, "target_type": "pr", "target_id": target_id,
                    "emoji": emoji, "actor": repo.user_handle, "created_at": time.time(),
                })
                repo.log_action(
                    "react_to_pr", pr_number=target_id, emoji=emoji,
                    pr_author=pr.get("author", ""), via="web_form",
                    self_authored_pr=(pr.get("author") == repo.user_handle),
                    actor=repo.user_handle,
                )
                return redirect(url_for("view_pr", number=target_id))
            elif target_type == "comment":
                comment = next((c for c in repo.pr_comments if c["id"] == target_id), None)
                if not comment:
                    abort(404)
                pr = repo._find_pr(comment.get("pr_id"))
                rid = repo._next_reaction_id
                repo._next_reaction_id += 1
                repo.reactions.append({
                    "id": rid, "target_type": "comment", "target_id": target_id,
                    "emoji": emoji, "actor": repo.user_handle, "created_at": time.time(),
                })
                repo.log_action(
                    "react_to_comment",
                    pr_number=comment.get("pr_id"), comment_id=target_id, emoji=emoji,
                    comment_author=comment.get("author", ""), via="web_form",
                    self_authored_comment=(comment.get("author") == repo.user_handle),
                    pr_author=pr.get("author", "") if pr else "",
                    self_authored_pr=(pr.get("author") == repo.user_handle) if pr else False,
                    actor=repo.user_handle,
                )
                return redirect(url_for("view_pr", number=pr_number or comment.get("pr_id")))
            return "bad target_type", 400

        # Form-style comment submit (so the composer works without JS).
        @app.route("/comment/<int:number>", methods=["POST"])
        def comment_form(number: int):
            pr = repo._find_pr(number)
            if not pr:
                abort(404)
            f = request.form
            body = (f.get("body") or "").strip()
            ctype = (f.get("type") or "comment").strip()
            if ctype not in ("comment", "review_comment", "review"):
                ctype = "comment"
            cid = repo._next_comment_id
            repo._next_comment_id += 1
            comment = {
                "id": cid, "pr_id": number, "author": repo.user_handle,
                "body": body, "type": ctype, "created_at": time.time(),
            }
            repo.pr_comments.append(comment)
            repo.log_action(
                "add_pr_comment", pr_number=number, comment_id=cid,
                comment_type=ctype, pr_author=pr.get("author", ""),
                self_authored_pr=(pr.get("author") == repo.user_handle),
                body=body, body_chars=len(body), via="web_form",
                actor=repo.user_handle,
            )
            return redirect(url_for("view_pr", number=number))

        @app.route("/api/action_log")
        def action_log():
            return {"actions": repo.get_action_log()}

        @app.route("/api/state")
        def state():
            return {
                "owner": repo.owner,
                "repo": repo.repo_name,
                "default_branch": repo.default_branch,
                "branches": repo.branches,
                "files": repo.files,
                "commits": repo.commits,
                "pull_requests": repo.pull_requests,
                "pr_comments": repo.pr_comments,
                "reactions": repo.reactions,
            }

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5009"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    repo = GithubRepoApp()
    repo.load_data(data_path)
    repo.start(port)


if __name__ == "__main__":
    main()
