"""Screenshots and attachments (openspec/changes/tracker-screenshots/specs/agent-coordination/spec.md)."""

from __future__ import annotations

import io
import json
import re
import struct
import zlib

import httpx
import pytest

from plane_agent import attachments, cli
from plane_agent.client import PlaneClient
from plane_agent.protocol import (
    EXIT_OWNERSHIP_LOST,
    EXIT_REFUSED,
    ProtocolRefusal,
    WriteFailed,
    latest_hold,
    signed_by,
    tail_statement,
)

from .conftest import agent
from .test_protocol import comments_on, ref, started, state_name


def png(w: int = 4, h: int = 3) -> bytes:
    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    raw = b"".join(b"\x00" + b"\x00\x00\x00" * w for _ in range(h))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))  # fmt: skip


def gif(w: int, h: int) -> bytes:
    return b"GIF89a" + struct.pack("<HH", w, h) + b"\x00\x00\x00;"


def jpeg(w: int, h: int) -> bytes:
    app0 = b"\xff\xe0" + struct.pack(">H", 16) + b"JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00"
    sof2 = b"\xff\xc2" + struct.pack(">HBHHB", 11, 8, h, w, 1) + b"\x01\x11\x00"
    return b"\xff\xd8" + app0 + b"\xff\xff" + sof2 + b"\xff\xda\x00\x02\xff\xd9"


def webp(kind: bytes, w: int, h: int) -> bytes:
    if kind == b"VP8 ":
        body = b"\x00\x00\x00" + b"\x9d\x01\x2a" + struct.pack("<HH", w, h)
    elif kind == b"VP8L":
        body = b"\x2f" + ((w - 1) | (h - 1) << 14).to_bytes(4, "little")
    else:
        body = b"\x00\x00\x00\x00" + (w - 1).to_bytes(3, "little") + (h - 1).to_bytes(3, "little")
    data = b"WEBP" + kind + struct.pack("<I", len(body)) + body + b"\x00" * 4
    return b"RIFF" + struct.pack("<I", len(data)) + data


@pytest.fixture
def files(tmp_path, monkeypatch):
    """Write files into a temp dir that is also the current directory (paths are relative to it)."""
    monkeypatch.chdir(tmp_path)

    def make(name: str, data: bytes | str = b"x") -> str:
        p = tmp_path / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data.encode() if isinstance(data, str) else data)
        return name

    return make


def attachment_posts(fake) -> list:
    return [r for r in fake.requests if r[0] == "POST" and "/attachments/" in r[1]]


# -- client --------------------------------------------------------------------


def test_client_attachment_requests_and_bare_presigned_upload():
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        if req.url.path == "/uploads":
            return httpx.Response(204)
        if req.method == "GET":
            return httpx.Response(200, json=[{"id": "a", "is_uploaded": True}, {"id": "b", "is_uploaded": False}])
        return httpx.Response(204 if req.method in ("PATCH", "DELETE") else 200, json={} if req.method == "POST" else None)

    c = PlaneClient("https://plane.test", "acme", "tok", transport=httpx.MockTransport(handle))
    base = "/api/v1/workspaces/acme/projects/p/work-items/i/attachments/"
    c.create_attachment("p", "i", "shot.png", "image/png", 12)
    c.upload_presigned({"url": "https://plane.test/uploads", "fields": {"key": "k", "policy": "x"}}, "shot.png", b"PNG", "image/png")
    c.mark_uploaded("p", "i", "a")
    assert [a["id"] for a in c.attachments("p", "i")] == ["a"]
    c.delete_attachment("p", "i", "a")
    create, upload, patch, _list, delete = seen
    assert (create.method, create.url.path, json.loads(create.content)) == ("POST", base, {"name": "shot.png", "type": "image/png", "size": 12})
    assert "X-API-Key" not in upload.headers and "X-API-Key" in create.headers
    names = re.findall(rb'; name="([^"]+)"', upload.read())
    assert names == [b"key", b"policy", b"file"]
    assert (patch.method, patch.url.path, json.loads(patch.content)) == ("PATCH", base + "a/", {"is_uploaded": True})
    assert (delete.method, delete.url.path) == ("DELETE", base + "a/")
    assert c.calls == 4  # the presigned POST isn't an API call


# -- file checks ---------------------------------------------------------------


@pytest.mark.parametrize("data,dims", [
    (png(1280, 800), (1280, 800)), (gif(30, 20), (30, 20)), (jpeg(640, 480), (640, 480)),
    (webp(b"VP8 ", 100, 50), (100, 50)), (webp(b"VP8L", 300, 200), (300, 200)), (webp(b"VP8X", 2000, 1000), (2000, 1000)),
])  # fmt: skip
def test_image_dimensions(files, data, dims):
    ext = {b"\x89P": "png", b"GI": "gif", b"\xff\xd8": "jpg", b"RI": "webp"}[data[:2]]
    (f,) = attachments.load([files(f"a.{ext}", data)])
    assert f.dims == dims and f.inline


def test_wide_image_is_capped_at_720():
    f = attachments.File("a.png", "image/png", b"", (1440, 900))
    assert 'width="720px" height="450px" aspectratio="1.6000"' in f.image_html("id")


def test_types_come_from_the_cli_table(files):
    got = attachments.load([files("run.log"), files("cfg.yml"), files("dump.tar.gz"), files("NOTES.MD")])
    assert [(f.name, f.type, f.inline) for f in got] == [
        ("run.log", "text/plain", False), ("cfg.yml", "text/plain", False),
        ("dump.tar.gz", "application/gzip", False), ("NOTES.MD", "text/markdown", False),
    ]  # fmt: skip


@pytest.mark.parametrize("case,expect", [
    ("missing", "no such file"), ("dir", "not a regular file"), ("empty", "empty file"),
    ("big", "25 MiB"), ("svg", ".svg is not accepted"), ("html", ".html is not accepted"),
    ("noext", ".(none) is not accepted"), ("fakepng", "not a valid PNG"), ("dupe", "also named"),
])  # fmt: skip
def test_refused_files_exit_5_before_any_request(fake, orch, files, tmp_path, case, expect):
    it, w = started(fake, orch, "api")
    paths = {
        "missing": ["nope.png"],
        "dir": [str(tmp_path)],
        "empty": [files("e.log", b"")],
        "svg": [files("x.svg", "<svg/>")],
        "html": [files("x.html", "<p>")],
        "noext": [files("Makefile", "all:")],
        "fakepng": [files("x.png", "not a png")],
        "dupe": [files("a/shot.png", png()), files("b/shot.png", png())],
    }.get(case)
    if case == "big":
        with open(tmp_path / "big.log", "wb") as fh:
            fh.truncate(attachments.MAX_BYTES + 1)
        paths = ["big.log"]
    before = len(fake.requests)
    with pytest.raises(ProtocolRefusal, match=re.escape(expect)) as e:
        w.done(ref(it), "s", attach=paths)
    assert e.value.code == EXIT_REFUSED and len(fake.requests) == before


def test_limit_is_inclusive(files, tmp_path):
    with open(tmp_path / "max.log", "wb") as fh:
        fh.truncate(attachments.MAX_BYTES)
    assert attachments.load(["max.log"])[0].size == 26_214_400


# -- write flow ----------------------------------------------------------------


def test_screenshot_of_a_fixed_page(fake, orch, files):
    """Scenario: Screenshot of a fixed page."""
    it, w = started(fake, orch, "api")
    w.done(ref(it), "Header fixed.", attach=[files("shots/header.png", png(1280, 800))])
    (att,) = fake.attachments
    assert att["is_uploaded"] and att["attributes"] == {"name": "header.png", "type": "image/png", "size": len(png(1280, 800))}
    assert att["data"] == png(1280, 800) and fake.uploads[0]["order"][-1] == "file"
    body = comments_on(fake, it)[-1]
    image = f'<image-component src="{att["id"]}" width="720px" height="450px" aspectratio="1.6000" alignment="left"></image-component>'
    assert body.startswith("<p>Done.</p><p>Header fixed.</p>" + image + "<hr><p>agent=api · session=s-api")
    assert signed_by(body, "api") and tail_statement(body) is None and state_name(fake, it) == "Done"


def test_log_file_is_named(fake, orch, files):
    """Scenario: Log file."""
    it, w = started(fake, orch, "api")
    w.note(ref(it), "Build output attached.", attach=[files("build.log", "x" * 3000), files("s.png", png())])
    body = comments_on(fake, it)[-1]
    assert [a["attributes"]["name"] for a in fake.attachments] == ["build.log", "s.png"]
    assert re.search(r"</image-component><p>Attached: build\.log \(3 KB\)</p><hr><p>agent=api ", body)
    tail = tail_statement(body)
    assert signed_by(body, "api") and (tail is None or latest_hold([{"comment_html": body, "created_at": "x"}]) is None)
    plain = comments_on(fake, it)[-1].replace(re.search(r"<image-component.*?</image-component>", body)[0], "")
    assert tail_statement(plain)[0].startswith("Attached:")  # the Attached paragraph is never a statement


def test_body_html_stays_escaped(fake, orch, files):
    it, w = started(fake, orch, "api")
    w.note(ref(it), '<image-component src="x"></image-component>', attach=[files("a.txt")])
    assert comments_on(fake, it)[-1].startswith("<p>&lt;image-component")


def test_review_and_block_take_files(fake, orch, files):
    it, w = started(fake, orch, "api")
    w.block_human(ref(it), "Which colour?", attach=[files("a.png", png())])
    assert "<image-component" in comments_on(fake, it)[-1] and state_name(fake, it) == "Blocked"
    it2, w2 = started(fake, orch, "ops", name="B")
    w2.review(ref(it2), "Check it.", attach=[files("b.png", png())])
    assert "<image-component" in comments_on(fake, it2)[-1] and state_name(fake, it2) == "In Review"
    it3, w3 = started(fake, orch, "web", name="C")
    other = fake.seed_item(fake.pid, "D", "Ready")
    w3.block_on(ref(it3), [ref(other)], "waits", attach=[files("c.log")])
    assert "Attached: c.log" in comments_on(fake, it3)[-1] and state_name(fake, it3) == "Blocked"


def test_refused_cycle_posts_needs_human_without_files(fake, orch, files):
    a, wa = started(fake, orch, "api")
    b, wb = started(fake, orch, "ops", name="B")
    wb.block_on(ref(b), [ref(a)], "")
    with pytest.raises(ProtocolRefusal):
        wa.block_on(ref(a), [ref(b)], "", attach=[files("x.png", png())])
    assert not fake.attachments and "<image-component" not in comments_on(fake, a)[-1]


# -- rollback ------------------------------------------------------------------


def test_not_the_holder_uploads_nothing(fake, orch, files):
    """Scenario: Not the holder."""
    it, _w = started(fake, orch, "api")
    with pytest.raises(ProtocolRefusal) as e:
        agent(fake, "ops").note(ref(it), "hi", attach=[files("a.png", png())])
    assert e.value.code == EXIT_OWNERSHIP_LOST and not attachment_posts(fake) and not fake.uploads


def test_upload_fails_partway(fake, orch, files):
    """Scenario: Upload fails partway."""
    it, w = started(fake, orch, "api")
    n = len(comments_on(fake, it))
    fake.fail_upload = 2
    with pytest.raises(WriteFailed, match="removed 2 of 2"):
        w.done(ref(it), "s", attach=[files("a.png", png()), files("b.png", png())])
    assert all(a["deleted"] for a in fake.attachments) and len(fake.attachments) == 2  # b was created, not uploaded
    assert state_name(fake, it) == "In Progress" and len(comments_on(fake, it)) == n


def test_failed_comment_removes_uploads(fake, orch, files):
    it, w = started(fake, orch, "api")
    n = len(comments_on(fake, it))
    fake.fail_once.append(("POST", r".*/work-items/[^/]+/comments/"))
    with pytest.raises(WriteFailed, match="removed 1 of 1"):
        w.note(ref(it), "x", attach=[files("a.log")])
    assert fake.attachments[0]["deleted"] and len(comments_on(fake, it)) == n


def test_orphan_is_named_with_its_id(fake, orch, files, monkeypatch, capsys):
    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    real = fake._upload

    def upload(req):
        resp = real(req)
        fake.undeletable.add(fake.uploads[-1]["id"])
        return resp

    monkeypatch.setattr(fake, "_upload", upload)
    fake.fail_once.append(("PATCH", r".*/work-items/[^/]+/"))
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "done", ref(it), "--summary", "s", "--attach", files("a.log")]) == 1
    aid = fake.attachments[0]["id"]
    assert f"could not remove: a.log (id {aid})" in capsys.readouterr().err and state_name(fake, it) == "In Progress"


# -- cli & readback ------------------------------------------------------------


def test_cli_done_with_stdin_summary_and_attachment(fake, orch, files, monkeypatch):
    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    monkeypatch.setattr("sys.stdin", io.StringIO("Fixed:\n\n- header\n"))
    shot = files("shot.png", png())
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "done", ref(it), "--summary", "-", "--attach", shot]) == 0
    body = comments_on(fake, it)[-1]
    assert "<li>header</li>" in body and "<image-component" in body and state_name(fake, it) == "Done"


def test_cli_refused_file_exits_5(fake, orch, files, monkeypatch, capsys):
    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    it, _w = started(fake, orch, "api")
    argv = ["--agent", "api", "--session", "t", "--where", "x", "comment", ref(it), "x", "--attach", files("a.svg", "<svg/>")]
    assert cli.main(argv) == 5 and "not accepted" in capsys.readouterr().err


def test_context_reads_earlier_screenshots(fake, orch, files, capsys, monkeypatch):
    """Scenario: Earlier screenshots, plus a removed one; --by filters comments, not attachments."""
    it, w = started(fake, orch, "api")
    w.note(ref(it), "Before.", attach=[files("before.png", png())])
    w.note(ref(it), "Gone.", attach=[files("gone.png", png())])
    fake.attachments[1]["deleted"] = True
    ctx = orch.context(ref(it), agent="orchestrator")
    assert ctx["attachments"] == [{"name": "before.png", "type": "image/png", "size": len(png())}]
    assert not any("Before." in c["text"] for c in ctx["comments"])
    texts = [c["text"] for c in w.context(ref(it))["comments"]]
    assert any(t.startswith("Before.\n\n[image: before.png]\n\nagent=api") for t in texts)
    assert any(t.startswith("Gone.\n\n[image: removed]") for t in texts)
    monkeypatch.setattr(cli.conf, "resolve_token", lambda _c, _f: "tok-claude")
    monkeypatch.setattr(cli, "PlaneClient", lambda *a, **k: PlaneClient(*a, **k, transport=fake.transport()))
    assert cli.main(["--agent", "api", "--session", "t", "--where", "x", "context", ref(it)]) == 0
    assert "attachments: before.png (image/png, " in capsys.readouterr().out
