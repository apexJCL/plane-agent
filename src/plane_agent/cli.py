"""plane-agent: agent-coordination verbs over a Plane tracker (Plane Cloud or self-hosted).

Every agent acts through one shared agent account; which agent holds an item is its agent:<slug> label, and
every comment is signed "agent=<slug> · session=… · where=… · openspec=…".

Setup: `plane-agent init --url URL --workspace SLUG --project KEY` writes .plane-agent.toml
(no secrets), then store the agent account's API token (`plane-agent config` says where it is looked for).
Settings (environment beats the project file, which beats the user file):
  PLANE_BASE_URL         instance URL (default https://api.plane.so, Plane Cloud)
  PLANE_WORKSPACE_SLUG   workspace slug, from the workspace's URL
  PLANE_PROJECT          default project key for queue, stale, search, create, bootstrap-project
  PLANE_TZ               optional IANA time zone that decides "today" (overdue, waits); default: this machine's
  PLANE_RATE_LIMIT       API calls a minute the instance allows (default 60)
  PLANE_AGENT            your agent slug, e.g. orchestrator or api-implementation (or --agent)
  PLANE_AGENT_SESSION    session/run id for the comment signature (or --session)
  PLANE_OPENSPEC_CHANGE  optional: the OpenSpec change you work under (or --openspec)
Token: --token-file, PLANE_API_KEY with PLANE_BASE_URL, the user file's [hosts."<host>"] entry, or the keyring
  (service "plane-agent", account = the host). Run `plane-agent config` to see what resolves.

Text bodies (comment text, --summary, --reason, --answer, --human, --description) are markdown. Pass `-` to read
one from stdin, which keeps code, tables and backticks away from shell quoting:
  plane-agent done HOME-12 --summary - <<'EOF' ... EOF

Files (comment, block, review, done): --attach FILE, repeatable, uploads it to the item. PNG, JPEG, GIF and WebP
show inline above the signature; pdf, txt, log, yaml, yml, diff, patch, md, json, csv, zip, gz, tgz, tar, mp4,
mov and webm are named in the comment. Up to 25 MiB each; 2 API calls per file. Never show a secret on screen.
  plane-agent done HOME-12 --summary "Fixed the header" --attach header.png

Exit codes: 0 ok · 1 error · 2 usage · 3 not dispatchable · 4 not held by you (stop work) · 5 refused
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from . import config as conf
from .attachments import human_size
from .client import PlaneClient, PlaneError
from .notify import notifier
from .protocol import KINDS, ORCHESTRATOR, PRIORITIES, Agent, ProtocolRefusal, WriteFailed

VERSION = "0.4.0"
VERBS = [
    "init", "config", "whoami", "bootstrap-project", "queue", "dispatch", "claim", "check", "context", "comment",
    "block", "review", "done", "answer", "accept", "reject", "rework", "close-epic", "ready", "backlog", "prioritize", "cancel", "reclaim", "create", "plan", "stale", "search",
]  # fmt: skip
REMOVED = {  # verbs and flags removed in 0.3.0: they exit 2 with this message instead of a bare usage error
    "report": "report was removed in 0.3.0 with the planning layer (plans, actuals, cycles); there is no replacement",
    "log": "log was removed in 0.3.0 with retroactive entries; file the work with `create`, then `done` or `review`",
    "stale --reclaim": "stale --reclaim was removed in 0.3.0; `stale` only lists now. Take each item back with "
                       "`reclaim REF --reason TEXT` (orchestrator)",
}  # fmt: skip


def _where() -> str:
    try:
        top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
        br = subprocess.run(["git", "branch", "--show-current"], capture_output=True, text=True, check=True)
        return f"{os.path.basename(top.stdout.strip())}@{br.stdout.strip() or 'detached'}"
    except (FileNotFoundError, subprocess.CalledProcessError):
        return os.path.basename(os.getcwd()) or "-"  # not the full path: signatures are visible to the whole workspace


def _attach(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--attach", action="append", default=[], metavar="FILE",
                        help="upload FILE to the item with this comment (repeatable); images show inline")  # fmt: skip


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="plane-agent", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--agent", default=os.environ.get("PLANE_AGENT"), help="your agent slug (label agent:<slug>)")
    p.add_argument("--session", default=os.environ.get("PLANE_AGENT_SESSION") or None,
                   help="session/run id for signatures (PLANE_AGENT_SESSION); every write needs one")  # fmt: skip
    p.add_argument("--openspec", default=os.environ.get("PLANE_OPENSPEC_CHANGE", ""), help="OpenSpec change name")
    p.add_argument("--where", default=None, help="repo@branch, a name and never a path; auto-detected from git")
    p.add_argument("--token-file", help="read the API token from this file")
    p.add_argument("--json", action="store_true", help="machine-readable output")
    p.add_argument("--version", action="version", version=f"plane-agent {VERSION}")
    sub = p.add_subparsers(dest="verb", required=True, metavar="VERB")

    it = sub.add_parser("init", help="write the settings file (.plane-agent.toml here, or --user); no token")
    it.add_argument("--url", required=True, help="instance URL, e.g. https://api.plane.so or https://plane.example.com")
    it.add_argument("--workspace", required=True, help="workspace slug, as in https://<host>/<slug>/projects")
    it.add_argument("--project", help="default project key, e.g. WEB")
    it.add_argument("--timezone", help="optional IANA time zone for \"today\", e.g. Europe/Madrid (default: local)")
    it.add_argument("--rate-limit", type=int, help="API calls a minute the instance allows (default 60)")
    it.add_argument("--user", action="store_true", help="write your user file instead of the project file")
    it.add_argument("--force", action="store_true", help="overwrite an existing file")
    sub.add_parser("config", help="show the resolved settings, where each came from, and where the token is read")

    sub.add_parser("whoami", help="show the account the token resolves to, and your agent slug")
    b = sub.add_parser("bootstrap-project", help="shape a project's states and labels to the protocol (idempotent)")
    b.add_argument("project", nargs="?", help="project key (default: the configured project)")

    q = sub.add_parser("queue", help="dispatchable items, in order; orchestrator also sweeps resolved Blocked items")
    q.add_argument("project", nargs="?", help="project key (default: the configured project)")
    q.add_argument("--sweep", action=argparse.BooleanOptionalAction, default=None,
                   help="default: on for agent orchestrator, off otherwise")  # fmt: skip

    d = sub.add_parser("dispatch", help="hand a Ready item to an agent slug: In Progress, label agent:<slug>")
    d.add_argument("ref")
    d.add_argument("--to", required=True, help="agent slug, e.g. api-implementation (yourself if working solo)")

    c = sub.add_parser("claim", help="worker start: confirm the item was dispatched to you, post claim comment")
    c.add_argument("ref", help="e.g. HOME-12")

    cx = sub.add_parser("context", help="description, labels, blockers and comment trail, to resume an item")
    cx.add_argument("ref")
    cx.add_argument("--by", metavar="SLUG", help="only comments signed by this agent slug")

    k = sub.add_parser("check", help="verify you still hold the item (before long or irreversible steps)")
    k.add_argument("ref")

    cm = sub.add_parser("comment", help="comment on an owned item")
    cm.add_argument("ref")
    cm.add_argument("text", help="markdown, or - for stdin: lists for enumerations and steps, `code` for names, tables for comparisons")
    cm.add_argument("--force", action="store_true", help="orchestrator/operator: comment without owning it")
    _attach(cm)

    bl = sub.add_parser("block", help="move an owned item to Blocked: on other items, on time (--wait) or on a human; "
                        "the orchestrator also holds Ready, In Review (not deploy) and Blocked items")  # fmt: skip
    bl.add_argument("ref")
    g = bl.add_mutually_exclusive_group(required=True)
    g.add_argument("--on", nargs="+", metavar="REF", help="blocked by these items (refuses cycles)")
    g.add_argument("--wait", metavar="TEXT", help="waiting on time or the outside world: label waiting, no "
                   "needs-human; the sweep returns it after --until")  # fmt: skip
    g.add_argument("--human", metavar="QUESTION", help="needs the operator; adds needs-human")
    bl.add_argument("--until", metavar="YYYY-MM-DD", help="last day of the --wait (default a week from today)")
    bl.add_argument("--reason", default="", help="context, with --on")
    _attach(bl)

    r = sub.add_parser("review", help="owned item -> In Review (deploy items end here); orchestrator: --for a "
                       "silent agent's item")  # fmt: skip
    r.add_argument("ref")
    r.add_argument("--for", dest="for_slug", metavar="SLUG", help="orchestrator: hand over agent SLUG's item, "
                   "signed as yourself; needs --reason and 24 h of silence from it (never sign as another slug)")  # fmt: skip
    r.add_argument("--reason", default="", help="with --for: why you hand it over")
    r.add_argument("--summary", default="", help="optional; required for kind:bug (cause and fix) and kind:deploy "
                   "(commands run, verification)")  # fmt: skip
    _attach(r)

    dn = sub.add_parser("done", help="owned item -> Done (not deploy/needs-human)")
    dn.add_argument("ref")
    dn.add_argument("--summary", default="", help="optional; required for kind:bug: what was wrong, how it was fixed")
    _attach(dn)

    an = sub.add_parser("answer", help="relay the operator's answer to a needs-human item (only with their approval)")
    an.add_argument("ref")
    an.add_argument("--answer", required=True, help="the operator's answer, quoted")

    ac = sub.add_parser("accept", help="orchestrator: In Review -> Done (not deploy/needs-human)")
    ac.add_argument("ref")
    ac.add_argument("--summary", default="", help="optional: what you verified, if it adds anything")

    rj = sub.add_parser("reject", help="orchestrator: In Review -> Ready, unassigned, with the reason")
    rj.add_argument("ref")
    rj.add_argument("--reason", required=True)

    rw = sub.add_parser("rework", help="orchestrator: In Review -> In Progress for the same holder, with notes")
    rw.add_argument("ref")
    rw.add_argument("notes", help="what is left to do (markdown, or - for stdin)")

    ce = sub.add_parser("close-epic", help="orchestrator: parent item -> Done once every sub-item is Done/Cancelled")
    ce.add_argument("ref")
    ce.add_argument("--summary", default="", help="required: what the epic delivered")

    rd = sub.add_parser("ready", help="orchestrator: Backlog -> Ready, unassigned (Blocked while blockers are open); "
                        "or release a Blocked item whose blockers are closed, ending its wait")  # fmt: skip
    rd.add_argument("ref")
    rd.add_argument("--reason", default="", help="why it is ready now; without one, no comment is written")

    bk = sub.add_parser("backlog", help="orchestrator: Ready -> Backlog for an item nobody holds, with the reason")
    bk.add_argument("ref")
    bk.add_argument("--reason", required=True)

    pr = sub.add_parser("prioritize", help="set an open item's priority (orchestrator: any; worker: the one it holds)")
    pr.add_argument("ref")
    pr.add_argument("priority", choices=PRIORITIES)
    pr.add_argument("--reason", default="", help="why; without one, no comment is written")

    rc = sub.add_parser("reclaim", help="orchestrator: take one agent-held In Progress item back to Ready, with "
                        "the reason (refused while its holder was active in the last 24 h, unless --force)")  # fmt: skip
    rc.add_argument("ref")
    rc.add_argument("--reason", required=True)
    rc.add_argument("--force", action="store_true", help="take it although its holder was active recently")

    cn = sub.add_parser("cancel", help="orchestrator: abandon an item nobody holds, with the reason")
    cn.add_argument("ref")
    cn.add_argument("--reason", required=True)

    cr = sub.add_parser("create", help="create an item with exactly one kind label")
    cr.add_argument("words", nargs="+", metavar="[PROJECT] NAME", help="project key (optional) and title")
    cr.add_argument("--kind", required=True, choices=KINDS)
    cr.add_argument("--description", default="", help="markdown, or - for stdin: goal, then ### Scope / ### Done when")
    cr.add_argument("--parent", metavar="REF")
    cr.add_argument("--priority", default="none", choices=["urgent", "high", "medium", "low", "none"])
    cr.add_argument("--ready", action="store_true", help="create in Ready instead of Backlog")
    cr.add_argument("--blocked-by", nargs="+", metavar="REF")
    cr.add_argument("--start", metavar="YYYY-MM-DD", help="start date (Plane's start_date)")
    cr.add_argument("--target", metavar="YYYY-MM-DD", help="target date (Plane's target_date); orders the queue")
    cr.add_argument("--estimate", help=argparse.SUPPRESS)  # removed in 0.3.0: accepted, ignored, with a notice

    pl = sub.add_parser("plan", help="set or clear an open item's start and target dates (a held item: its holder "
                        "or the orchestrator)")  # fmt: skip
    pl.add_argument("ref")
    pl.add_argument("--target", metavar="YYYY-MM-DD", help="target date")
    pl.add_argument("--start", metavar="YYYY-MM-DD", help="start date")
    pl.add_argument("--clear", action="store_true", help="remove both dates")
    pl.add_argument("--reason", default="", help="why; without one, no comment is written")
    pl.add_argument("--estimate", help=argparse.SUPPRESS)

    st = sub.add_parser("stale", help="agent-held items with no comment signed by that agent for --hours; then "
                        "expired waits, items In Review over 72 h, and what waits on the operator")  # fmt: skip
    st.add_argument("project", nargs="?", help="project key (default: the configured project)")
    st.add_argument("--hours", type=float, default=24)
    st.add_argument("--reclaim", action="store_true", help=argparse.SUPPRESS)  # removed in 0.3.0

    se = sub.add_parser("search", help="find items whose ref, title or description has every term (reads only; no "
                        "session needed); put terms starting with - after --")  # fmt: skip
    se.add_argument("terms", nargs="+", metavar="TERM", help="each argument is one term; quote a phrase. Case and "
                    "accents are ignored")  # fmt: skip
    only = se.add_mutually_exclusive_group()
    only.add_argument("--open", action="store_true", help="only items not Done or Cancelled")
    only.add_argument("--state", action="append", default=[], metavar="NAME", help="only this state (repeatable)")
    se.add_argument("--kind", action="append", default=[], choices=KINDS, help="only this kind (repeatable)")
    se.add_argument("--label", action="append", default=[], metavar="NAME", help="must carry this label (repeatable; "
                    "all of them)")  # fmt: skip
    se.add_argument("--project", help="project key (default: the configured project)")
    se.add_argument("--limit", type=_positive, default=20, help="most results to show (default 20)")

    for verb in ("report", "log"):  # removed in 0.3.0; unlisted, so old scripts get REMOVED's message
        sub.add_parser(verb, add_help=False).add_argument("rest", nargs=argparse.REMAINDER)
    return p


def _removed(args: argparse.Namespace) -> str | None:
    if args.verb in ("report", "log"):
        return REMOVED[args.verb]
    if args.verb == "stale" and args.reclaim:
        return REMOVED["stale --reclaim"]
    return None


def _deprecated(args: argparse.Namespace) -> None:
    if getattr(args, "estimate", None) is not None:
        _warn(f"--estimate is deprecated and ignored since 0.3.0 (estimates were removed); drop it from {args.verb}")


# Orchestrator-only moves (spec: the orchestrator sweeps and reclaims; nudges come from it or the operator).
def _orchestrator_only(args: argparse.Namespace) -> str | None:
    if args.verb == "comment" and args.force:
        return "comment --force"
    if args.verb == "queue" and args.sweep:
        return "queue --sweep"
    if args.verb == "review" and args.for_slug:
        return "review --for"
    if args.verb in ("accept", "reject", "rework", "close-epic", "ready", "backlog", "cancel", "reclaim"):
        return args.verb
    return None


def _positive(value: str) -> int:
    n = int(value)
    if n < 1:
        raise argparse.ArgumentTypeError("must be 1 or more")
    return n


def _search(args: argparse.Namespace, agent: Agent) -> int:
    """Results on stdout; the count, which scripts rarely want, on stderr (as the last line with --json)."""
    hits = agent.search(args.project, args.terms, args.open, args.state, args.kind, args.label)
    for note in agent.notices:
        _warn(note)
    for h in hits[: args.limit]:
        if args.json:
            print(json.dumps(h, ensure_ascii=False))
            continue
        print("  ".join((h["ref"], h["state"], h["kind"] or "-", f"({h['match']})", h["title"], h["url"])))
        if h["snippet"]:
            print(f"    {h['snippet']}")
    if args.json:
        print(json.dumps({"total": len(hits)}))
    shown = f"; showing {args.limit}" if len(hits) > args.limit else ""
    count = f"{len(hits)} item{'s' if len(hits) != 1 else ''} match{'es' if len(hits) == 1 else ''}"
    print(f"plane-agent: {count}{shown}" if hits else "plane-agent: no items match", file=sys.stderr)
    return 0


def _warn(message: str | None) -> None:
    """A warning the command survives (exit 0), e.g. a deprecated flag."""
    if message:
        print(f"plane-agent: warning: {message}", file=sys.stderr)


def _footer(att: dict[str, list[dict]], proj) -> None:
    """One stderr line on what waits on the operator, under queue and stale."""
    bits = []
    if att["deploy_review"]:
        bits.append(f"deploy items In Review, waiting on the operator: {', '.join(proj.ref(i) for i in att['deploy_review'])}")
    if att["needs_human"]:
        bits.append(f"needs-human: {', '.join(proj.ref(i) for i in att['needs_human'])}")
    if att["overdue"]:
        bits.append(f"{len(att['overdue'])} open item(s) past their target date")
    if bits:
        print("plane-agent: " + "; ".join(bits), file=sys.stderr)


def run(args: argparse.Namespace, agent: Agent) -> int:
    out = []
    v = args.verb
    gated = _orchestrator_only(args)
    if gated and agent.slug != ORCHESTRATOR:
        print(f"plane-agent: {gated} is an orchestrator move; run it with --agent {ORCHESTRATOR}", file=sys.stderr)
        return 5
    if v == "whoami":
        me = agent.me
        out.append({"agent": agent.slug, "account": me.get("display_name"), "id": me["id"], "email": me.get("email")})
    elif v == "bootstrap-project":
        out = agent.bootstrap_project(args.project) or ["no changes"]
    elif v == "queue":
        sweep = args.sweep if args.sweep is not None else agent.slug == ORCHESTRATOR
        items, log = agent.queue(args.project, sweep=sweep)
        proj = agent.project(args.project)
        out = log + [
            {"ref": proj.ref(i), "priority": i.get("priority"), "target": i.get("target_date"), "name": i["name"]}
            for i in items
        ]
        _footer(agent.attention(args.project), proj)
    elif v == "dispatch":
        agent.dispatch(args.ref, args.to)
        out.append({"dispatched": args.ref.upper(), "to": f"agent:{args.to}"})
    elif v == "claim":
        item = agent.claim(args.ref)
        out.append({"claimed": args.ref.upper(), "name": item["name"]})
    elif v == "context":
        ctx = agent.context(args.ref, args.by)
        if args.json:
            out.append(ctx)
        else:
            out.append(f"{ctx['ref']}  {ctx['name']}  [{ctx['state']}]  labels: {', '.join(ctx['labels']) or '-'}")
            if ctx["attachments"]:
                out.append("attachments: " + ", ".join(f"{a['name']} ({a['type']}, {human_size(a['size'] or 0)})"
                                                       for a in ctx["attachments"]))  # fmt: skip
            if ctx["blocked_by"]:
                out.append(f"blocked by: {len(ctx['blocked_by'])} item(s)")
            out.append("\n" + (ctx["description"] or "(no description)"))
            out += [f"\n--- {c['at']}\n{c['text']}" for c in ctx["comments"]]
    elif v == "check":
        out.append({"ref": args.ref.upper(), "owned": True, "state": agent.check(args.ref)})
    elif v == "comment":
        agent.note(args.ref, args.text, force=args.force, attach=args.attach)
        out.append("commented")
    elif v == "block":
        if args.until and not args.wait:
            print("plane-agent: --until goes with --wait", file=sys.stderr)
            return 2
        if args.on:
            agent.block_on(args.ref, args.on, args.reason, attach=args.attach)
        elif args.wait:
            out.append({"blocked": args.ref.upper(), "waiting_until": agent.block_wait(args.ref, args.wait, args.until,
                                                                                      args.attach).isoformat()})  # fmt: skip
        else:
            agent.block_human(args.ref, args.human, attach=args.attach)
        out = out or ["blocked"]
    elif v == "review" and args.for_slug:
        if args.attach:
            print("plane-agent: review --for takes no --attach", file=sys.stderr)
            return 2
        last = agent.review_for(args.ref, args.for_slug, args.reason, args.summary)
        out.append({"in_review": args.ref.upper(), "for": f"agent:{args.for_slug}",
                    "holder_last_active": last.isoformat(timespec="minutes")})  # fmt: skip
    elif v == "review":
        if args.reason:
            print("plane-agent: --reason goes with --for; use --summary", file=sys.stderr)
            return 2
        agent.review(args.ref, args.summary, attach=args.attach)
        out.append("in review")
    elif v == "done":
        agent.done(args.ref, args.summary, attach=args.attach)
        out.append("done")
    elif v == "answer":
        out.append({"answered": args.ref.upper(), "state": agent.answer(args.ref, args.answer)})
    elif v == "accept":
        agent.accept(args.ref, args.summary)
        out.append("accepted")
    elif v == "rework":
        out.append({"rework": args.ref.upper(), "for": f"agent:{agent.rework(args.ref, args.notes)}"})
    elif v == "reject":
        agent.reject(args.ref, args.reason)
        out.append("rejected")
    elif v == "close-epic":
        out.append({"closed": args.ref.upper(), "sub_items": agent.close_epic(args.ref, args.summary)})
    elif v == "ready":
        out.append({"ref": args.ref.upper(), "state": agent.ready(args.ref, args.reason)})
    elif v == "backlog":
        agent.backlog(args.ref, args.reason)
        out.append({"ref": args.ref.upper(), "state": "Backlog"})
    elif v == "prioritize":
        agent.prioritize(args.ref, args.priority, args.reason)
        out.append({"ref": args.ref.upper(), "priority": args.priority})
    elif v == "reclaim":
        out.append({"reclaimed": args.ref.upper(), "from": f"agent:{agent.reclaim(args.ref, args.reason, args.force)}"})
    elif v == "cancel":
        agent.cancel(args.ref, args.reason)
        out.append("cancelled")
    elif v == "create":
        item = agent.create(
            args.project, args.name, args.kind, args.description, args.parent, args.priority,
            args.ready, args.blocked_by, args.start, args.target,
        )  # fmt: skip
        out.append({"created": f"{args.project.upper()}-{item['sequence_id']}", "id": item["id"]})
    elif v == "plan":
        dated = args.target or args.start or args.clear
        if args.clear and (args.target or args.start):
            print("plane-agent: plan --clear takes no --target or --start", file=sys.stderr)
            return 2
        if not dated and args.estimate is None:
            print("plane-agent: plan needs --target, --start or --clear", file=sys.stderr)
            return 2
        if not dated:
            out.append({"ref": args.ref.upper(), "unchanged": True})  # an old estimate-only revision: a no-op
        else:
            dates = agent.plan(args.ref, args.target, args.start, args.clear, args.reason)
            out.append({"ref": args.ref.upper(), **{k.removesuffix("_date"): v for k, v in dates.items()}})
    elif v == "search":
        return _search(args, agent)
    elif v == "stale":
        found = agent.stale(args.project, args.hours)
        proj = agent.project(args.project)
        out = [{"ref": proj.ref(i), "agent": f"agent:{slug}", "last_activity": t.isoformat(timespec="minutes")} if t else
               {"ref": proj.ref(i), "agent": slug, "problem": "several agent labels; operator must fix"}
               for i, slug, t in found]  # fmt: skip
        out += [{"ref": proj.ref(i), "expired_wait": str(d) if d else "no date on record", "name": i["name"]}
                for i, d in agent.expired_waits(args.project)]  # fmt: skip
        att = agent.attention(args.project)
        out += [{"ref": proj.ref(i), "in_review_since": i.get("updated_at", "")[:16], "name": i["name"]}
                for i in att["review_old"]]  # fmt: skip
        _footer(att, proj)
    for note in agent.notices:
        _warn(note)
    for line in out:
        if args.json:
            print(json.dumps(line))
        elif isinstance(line, dict):
            print("  ".join(f"{k}={val}" for k, val in line.items()))
        else:
            print(line)
    return 0


# Free-text arguments; any one of them may be `-` to read it from stdin.
TEXT_ARGS = ("text", "notes", "summary", "reason", "answer", "human", "wait", "description")


def _stdin_text(args: argparse.Namespace) -> str | None:
    """Replace a `-` text argument with stdin. Returns an error message when stdin can't supply it."""
    dashed = [a for a in TEXT_ARGS if getattr(args, a, None) == "-"]
    if not dashed:
        return None
    if len(dashed) > 1:
        return f"only one argument can read stdin, got {', '.join('--' + a for a in dashed)}"
    text = sys.stdin.read()
    if not text.strip():
        return f"{dashed[0]} was `-` but stdin was empty"
    setattr(args, dashed[0], text)
    return None


PROJECT_VERBS = ("bootstrap-project", "queue", "stale", "create", "search")


def _project_args(args: argparse.Namespace, cfg: conf.Config) -> str | None:
    """Fill in the default project (and split `[PROJECT] NAME` for create/log). Returns a usage error."""
    if args.verb == "create":
        if len(args.words) > 2:
            return f"{args.verb} takes [PROJECT] NAME; quote the title"
        args.project, args.name = args.words if len(args.words) == 2 else (None, args.words[0])
    if args.verb in PROJECT_VERBS and not args.project:
        if not cfg.project:
            return "no project: pass the project key or set `project` (PLANE_PROJECT)"
        args.project = cfg.project
    return None


def _writes(args: argparse.Namespace) -> bool:
    """True when the command may write to the tracker, so it must carry a session id."""
    if args.verb in ("whoami", "context", "check", "stale", "search"):
        return False
    if args.verb == "queue":
        return args.sweep if args.sweep is not None else args.agent == ORCHESTRATOR
    return True


def _clean_where(value: str) -> str:
    """A signature names a directory, never a path: signatures are visible to the whole workspace."""
    if "/" not in value.rstrip("/"):
        return value.rstrip("/") or "-"
    name = os.path.basename(value.rstrip("/")) or "-"
    _warn(f"--where {value!r} is a path; signing with its last part, {name!r}")
    return name


CLOUD_API = "api.plane.so"
CLOUD_APP = "app.plane.so"  # where people browse Plane Cloud; its API is on CLOUD_API


def _cloud_notice(host: str) -> None:
    if host == CLOUD_API:
        print("plane-agent: note: Plane Cloud is untested; this CLI is developed against self-hosted Plane CE. "
              "Please report what works and what doesn't (README, Compatibility)", file=sys.stderr)  # fmt: skip


def _init(args: argparse.Namespace) -> int:
    url = args.url.rstrip("/")
    if url.split("://", 1)[-1].split("/", 1)[0] == CLOUD_APP:
        args.url = url.replace(CLOUD_APP, CLOUD_API, 1)
        print(f"plane-agent: {CLOUD_APP} is Plane Cloud's web app; its API is {args.url}, so that is written",
              file=sys.stderr)  # fmt: skip
    try:
        _cloud_notice(conf.host_of(args.url))
    except conf.ConfigError as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return 2
    if args.timezone:
        from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

        try:
            ZoneInfo(args.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            print(f"plane-agent: --timezone {args.timezone!r} is not an IANA time zone", file=sys.stderr)
            return 2
    if args.rate_limit is not None and args.rate_limit < 1:
        print("plane-agent: --rate-limit must be at least 1", file=sys.stderr)
        return 2
    if args.user:
        path = conf.user_file()
    else:
        try:
            top = subprocess.run(["git", "rev-parse", "--show-toplevel"], capture_output=True, text=True, check=True)
            root = top.stdout.strip()
        except (FileNotFoundError, subprocess.CalledProcessError):
            root = os.getcwd()
        path = conf.Path(root) / conf.PROJECT_FILE
    if path.exists() and not args.force:
        print(f"plane-agent: {path} exists; pass --force to overwrite it", file=sys.stderr)
        return 5
    values = {"base_url": args.url.rstrip("/"), "workspace": args.workspace,
              "project": (args.project or "").upper(), "timezone": args.timezone, "rate_limit": args.rate_limit}  # fmt: skip
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(conf.render_settings(values))
    print(f"wrote {path}")
    try:
        cfg = conf.load()
    except conf.ConfigError as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return 2
    try:
        conf.resolve_token(cfg, None)
        print(f"token: found ({conf.token_source(cfg, None)})")
    except conf.ConfigError:
        account = cfg.host_entry.get("keychain_account") or cfg.host
        print(f"next: store the agent account's API token for {cfg.host}:\n  {conf.keyring_store_hint(account)}")
    print("check it with: plane-agent --agent orchestrator whoami")
    return 0


def _show_config(args: argparse.Namespace, cfg: conf.Config) -> int:
    rows = {k: getattr(cfg, k) for k in conf.SETTINGS}
    src = {k: cfg.sources.get(k, "default") for k in conf.SETTINGS}
    token = conf.token_source(cfg, args.token_file)
    files = {"project_file": str(cfg.project_file or "-"), "user_file": str(cfg.user_file or conf.user_file())}
    if args.json:
        print(json.dumps({"settings": rows, "sources": src, "token": token, **files}))
        return 0
    for k in conf.SETTINGS:
        print(f"{k:<11} {rows[k] or '-':<40} ({src[k]})")
    print(f"{'token':<11} {token}")
    print(f"{'files':<11} project {files['project_file']}; user {files['user_file']}")
    print(f"{'user-only':<11} require_session={str(cfg.require_session).lower()}; "
          f"notify_command={'set' if cfg.notify_command else '-'}")  # fmt: skip
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if gone := _removed(args):
        print(f"plane-agent: {gone}", file=sys.stderr)
        return 2
    _deprecated(args)
    if args.verb == "init":
        return _init(args)
    try:
        cfg = conf.load()
    except conf.ConfigError as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return 2
    if args.verb == "config":
        _cloud_notice(cfg.host)
        return _show_config(args, cfg)
    if err := _stdin_text(args) or _project_args(args, cfg):
        print(f"plane-agent: {err}", file=sys.stderr)
        return 2
    if not args.agent:
        print("plane-agent: set PLANE_AGENT or pass --agent <slug>", file=sys.stderr)
        return 2
    if not args.session and _writes(args) and cfg.require_session:
        print(f"plane-agent: {args.verb} writes to the tracker and needs a session id: pass --session ID or set "
              "PLANE_AGENT_SESSION (an orchestrator gives each subagent its own). To sign as session=unknown "
              "instead, set `require_session = false` in your user file", file=sys.stderr)  # fmt: skip
        return 2
    try:
        cfg.require("workspace")
        token = conf.resolve_token(cfg, args.token_file)
    except conf.ConfigError as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return 2
    client = PlaneClient(cfg.base_url, cfg.workspace, token, rate_limit=cfg.rate_limit)
    try:
        where = _clean_where(args.where) if args.where else _where()
        push = notifier(cfg.notify_command) if cfg.notify_command else None
        agent = Agent(client, args.agent, session=args.session or "unknown", where=where, change=args.openspec,
                      notify=push, tz=cfg.timezone or None)  # fmt: skip
        return run(args, agent)
    except ProtocolRefusal as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return e.code
    except (PlaneError, WriteFailed) as e:
        print(f"plane-agent: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
