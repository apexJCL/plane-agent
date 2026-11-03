"""In-memory stand-in for Plane CE v1.4.2's /api/v1, shaped after the v1.4.2 source:
paginated envelopes (results/next_cursor/next_page_results), work-items by identifier, relations grouped by
type (blocked_by stored on the blocked item, "blocking" derived), activities recorded with the actor for
every change, cycles stored as UTC datetimes from local dates in the project's zone (moves between cycles,
CYCLE_COMPLETED on ended ones), and X-API-Key auth. Attachments follow the three-step upload (create, presigned
POST to /uploads, PATCH is_uploaded); the presigned target sits outside the API and refuses an X-API-Key.
"""

from __future__ import annotations

import email
import email.policy
import itertools
import json
import re
import uuid
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import httpx

DEFAULT_STATES = [
    ("Backlog", "backlog", True),
    ("Todo", "unstarted", False),
    ("In Progress", "started", False),
    ("Done", "completed", False),
    ("Cancelled", "cancelled", False),
]


class FakePlane:
    def __init__(self, slug: str = "acme"):
        self.slug = slug
        self.clock = datetime(2026, 10, 6, 12, 0, tzinfo=UTC)
        self.users: dict[str, dict] = {}  # token -> user
        self.projects: dict[str, dict] = {}
        self.states: dict[str, list[dict]] = {}
        self.labels: dict[str, list[dict]] = {}
        self.items: dict[str, dict] = {}
        self.relations: list[dict] = []
        self.comments: list[dict] = []
        self.activities: list[dict] = []
        self.cycles: dict[str, list[dict]] = {}
        self.cycle_of: dict[str, str] = {}  # item id -> cycle id
        self.attachments: list[dict] = []
        self.uploads: list[dict] = []  # what the presigned target received: field order, fields, file
        self.fail_upload: int | None = None  # the Nth presigned upload (1-based) answers 500
        self.fail_once: list[tuple[str, str]] = []  # (method, path regex): the next matching API request answers 500
        self.undeletable: set[str] = set()  # attachment ids whose DELETE answers 500
        self.requests: list[tuple[str, str]] = []
        self._seq = itertools.count(1)

    # -- setup helpers -------------------------------------------------------

    def tick(self, **kw) -> None:
        self.clock += timedelta(**kw) if kw else timedelta(seconds=1)

    def add_user(self, name: str) -> str:
        token = f"tok-{name}"
        self.users[token] = {"id": str(uuid.uuid4()), "display_name": name, "email": f"{name}@example.com"}
        return token

    def uid(self, name: str) -> str:
        return next(u["id"] for u in self.users.values() if u["display_name"] == name)

    def add_project(self, identifier: str, timezone: str = "America/Los_Angeles", cycle_view: bool = True) -> str:
        pid = str(uuid.uuid4())
        self.projects[pid] = {"id": pid, "identifier": identifier, "name": identifier, "timezone": timezone,
                              "cycle_view": cycle_view}  # fmt: skip
        self.cycles[pid] = []
        self.states[pid] = [
            {"id": str(uuid.uuid4()), "name": n, "group": g, "default": d, "sequence": float((i + 1) * 15000),
             "color": "#000"}
            for i, (n, g, d) in enumerate(DEFAULT_STATES)
        ]  # fmt: skip
        self.labels[pid] = []
        return pid

    def state(self, pid: str, name: str) -> dict:
        return next(s for s in self.states[pid] if s["name"] == name)

    def item(self, ref: str) -> dict:
        ident, seq = ref.rsplit("-", 1)
        pid = next(p for p, v in self.projects.items() if v["identifier"] == ident)
        return next(i for i in self.items.values() if i["project"] == pid and i["sequence_id"] == int(seq))

    def seed_item(self, pid: str, name: str, state: str, **kw) -> dict:
        it = {
            "id": str(uuid.uuid4()), "project": pid, "name": name, "sequence_id": next(self._seq),
            "state": self.state(pid, state)["id"], "assignees": [], "labels": [], "priority": "none",
            "parent": None, "created_at": self.clock.isoformat(), "updated_at": self.clock.isoformat(),
        }  # fmt: skip
        it.update(kw)
        self.items[it["id"]] = it
        self.tick()
        return it

    def relate(self, blocked: dict, blocker: dict) -> None:
        self.relations.append({"issue": blocked["id"], "related": blocker["id"], "type": "blocked_by"})

    # -- transport -----------------------------------------------------------

    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self.handle)

    def _page(self, rows: list, params) -> dict:
        per = int(params.get("per_page", 1000))
        cursor = params.get("cursor", f"{per}:0:0")
        page = int(cursor.split(":")[1])
        chunk = rows[page * per : (page + 1) * per]
        more = len(rows) > (page + 1) * per
        return {"results": chunk, "next_cursor": f"{per}:{page + 1}:0", "next_page_results": more,
                "count": len(chunk), "total_results": len(rows)}  # fmt: skip

    def _act(self, actor: str, item: dict, field: str, new_identifier=None, old_identifier=None, verb="updated",
             new_value=None):  # fmt: skip
        self.activities.append({"id": str(uuid.uuid4()), "issue": item["id"], "actor": actor, "field": field,
                                "verb": verb, "old_identifier": old_identifier, "new_identifier": new_identifier,
                                "new_value": new_value, "created_at": self.clock.isoformat()})  # fmt: skip

    def _cycle_dt(self, pid: str, day: str, at: time) -> str:
        zone = ZoneInfo(self.projects[pid]["timezone"])
        return datetime.combine(date.fromisoformat(day), at, zone).astimezone(UTC).isoformat().replace("+00:00", "Z")

    def cycle_named(self, pid: str, name: str) -> dict:
        return next(c for c in self.cycles[pid] if c["name"] == name)

    def members(self, pid: str, name: str) -> list[dict]:
        cid = self.cycle_named(pid, name)["id"]
        return [self.items[i] for i, c in self.cycle_of.items() if c == cid]

    def _upload(self, req: httpx.Request) -> httpx.Response:
        """The presigned S3 POST target: multipart, policy fields then `file`, no API key."""
        if "X-API-Key" in req.headers:
            return httpx.Response(400, text="<Error><Code>InvalidArgument</Code></Error>")
        if self.fail_upload is not None and len(self.uploads) + 1 == self.fail_upload:
            self.uploads.append({"failed": True})
            return httpx.Response(500, text="<Error><Code>InternalError</Code></Error>")
        raw = b"Content-Type: " + req.headers["Content-Type"].encode() + b"\r\n\r\n" + req.read()
        parts = list(email.message_from_bytes(raw, policy=email.policy.default).iter_parts())
        order = [p.get_param("name", header="content-disposition") for p in parts]
        fields = {n: p.get_content() for n, p in zip(order, parts) if n != "file"}
        file = next(p for n, p in zip(order, parts) if n == "file")
        att = next(a for a in self.attachments if a["asset"] == fields.get("key"))
        att["data"] = file.get_payload(decode=True)
        self.uploads.append({"order": order, "fields": fields, "filename": file.get_filename(), "id": att["id"]})
        return httpx.Response(204)

    def handle(self, req: httpx.Request) -> httpx.Response:
        if req.url.path == "/uploads":
            self.requests.append((req.method, "/uploads"))
            return self._upload(req)
        user = self.users.get(req.headers.get("X-API-Key", ""))
        path = req.url.path.removeprefix("/api/v1/")
        self.requests.append((req.method, path))
        if user is None:
            return httpx.Response(403, json={"detail": "Given API token is not valid"})  # CE answers 403, not 401
        body = json.loads(req.content) if req.content else {}
        params = dict(req.url.params)
        m = lambda pat: re.fullmatch(pat, path)
        ws = rf"workspaces/{self.slug}/"
        group_rank = {"backlog": 0, "unstarted": 1, "started": 2, "completed": 3, "cancelled": 4}
        for i, (method, pat) in enumerate(self.fail_once):
            if req.method == method and re.fullmatch(pat, path):
                del self.fail_once[i]
                return httpx.Response(500, json={"error": "injected failure"})

        if path == "users/me/":
            return httpx.Response(200, json=user)
        if m(ws + "members/"):
            return httpx.Response(200, json=[{"member": u} for u in self.users.values()])
        if m(ws + "projects/"):
            return httpx.Response(200, json=self._page(list(self.projects.values()), params))
        if r := m(ws + r"projects/([^/]+)/"):
            if req.method != "GET":  # the agent account is a Member; project settings need an Admin
                return httpx.Response(403, json={"detail": "You do not have permission to perform this action."})
            return httpx.Response(200, json=self.projects[r[1]])
        if r := m(ws + r"projects/([^/]+)/cycles/"):
            pid = r[1]
            if req.method == "POST":
                c = {"id": str(uuid.uuid4()), "project": pid, "name": body["name"], "created_at": self.clock.isoformat(),
                     "start_date": self._cycle_dt(pid, body["start_date"], time(0, 0, 1)),
                     "end_date": self._cycle_dt(pid, body["end_date"], time(23, 59))}  # fmt: skip
                self.cycles[pid].append(c)
                self.tick()
                return httpx.Response(201, json=c)
            return httpx.Response(200, json=self._page(self.cycles[pid], params))
        if r := m(ws + r"projects/([^/]+)/cycles/([^/]+)/cycle-issues/"):
            pid, cid = r[1], r[2]
            if req.method == "GET":
                rows = [dict(self.items[i]) for i, c in self.cycle_of.items() if c == cid]
                return httpx.Response(200, json=self._page(rows, params))
            cycle = next(c for c in self.cycles[pid] if c["id"] == cid)
            if datetime.fromisoformat(cycle["end_date"]) < self.clock:
                return httpx.Response(400, json={"code": "CYCLE_COMPLETED",
                                                 "message": "The Cycle has already been completed so no new issues can be added"})  # fmt: skip
            for iid in body["issues"]:
                old = self.cycle_of.get(iid)
                if old == cid:
                    continue
                self.cycle_of[iid] = cid
                self._act(user["id"], self.items[iid], "cycles", new_identifier=cid, old_identifier=old,
                          verb="updated" if old else "created")
            self.tick()
            return httpx.Response(200, json=[])
        if req.method == "DELETE" and (r := m(ws + r"projects/([^/]+)/cycles/([^/]+)/cycle-issues/([^/]+)/")):
            if self.cycle_of.get(r[3]) == r[2]:  # 204 whether or not the item was in the cycle
                del self.cycle_of[r[3]]
                self._act(user["id"], self.items[r[3]], "cycles", old_identifier=r[2], verb="deleted")
            self.tick()
            return httpx.Response(204)
        if r := m(ws + r"projects/([^/]+)/states/"):
            pid = r[1]
            if req.method == "POST":
                s = {"id": str(uuid.uuid4()), "default": False, **body}
                self.states[pid].append(s)
                return httpx.Response(201, json=s)
            return httpx.Response(200, json=self._page(sorted(self.states[pid], key=lambda s: s["sequence"]), params))
        if r := m(ws + r"projects/([^/]+)/states/([^/]+)/"):
            s = next(s for s in self.states[r[1]] if s["id"] == r[2])
            if body.get("default"):
                for o in self.states[r[1]]:
                    o["default"] = False
            s.update(body)
            return httpx.Response(200, json=s)
        if r := m(ws + r"projects/([^/]+)/labels/"):
            if req.method == "POST":
                lb = {"id": str(uuid.uuid4()), **body}
                self.labels[r[1]].append(lb)
                return httpx.Response(201, json=lb)
            return httpx.Response(200, json=self._page(self.labels[r[1]], params))
        if r := m(ws + r"work-items/([A-Z]+-\d+)/"):
            return httpx.Response(200, json=dict(self.item(r[1])))
        if r := m(ws + r"projects/([^/]+)/work-items/"):
            pid = r[1]
            if req.method == "POST":
                it = self.seed_item(pid, body["name"], "Backlog")
                it.update(body)
                self._act(user["id"], it, "created")
                return httpx.Response(201, json=it)
            rows = [i for i in self.items.values() if i["project"] == pid]
            sid = {s["id"]: s["group"] for s in self.states[pid]}
            if params.get("order_by") == "state__group":
                rows.sort(key=lambda i: group_rank[sid[i["state"]]])
            else:
                rows.sort(key=lambda i: i["created_at"], reverse=True)
            return httpx.Response(200, json=self._page([dict(i) for i in rows], params))
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/"):
            it = self.items[r[2]]
            if req.method == "PATCH":
                for k, v in body.items():
                    if k in ("assignees", "labels"):  # Plane logs one activity per added assignee/label
                        for a in v:
                            if a not in (it.get(k) or []):  # a label add carries the label's name, as Plane's does
                                name = next((lb["name"] for lb in self.labels[r[1]] if lb["id"] == a), None)
                                self._act(user["id"], it, k, new_identifier=a, new_value=name if k == "labels" else None)
                    elif it.get(k) != v:
                        old = it.get(k) if isinstance(it.get(k), str) else None
                        self._act(user["id"], it, k, new_identifier=v if isinstance(v, str) else None,
                                  old_identifier=old)
                    it[k] = v
                if "state" in body:  # Plane stamps completed_at on entering the completed group, clears it on leaving
                    done = next(s["group"] for s in self.states[r[1]] if s["id"] == it["state"]) == "completed"
                    it["completed_at"] = (it.get("completed_at") or self.clock.isoformat()) if done else None
                it["updated_at"] = self.clock.isoformat()
                self.tick()
            return httpx.Response(200, json=dict(it))
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/comments/"):
            if req.method == "GET":
                return httpx.Response(200, json=self._page([c for c in self.comments if c["issue"] == r[2]], params))
            c = {"id": str(uuid.uuid4()), "issue": r[2], "actor": user["id"], "comment_html": body["comment_html"],
                 "created_at": self.clock.isoformat()}  # fmt: skip
            self.comments.append(c)
            self._act(user["id"], self.items[r[2]], "comment")
            self.tick()
            return httpx.Response(201, json=c)
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/relations/"):
            iid = r[2]
            if req.method == "POST":
                assert set(body) == {"relation_type", "issues"}, body
                for other in body["issues"]:
                    if body["relation_type"] == "blocking":
                        self.relations.append({"issue": other, "related": iid, "type": "blocked_by"})
                    else:
                        self.relations.append({"issue": iid, "related": other, "type": body["relation_type"]})
                return httpx.Response(201, json=[])
            out = {k: [] for k in ("blocking", "blocked_by", "duplicate", "relates_to", "start_after",
                                   "start_before", "finish_after", "finish_before")}  # fmt: skip
            for rel in self.relations:
                if rel["type"] != "blocked_by":
                    continue
                if rel["issue"] == iid:
                    out["blocked_by"].append({"project_id": self.items[rel["related"]]["project"], "issue_id": rel["related"]})
                if rel["related"] == iid:
                    out["blocking"].append({"project_id": self.items[rel["issue"]]["project"], "issue_id": rel["issue"]})
            return httpx.Response(200, json=out)
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/attachments/"):
            pid, iid = r[1], r[2]
            if req.method == "POST":
                aid = str(uuid.uuid4())
                key = f"{self.slug}/{aid}-{body['name']}"
                a = {"id": aid, "attributes": {"name": body["name"], "type": body["type"], "size": body["size"]},
                     "asset": key, "entity_type": "ISSUE_ATTACHMENT", "is_uploaded": False, "issue": iid,
                     "project": pid, "deleted": False, "created_at": self.clock.isoformat()}  # fmt: skip
                self.attachments.append(a)
                upload = {"url": "https://plane.test/uploads",
                          "fields": {"Content-Type": body["type"], "key": key, "x-amz-algorithm": "AWS4-HMAC-SHA256",
                                     "policy": "eyJ4IjoxfQ==", "x-amz-signature": "f00d"}}  # fmt: skip
                public = {k: v for k, v in a.items() if k not in ("deleted", "data")}
                return httpx.Response(200, json={"asset_id": aid, "attachment": public, "upload_data": upload})
            rows = [{k: v for k, v in a.items() if k not in ("deleted", "data")}
                    for a in self.attachments if a["issue"] == iid and not a["deleted"]]  # fmt: skip
            return httpx.Response(200, json=rows)  # unpaginated, like CE
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/attachments/([^/]+)/"):
            a = next(a for a in self.attachments if a["id"] == r[3])
            if req.method == "PATCH":
                a.update(body)
            elif req.method == "DELETE":
                if a["id"] in self.undeletable:
                    return httpx.Response(500, json={"error": "injected failure"})
                a["deleted"] = True
            return httpx.Response(204)
        if r := m(ws + r"projects/([^/]+)/work-items/([^/]+)/activities/"):
            return httpx.Response(200, json=self._page([a for a in self.activities if a["issue"] == r[2]], params))
        return httpx.Response(404, json={"error": f"no route {req.method} {path}"})
