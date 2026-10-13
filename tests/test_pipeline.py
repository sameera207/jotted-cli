import json
import os
import random
import re
import stat
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))
import synth  # noqa: E402

from jotted import config  # noqa: E402
from jotted.ink.lines import Line, cluster  # noqa: E402
from jotted.ink.strokes import make_stroke  # noqa: E402
from jotted.plugins.remarkable import cloud, notebook, rmfile  # noqa: E402

ROOT = Path(__file__).parent.parent


@pytest.fixture
def cfg(tmp_path, monkeypatch):
    # Tests never call external APIs (they are stubbed where exercised), and no key from the
    # shell turns the Jev plugin on behind their back.
    path = tmp_path / "config.toml"
    path.write_text(config.EXAMPLE.read_text())
    monkeypatch.setenv(config.ENV_VAR, str(path))
    for name in ("ANTHROPIC_API_KEY", "TYPESAFE_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    return config.load()


def to_strokes(paths, first_id=100):
    return [make_stroke(f"1:{first_id + i}", "fineliner", p) for i, p in enumerate(paths)]


# ---------------------------------------------------------------- config


def test_example_config_loads_and_resolves_paths(cfg, tmp_path):
    assert cfg.paths.cache_dir == (tmp_path / "cache").resolve()
    assert cfg.rmapi.token_file == (tmp_path / ".secrets/rmapi.conf").resolve()
    assert cfg.template.scale == 1.0525


def test_old_config_files_still_load(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[paths]\noutput_dir = "./out"\n[notebook]\nname = "Tasks"\n[checkbox]\nlead_zone = 3.0\n'
                 '[output]\nkeep_runs = 50\n[classification]\ntodo_threshold = 0.6\n'
                 '[template]\npages = 20\nscale = 1.05\n')
    assert config.load(p).template.scale == 1.05


def test_old_ai_sections_move_to_llm_judging_and_jev(tmp_path):
    p = tmp_path / "c.toml"
    p.write_text('[recognition]\nenabled = true\nmodel = "claude-x"\neffort = "high"\n'
                 '[classification]\nenabled = true\nprovider = "typesafe"\nmodel = "jev-9"\n'
                 'api_key_env = "MY_JEV"\ncontinuation_threshold = 0.4\ncontext_lines = 3\n'
                 '[llm]\neffort = "medium"\n')
    cfg = config.load(p)
    assert (cfg.llm.model, cfg.llm.effort) == ("claude-x", "medium")  # the new section wins
    assert (cfg.jev.model, cfg.jev.api_key_env) == ("jev-9", "MY_JEV")
    assert (cfg.judging.continuation_threshold, cfg.judging.context_lines) == (0.4, 3)


@pytest.mark.parametrize(
    "extra, message",
    [
        ("[nope]\n", "unknown section"),
        ("[lines]\nband_tolrance = 1\n", "unknown key"),
        ("[template]\nscale = 3\n", "template.scale"),
        ('[llm]\nprovider = "nope"\n', "llm.provider"),
        ('[recognition]\nprovider = "nope"\n', "llm.provider"),  # the old name for [llm]
        ("[classification]\nbogus = 1\n", "unknown key"),
    ],
)
def test_config_rejects_bad_values(tmp_path, extra, message):
    p = tmp_path / "c.toml"
    p.write_text(extra)
    with pytest.raises(config.ConfigError, match=message):
        config.load(p)


# ---------------------------------------------------------------- strokes


def test_load_strokes_drops_deleted_ignored_and_dots(tmp_path, cfg):
    paths = [[(0, 0), (10, 10)], [(5, 5)]]  # the second is a single-point dot
    data = synth.rm_bytes(paths, deleted=[500])
    hl = synth.rm_bytes([[(0, 0), (50, 0)]], tool=synth.si.Pen.HIGHLIGHTER_2)
    (tmp_path / "a.rm").write_bytes(data)
    (tmp_path / "b.rm").write_bytes(hl)

    got = rmfile.load_strokes(tmp_path / "a.rm", cfg.strokes)
    assert [s.id for s in got] == ["1:100"]
    assert got[0].tool == "fineliner"
    assert got[0].bbox == (0, 0, 10, 10)
    assert rmfile.load_strokes(tmp_path / "b.rm", cfg.strokes) == []


# ---------------------------------------------------------------- lines


def test_cluster_separates_lines_and_places_tall_strokes(cfg):
    rng = random.Random(3)
    paths = synth.text(0, 100, 3, rng) + synth.text(0, 190, 3, rng)
    tall = [(-40, 80), (-40, 125)]  # margin mark overlapping line 1
    far = [(900, 1000), (900, 1400)]  # overlaps nothing
    lines, unassigned = cluster(to_strokes(paths + [tall, far]), cfg.lines)
    assert len(lines) == 2
    assert lines[0].strokes[0].points[0] == tall[0]
    assert [s.points[0] for s in unassigned] == [far[0]]


# ---------------------------------------------------------------- documents


def test_page_order_follows_the_tablet_and_skips_deleted_pages():
    content = {"cPages": {"pages": [{"id": "b", "idx": {"value": "bb"}}, {"id": "a", "idx": {"value": "ba"}},
                                    {"id": "x", "idx": {"value": "aa"}, "deleted": {"value": 1}}]}}
    assert notebook.page_order(content) == ["a", "b"]
    assert notebook.page_order({"pages": ["p1", "p2"]}) == ["p1", "p2"]


# ---------------------------------------------------------------- cloud wrapper, with a fake rmapi


FAKE_RMAPI = """#!/bin/sh
echo "$RMAPI_CONFIG|$RMAPI_TRACE|$*" >> "{log}"
case "$*" in
  "ls /") read code; echo "token-for-$code" > "$RMAPI_CONFIG" ;;
  "-ni -json find /") echo 'Refreshing tree...'; echo '[{{"id":"abc","name":"Tasks","type":"DocumentType","version":42,"parent":""}},{{"id":"f1","name":"Tasks","type":"CollectionType","version":0,"parent":""}}]' ;;
  "-ni get --id abc") cp "{doc}" "Tasks.rmdoc" ;;
  *) echo "unexpected: $*" >&2; exit 1 ;;
esac
"""


def test_cloud_flow_with_fake_rmapi(tmp_path, cfg, monkeypatch):
    doc = synth.rmdoc(tmp_path / "src.rmdoc", [synth.rm_bytes([[(0, 0), (5, 5)]])])
    calls = tmp_path / "calls.log"
    fake = tmp_path / "rmapi"
    fake.write_text(FAKE_RMAPI.format(log=calls, doc=doc))
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("RMAPI_TRACE", "1")  # must not leak through when trace = false

    cloud.register(cfg, "abcd1234")
    token = cfg.rmapi.token_file
    assert token.read_text().strip() == "token-for-abcd1234"
    assert stat.S_IMODE(token.parent.stat().st_mode) == 0o700
    assert stat.S_IMODE(token.stat().st_mode) == 0o600

    ref = cloud.find_document(cfg, "Tasks")  # the document, not the folder of the same name
    assert (ref.id, ref.version) == ("abc", 42)
    path = cloud.download(cfg, ref)
    assert path == cfg.paths.cache_dir / "abc.rmdoc"
    assert json.loads(path.with_suffix(".json").read_text())["version"] == 42

    for line in calls.read_text().splitlines():
        conf, trace, _ = line.split("|")
        assert conf == str(token) and trace == ""


def test_download_copes_with_a_slash_in_the_name(tmp_path, cfg, monkeypatch):
    """rmapi saves "<name>.rmdoc" in its working folder, so "Ana / Sameera" needs an "Ana " folder."""
    doc = synth.rmdoc(tmp_path / "src.rmdoc", [synth.rm_bytes([[(0, 0), (5, 5)]])])
    fake = tmp_path / "rmapi"
    fake.write_text(f"""#!/bin/sh
cp "{doc}" "$(printf '%s' "$NAME").rmdoc"  # fails like rmapi when the folder is missing
""")
    fake.chmod(fake.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{tmp_path}{os.pathsep}{os.environ['PATH']}")
    cfg.rmapi.token_file.parent.mkdir(parents=True, exist_ok=True)
    for name in ("1-1 Dmytro / Sameera", "Final Team Fit/Technical Chat - Jo", "Plain name"):
        monkeypatch.setenv("NAME", name)
        ref = cloud.DocRef(id=f"id-{len(name)}", name=name, version=1, modified="m", parent="")
        path = cloud.download(cfg, ref)
        assert path == cfg.paths.cache_dir / f"{ref.id}.rmdoc" and path.read_bytes() == doc.read_bytes()
    assert sorted(p.name for p in cfg.paths.cache_dir.iterdir() if p.name.startswith(".dl-")) == []  # cleaned up

    monkeypatch.setenv("NAME", "../escape")  # a name pointing outside: no folder is made out there
    with pytest.raises(cloud.CloudError):
        cloud.download(cfg, cloud.DocRef(id="x", name="../../escape", version=1, modified="m", parent=""))
    assert not (cfg.paths.cache_dir.parent / "escape").exists()


RESET = ("ERROR: 2026/10/11 11:09:04 transport.go:258: http request failed with Get "
         "\"https://internal.cloud.remarkable.com/sync/v3/files/0c37\": read tcp 192.168.86.215:55453->35.201.126.131:443: "
         "read: connection reset by peer\nERROR: 2026/10/11 11:09:04 main.go:86: Error: Failed to download file "
         "Quick sheets with Get \"https://internal.cloud.remarkable.com/x\": read: connection reset by peer")


@pytest.fixture
def rmapi_replies(cfg, monkeypatch):
    """rmapi stubbed out: each call takes the next reply (exit status and stderr, or "timeout")."""
    replies, calls = [], []

    def run(cmd, **kw):
        calls.append(cmd[1:])
        reply = replies.pop(0)
        if reply == "timeout":
            raise subprocess.TimeoutExpired(cmd, kw["timeout"])
        status, stderr = reply
        return subprocess.CompletedProcess(cmd, status, "[]" if status == 0 else "", stderr)

    monkeypatch.setattr(cloud, "_binary", lambda cfg: "rmapi")
    monkeypatch.setattr(cloud.subprocess, "run", run)
    monkeypatch.setattr(cloud, "RETRY_DELAYS_S", (0, 0))
    cfg.rmapi.token_file.parent.mkdir(parents=True, exist_ok=True)
    cfg.rmapi.token_file.write_text("token")
    return replies, calls


def test_reads_are_retried_after_a_network_failure(cfg, rmapi_replies):
    replies, calls = rmapi_replies
    replies[:] = [(1, RESET), "timeout", (0, "")]
    assert cloud.library(cfg) == ([], [])
    assert len(calls) == 3


def test_a_failure_that_lasts_ends_in_a_short_message(cfg, rmapi_replies):
    replies, calls = rmapi_replies
    replies[:] = [(1, RESET)] * 3
    doc = cloud.DocRef(id="0f1f", name="Quick sheets", version=1, modified="m", parent="")
    with pytest.raises(cloud.CloudError) as e:
        cloud.download(cfg, doc)
    assert str(e.value) == "Couldn't download Quick sheets: the connection to reMarkable cloud dropped."
    assert len(calls) == 3


def test_two_timeouts_end_the_retries(cfg, rmapi_replies):
    replies, calls = rmapi_replies
    replies[:] = ["timeout", "timeout", (0, "")]
    with pytest.raises(cloud.CloudError, match=r"^Couldn't list your documents: reMarkable cloud didn't answer in time\.$"):
        cloud.library(cfg)
    assert len(calls) == 2


@pytest.mark.parametrize("stderr, said", [
    ("Get \"https://x\": dial tcp: lookup internal.cloud.remarkable.com: no such host", "couldn't reach reMarkable cloud"),
    ("Get \"https://x\": net/http: TLS handshake timeout", "the connection to reMarkable cloud dropped"),
    ("Get \"https://x\": unexpected EOF", "the connection to reMarkable cloud dropped"),
    ("request failed with status 503", "reMarkable cloud had a problem on its side"),
])
def test_network_failures_are_told_plainly(cfg, rmapi_replies, stderr, said):
    replies, calls = rmapi_replies
    replies[:] = [(1, stderr), (0, "")]
    assert cloud.library(cfg) == ([], [])  # and retried
    replies[:] = [(1, stderr)] * 3
    with pytest.raises(cloud.CloudError, match=f"^Couldn't list your documents: {re.escape(said)}"):
        cloud.library(cfg)


def test_other_failures_are_not_retried(cfg, rmapi_replies):
    replies, calls = rmapi_replies
    replies[:] = [(1, "ERROR: 2026/10/11 11:09:04 main.go:86: Error: failed to get user token: 401 Unauthorized")]
    with pytest.raises(cloud.CloudError) as e:
        cloud.library(cfg)
    assert str(e.value) == "Couldn't list your documents: failed to get user token: 401 Unauthorized."
    assert len(calls) == 1


def test_writes_are_not_retried(cfg, rmapi_replies, tmp_path):
    replies, calls = rmapi_replies
    replies[:] = [(1, RESET)]
    with pytest.raises(cloud.CloudError, match="^Couldn't upload To-do: the connection to reMarkable cloud dropped"):
        cloud.upload_pdf(cfg, tmp_path / "To-do.pdf", content_only=True)
    assert len(calls) == 1


# ---------------------------------------------------------------- recognition and classification (stubbed)

from jotted import classify as classify_mod, llm, recognise  # noqa: E402
from jotted.aicache import AICache  # noqa: E402
from jotted.llm import Document, Transcript  # noqa: E402


def test_render_line_is_png(cfg):
    line = Line(to_strokes(synth.bracket_pair(0, 20) + synth.text(60, 20, 2)))
    png = recognise.render_line(line)
    assert png[:8] == b"\x89PNG\r\n\x1a\n"


def test_anchor_is_first_written_stroke():
    line = Line([make_stroke(i, "fineliner", [(0, 0), (1, 1)]) for i in ("2:5", "1:9", "1:3")])
    assert line.anchor_id == "1:3"


class _FakeAnthropic:
    calls = 0

    def __init__(self, **kw):
        self.beta = self
        self.messages = self

    def create(self, **kw):
        _FakeAnthropic.calls += 1
        _FakeAnthropic.last = kw
        content = kw["messages"][0]["content"]
        if "judge" in kw["system"].split("\n")[0]:  # judging: lines containing "TODO" are actions
            q = json.loads(content[0]["text"])
            if "pairs" in q:
                body = json.dumps({"pairs": [dict(above=p["above"], below=p["below"], continues=True) for p in q["pairs"]]})
            else:
                body = json.dumps({"lines": [{"i": i, "action": "TODO" in q["lines"][i]["text"], "owner": "me"}
                                             for i in q["judge"]]})
        else:
            ns = [int(b["text"].split()[1].rstrip(":")) for b in content
                  if b["type"] == "text" and b["text"].startswith("Line ")]
            body = json.dumps({"lines": [{"n": n, "drawing": False, "checkbox": "empty", "text": f"line {n}"}
                                         for n in ns]})
        from types import SimpleNamespace as NS
        return NS(stop_reason="end_turn", content=[NS(type="text", text=body)],
                  usage=NS(input_tokens=1, output_tokens=1), _request_id="req_test")


def test_transcribe_sends_only_uncached_lines(tmp_path, cfg, monkeypatch):
    from jotted.adapters import anthropic_llm

    monkeypatch.setattr(anthropic_llm.anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv(cfg.llm.api_key_env, "test-key")
    _FakeAnthropic.calls = 0
    lines_, _ = cluster(to_strokes(synth.demo_page()[0]), cfg.lines)
    cache = AICache(tmp_path / "ai")
    got = recognise.transcribe(lines_, cfg.llm, cache)
    assert _FakeAnthropic.calls == 1 and len(got) == len(lines_)
    assert got[lines_[0].n].text == f"line {lines_[0].n}" and got[lines_[0].n].checkbox == "empty"
    again = recognise.transcribe(lines_, cfg.llm, cache)
    assert _FakeAnthropic.calls == 1 and all(t.cached for t in again.values())


def test_claude_judges_actions_and_wrapped_lines_when_jev_is_off(tmp_path, cfg, monkeypatch):
    from jotted.adapters import anthropic_llm
    from jotted.adapters.action_judge import ModelActionJudge
    from jotted.core.model import DocInfo, PageInfo, SourceLine

    monkeypatch.setattr(anthropic_llm.anthropic, "Anthropic", _FakeAnthropic)
    monkeypatch.setenv(cfg.llm.api_key_env, "test-key")
    assert isinstance(classify_mod.judge_for(cfg), anthropic_llm.AnthropicLLM)
    assert classify_mod.judge_model(cfg) == cfg.llm.model

    doc, page = DocInfo("remarkable", "d", "Weekly", "/Meetings", "m"), PageInfo("d", "p", 1, "h")
    lines_ = [SourceLine(anchor=f"1:{i}", key=f"k{i}", text=t, bbox=(0, 0, 1, 1))
              for i, t in enumerate(["Agenda", "TODO book the room"])]
    _FakeAnthropic.calls = 0
    got = ModelActionJudge(cfg, AICache(tmp_path / "ai")).judge(doc, page, lines_, lines_)
    assert (got["1:0"].p_action, got["1:1"].p_action, got["1:1"].owner) == (0.0, 1.0, "me")
    assert _FakeAnthropic.calls == 1 and _FakeAnthropic.last["model"] == cfg.llm.model
    assert json.loads(_FakeAnthropic.last["messages"][0]["content"][0]["text"])["document"]["name"] == "Weekly"

    page_ = [(0, "+ email the landlord about"), (40, "the broken heater"), (140, "buy milk")]
    lines2 = [Line(to_strokes([[(20, y), (300, y + 20)]]), n=i + 1) for i, (y, _) in enumerate(page_)]
    ts = {i + 1: Transcript(i + 1, "none", text) for i, (_, text) in enumerate(page_)}
    assert classify_mod.continuations(lines2, ts, cfg, AICache(tmp_path / "ai")) == [(1, 2)]


class _FakeReader:
    """Any provider: reads each line as its number, and skips line 2."""
    model = "fake-model"

    def __init__(self):
        self.seen: list[list[int]] = []

    def read(self, images):
        self.seen.append([img.n for img in images])
        assert all(img.png[:8] == b"\x89PNG\r\n\x1a\n" for img in images)
        return {img.n: Transcript(img.n, "none", f" text {img.n} ") for img in images if img.n != 2}


def test_any_reader_plugs_in_and_its_answers_are_cached(tmp_path, cfg):
    lines_, _ = cluster(to_strokes(synth.demo_page()[0]), cfg.lines)
    reader, cache = _FakeReader(), AICache(tmp_path / "ai")
    got = recognise.transcribe(lines_, cfg.llm, cache, reader=reader)
    assert reader.seen == [[ln.n for ln in lines_]]
    assert 2 not in got and got[1].text == "text 1"  # a skipped line is left out, text is trimmed
    recognise.transcribe(lines_, cfg.llm, cache, reader=reader)
    assert reader.seen[1] == [2]  # only the line without an answer is asked again


def test_a_cached_page_needs_no_reader_or_key(tmp_path, cfg, monkeypatch):
    lines_, _ = cluster(to_strokes(synth.demo_page()[0]), cfg.lines)
    cache = AICache(tmp_path / "ai")
    recognise.transcribe(lines_, cfg.llm, cache, reader=_FakeReader())
    lines_ = [ln for ln in lines_ if ln.n != 2]
    monkeypatch.setattr(recognise, "llm_for", lambda c: pytest.fail("no reader should be made"))
    assert all(t.cached for t in recognise.transcribe(lines_, cfg.llm, cache).values())


def test_providers_are_registered_and_checked(cfg, monkeypatch):
    import dataclasses

    from jotted.adapters.anthropic_llm import AnthropicLLM

    assert set(llm.PROVIDERS) == config.LLM_PROVIDERS
    monkeypatch.setenv(cfg.llm.api_key_env, "test-key")
    assert isinstance(llm.llm_for(cfg.llm), AnthropicLLM)
    monkeypatch.delenv(cfg.llm.api_key_env)
    with pytest.raises(llm.ModelError, match="is not set"):
        llm.llm_for(cfg.llm)
    with pytest.raises(llm.ModelError, match="unknown LLM provider"):
        llm.llm_for(dataclasses.replace(cfg.llm, provider="nope"))


class _FakeJudge:
    """Any provider: every pair continues (P 0.9), every line is my action."""
    model = "fake-judge"

    def __init__(self):
        self.calls: list[tuple] = []

    def continues(self, lines, pairs):
        self.calls.append(("continues", [(lines[q.above].n, lines[q.below].n) for q in pairs]))
        return {(q.above, q.below): 0.9 for q in pairs}

    def actions(self, document, lines, targets, context):
        from jotted.core.model import Judgment as ActionJudgment
        self.calls.append(("actions", document, [lines[i].text for i in targets]))
        return {i: ActionJudgment(p_action=0.95, owner="me") for i in targets}


def test_any_judge_plugs_in_for_continuations(tmp_path, cfg):
    page = [(0, "+ email the landlord about"), (40, "the broken heater"), (140, "buy milk")]
    lines_ = [Line(to_strokes([[(20, y), (300, y + 20)]]), n=i + 1) for i, (y, _) in enumerate(page)]
    ts = {i + 1: Transcript(i + 1, "none", text) for i, (_, text) in enumerate(page)}
    judge, cache = _FakeJudge(), AICache(tmp_path / "ai")
    assert classify_mod.continuations(lines_, ts, cfg, cache, judge=judge) == [(1, 2)]
    assert judge.calls == [("continues", [(1, 2)])]
    assert classify_mod.continuations(lines_, ts, cfg, cache, judge=judge) == [(1, 2)]
    assert len(judge.calls) == 1  # cached


def test_action_judge_asks_the_provider_once_per_line(tmp_path, cfg):
    from jotted.adapters.action_judge import ModelActionJudge
    from jotted.core.model import DocInfo, PageInfo, SourceLine

    doc, page = DocInfo("remarkable", "d", "Weekly", "/Meetings", "m"), PageInfo("d", "p", 3, "h")
    lines_ = [SourceLine(anchor=f"1:{i}", key=f"k{i}", text=t, bbox=(0, 0, 1, 1))
              for i, t in enumerate(["Agenda", "book the room", ""])]
    fake = _FakeJudge()
    judge = ModelActionJudge(cfg, AICache(tmp_path / "ai"), judge=fake)
    got = judge.judge(doc, page, lines_, [lines_[1], lines_[2]])
    assert set(got) == {"1:1"} and got["1:1"].p_action == 0.95  # the empty line is not judged
    assert fake.calls == [("actions", Document("Weekly", "/Meetings", 3), ["book the room"])]
    assert judge.judge(doc, page, lines_, [lines_[1]])["1:1"].owner == "me" and len(fake.calls) == 1


def test_jev_judges_while_its_key_is_set(cfg, monkeypatch):
    from jotted.adapters.typesafe_judge import TypeSafeJudge

    assert not classify_mod.jev_enabled(cfg)
    monkeypatch.setenv(cfg.jev.api_key_env, "test-key")
    assert classify_mod.jev_enabled(cfg) and classify_mod.judge_model(cfg) == cfg.jev.model
    assert isinstance(classify_mod.judge_for(cfg), TypeSafeJudge)


# ---------------------------------------------------------------- drawings and wrapped lines


def _line(paths, n, first_id):
    ln = Line(to_strokes(paths, first_id))
    ln.n = n
    return ln


def test_drawing_fragments_merge_into_one_line(cfg):
    # A tall box with a label and an arrow inside: the band sweep cuts it into pieces.
    box = [[(0, 0), (200, 0), (200, 150), (0, 150), (0, 0)]]
    label = synth.text(20, 40, 1)
    arrow = [[(20, 110), (180, 110)]]
    text_line = synth.text(0, 300, 2)
    lines_, _ = cluster(to_strokes(box + label + arrow + text_line), cfg.lines)
    assert len(lines_) == 2
    assert lines_[0].bbox[3] <= 160 and lines_[1].bbox[1] > 250


def test_continuation_candidates_use_page_spacing(cfg):
    ts = {n: Transcript(n, "none", t) for n, t in [(1, "buy milk"), (2, "call mum"), (3, "email the landlord about"),
                                                   (4, "the broken heater"), (5, "- pay rent"), (6, "tidy desk")]}
    ys = {1: 100, 2: 200, 3: 300, 4: 345, 5: 390, 6: 500}  # 4 sits tight under 3; 5 is tight but bulleted
    lines_ = [_line(synth.text(0, ys[n], 2), n, 100 * n) for n in ts]
    got = classify_mod.continuation_candidates(lines_, ts, cfg.judging)
    assert [(a, b) for a, b, _, _ in got] == [(3, 4)]
    assert got[0][2] < 0.75


def test_indented_line_under_a_bullet_is_a_candidate_at_normal_spacing(cfg):
    ts = {1: Transcript(1, "none", "- get the new items to prod"), 2: Transcript(2, "none", "- get connectors involved in"),
          3: Transcript(3, "none", "testing"), 4: Transcript(4, "none", "- pay rent")}
    lines_ = [_line(synth.text(0, 100, 3), 1, 100), _line(synth.text(0, 200, 3), 2, 200),
              _line(synth.text(250, 300, 1), 3, 300), _line(synth.text(0, 400, 2), 4, 400)]
    got = classify_mod.continuation_candidates(lines_, ts, cfg.judging)
    assert [(a, b, ind) for a, b, _, ind in got] == [(2, 3, True)]


def test_loose_descender_joins_its_row(cfg):
    word = synth.text(0, 100, 3)
    descender = [[(40, 92), (40, 145)]]  # the p of "prod": overlaps the row by ~45%, hangs below it
    lines_, _ = cluster(to_strokes(word + descender + synth.text(0, 260, 2)), cfg.lines)
    assert len(lines_) == 2


def test_adjacent_drawings_pair_up(cfg):
    ts = {1: Transcript(1, "none", "", drawing=True), 2: Transcript(2, "none", "", drawing=True),
          3: Transcript(3, "none", "later", drawing=False)}
    lines_ = [_line([[(0, 0), (100, 0), (100, 90), (0, 90)]], 1, 100),
              _line([[(10, 95), (90, 96)]], 2, 200),
              _line(synth.text(0, 200, 1), 3, 300)]
    assert classify_mod.adjacent_drawings(lines_, ts, cfg.judging) == [(1, 2)]


def test_merge_continuations_keeps_first_line_and_anchor():
    a, b, c = _line(synth.text(0, 100, 1), 1, 100), _line(synth.text(0, 140, 1), 2, 200), _line(synth.text(0, 180, 1), 3, 300)
    ts = {1: Transcript(1, "empty", "email the landlord"), 2: Transcript(2, "none", "about the"),
          3: Transcript(3, "none", "broken heater")}
    merged_lines, merged_ts, parts = classify_mod.merge_continuations([a, b, c], ts, [(1, 2), (2, 3)])
    assert [ln.n for ln in merged_lines] == [1] and parts == {1: [1, 2, 3]}
    assert merged_ts[1].text == "email the landlord about the broken heater"
    assert merged_ts[1].checkbox == "empty"
    assert merged_lines[0].anchor_id == a.anchor_id


# ---------------------------------------------------------------- web edits reach the tablet once


def test_scheduler_coalesces_bursts_of_edits():
    import threading
    import time as _time

    from jotted.app import Scheduler

    class FakeApp:
        pushes = 0
        lock = threading.Lock()

        def sync_todo(self):
            FakeApp.pushes += 1
            return {}

    sched = Scheduler(FakeApp(), push_delay_s=0.2)
    threading.Thread(target=sched._push_loop, daemon=True).start()
    for _ in range(5):
        sched.push_soon()
        _time.sleep(0.05)
    assert sched.describe()["push"]["scheduled"]
    for _ in range(40):
        if FakeApp.pushes:
            break
        _time.sleep(0.05)
    _time.sleep(0.3)
    assert FakeApp.pushes == 1 and not sched.describe()["push"]["scheduled"]


def test_bullets_are_stripped_from_item_text():
    from jotted.adapters.sqlite_repo import clean_text

    assert clean_text("- test prod") == "test prod"
    assert clean_text("• call Bob") == "call Bob"
    assert clean_text("buy milk") == "buy milk"
