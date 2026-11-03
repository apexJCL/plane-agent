"""The agent-coordination protocol (openspec/specs/agent-coordination).

Every rule the spec states lives here. cli.py only parses arguments and prints.
"""

from __future__ import annotations

import html
import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta, tzinfo
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import httpx
from markdown_it import MarkdownIt

from .attachments import File, FileRefused, human_size
from .attachments import load as load_files
from .client import PlaneClient, PlaneError

# Ordered state model. Bootstrap maps Plane's defaults onto it, so "Todo" becomes "Ready".
STATES: list[tuple[str, str, str]] = [
    ("Backlog", "backlog", "#a3a3a3"),
    ("Ready", "unstarted", "#3b82f6"),
    ("In Progress", "started", "#f59e0b"),
    ("Blocked", "started", "#ef4444"),
    ("In Review", "started", "#8b5cf6"),
    ("Done", "completed", "#16a34a"),
    ("Cancelled", "cancelled", "#6b7280"),
]
RENAMES = {"Ready": "Todo"}
CLOSED_GROUPS = {"completed", "cancelled"}

KINDS = ["feature", "bug", "chore", "spike", "deploy"]
NEEDS_HUMAN = "needs-human"
WAITING = "waiting"  # Blocked on time or the outside world (`block --wait`), not on an item or the operator
LABELS: list[tuple[str, str]] = [(f"kind:{k}", "#64748b") for k in KINDS] + [(NEEDS_HUMAN, "#dc2626"),
                                                                              (WAITING, "#eab308")]  # fmt: skip
HUMAN_ONLY = ("kind:deploy", NEEDS_HUMAN)  # only the operator closes these

# Every agent acts through one shared agent account (e.g. a `claude` bot user); which agent holds an item is its `agent:<slug>` label.
AGENT_PREFIX = "agent:"
AGENT_COLOR = "#0ea5e9"
ORCHESTRATOR = "orchestrator"
SLUG_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,47}")

PRIORITY_RANK = {"urgent": 0, "high": 1, "medium": 2, "low": 3, "none": 4, None: 4}
PRIORITIES = ["urgent", "high", "medium", "low", "none"]
WAIT_DAYS = 7  # a wait without --until ends a week from today
REVIEW_STALE_H = 72  # `stale` lists items In Review longer than this

# Exit codes the CLI maps refusals to; agents branch on them.
EXIT_NOT_CLAIMABLE = 3
EXIT_OWNERSHIP_LOST = 4
EXIT_REFUSED = 5

AGENT_LABELS_WARN = 20  # more agent:<slug> labels than this: reuse roles instead of minting new slugs
RECLAIM_FLOOR_H = 24  # spec: a claim is stale after 24 h of silence; earlier takes `reclaim REF --reason --force`


class ProtocolRefusal(Exception):
    def __init__(self, message: str, code: int):
        super().__init__(message)
        self.code = code


class WriteFailed(Exception):
    """A write with attachments failed after the first upload began. The attachments it made were removed, or are
    named in the message with their ids (exit 1)."""


# CommonMark plus tables and strikethrough, as Plane's editor shows them. Raw HTML is escaped, never passed through.
_MD = MarkdownIt("commonmark", {"html": False, "breaks": True}).enable(["table", "strikethrough"])
_TASK = re.compile(r"\[([ xX])\] ")


def _task_lists(state) -> None:
    """`- [ ] item` and `- [x] item` become Plane's to-do list (tiptap's taskList/taskItem), when every item of
    the list is one. Mixed lists stay plain lists, brackets and all."""
    toks = state.tokens
    stack: list[tuple[int, list[tuple[int, int, re.Match]]]] = []
    for i, t in enumerate(toks):
        if t.type == "bullet_list_open":
            stack.append((i, []))
        elif t.type == "list_item_open" and stack:
            inline = toks[i + 2] if i + 2 < len(toks) and toks[i + 1].type == "paragraph_open" else None
            m = _TASK.match(inline.content) if inline is not None and inline.type == "inline" else None
            stack[-1][1].append((i, i + 2, m))  # type: ignore[arg-type]
        elif t.type == "bullet_list_close" and stack:
            start, items = stack.pop()
            if not items or not all(m for _, _, m in items):
                continue
            toks[start].attrSet("data-type", "taskList")
            for li, inl, m in items:
                toks[li].attrSet("data-type", "taskItem")
                toks[li].attrSet("data-checked", "true" if m[1] in "xX" else "false")
                toks[li + 1].hidden = toks[inl + 1].hidden = False  # a task item's text is a paragraph
                inline, cut = toks[inl], m.end()
                inline.content = inline.content[cut:]
                if inline.children and inline.children[0].type == "text":
                    inline.children[0].content = inline.children[0].content[cut:]


_MD.core.ruler.push("task_lists", _task_lists)


def _md(text: str) -> str:
    """Markdown to the HTML Plane stores (its API takes HTML only). Single newlines stay line breaks."""
    return re.sub(r">\n<", "><", _MD.render(text.strip())).strip()


def _p(text: str) -> str:
    """Plain text to the comment HTML Plane stores; blank lines separate paragraphs."""
    paras = [p.strip() for p in text.strip().split("\n\n") if p.strip()]
    return "".join("<p>" + html.escape(p).replace("\n", "<br>") + "</p>" for p in paras)


def _signature(sig: str) -> str:
    """The signature paragraph, set off from the text by a horizontal rule so it doesn't blend in. It stays a
    plain last paragraph: `signed_by` and `tail_statement` match it there, with or without the rule."""
    return "<hr>" + _p(sig)


# -- holds: the one machine statement left in the trail ------------------------
# A block or wait records where the item returns when released, as the plain paragraph right before the signature:
#     hold: from=In Progress until=2026-10-23
# Anywhere else (quoted, earlier in the body, unsigned) it doesn't count. Trails from before 0.3.0 also carry
# `plan:` and `actual:` statements; they are ordinary comments now.

_TAIL = re.compile(r"(?:^|</[a-z0-9]+>)<p>([^<]*)</p>(?:<hr[^>]*>)?<p>agent=([a-z0-9][a-z0-9-]*) · session=[^<]*</p>\s*$")
_HOLD = re.compile(r"hold: from=(Ready|In Progress|In Review)(?: until=(\d{4}-\d\d-\d\d))?")
_SIG = re.compile(r"(?:^|<p[^>]*>)agent=([a-z0-9][a-z0-9-]*) · session=([^<·]*?)(?: · [^<]*)?</p>\s*$")


def tail_statement(comment_html: str) -> tuple[str, str] | None:
    """(statement text, signing slug) when the comment ends in a statement paragraph plus a signature."""
    m = _TAIL.search(comment_html or "")
    return (html.unescape(m[1]), m[2]) if m else None


def hold_statement(origin: str, until: date | None = None) -> str:
    """Where a blocked item returns when it is released, and for a wait, its last day."""
    return f"hold: from={origin}" + (f" until={until.isoformat()}" if until else "")


def latest_hold(comments: list[dict]) -> tuple[str, date | None] | None:
    """(state the item returns to, the wait's last day or None) from the newest hold statement."""
    for c in sorted(comments, key=lambda c: c["created_at"], reverse=True):
        t = tail_statement(c.get("comment_html", ""))
        if t and (m := _HOLD.fullmatch(t[0])):
            return m[1], date.fromisoformat(m[2]) if m[2] else None
    return None


def signature(comment_html: str) -> tuple[str, str] | None:
    """(slug, session) from the comment's signature paragraph, if it has one."""
    m = _SIG.search(comment_html or "")
    return (m[1], html.unescape(m[2]).strip()) if m else None


def parse_day(value: str | None, what: str) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value)
    except ValueError:
        raise ProtocolRefusal(f"{what} {value!r} must be a date like 2026-10-23", EXIT_REFUSED) from None


def _zone(name: str | None) -> tzinfo:
    """The configured IANA zone, else this machine's local zone. It only decides what "today" is."""
    return ZoneInfo(name) if name else datetime.now().astimezone().tzinfo or UTC


def _ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


_IMAGE = re.compile(r'<image-component[^>]*?\ssrc="([^"]*)"[^>]*>(?:</image-component>)?')


def _text(html_body: str, images: dict[str, str] | None = None) -> str:
    """Comment/description HTML back to plain text, for `context`. An embedded image reads back as
    `[image: name]`, from `images` (attachment id -> name), or `[image: removed]` when its attachment is gone."""
    names = images or {}
    t = _IMAGE.sub(lambda m: f"<p>{html.escape(f'[image: {names.get(html.unescape(m[1]), 'removed')}]')}</p>",
                   html_body or "")  # fmt: skip
    t = re.sub(r"<br\s*/?>", "\n", t)
    t = re.sub(r'<li[^>]*data-checked="true"[^>]*>\s*', "- [x] ", t)
    t = re.sub(r'<li[^>]*data-checked="false"[^>]*>\s*', "- [ ] ", t)
    t = re.sub(r"<li[^>]*>\s*", "- ", t)
    t = re.sub(r"</t[hd]>\s*(?=<t[hd])", " | ", t)
    t = re.sub(r"</(?:li|tr)>\s*", "\n", t)
    t = re.sub(r"</(?:p|h[1-6]|pre|blockquote|ul|ol|table)>\s*", "\n\n", t)
    return re.sub(r"\n{3,}", "\n\n", html.unescape(re.sub(r"<[^>]+>", "", t))).strip()


def signed_by(comment_html: str, slug: str) -> bool:
    """True if the comment's signature, which the CLI writes as its last top-level paragraph `<p>agent=<slug> ·
    session=…</p>`, names this slug. A comment that quotes or mentions `agent=<slug>` anywhere else doesn't count,
    including a markdown blockquote, which ends in </blockquote> rather than the signature paragraph."""
    pattern = rf"(?:^|<p[^>]*>)agent={re.escape(slug)} · session=[^<]*</p>\s*$"
    return re.search(pattern, comment_html or "") is not None


def check_body(text: str, what: str) -> None:
    """A free-text body that ends right before the signature can't itself end in a hold statement, or the sweep
    would read it as one. Checked before any write."""
    if text.strip() and _HOLD.fullmatch(text.strip().split("\n\n")[-1].strip()):
        raise ProtocolRefusal(f"{what} can't end in a hold statement; use `block`", EXIT_REFUSED)


def _then(text: str) -> str:
    """An optional body as its own paragraphs after the CLI's wording."""
    return f"\n\n{text.strip()}" if text.strip() else ""


def check_slug(slug: str) -> str:
    if not SLUG_RE.fullmatch(slug or ""):
        raise ProtocolRefusal(
            f"agent slug {slug!r} must be lowercase letters, digits and dashes (max 48), e.g. api-implementation",
            EXIT_REFUSED,
        )
    return slug


@dataclass
class Project:
    id: str
    identifier: str
    states: dict[str, dict]  # by name
    labels: dict[str, dict]  # by name

    @property
    def state_by_id(self) -> dict[str, dict]:
        return {s["id"]: s for s in self.states.values()}

    def state_id(self, name: str) -> str:
        if name not in self.states:
            raise ProtocolRefusal(
                f"project {self.identifier} has no state {name!r}; run bootstrap-project", EXIT_REFUSED
            )
        return self.states[name]["id"]

    def label_id(self, name: str) -> str:
        if name not in self.labels:
            raise ProtocolRefusal(
                f"project {self.identifier} has no label {name!r}; run bootstrap-project", EXIT_REFUSED
            )
        return self.labels[name]["id"]

    def state_name(self, item: dict) -> str:
        return self.state_by_id.get(item["state"], {}).get("name", "?")

    def group(self, item: dict) -> str:
        return self.state_by_id.get(item["state"], {}).get("group", "?")

    def ref(self, item: dict) -> str:
        return f"{self.identifier}-{item['sequence_id']}"


@dataclass
class Agent:
    """One agent acting under the protocol, through the shared account the token belongs to."""

    client: PlaneClient
    slug: str  # e.g. orchestrator, api-implementation; the item label is agent:<slug>
    session: str = "unknown"
    where: str = ""
    change: str = ""  # OpenSpec change this agent works under, if any
    sleep: Callable[[float], None] = time.sleep
    now: Callable[[], datetime] = field(default=lambda: datetime.now(UTC))
    notify: Callable[[dict], None] | None = None  # the user's notify_command (notify.py); None: no push
    tz: str | None = None  # IANA zone for "today" (overdue, waits); None: this machine's local zone
    notices: list[str] = field(default_factory=list)  # warnings the command survives; the CLI prints them
    _me: dict | None = None
    _projects: dict[str, Project] = field(default_factory=dict)
    _seen: dict[str, dict[str, dict]] = field(default_factory=dict)  # open items the last queue read, by project

    # -- lookups -----------------------------------------------------------

    @property
    def me(self) -> dict:
        if self._me is None:
            self._me = self.client.me()
        return self._me

    def project(self, identifier: str) -> Project:
        identifier = identifier.upper()
        if identifier not in self._projects:
            match = [p for p in self.client.projects() if p["identifier"].upper() == identifier]
            if not match:
                raise ProtocolRefusal(f"no project with identifier {identifier}", EXIT_REFUSED)
            self._projects[identifier] = self._load(match[0], identifier)
        return self._projects[identifier]

    def _load(self, raw: dict, identifier: str) -> Project:
        states = {s["name"]: s for s in self.client.states(raw["id"])}
        labels = {lb["name"]: lb for lb in self.client.labels(raw["id"])}
        return Project(raw["id"], identifier, states, labels)

    def today(self) -> date:
        return self.now().astimezone(_zone(self.tz)).date()

    def resolve(self, ref: str) -> tuple[Project, dict]:
        """'HOME-12' -> (project, fresh item)."""
        ident = ref.rsplit("-", 1)[0]
        proj = self.project(ident)
        return proj, self.client.item_by_identifier(ref.upper())

    def reread(self, proj: Project, item: dict) -> dict:
        return self.client.get_item(proj.id, item["id"])

    def member_id(self, name: str) -> str:
        for m in self.client.workspace_members():
            u = m.get("member", m)
            if name in (u.get("display_name"), u.get("email"), u.get("id")):
                return u["id"]
        raise ProtocolRefusal(f"no workspace member {name!r}", EXIT_REFUSED)

    def comment(self, proj: Project, item: dict, text: str, files: Iterable[tuple[File, str]] = (),
                statement: str = "") -> None:  # fmt: skip
        """Markdown body; the trailing signature stays a plain, escaped paragraph so `signed_by` can match it.
        Uploaded `files` (file, attachment id) go between the two: images inline, then an `Attached:` paragraph
        naming the rest. The image nodes are the only HTML the CLI writes itself."""
        files = list(files)
        shown = "".join(f.image_html(aid) for f, aid in files if f.inline)
        if others := [f"{f.name} ({human_size(f.size)})" for f, _ in files if not f.inline]:
            shown += _p("Attached: " + ", ".join(others))
        sig = self._sig()
        if statement:  # a machine-read hold line, last before the signature
            shown += _p(statement)
        if text.endswith(sig):
            html_body = _md(text[: -len(sig)]) + shown + _signature(sig)
        else:
            html_body = _md(text) + shown
        self.client.add_comment(proj.id, item["id"], html_body)

    def _files(self, paths: Iterable[str] | None) -> list[File]:
        """The local file checks, before any request."""
        try:
            return load_files(list(paths or []))
        except FileRefused as e:
            raise ProtocolRefusal(str(e), EXIT_REFUSED) from e

    def _write(self, proj: Project, item: dict, text: str, files: Iterable[File] = (),
               change: Callable[[], None] | None = None, statement: str = "") -> None:  # fmt: skip
        """Uploads, then the item change, then the signed comment. A failure after the first upload began removes
        every attachment made here, including one created but not uploaded, and raises WriteFailed."""
        made: list[tuple[File, str]] = []
        try:
            for f in files:
                res = self.client.create_attachment(proj.id, item["id"], f.name, f.type, f.size)
                made.append((f, res["asset_id"]))
                self.client.upload_presigned(res["upload_data"], f.name, f.data, f.type)
                self.client.mark_uploaded(proj.id, item["id"], res["asset_id"])
            if change:
                change()
            self.comment(proj, item, text, made, statement)
        except (PlaneError, httpx.HTTPError) as e:
            if not made:
                raise
            left = []
            for f, aid in made:
                try:
                    self.client.delete_attachment(proj.id, item["id"], aid)
                except (PlaneError, httpx.HTTPError):
                    left.append(f"{f.name} (id {aid})")
            msg = f"{e}; removed {len(made) - len(left)} of {len(made)} attachment(s)"
            raise WriteFailed(msg + (f"; could not remove: {', '.join(left)}" if left else "")) from e

    def __post_init__(self) -> None:
        check_slug(self.slug)
        if self.tz:
            try:
                ZoneInfo(self.tz)
            except (ZoneInfoNotFoundError, ValueError):
                raise ProtocolRefusal(f"timezone {self.tz!r} is not an IANA time zone, e.g. Europe/Madrid",
                                      EXIT_REFUSED) from None  # fmt: skip

    @property
    def label(self) -> str:
        return AGENT_PREFIX + self.slug

    def _sig(self) -> str:
        """Signature line on every comment. `stale` and `context` find an agent's comments by `agent=<slug>`."""
        bits = [f"agent={self.slug}", f"session={self.session}"]
        if self.where:
            bits.append(f"where={self.where}")
        if self.change:
            bits.append(f"openspec={self.change}")
        return " · ".join(bits)

    def _agent_labels(self, proj: Project, item: dict) -> set[str]:
        by_id = {lb["id"]: n for n, lb in proj.labels.items()}
        return {by_id[i] for i in item.get("labels") or [] if by_id.get(i, "").startswith(AGENT_PREFIX)}

    def _agent_label_id(self, proj: Project, slug: str) -> str:
        """agent:<slug> label id, creating the label on first use in this project."""
        name = AGENT_PREFIX + check_slug(slug)
        if name not in proj.labels:
            proj.labels.update({lb["name"]: lb for lb in self.client.labels(proj.id)})
        if name not in proj.labels:
            proj.labels[name] = self.client.create_label(proj.id, {"name": name, "color": AGENT_COLOR})
            n = sum(1 for lb in proj.labels if lb.startswith(AGENT_PREFIX))
            if n > AGENT_LABELS_WARN:
                self.notices.append(f"created {name}; {proj.identifier} now has {n} agent labels. Slugs are roles: "
                                    "reuse a free one (the busy-slug refusal lists them) rather than one per task")  # fmt: skip
        return proj.labels[name]["id"]

    def _free_roles(self, proj: Project, items: dict[str, dict]) -> list[str]:
        """Agent slugs with a label in the project and no In Progress item: roles a dispatch can reuse."""
        ip = proj.state_id("In Progress")
        held = {i for it in items.values() if it["state"] == ip for i in it.get("labels") or []}
        return sorted(n.removeprefix(AGENT_PREFIX) for n, lb in proj.labels.items()
                      if n.startswith(AGENT_PREFIX) and lb["id"] not in held and n != AGENT_PREFIX + ORCHESTRATOR)  # fmt: skip

    def _ensure_label(self, proj: Project, name: str, color: str) -> str:
        """A protocol label's id, creating it in a project bootstrapped before the label existed."""
        if name not in proj.labels:
            proj.labels.update({lb["name"]: lb for lb in self.client.labels(proj.id)})
        if name not in proj.labels:
            proj.labels[name] = self.client.create_label(proj.id, {"name": name, "color": color})
        return proj.labels[name]["id"]

    def _has(self, proj: Project, item: dict, *names: str) -> bool:
        ids = {proj.labels[n]["id"] for n in names if n in proj.labels}
        return bool(ids & set(item.get("labels") or []))

    # -- bootstrap ---------------------------------------------------------

    def bootstrap_project(self, identifier: str) -> list[str]:
        """Shape a project's states and labels to the protocol. Idempotent; returns the changes made."""
        proj = self.project(identifier)
        changes: list[str] = []
        states = proj.states
        for i, (name, group, color) in enumerate(STATES):
            seq = float((i + 1) * 10000)
            cur = states.get(name)
            if cur is None and name in RENAMES and RENAMES[name] in states:
                cur = states.pop(RENAMES[name])
                self.client.update_state(proj.id, cur["id"], {"name": name})
                cur = {**cur, "name": name}
                states[name] = cur
                changes.append(f"renamed state {RENAMES[name]} -> {name}")
            if cur is None:
                cur = self.client.create_state(
                    proj.id, {"name": name, "group": group, "color": color, "sequence": seq}
                )
                states[name] = cur
                changes.append(f"created state {name}")
                continue
            patch = {}
            if cur.get("group") != group:
                patch["group"] = group
            if float(cur.get("sequence") or 0) != seq:
                patch["sequence"] = seq
            if patch:
                self.client.update_state(proj.id, cur["id"], patch)
                states[name] = {**cur, **patch}
                changes.append(f"updated state {name}: {sorted(patch)}")
        backlog = states["Backlog"]
        if not backlog.get("default"):
            self.client.update_state(proj.id, backlog["id"], {"default": True})
            states["Backlog"] = {**backlog, "default": True}
            changes.append("made Backlog the default state")
        wanted = {name for name, _, _ in STATES}
        extra = [n for n, s in states.items() if n not in wanted and s.get("group") != "triage"]
        if extra:
            changes.append(f"WARNING: extra states not in the protocol (left in place): {', '.join(sorted(extra))}")
        for name, color in LABELS:
            if name not in proj.labels:
                proj.labels[name] = self.client.create_label(proj.id, {"name": name, "color": color})
                changes.append(f"created label {name}")
        return changes

    # -- queue -------------------------------------------------------------

    def _blockers(self, proj: Project, item: dict) -> list[dict]:
        rel = self.client.relations(proj.id, item["id"])
        return rel.get("blocked_by", [])

    def _is_closed(self, proj: Project, ref: dict, known: dict[str, dict]) -> bool:
        blocker = known.get(ref["issue_id"])
        if blocker is None:
            bproj = proj if ref.get("project_id") in (None, proj.id) else None
            if bproj is None:  # cross-project blocker: fetch its project's states
                p = next(p for p in self.client.projects() if p["id"] == ref["project_id"])
                bproj = self.project(p["identifier"])
            blocker = self.client.get_item(bproj.id, ref["issue_id"])
            return bproj.group(blocker) in CLOSED_GROUPS
        return proj.group(blocker) in CLOSED_GROUPS

    def _open_items(self, proj: Project) -> dict[str, dict]:
        """Every non-closed item. Pages in state-group order (closed groups sort last) and stops after a
        full page of consecutive closed items, so a server that ignored the ordering can't hide open work."""
        out: dict[str, dict] = {}
        closed_streak = 0
        for it in self.client.iter_items(proj.id, order_by="state__group"):
            if proj.group(it) in CLOSED_GROUPS:
                closed_streak += 1
                if closed_streak >= 100:
                    break
                continue
            closed_streak = 0
            out[it["id"]] = it
        return out

    def queue(self, identifier: str, sweep: bool) -> tuple[list[dict], list[str]]:
        """Claimable items in protocol order (priority, then target date, then creation) and a log of sweep
        actions."""
        proj = self.project(identifier)
        items = self._open_items(proj)
        log: list[str] = []
        ready_id, blocked_id = proj.state_id("Ready"), proj.state_id("Blocked")
        nh = proj.labels.get(NEEDS_HUMAN, {}).get("id")

        if sweep:
            for it in [i for i in items.values() if i["state"] == blocked_id]:
                if nh and nh in (it.get("labels") or []):
                    continue  # the operator's question; the operator removes the label
                if (it.get("assignees") or []) not in ([], [self.me["id"]]):
                    continue  # held by a human: the operator decides when it moves
                if line := self._release(proj, it, items):
                    log.append(line)

        claimable = []
        for it in items.values():
            if it["state"] != ready_id or it.get("assignees"):
                continue
            if all(self._is_closed(proj, b, items) for b in self._blockers(proj, it)):
                claimable.append(it)
        claimable.sort(key=lambda i: (PRIORITY_RANK.get(i.get("priority"), 4), i.get("target_date") or "9999",
                                      _ts(i["created_at"])))  # fmt: skip
        self._seen[proj.identifier] = items
        return claimable, log

    def _release(self, proj: Project, it: dict, items: dict[str, dict]) -> str | None:
        """Sweep one Blocked item: back to where it was held from once every blocker is closed and any wait has
        expired. A wait (label `waiting`) is a known state until its last day; Blocked with neither a blocker
        relation nor a wait is reported and left alone. Returns the log line, if any."""
        ref = proj.ref(it)
        wid = proj.labels.get(WAITING, {}).get("id")
        waiting = bool(wid) and wid in (it.get("labels") or [])
        blockers = self._blockers(proj, it)
        if not blockers and not waiting:
            return f"{ref}: Blocked with no blocker relation, no wait and no {NEEDS_HUMAN}; left alone"
        if not all(self._is_closed(proj, b, items) for b in blockers):
            return None
        hold = latest_hold(self.client.comments(proj.id, it["id"]))
        why = "every blocker is Done or Cancelled"
        if waiting:
            until = hold[1] if hold else None
            if until is None:
                return f"{ref}: labelled {WAITING} with no wait date on record; left alone"
            if self.today() <= until:
                return None  # a known wait
            why = f"the wait until {until} has expired" + (", and every blocker is closed" if blockers else "")
        fresh = self.reread(proj, it)
        if fresh["state"] != it["state"]:
            return None
        target = self._return_to(proj, fresh, hold)
        self.comment(proj, it, f"Unblocked: {why}; returned to {target}.\n\n{self._sig()}")
        if waiting:
            self._push("wait_expired", proj, fresh, target, f"The wait until {hold[1]} has expired.")
        items[it["id"]] = {**fresh, "state": proj.state_id(target),
                           "assignees": fresh.get("assignees") if target == "In Review" else []}  # fmt: skip
        return f"{ref}: {'wait expired' if waiting else 'unblocked'} -> {target}"

    def _return_to(self, proj: Project, item: dict, hold: tuple[str, date | None] | None) -> str:
        """Move a released Blocked item back and drop `waiting`. An item held from In Review returns to it, still
        assigned and labelled; anything else goes to Ready, unassigned, for a fresh dispatch."""
        wid = proj.labels.get(WAITING, {}).get("id")
        target = "In Review" if hold and hold[0] == "In Review" else "Ready"
        body: dict = {"state": proj.state_id(target), "labels": [x for x in item.get("labels") or [] if x != wid]}
        if target == "Ready":
            body["assignees"] = []
        self.client.update_item(proj.id, item["id"], body)
        return target

    def attention(self, identifier: str) -> dict[str, list[dict]]:
        """What waits on the operator or has slipped, from the open items the last queue read (or a fresh read):
        In Review deploy items, needs-human items, other items In Review over REVIEW_STALE_H hours since their
        last update, and items past their target date."""
        proj = self.project(identifier)
        items = self._items(proj)
        today, cutoff = self.today(), self.now() - timedelta(hours=REVIEW_STALE_H)
        ir = proj.state_id("In Review")
        out: dict[str, list[dict]] = {"deploy_review": [], "needs_human": [], "review_old": [], "overdue": []}
        for it in sorted(items.values(), key=lambda i: i["sequence_id"]):
            if it["state"] == ir and self._has(proj, it, "kind:deploy"):
                out["deploy_review"].append(it)
            elif it["state"] == ir and _ts(it.get("updated_at") or it["created_at"]) < cutoff:
                out["review_old"].append(it)
            if self._has(proj, it, NEEDS_HUMAN):
                out["needs_human"].append(it)
            if (t := it.get("target_date")) and date.fromisoformat(t[:10]) < today:
                out["overdue"].append(it)
        return out

    def expired_waits(self, identifier: str) -> list[tuple[dict, date | None]]:
        """Blocked items labelled `waiting` whose last day has passed (or that have none on record): what the next
        sweep releases, or what a needs-human question still holds."""
        proj = self.project(identifier)
        wid = proj.labels.get(WAITING, {}).get("id")
        if not wid:
            return []
        out = []
        for it in self._items(proj).values():
            if it["state"] == proj.state_id("Blocked") and wid in (it.get("labels") or []):
                hold = latest_hold(self.client.comments(proj.id, it["id"]))
                until = hold[1] if hold else None
                if until is None or until < self.today():
                    out.append((it, until))
        return out

    def _items(self, proj: Project) -> dict[str, dict]:
        """The open items this command already read, else a fresh read."""
        if proj.identifier not in self._seen:
            self._seen[proj.identifier] = self._open_items(proj)
        return self._seen[proj.identifier]

    def _push(self, event: str, proj: Project, item: dict, state: str, text: str) -> None:
        """Hand an event to the user's notify_command, if one is set. Never fails the write it follows."""
        if self.notify is None:
            return
        ref = proj.ref(item)
        self.notify({"event": event, "ref": ref, "title": item.get("name", ""), "url": self.client.item_url(ref),
                     "agent": self.slug, "session": self.session, "state": state, "text": text})  # fmt: skip

    # -- dispatch / claim --------------------------------------------------

    def holding(self, proj: Project, slug: str, items: dict[str, dict] | None = None) -> list[dict]:
        """In Progress items currently labelled agent:<slug>."""
        lid = proj.labels.get(AGENT_PREFIX + slug, {}).get("id")
        if not lid:
            return []
        items = items if items is not None else self._open_items(proj)
        ip = proj.state_id("In Progress")
        return [i for i in items.values() if i["state"] == ip and lid in (i.get("labels") or [])]

    def dispatch(self, ref: str, to: str) -> dict:
        """Hand a claimable item to agent <to>: assignee = this account, In Progress, label agent:<to>.
        Only the dispatcher assigns work, so there is no claim race to resolve."""
        check_slug(to)
        proj, item = self.resolve(ref)
        if item["state"] != proj.state_id("Ready") or item.get("assignees"):
            raise ProtocolRefusal(
                f"{ref} is not dispatchable (state {proj.state_name(item)}, assignees {item.get('assignees')})",
                EXIT_NOT_CLAIMABLE,
            )
        if not all(self._is_closed(proj, b, {}) for b in self._blockers(proj, item)):
            raise ProtocolRefusal(f"{ref} still has open blockers", EXIT_NOT_CLAIMABLE)
        nh = proj.labels.get(NEEDS_HUMAN, {}).get("id")
        if nh and nh in (item.get("labels") or []):
            raise ProtocolRefusal(
                f"{ref} still carries {NEEDS_HUMAN}; the operator removes it when the question is answered",
                EXIT_REFUSED,
            )
        self._refuse_busy(proj, to)
        lid = self._agent_label_id(proj, to)
        others = [i for i in item.get("labels") or [] if i not in {lb["id"] for n, lb in proj.labels.items()
                                                                    if n.startswith(AGENT_PREFIX)}]  # fmt: skip
        me = self.me["id"]
        ip = proj.state_id("In Progress")
        self.client.update_item(proj.id, item["id"], {"assignees": [me], "state": ip, "labels": others + [lid]})
        fresh = self.reread(proj, item)
        if (
            (fresh.get("assignees") or []) != [me]
            or fresh["state"] != ip
            or self._agent_labels(proj, fresh) != {AGENT_PREFIX + to}
        ):
            raise ProtocolRefusal(f"dispatch of {ref} was overwritten; re-check the item", EXIT_NOT_CLAIMABLE)
        target = f" Target {fresh['target_date'][:10]}." if fresh.get("target_date") else ""
        self.comment(proj, item, f"Dispatched to agent:{to}.{target}\n\n{self._sig()}")
        return fresh

    def _refuse_busy(self, proj: Project, slug: str) -> None:
        """One In Progress item per slug: refuse, naming the roles that are free."""
        items = self._open_items(proj)
        busy = self.holding(proj, slug, items)
        if busy:
            free = self._free_roles(proj, items)
            hint = f"free roles: {', '.join(free[:12])}" if free else "no other role is free"
            raise ProtocolRefusal(f"agent:{slug} already holds {', '.join(proj.ref(i) for i in busy)}; finish that "
                                  f"first or use another slug ({hint})", EXIT_REFUSED)  # fmt: skip

    def claim(self, ref: str) -> dict:
        """Worker start: confirm the item was dispatched to me and post the one-line claim comment. Its signature
        records the holding session, which later writes are checked against."""
        proj, item = self._owned(ref)
        self.comment(proj, item, f"Claimed by agent:{self.slug}.\n\n{self._sig()}")
        return item

    # -- ownership-checked writes -----------------------------------------

    def _owned(self, ref: str) -> tuple[Project, dict]:
        """Re-read and require: In Progress, assigned to this account alone, and agent:<me> its only agent label.
        Otherwise refuse (exit 4) without writing anything: the reason goes to stderr, never to the item."""
        proj, item = self.resolve(ref)
        ok = (
            item["state"] == proj.state_id("In Progress")
            and (item.get("assignees") or []) == [self.me["id"]]
            and self._agent_labels(proj, item) == {self.label}
        )
        if not ok:
            why = (f"state {proj.state_name(item)}, agent labels {sorted(self._agent_labels(proj, item)) or 'none'}, "
                   f"assignees {'this account' if item.get('assignees') == [self.me['id']] else item.get('assignees')}")  # fmt: skip
            raise ProtocolRefusal(f"{ref} is not held by agent:{self.slug} ({why}); stop", EXIT_OWNERSHIP_LOST)
        return proj, item

    def _check_session(self, proj: Project, item: dict) -> None:
        """Warn (stderr only) when this write signs as the holder from another session than the one that claimed
        it: a resumed session, or the orchestrator relaying for a finished agent (use `review --for` instead)."""
        claims = [signature(c.get("comment_html", "")) for c in sorted(self.client.comments(proj.id, item["id"]),
                  key=lambda c: c["created_at"]) if f"Claimed by agent:{self.slug}." in c.get("comment_html", "")]  # fmt: skip
        claimed = next((sig[1] for sig in reversed(claims) if sig and sig[0] == self.slug), None)
        if claimed and claimed != self.session:
            self.notices.append(f"signing as agent:{self.slug} but agent:{self.slug} claimed {proj.ref(item)} in "
                                f"session {claimed}; never sign as another agent's slug")  # fmt: skip

    def note(self, ref: str, text: str, force: bool = False, attach: Iterable[str] = ()) -> None:
        check_body(text, "a comment")
        files = self._files(attach)
        if force:
            proj, item = self.resolve(ref)
        else:
            proj, item = self._owned(ref)
        self._write(proj, item, f"{text}\n\n{self._sig()}", files)

    def check(self, ref: str) -> str:
        """Ownership check before long-running or irreversible steps. Raises if lost."""
        proj, item = self._owned(ref)
        return proj.state_name(item)

    def context(self, ref: str, agent: str | None = None) -> dict:
        """Everything an agent needs to resume an item: description, labels, blockers, attachments and the comment
        trail, optionally only the comments signed by one agent slug (the attachments stay unfiltered)."""
        proj, item = self.resolve(ref)
        by_id = {lb["id"]: n for n, lb in proj.labels.items()}
        comments = sorted(self.client.comments(proj.id, item["id"]), key=lambda c: c["created_at"])
        if agent:
            comments = [c for c in comments if signed_by(c.get("comment_html", ""), check_slug(agent))]
        atts = [(a["id"], a.get("attributes") or {}) for a in self.client.attachments(proj.id, item["id"])]
        images = {aid: at.get("name", "?") for aid, at in atts}
        return {
            "ref": proj.ref(item),
            "name": item["name"],
            "state": proj.state_name(item),
            "labels": sorted(by_id.get(i, i) for i in item.get("labels") or []),
            "blocked_by": [b["issue_id"] for b in self._blockers(proj, item)],
            "description": _text(item.get("description_html") or "", images),
            "attachments": [{"name": at.get("name"), "type": at.get("type"), "size": at.get("size")} for _, at in atts],
            "comments": [{"at": c["created_at"], "text": _text(c.get("comment_html", ""), images)} for c in comments],
        }

    def _would_cycle(self, proj: Project, item_id: str, blocker_ids: Iterable[str]) -> bool:
        """True if item_id is reachable from any blocker by following blocked_by."""
        seen: set[str] = set()
        stack = list(blocker_ids)
        while stack:
            cur = stack.pop()
            if cur == item_id:
                return True
            if cur in seen:
                continue
            seen.add(cur)
            rel = self.client.relations(proj.id, cur)
            stack.extend(r["issue_id"] for r in rel.get("blocked_by", []))
        return False

    def _blockable(self, ref: str) -> tuple[Project, dict, str]:
        """The item to block and where it returns when released. A worker blocks only the In Progress item it
        holds. The orchestrator may also hold a Ready item nobody holds, an In Review item that is not kind:deploy
        (its holder label stays), and an item already Blocked: a blocker or a wait added to a needs-human question
        keeps the label, which only the operator removes."""
        proj, item = self.resolve(ref)
        state = proj.state_name(item)
        if self.slug != ORCHESTRATOR or state == "In Progress":
            return (*self._owned(ref), "In Progress")
        if state == "Ready" and not item.get("assignees"):
            return proj, item, "Ready"
        if state == "In Review":
            if self._has(proj, item, "kind:deploy"):
                raise ProtocolRefusal(f"{ref} is kind:deploy and In Review: it waits on the operator by design, and "
                                      "only the operator moves it", EXIT_REFUSED)  # fmt: skip
            return proj, item, "In Review"
        if state == "Blocked":
            if (item.get("assignees") or []) not in ([], [self.me["id"]]):
                raise ProtocolRefusal(f"{ref} is held by a human; the operator moves it", EXIT_REFUSED)
            hold = latest_hold(self.client.comments(proj.id, item["id"]))
            return proj, item, hold[0] if hold else "Ready"
        raise ProtocolRefusal(f"{ref} is {state}{' and assigned' if item.get('assignees') else ''}; the orchestrator "
                              "blocks Ready items nobody holds, In Review items and Blocked ones", EXIT_REFUSED)  # fmt: skip

    def block_on(self, ref: str, blocker_refs: list[str], reason: str, attach: Iterable[str] = ()) -> None:
        check_body(reason, "--reason")
        files = self._files(attach)
        proj, item, origin = self._blockable(ref)
        blockers = [self.resolve(b)[1] for b in blocker_refs]
        ids = [b["id"] for b in blockers]
        if self._would_cycle(proj, item["id"], ids):
            if origin != "In Progress":  # not ours to flag: the operator's labels and states stay as they are
                raise ProtocolRefusal(f"blocking {ref} on {blocker_refs} would create a cycle", EXIT_REFUSED)
            self._flag_human(
                proj,
                item,
                f"Refused to record {ref} blocked by {', '.join(blocker_refs)}: it would create a dependency "
                f"cycle. Needs a human to untangle the relations." + (f"\n\nContext:\n\n{reason.strip()}" if reason.strip() else ""),
            )
            raise ProtocolRefusal(f"blocking {ref} on {blocker_refs} would create a cycle", EXIT_REFUSED)
        def change() -> None:
            self.client.add_relation(proj.id, item["id"], "blocked_by", ids)
            if item["state"] != proj.state_id("Blocked"):
                self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Blocked")})

        why = f"\n\n{reason.strip()}" if reason.strip() else ""
        self._write(proj, item, f"Blocked by {', '.join(blocker_refs)}.{why}\n\n{self._sig()}", files, change,
                    hold_statement(origin))  # fmt: skip

    def block_wait(self, ref: str, text: str, until: str | None = None, attach: Iterable[str] = ()) -> date:
        """Blocked on time or the outside world: label `waiting` until a last day (default a week from today).
        The sweep returns it when that day has passed; it adds no needs-human. Returns the last day."""
        if not text.strip():
            raise ProtocolRefusal("--wait must say what the item waits for", EXIT_REFUSED)
        check_body(text, "--wait")
        files = self._files(attach)
        proj, item, origin = self._blockable(ref)
        today = self.today()
        day = parse_day(until, "--until") or today + timedelta(days=WAIT_DAYS)
        if day < today:
            raise ProtocolRefusal(f"--until {day} has passed; give a day from {today} on", EXIT_REFUSED)
        wid = self._ensure_label(proj, WAITING, dict(LABELS)[WAITING])
        labels = list(item.get("labels") or [])
        body = {"state": proj.state_id("Blocked"), "labels": labels + [wid] if wid not in labels else labels}
        self._write(proj, item, f"Waiting until {day}: {text.strip()}\n\n{self._sig()}", files,
                    lambda: self.client.update_item(proj.id, item["id"], body), hold_statement(origin, day))  # fmt: skip
        return day

    def _flag_human(self, proj: Project, item: dict, text: str, files: Iterable[File] = ()) -> None:
        labels = list(item.get("labels") or [])
        nh = proj.label_id(NEEDS_HUMAN)
        if nh not in labels:
            labels.append(nh)
        body = {"state": proj.state_id("Blocked"), "labels": labels}
        self._write(proj, item, f"{text}\n\n{self._sig()}", files, lambda: self.client.update_item(proj.id, item["id"], body))
        self._push("needs_human", proj, item, "Blocked", text)

    def block_human(self, ref: str, question: str, attach: Iterable[str] = ()) -> None:
        check_body(question, "--human")
        files = self._files(attach)
        proj, item = self._owned(ref)
        self._flag_human(proj, item, f"Needs a human:\n\n{question.strip()}", files)

    def _needs_summary(self, proj: Project, item: dict, summary: str, kinds: Iterable[str]) -> None:
        """A summary is optional, except where the ticket needs one: a bugfix says what was wrong and how it was
        fixed, and a deploy hand-off lists the commands run and how they were verified."""
        why = {"kind:bug": "what was wrong and how it was fixed",
               "kind:deploy": "the commands run and how they were verified"}  # fmt: skip
        labels = set(item.get("labels") or [])
        for kind in kinds:
            if not summary.strip() and proj.labels.get(kind, {}).get("id") in labels:
                raise ProtocolRefusal(f"{proj.ref(item)} is {kind}; --summary must say {why[kind]}", EXIT_REFUSED)

    def review(self, ref: str, summary: str = "", attach: Iterable[str] = ()) -> None:
        check_body(summary, "--summary")
        files = self._files(attach)
        proj, item = self._owned(ref)
        self._check_session(proj, item)
        self._needs_summary(proj, item, summary, ("kind:bug", "kind:deploy"))
        change = lambda: self.client.update_item(proj.id, item["id"], {"state": proj.state_id("In Review")})
        self._write(proj, item, f"Ready for review.{_then(summary)}\n\n{self._sig()}", files, change)
        if self._has(proj, item, "kind:deploy"):
            self._push("deploy_review", proj, item, "In Review", summary)

    def done(self, ref: str, summary: str = "", attach: Iterable[str] = ()) -> None:
        check_body(summary, "--summary")
        files = self._files(attach)
        proj, item = self._owned(ref)
        self._check_session(proj, item)
        labels = set(item.get("labels") or [])
        human_only = {proj.labels[n]["id"] for n in ("kind:deploy", NEEDS_HUMAN) if n in proj.labels}
        if labels & human_only:
            raise ProtocolRefusal(
                f"{ref} is kind:deploy or needs-human; only the operator moves it to Done. Use `review`.",
                EXIT_REFUSED,
            )
        self._needs_summary(proj, item, summary, ("kind:bug",))
        change = lambda: self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Done")})
        self._write(proj, item, f"Done.{_then(summary)}\n\n{self._sig()}", files, change)

    def answer(self, ref: str, answer: str) -> str:
        """Relay the operator's answer to a needs-human item, given in this agent's session. Removes the label; a
        Blocked item then goes where the sweep would send it: it stays Blocked while other blockers are open or a
        wait (label `waiting`) is on, else it returns to where it was held from (In Review, or Ready, unassigned).
        Returns the resulting state."""
        if not answer.strip():
            raise ProtocolRefusal("--answer must quote the operator's answer", EXIT_REFUSED)
        proj, item = self.resolve(ref)
        nh = proj.labels.get(NEEDS_HUMAN, {}).get("id")
        labels = list(item.get("labels") or [])
        if not nh or nh not in labels:
            raise ProtocolRefusal(f"{ref} is not labelled {NEEDS_HUMAN}", EXIT_REFUSED)
        body: dict = {"labels": [x for x in labels if x != nh]}
        state = proj.state_name(item)
        blocked = item["state"] == proj.state_id("Blocked")
        wid = proj.labels.get(WAITING, {}).get("id")
        waiting = bool(wid) and wid in labels  # the sweep ends the wait on its day
        if blocked and not waiting and all(self._is_closed(proj, b, {}) for b in self._blockers(proj, item)):
            hold = latest_hold(self.client.comments(proj.id, item["id"]))
            state = "In Review" if hold and hold[0] == "In Review" else "Ready"
            body["state"] = proj.state_id(state)
            if state == "Ready":
                body["assignees"] = []
        self.client.update_item(proj.id, item["id"], body)
        self.comment(
            proj, item, f"Operator's answer, relayed by agent:{self.slug}:\n\n{answer}\n\nNow {state}.\n\n{self._sig()}"
        )
        return state

    # -- review verdicts (orchestrator) ------------------------------------

    def _in_review(self, ref: str, verb: str) -> tuple[Project, dict]:
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal(f"{verb} is an orchestrator move", EXIT_REFUSED)
        proj, item = self.resolve(ref)
        if item["state"] != proj.state_id("In Review"):
            raise ProtocolRefusal(f"{ref} is {proj.state_name(item)}, not In Review", EXIT_REFUSED)
        return proj, item

    def accept(self, ref: str, summary: str = "") -> None:
        """Review passed: In Review -> Done. Deploy and needs-human items stay the operator's to close."""
        check_body(summary, "--summary")
        proj, item = self._in_review(ref, "accept")
        labels = set(item.get("labels") or [])
        human_only = {proj.labels[n]["id"] for n in ("kind:deploy", NEEDS_HUMAN) if n in proj.labels}
        if labels & human_only:
            raise ProtocolRefusal(
                f"{ref} is kind:deploy or needs-human; only the operator moves it to Done.", EXIT_REFUSED
            )
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Done")})
        self.comment(proj, item, f"Accepted.{_then(summary)}\n\n{self._sig()}")

    def reject(self, ref: str, reason: str) -> None:
        """Review failed: In Review -> Ready, unassigned, for another dispatch."""
        check_body(reason, "--reason")
        proj, item = self._in_review(ref, "reject")
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Ready"), "assignees": []})
        self.comment(proj, item, f"Rejected; returned to Ready.\n\n{reason}\n\n{self._sig()}")

    def _holder(self, proj: Project, item: dict, ref: str) -> str:
        slugs = sorted(self._agent_labels(proj, item))
        if len(slugs) != 1:
            raise ProtocolRefusal(f"{ref} has agent labels {slugs or 'none'}; the operator fixes the labels", EXIT_REFUSED)
        return slugs[0].removeprefix(AGENT_PREFIX)

    def rework(self, ref: str, notes: str) -> str:
        """Review found more to do: In Review -> In Progress for the same holder, with the notes. Its stale clock
        restarts from this comment. Deploy and needs-human items are the operator's. Returns the holder's slug."""
        if not notes.strip():
            raise ProtocolRefusal("rework needs notes saying what is left", EXIT_REFUSED)
        check_body(notes, "notes")
        proj, item = self._in_review(ref, "rework")
        if self._has(proj, item, *HUMAN_ONLY):
            raise ProtocolRefusal(f"{ref} is kind:deploy or needs-human; the operator decides what happens next",
                                  EXIT_REFUSED)  # fmt: skip
        slug = self._holder(proj, item, ref)
        self._refuse_busy(proj, slug)
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("In Progress"), "assignees": [self.me["id"]]})
        self.comment(proj, item, f"Rework for agent:{slug}:\n\n{notes}\n\n{self._sig()}")
        return slug

    def review_for(self, ref: str, slug: str, reason: str, summary: str = "") -> datetime:
        """Hand a silent agent's In Progress item to review, signed as the orchestrator: the way to close out for
        a finished agent without signing as it. Refused while the holder was active within RECLAIM_FLOOR_H hours,
        and for deploy and needs-human items. Returns the holder's last activity."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("review --for is an orchestrator move", EXIT_REFUSED)
        if not reason.strip():
            raise ProtocolRefusal("--reason must say why the orchestrator hands it over", EXIT_REFUSED)
        check_body(reason, "--reason")
        check_body(summary, "--summary")
        proj, item = self.resolve(ref)
        if item["state"] != proj.state_id("In Progress") or (item.get("assignees") or []) != [self.me["id"]]:
            raise ProtocolRefusal(f"{ref} is {proj.state_name(item)} and not agent-held", EXIT_REFUSED)
        if self._holder(proj, item, ref) != check_slug(slug):
            raise ProtocolRefusal(f"{ref} is held by {min(self._agent_labels(proj, item))}, not agent:{slug}",
                                  EXIT_REFUSED)  # fmt: skip
        if self._has(proj, item, *HUMAN_ONLY):
            raise ProtocolRefusal(f"{ref} is kind:deploy or needs-human; its holder hands it over, or the operator "
                                  "does", EXIT_REFUSED)  # fmt: skip
        last = self._last_agent_activity(proj, item, slug)
        if last > self.now() - timedelta(hours=RECLAIM_FLOOR_H):
            raise ProtocolRefusal(f"agent:{slug} was active on {ref} at {last.isoformat(timespec='minutes')}, under "
                                  f"{RECLAIM_FLOOR_H} h ago; let it hand over itself", EXIT_REFUSED)  # fmt: skip
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("In Review")})
        self.comment(proj, item, f"Ready for review, handed over for agent:{slug} (silent since "
                                 f"{last.isoformat(timespec='minutes')}).\n\n{reason}{_then(summary)}\n\n{self._sig()}")  # fmt: skip
        return last

    def close_epic(self, ref: str, summary: str) -> list[str]:
        """Parent item -> Done once every child is Done or Cancelled. Returns the children's refs."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("close-epic is an orchestrator move", EXIT_REFUSED)
        if not summary.strip():
            raise ProtocolRefusal("--summary must say what the epic delivered", EXIT_REFUSED)
        check_body(summary, "--summary")
        proj, item = self.resolve(ref)
        if proj.group(item) in CLOSED_GROUPS:
            raise ProtocolRefusal(f"{ref} is already {proj.state_name(item)}", EXIT_REFUSED)
        if proj.state_name(item) == "In Progress":
            raise ProtocolRefusal(f"{ref} is In Progress; whoever holds it closes it", EXIT_REFUSED)
        labels = set(item.get("labels") or [])
        human_only = {proj.labels[n]["id"] for n in ("kind:deploy", NEEDS_HUMAN) if n in proj.labels}
        if labels & human_only:
            raise ProtocolRefusal(f"{ref} is kind:deploy or needs-human; only the operator closes it.", EXIT_REFUSED)
        children = self._children(proj, item)
        if not children:
            raise ProtocolRefusal(f"{ref} has no sub-items; use done or accept", EXIT_REFUSED)
        open_ = [f"{proj.ref(c)} ({proj.state_name(c)})" for c in children if proj.group(c) not in CLOSED_GROUPS]
        if open_:
            raise ProtocolRefusal(f"{ref} still has open sub-items: {', '.join(open_)}", EXIT_REFUSED)
        refs = [f"{proj.ref(c)} ({proj.state_name(c)})" for c in children]
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Done")})
        self.comment(proj, item, f"Epic closed: every sub-item is closed ({', '.join(refs)}).\n\n{summary}\n\n{self._sig()}")
        return refs

    def _children(self, proj: Project, item: dict) -> list[dict]:
        """Sub-items, by matching `parent`: the v1 list has no parent filter."""
        return sorted(
            (i for i in self.client.iter_items(proj.id) if i.get("parent") == item["id"]),
            key=lambda i: i["sequence_id"],
        )

    # -- triage (orchestrator) ---------------------------------------------

    def ready(self, ref: str, reason: str = "") -> str:
        """Backlog -> Ready, unassigned (Blocked instead while a blocker is open, so the sweep releases it later),
        or end a hold early: a Blocked item whose blockers are all closed returns to where it was held from, and
        loses `waiting`. Returns the resulting state."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("ready is an orchestrator move", EXIT_REFUSED)
        check_body(reason, "--reason")
        proj, item = self.resolve(ref)
        now = proj.state_name(item)
        if now not in ("Backlog", "Blocked"):
            raise ProtocolRefusal(f"{ref} is {now}, not Backlog or Blocked", EXIT_REFUSED)
        if self._has(proj, item, NEEDS_HUMAN):
            raise ProtocolRefusal(f"{ref} carries {NEEDS_HUMAN}; the operator removes it once the question is "
                                  "answered (agents never do)", EXIT_REFUSED)  # fmt: skip
        open_blockers = not all(self._is_closed(proj, b, {}) for b in self._blockers(proj, item))
        if now == "Blocked":
            if (item.get("assignees") or []) not in ([], [self.me["id"]]):
                raise ProtocolRefusal(f"{ref} is held by a human; the operator moves it", EXIT_REFUSED)
            if open_blockers:
                raise ProtocolRefusal(f"{ref} still has open blockers; the sweep releases it once they close",
                                      EXIT_REFUSED)  # fmt: skip
            state = self._return_to(proj, item, latest_hold(self.client.comments(proj.id, item["id"])))
            if reason.strip():
                self.comment(proj, item, f"Released from Blocked; now {state}.\n\n{reason}\n\n{self._sig()}")
            return state
        if self._children(proj, item):
            raise ProtocolRefusal(f"{ref} has sub-items; epics are closed with close-epic, never dispatched", EXIT_REFUSED)
        state = "Blocked" if open_blockers else "Ready"
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id(state), "assignees": []})
        if reason.strip():  # the activity log records the move; a comment only when it says why
            tail = " It has open blockers, so it returns to Ready once they close." if open_blockers else ""
            self.comment(proj, item, f"Promoted from Backlog; now {state}.{tail}\n\n{reason}\n\n{self._sig()}")
        return state

    def backlog(self, ref: str, reason: str) -> None:
        """Ready -> Backlog for an item nobody holds: not dispatchable until it is promoted again. Its dates stay;
        `plan REF --clear` removes them."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("backlog is an orchestrator move", EXIT_REFUSED)
        if not reason.strip():
            raise ProtocolRefusal("--reason must say why the item goes back to Backlog", EXIT_REFUSED)
        check_body(reason, "--reason")
        proj, item = self.resolve(ref)
        if proj.state_name(item) != "Ready" or item.get("assignees"):
            raise ProtocolRefusal(f"{ref} is {proj.state_name(item)}{', assigned' if item.get('assignees') else ''}; "
                                  "backlog takes a Ready item nobody holds", EXIT_REFUSED)  # fmt: skip
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Backlog")})
        self.comment(proj, item, f"Returned to Backlog.\n\n{reason}\n\n{self._sig()}")

    def prioritize(self, ref: str, priority: str, reason: str = "") -> None:
        """Set an open item's priority: the orchestrator on any open item, a worker on the item it holds. The
        activity log records the change; a comment only when there is a reason."""
        if priority not in PRIORITIES:
            raise ProtocolRefusal(f"priority must be one of {PRIORITIES}", EXIT_REFUSED)
        check_body(reason, "--reason")
        if self.slug == ORCHESTRATOR:
            proj, item = self.resolve(ref)
            if proj.group(item) in CLOSED_GROUPS:
                raise ProtocolRefusal(f"{ref} is {proj.state_name(item)}", EXIT_REFUSED)
        else:
            proj, item = self._owned(ref)
        self.client.update_item(proj.id, item["id"], {"priority": priority})
        if reason.strip():
            self.comment(proj, item, f"Priority {priority}.\n\n{reason}\n\n{self._sig()}")

    def cancel(self, ref: str, reason: str) -> None:
        """Abandon an item nobody holds. Refuses anything whose cancellation would release or orphan open work."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("cancel is an orchestrator move", EXIT_REFUSED)
        if not reason.strip():
            raise ProtocolRefusal("--reason must say why the item is abandoned", EXIT_REFUSED)
        check_body(reason, "--reason")
        proj, item = self.resolve(ref)
        if proj.group(item) in CLOSED_GROUPS:
            raise ProtocolRefusal(f"{ref} is already {proj.state_name(item)}", EXIT_REFUSED)
        if proj.state_name(item) == "In Progress":
            raise ProtocolRefusal(f"{ref} is In Progress; reclaim it or let its holder finish first", EXIT_REFUSED)
        labels = set(item.get("labels") or [])
        human_only = {proj.labels[n]["id"] for n in ("kind:deploy", NEEDS_HUMAN) if n in proj.labels}
        if labels & human_only:
            raise ProtocolRefusal(f"{ref} is kind:deploy or needs-human; only the operator cancels it.", EXIT_REFUSED)
        open_children = [f"{proj.ref(c)} ({proj.state_name(c)})" for c in self._children(proj, item)
                         if proj.group(c) not in CLOSED_GROUPS]  # fmt: skip
        if open_children:
            raise ProtocolRefusal(f"{ref} still has open sub-items: {', '.join(open_children)}", EXIT_REFUSED)
        # A Cancelled blocker counts as closed, so cancelling would release work whose prerequisite was dropped.
        dependents = self.client.relations(proj.id, item["id"]).get("blocking", [])
        open_dependents = [d for d in dependents if not self._is_closed(proj, d, {})]
        if open_dependents:
            by_id = {i["id"]: i for i in self.client.iter_items(proj.id)}
            names = [proj.ref(by_id[d["issue_id"]]) if d["issue_id"] in by_id else d["issue_id"] for d in open_dependents]
            raise ProtocolRefusal(
                f"{ref} blocks open items {', '.join(names)}; cancel or re-plan those first", EXIT_REFUSED
            )
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Cancelled")})
        self.comment(proj, item, f"Cancelled.\n\n{reason}\n\n{self._sig()}")

    # -- dates -------------------------------------------------------------

    def _dates(self, item: dict, target: date | None, start: date | None, clear: bool = False) -> dict:
        """The date fields to write: the ones given, checked against the ones kept. Refuses a start after the
        target."""
        if clear:
            return {"start_date": None, "target_date": None}
        body = {k: v.isoformat() for k, v in (("start_date", start), ("target_date", target)) if v is not None}
        st = start or parse_day((item.get("start_date") or "")[:10], "start")
        tg = target or parse_day((item.get("target_date") or "")[:10], "target")
        if st and tg and st > tg:
            raise ProtocolRefusal(f"start {st} is after target {tg}", EXIT_REFUSED)
        return body

    def plan(self, ref: str, target: str | None = None, start: str | None = None, clear: bool = False,
             reason: str = "") -> dict:  # fmt: skip
        """Set or clear an open item's native start and target dates. Anyone may date an item nobody holds; a held
        one is its holder's or the orchestrator's (exit 4 otherwise). A signed comment only with a reason. Returns
        the dates written."""
        if clear and (target or start):
            raise ProtocolRefusal("plan --clear takes no --target or --start", EXIT_REFUSED)
        if not clear and not (target or start):
            raise ProtocolRefusal("plan needs --target, --start or --clear", EXIT_REFUSED)
        check_body(reason, "--reason")
        tgt, st = parse_day(target, "--target"), parse_day(start, "--start")
        proj, item = self.resolve(ref)
        if proj.group(item) in CLOSED_GROUPS:
            raise ProtocolRefusal(f"{ref} is {proj.state_name(item)}; a closed item's dates don't change", EXIT_REFUSED)
        holders = self._agent_labels(proj, item)
        if holders and proj.group(item) == "started" and self.slug != ORCHESTRATOR and self.label not in holders:
            raise ProtocolRefusal(f"{ref} is held by {', '.join(sorted(holders))}; only it or the orchestrator "
                                  "changes its dates", EXIT_OWNERSHIP_LOST)  # fmt: skip
        body = self._dates(item, tgt, st, clear)
        if any(item.get(k) != v for k, v in body.items()):
            self.client.update_item(proj.id, item["id"], body)
        if reason.strip():
            what = "Dates cleared." if clear else "Dates: " + ", ".join(
                f"{k.removesuffix('_date')} {v}" for k, v in body.items()) + "."  # fmt: skip
            self.comment(proj, item, f"{what}\n\n{reason.strip()}\n\n{self._sig()}")
        return body

    # -- create ------------------------------------------------------------

    def create(
        self,
        identifier: str,
        name: str,
        kind: str,
        description: str = "",
        parent: str | None = None,
        priority: str = "none",
        ready: bool = False,
        blocked_by: list[str] | None = None,
        start: str | None = None,
        target: str | None = None,
    ) -> dict:
        """Create an item, with its native start and target dates when given."""
        if kind not in KINDS:
            raise ProtocolRefusal(f"kind must be one of {KINDS}", EXIT_REFUSED)
        proj = self.project(identifier)
        dates = self._dates({}, parse_day(target, "--target"), parse_day(start, "--start"))
        body: dict = {
            "name": name,
            "labels": [proj.label_id(f"kind:{kind}")],
            "priority": priority,
            "state": proj.state_id("Ready" if ready else "Backlog"),
            **dates,
        }
        if self.change:
            description = f"{description}\n\n**OpenSpec change:** `{self.change}`".strip()
        if description:
            body["description_html"] = _md(description)
        parent_item = self.resolve(parent)[1] if parent else None
        if parent_item:
            body["parent"] = parent_item["id"]
        item = self.client.create_item(proj.id, body)
        if blocked_by:
            ids = [self.resolve(b)[1]["id"] for b in blocked_by]
            if self._would_cycle(proj, item["id"], ids):
                raise ProtocolRefusal("blocked_by would create a cycle", EXIT_REFUSED)
            self.client.add_relation(proj.id, item["id"], "blocked_by", ids)
        return item

    # -- stale -------------------------------------------------------------

    def _last_agent_activity(self, proj: Project, item: dict, slug: str) -> datetime:
        """Latest moment agent <slug> showed life on the item: a comment signed by it, its dispatch or rework comment
        (old trails' "Dispatched to agent:<slug>." still match), or the
        activity that added its label. Everything is written by the same account, so only signatures tell agents
        apart; others' comments (nudges included) never count. If no dispatch record survives (comment POST failed,
        comment deleted), fall back to the item's updated_at rather than its creation, so a fresh dispatch of an old
        item is never instantly stale."""
        times: list[datetime] = []
        for c in self.client.comments(proj.id, item["id"]):
            body = c.get("comment_html", "")
            if signed_by(body, slug) or f"Dispatched to agent:{slug}." in body or f"Rework for agent:{slug}:" in body:
                times.append(_ts(c["created_at"]))
        lid = proj.labels.get(AGENT_PREFIX + slug, {}).get("id")
        for a in self.client.activities(proj.id, item["id"]):
            if a.get("field") == "labels" and lid and a.get("new_identifier") == lid:
                times.append(_ts(a["created_at"]))
        if not times:
            times.append(_ts(item.get("updated_at") or item["created_at"]))
        return max(times)

    def _take_back(self, proj: Project, item: dict, text: str) -> bool:
        """In Progress -> Ready, unassigned, if the item hasn't moved since it was read. True if it was taken."""
        fresh = self.reread(proj, item)
        if fresh["state"] != proj.state_id("In Progress") or fresh.get("labels") != item.get("labels"):
            return False
        self.client.update_item(proj.id, item["id"], {"state": proj.state_id("Ready"), "assignees": []})
        self.comment(proj, item, f"{text}\n\n{self._sig()}")
        return True

    def reclaim(self, ref: str, reason: str, force: bool = False) -> str:
        """Take one agent-held In Progress item back early, with the reason on record. Refused while its holder
        showed activity in the last RECLAIM_FLOOR_H hours, unless forced. Returns the slug it was taken from."""
        if self.slug != ORCHESTRATOR:
            raise ProtocolRefusal("reclaim is an orchestrator move", EXIT_REFUSED)
        if not reason.strip():
            raise ProtocolRefusal("--reason must say why the item is taken back", EXIT_REFUSED)
        check_body(reason, "--reason")
        proj, item = self.resolve(ref)
        slugs = sorted(self._agent_labels(proj, item))
        if item["state"] != proj.state_id("In Progress") or (item.get("assignees") or []) != [self.me["id"]]:
            raise ProtocolRefusal(f"{ref} is {proj.state_name(item)} and not agent-held; nothing to reclaim", EXIT_REFUSED)
        if len(slugs) != 1:
            raise ProtocolRefusal(f"{ref} has agent labels {slugs or 'none'}; the operator fixes the labels", EXIT_REFUSED)
        slug = slugs[0].removeprefix(AGENT_PREFIX)
        last = self._last_agent_activity(proj, item, slug)
        if not force and last > self.now() - timedelta(hours=RECLAIM_FLOOR_H):
            raise ProtocolRefusal(
                f"agent:{slug} was active on {ref} at {last.isoformat(timespec='minutes')}, under "
                f"{RECLAIM_FLOOR_H} h ago; it may still be working. Pass --force with the reason to take it anyway",
                EXIT_REFUSED,
            )
        if not self._take_back(proj, item, f"Reclaimed from agent:{slug}; returned to Ready, unassigned.\n\n{reason}"):
            raise ProtocolRefusal(f"{ref} changed while reclaiming it; re-check the item", EXIT_REFUSED)
        return slug

    def stale(self, identifier: str, hours: float) -> list[tuple[dict, str, datetime | None]]:
        """Agent-held items whose agent has been silent for `hours`; it lists, never moves (`reclaim REF` takes one
        back). An item with several agent: labels is reported with last=None: no agent can write to it, so the
        operator has to fix the labels."""
        proj = self.project(identifier)
        in_progress = proj.state_id("In Progress")
        cutoff = self.now() - timedelta(hours=hours)
        me = self.me["id"]
        found: list[tuple[dict, str, datetime | None]] = []
        self._seen.pop(proj.identifier, None)
        for it in self._items(proj).values():
            if it["state"] != in_progress or (it.get("assignees") or []) != [me]:
                continue  # unassigned or held by a human: not an agent claim
            slugs = sorted(self._agent_labels(proj, it))
            if not slugs:
                continue
            if len(slugs) > 1:
                found.append((it, "+".join(slugs), None))
                continue
            slug = slugs[0].removeprefix(AGENT_PREFIX)
            last = self._last_agent_activity(proj, it, slug)
            if last < cutoff:
                found.append((it, slug, last))
        return found
