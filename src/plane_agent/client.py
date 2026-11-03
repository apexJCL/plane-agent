"""Thin client for Plane's public REST API (/api/v1, X-API-Key auth): Plane Cloud and self-hosted Plane.

Only the routes the coordination protocol needs. Shapes were checked against the Plane CE v1.4.2 source and the
Plane Cloud API reference:
- list endpoints return {"results": [...], "next_cursor": "...", "next_page_results": bool, ...}
- the work-item list has no state/assignee filters on CE, only order_by, cursor and per_page
- relations are GET/POST only; POST takes {"relation_type": ..., "issues": [ids]}
- attachments live under work-items/<id>/attachments/ (CE v1.4.2 also answers the older
  issues/<id>/issue-attachments/); create answers asset_id, attachment and upload_data (a presigned S3 POST), and
  the file only counts once PATCHed is_uploaded
"""

from __future__ import annotations

import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import httpx


class PlaneError(RuntimeError):
    def __init__(self, status: int, method: str, path: str, body: str):
        super().__init__(f"{method} {path} -> {status}: {body[:300]}")
        self.status = status


class PlaneClient:
    def __init__(
        self,
        base_url: str,
        workspace: str,
        token: str,
        transport: httpx.BaseTransport | None = None,
        timeout: float = 20.0,
        sleep: Callable[[float], None] = time.sleep,
        max_wait: float = 180.0,
        rate_limit: int = 60,
        wall: Callable[[], float] = time.time,
    ):
        self.workspace = workspace
        # Where people open items: Plane Cloud serves the API and the app on different hosts.
        self.web_url = base_url.rstrip("/").replace("://api.plane.so", "://app.plane.so")
        self.rate_limit = rate_limit  # calls a minute the server allows this token (all agents share it)
        self._wall = wall
        self._sleep = sleep
        self._max_wait = max_wait  # total seconds one call may spend waiting out 429s
        self.calls = 0  # requests sent, retries included
        self._transport = transport  # the presigned upload client reuses it (tests)
        self._timeout = timeout
        self._http = httpx.Client(
            base_url=base_url.rstrip("/") + "/api/v1/",
            headers={"X-API-Key": token, "Accept": "application/json"},
            transport=transport,
            timeout=timeout,
        )

    # -- plumbing ----------------------------------------------------------

    def item_url(self, ref: str) -> str:
        return f"{self.web_url}/{self.workspace}/browse/{ref}/"

    def _send(self, method: str, path: str, **kw: Any) -> httpx.Response:
        self.calls += 1
        return self._http.request(method, path, **kw)

    def _req(self, method: str, path: str, **kw: Any) -> Any:
        # Every agent shares one token, so one budget (rate_limit calls a minute). A throttled request is
        # rejected before the view runs, so retrying it, writes included, is safe.
        waited = 0.0
        while True:
            r = self._send(method, path, **kw)
            if r.status_code != 429:
                break
            delay = self._retry_delay(r)
            if waited + delay > self._max_wait:
                break
            print(f"plane-agent: rate limited, retrying in {delay:.0f} s", file=sys.stderr)
            self._sleep(delay)
            waited += delay
        if r.status_code >= 400:
            raise PlaneError(r.status_code, method, path, r.text)
        return r.json() if r.content else None

    def _retry_delay(self, r: httpx.Response) -> float:
        """Retry-After (seconds), else X-RateLimit-Reset (an epoch time or seconds left), else 10 s."""
        try:
            return max(1.0, float(r.headers["Retry-After"]))
        except (KeyError, ValueError):
            pass
        try:
            v = float(r.headers["X-RateLimit-Reset"])
        except (KeyError, ValueError):
            return 10.0
        if v > 1e9:  # an epoch timestamp
            v -= self._wall()
        return min(max(1.0, v), 60.0)  # the window is a minute

    def _ws(self, path: str) -> str:
        return f"workspaces/{self.workspace}/{path}"

    def _proj(self, project_id: str, path: str) -> str:
        return self._ws(f"projects/{project_id}/{path}")

    def _paged(self, path: str, params: dict[str, Any] | None = None) -> Iterator[dict]:
        params = dict(params or {})
        params.setdefault("per_page", 100)
        while True:
            data = self._req("GET", path, params=params)
            if isinstance(data, list):  # some CE endpoints are unpaginated
                yield from data
                return
            yield from data.get("results", [])
            if not data.get("next_page_results"):
                return
            params["cursor"] = data["next_cursor"]

    # -- identity & workspace ----------------------------------------------

    def me(self) -> dict:
        return self._req("GET", "users/me/")

    def workspace_members(self) -> list[dict]:
        data = self._req("GET", self._ws("members/"))
        return data if isinstance(data, list) else data.get("results", [])

    def projects(self) -> list[dict]:
        return list(self._paged(self._ws("projects/")))

    # -- states & labels ---------------------------------------------------

    def states(self, project_id: str) -> list[dict]:
        return list(self._paged(self._proj(project_id, "states/")))

    def create_state(self, project_id: str, body: dict) -> dict:
        return self._req("POST", self._proj(project_id, "states/"), json=body)

    def update_state(self, project_id: str, state_id: str, body: dict) -> dict:
        return self._req("PATCH", self._proj(project_id, f"states/{state_id}/"), json=body)

    def labels(self, project_id: str) -> list[dict]:
        return list(self._paged(self._proj(project_id, "labels/")))

    def create_label(self, project_id: str, body: dict) -> dict:
        return self._req("POST", self._proj(project_id, "labels/"), json=body)

    # -- work items --------------------------------------------------------

    def iter_items(self, project_id: str, order_by: str = "state__group") -> Iterator[dict]:
        return self._paged(self._proj(project_id, "work-items/"), {"order_by": order_by})

    def item_by_identifier(self, identifier: str) -> dict:
        """identifier like 'HOME-12'; the response carries 'project' (its id)."""
        return self._req("GET", self._ws(f"work-items/{identifier}/"))

    def get_item(self, project_id: str, item_id: str) -> dict:
        return self._req("GET", self._proj(project_id, f"work-items/{item_id}/"))

    def create_item(self, project_id: str, body: dict) -> dict:
        return self._req("POST", self._proj(project_id, "work-items/"), json=body)

    def update_item(self, project_id: str, item_id: str, body: dict) -> dict:
        return self._req("PATCH", self._proj(project_id, f"work-items/{item_id}/"), json=body)

    def add_comment(self, project_id: str, item_id: str, html: str) -> dict:
        return self._req(
            "POST", self._proj(project_id, f"work-items/{item_id}/comments/"), json={"comment_html": html}
        )

    def relations(self, project_id: str, item_id: str) -> dict[str, list[dict]]:
        return self._req("GET", self._proj(project_id, f"work-items/{item_id}/relations/"))

    def add_relation(self, project_id: str, item_id: str, relation_type: str, issue_ids: list[str]) -> Any:
        return self._req(
            "POST",
            self._proj(project_id, f"work-items/{item_id}/relations/"),
            json={"relation_type": relation_type, "issues": issue_ids},
        )

    def activities(self, project_id: str, item_id: str) -> list[dict]:
        return list(self._paged(self._proj(project_id, f"work-items/{item_id}/activities/")))

    def comments(self, project_id: str, item_id: str) -> list[dict]:
        return list(self._paged(self._proj(project_id, f"work-items/{item_id}/comments/")))

    # -- attachments -------------------------------------------------------

    def _att(self, project_id: str, item_id: str, rest: str = "") -> str:
        return self._proj(project_id, f"work-items/{item_id}/attachments/{rest}")

    def create_attachment(self, project_id: str, item_id: str, name: str, type_: str, size: int) -> dict:
        return self._req("POST", self._att(project_id, item_id), json={"name": name, "type": type_, "size": size})

    def upload_presigned(self, upload_data: dict, name: str, data: bytes, type_: str) -> None:
        """POST the file to the presigned storage URL. Not an API call: no X-API-Key (it would land in the storage
        access log), no 429 loop, outside the shared budget. The policy fields go first and the file last."""
        with httpx.Client(transport=self._transport, timeout=self._timeout) as http:
            r = http.post(upload_data["url"], data=upload_data.get("fields") or {}, files={"file": (name, data, type_)})
        if r.status_code >= 300:
            raise PlaneError(r.status_code, "POST", upload_data["url"], r.text)

    def mark_uploaded(self, project_id: str, item_id: str, attachment_id: str) -> None:
        self._req("PATCH", self._att(project_id, item_id, f"{attachment_id}/"), json={"is_uploaded": True})

    def attachments(self, project_id: str, item_id: str) -> list[dict]:
        """The item's uploaded attachments; one created but never uploaded doesn't count."""
        return [a for a in self._paged(self._att(project_id, item_id)) if a.get("is_uploaded")]

    def delete_attachment(self, project_id: str, item_id: str, attachment_id: str) -> None:
        self._req("DELETE", self._att(project_id, item_id, f"{attachment_id}/"))
