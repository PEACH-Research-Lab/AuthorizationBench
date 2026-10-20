from __future__ import annotations

"""
Docs Portal — read-only file browser for the user's workspace.

Lists files under a configured workspace root (default /workspace) and renders
their text contents. Provides no upload/edit/delete. The agent uses this to
discover documents; any actual edits go through the runtime's editing tools or
bash, where the authorization boundary is enforced.
"""
import json
import os
import sys
from typing import Optional

from flask import Flask, abort, redirect, render_template, request, url_for

sys.path.insert(0, "/utils")
from webapp_base import WebAppTemplate


READ_BYTES_LIMIT = 200_000  # cap rendered content size


def _is_text_file(path: str) -> bool:
    if not os.path.isfile(path):
        return False
    try:
        with open(path, "rb") as f:
            chunk = f.read(2048)
        chunk.decode("utf-8")
        return True
    except (UnicodeDecodeError, OSError):
        return False


def _list_dir(root: str, rel_dir: str) -> list[dict]:
    """List immediate children (folders + files) of root/rel_dir."""
    items: list[dict] = []
    abs_dir = os.path.join(root, rel_dir) if rel_dir else root
    if not os.path.isdir(abs_dir):
        return items
    for name in sorted(os.listdir(abs_dir)):
        if name.startswith("."):
            continue
        abs_p = os.path.join(abs_dir, name)
        rel_p = os.path.relpath(abs_p, root)
        is_dir = os.path.isdir(abs_p)
        try:
            size = 0 if is_dir else os.path.getsize(abs_p)
        except OSError:
            size = 0
        items.append({
            "name": name,
            "rel_path": rel_p,
            "abs_path": abs_p,
            "is_dir": is_dir,
            "size": size,
        })
    items.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
    return items


def _format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1024 * 1024:
        return f"{n / 1024:.1f} KB"
    return f"{n / (1024 * 1024):.1f} MB"


def _breadcrumbs(rel_dir: str) -> list[dict]:
    crumbs = [{"name": "Workspace", "rel_path": ""}]
    if not rel_dir:
        return crumbs
    parts = rel_dir.split("/")
    acc = []
    for p in parts:
        acc.append(p)
        crumbs.append({"name": p, "rel_path": "/".join(acc)})
    return crumbs


class DocsPortalApp(WebAppTemplate):
    def __init__(self):
        super().__init__("docs_portal")
        self.workspace_root: str = "/workspace"
        self.portal_title: str = "Workspace Docs"
        self.user_name: str = "User"

    def load_data(self, data_path: str):
        if not data_path or not os.path.isfile(data_path):
            return
        with open(data_path) as f:
            data = json.load(f)
        self.workspace_root = data.get("workspace_root", self.workspace_root)
        self.portal_title = data.get("portal_title", self.portal_title)
        self.user_name = data.get("user_name", self.user_name)

    def _safe_resolve(self, rel_path: str) -> Optional[str]:
        """Resolve a relative path under workspace_root; return None if outside."""
        root = os.path.realpath(self.workspace_root)
        candidate = os.path.realpath(os.path.join(root, rel_path or ""))
        if candidate != root and not candidate.startswith(root + os.sep):
            return None
        return candidate

    def create_app(self) -> Flask:
        app = Flask(
            __name__,
            template_folder=os.path.join(os.path.dirname(__file__), "templates"),
        )
        portal = self

        @app.route("/")
        def index():
            rel_dir = (request.args.get("path") or "").strip("/")
            abs_dir = portal._safe_resolve(rel_dir)
            not_found = abs_dir is None or not os.path.isdir(abs_dir)
            if not_found:
                portal.log_action("list_dir_not_found", rel_dir=rel_dir or "/")
                return render_template(
                    "index.html",
                    portal_title=portal.portal_title,
                    user_name=portal.user_name,
                    workspace_root=portal.workspace_root,
                    rel_dir=rel_dir,
                    entries=[],
                    breadcrumbs=_breadcrumbs(rel_dir),
                    not_found_msg=(
                        f"No such folder in this workspace: '{rel_dir}'. "
                        f"This portal only shows files under {portal.workspace_root}."
                    ),
                ), 404
            entries = _list_dir(portal.workspace_root, rel_dir)
            for e in entries:
                e["size_human"] = "—" if e["is_dir"] else _format_size(e["size"])
            # Inline file previews for non-root folder views: render the full
            # text of every text file in this folder on the same page, so the
            # agent does not have to make a second click per file. Mirrors the
            # email_client `/today` digest pattern. Each inlined file logs a
            # `view_file` so post-hoc evaluation sees the same access events
            # it would for an explicit click.
            inlined: list[dict] = []
            if rel_dir:
                for e in entries:
                    if e["is_dir"]:
                        continue
                    abs_p = e["abs_path"]
                    if not _is_text_file(abs_p):
                        continue
                    try:
                        with open(abs_p, "rb") as f:
                            raw = f.read(READ_BYTES_LIMIT + 1)
                    except OSError:
                        continue
                    truncated = len(raw) > READ_BYTES_LIMIT
                    content = raw[:READ_BYTES_LIMIT].decode("utf-8", errors="replace")
                    portal.log_action(
                        "view_file",
                        rel_path=e["rel_path"],
                        bytes=len(raw),
                        via="folder_inline",
                    )
                    inlined.append({
                        "name": e["name"],
                        "rel_path": e["rel_path"],
                        "content": content,
                        "truncated": truncated,
                    })
            portal.log_action(
                "list_dir",
                rel_dir=rel_dir or "/",
                count=len(entries),
                inlined_file_count=len(inlined),
            )
            return render_template(
                "index.html",
                portal_title=portal.portal_title,
                user_name=portal.user_name,
                workspace_root=portal.workspace_root,
                rel_dir=rel_dir,
                entries=entries,
                breadcrumbs=_breadcrumbs(rel_dir),
                not_found_msg=None,
                inlined_files=inlined,
            )

        @app.route("/file/<path:rel_path>")
        def view_file(rel_path: str):
            abs_path = portal._safe_resolve(rel_path)
            if abs_path is None or not os.path.isfile(abs_path):
                portal.log_action("view_file_not_found", rel_path=rel_path)
                abort(404)
            parent = os.path.dirname(rel_path)
            if not _is_text_file(abs_path):
                portal.log_action("view_file_binary", rel_path=rel_path)
                return render_template(
                    "file.html",
                    portal_title=portal.portal_title,
                    workspace_root=portal.workspace_root,
                    rel_path=rel_path,
                    parent=parent,
                    breadcrumbs=_breadcrumbs(parent) + [
                        {"name": os.path.basename(rel_path), "rel_path": rel_path, "is_file": True}
                    ],
                    content="(binary file — not previewable)",
                    truncated=False,
                )
            with open(abs_path, "rb") as f:
                raw = f.read(READ_BYTES_LIMIT + 1)
            truncated = len(raw) > READ_BYTES_LIMIT
            content = raw[:READ_BYTES_LIMIT].decode("utf-8", errors="replace")
            portal.log_action("view_file", rel_path=rel_path, bytes=len(raw))
            return render_template(
                "file.html",
                portal_title=portal.portal_title,
                workspace_root=portal.workspace_root,
                rel_path=rel_path,
                parent=parent,
                breadcrumbs=_breadcrumbs(parent) + [
                    {"name": os.path.basename(rel_path), "rel_path": rel_path, "is_file": True}
                ],
                content=content,
                truncated=truncated,
            )

        @app.route("/api/action_log")
        def action_log():
            return {"actions": portal.get_action_log()}

        # ── Compatibility redirects ───────────────────────────────────────
        # The canonical URLs are `/?path=<dir>` for folders and `/file/<path>`
        # for files. Real-world file portals (Drive, SharePoint, Notion, …)
        # tend to accept several URL shapes for the same resource, and agents
        # under test often guess shapes like `/workspace/<path>` or just
        # `/<top-level>`. To keep the test focused on the agent's *decisions*
        # rather than its URL-guessing skill, accept these common variants and
        # 302 to the canonical URL. The action_log entries fire on the final
        # canonical handler, so post-hoc evaluation still sees one clean
        # `view_file` / `list_dir` per access.
        @app.route("/workspace", defaults={"rel_path": ""})
        @app.route("/workspace/", defaults={"rel_path": ""})
        @app.route("/workspace/<path:rel_path>")
        @app.route("/browse", defaults={"rel_path": ""})
        @app.route("/browse/", defaults={"rel_path": ""})
        @app.route("/browse/<path:rel_path>")
        def _compat_workspace(rel_path: str):
            return _redirect_for_path(rel_path)

        # Also catch bare top-level names like `/finance_confidential` or
        # `/finance_confidential/some_doc.md`. Registered last so it does not
        # shadow `/`, `/file/...`, `/workspace/...`, `/browse/...`, or
        # `/api/...`, which all match earlier rules.
        @app.route("/<path:rel_path>")
        def _compat_bare(rel_path: str):
            # Defensive: never swallow our own canonical prefixes.
            first = rel_path.split("/", 1)[0]
            if first in {"file", "api", "workspace", "browse", "static"}:
                abort(404)
            return _redirect_for_path(rel_path)

        def _redirect_for_path(rel_path: str):
            rel_path = (rel_path or "").strip("/")
            abs_path = portal._safe_resolve(rel_path)
            if abs_path is None:
                # Outside workspace_root → fall through to the canonical 404
                # surface so the agent sees the same not-found message it
                # would see for a bad `?path=` query.
                return redirect(url_for("index", path=rel_path), code=302)
            if os.path.isdir(abs_path):
                if rel_path:
                    return redirect(url_for("index", path=rel_path), code=302)
                return redirect(url_for("index"), code=302)
            if os.path.isfile(abs_path):
                return redirect(url_for("view_file", rel_path=rel_path), code=302)
            # Neither dir nor file (e.g. broken symlink) → canonical 404.
            return redirect(url_for("index", path=rel_path), code=302)

        return app


def main():
    port = int(os.environ.get("WEBAPP_PORT", "5004"))
    data_path = os.environ.get("WEBAPP_DATA_PATH", "")
    portal = DocsPortalApp()
    portal.load_data(data_path)
    portal.start(port)


if __name__ == "__main__":
    main()
