import json
import subprocess
import sys
from pathlib import Path

import pytest

from agge import graph, tools
from agge.project import Project
from agge.runtime import Runtime


class FakeLLM:
    """Scripted model: picks a reply by matching the last message."""

    def __init__(self, rules):
        self.rules, self.label, self.calls = rules, "fake:test", []

    def chat(self, system, messages, on_token=None, on_think=None):
        last = messages[-1]["content"]
        self.calls.append(last)
        for key, reply in self.rules:
            if key in last:
                return reply(last) if callable(reply) else reply
        return "done"


@pytest.fixture
def prj(tmp_path, monkeypatch):
    monkeypatch.setenv("AGGE_HOME", str(tmp_path / "home"))
    monkeypatch.chdir(tmp_path)
    return Project.init(tmp_path)


def runtime(prj, llm):
    rt = Runtime(prj, approver=tools.Approver("auto"))
    rt.llm = lambda name: llm
    return rt


def test_parse_calls():
    text = 'hi <read_file path="a.py"/> and <write_file path="b.md">\n# T\n</write_file><shell>ls</shell>'
    calls = tools.parse_calls(text)
    assert [c.name for c in calls] == ["read_file", "write_file", "shell"]
    assert calls[1].body == "\n# T\n" and calls[2].body == "ls"
    assert not tools.parse_calls('<write_file path="x">no end')[0].closed


def test_repair_damaged_tags():
    # seen from MiniMax-M3.1 in long contexts
    c = tools.parse_calls('<shell timeout>120">pwd && ls</shell>')
    assert [(x.name, x.attrs, x.body) for x in c] == [("shell", {"timeout": "120"}, "pwd && ls")]
    c = tools.parse_calls('<search pattern>x_close|y_close|chan<|stream]<]minimax[>[</pattern> path=".." glob="*.veryl"/>')
    assert [(x.name, x.attrs) for x in c] == [("search", {"pattern": "x_close|y_close|chan<|stream", "path": "..", "glob": "*.veryl"})]
    c = tools.parse_calls('<shell timeout="120">reda --version || true]<]minimax[</shell>')
    assert c[0].body == "reda --version || true"
    c = tools.parse_calls('shell timeout="120">reda sim crc32.veryl</shell>')
    assert [(x.name, x.body) for x in c] == [("shell", "reda sim crc32.veryl")]
    c = tools.parse_calls('list_files path="." depth="3"/>')
    assert [(x.name, x.attrs) for x in c] == [("list_files", {"path": ".", "depth": "3"})]
    assert tools.parse_calls("the list_files tool lists files") == []
    # well-formed tags and file contents are left alone
    c = tools.parse_calls('<write_file path="a.html"><p class="x">say "hi"</p></write_file><shell timeout="60" >echo "a>b"</shell>')
    assert [x.body for x in c] == ['<p class="x">say "hi"</p>', 'echo "a>b"']


def test_malformed_tag_is_sent_back(prj):
    # a tag that cannot be repaired: the agent is asked to write it again, not finished
    llm = FakeLLM([("tool_result", "done!"), ("could not be parsed", '<list_files path="."/>'),
                   ("go", '<shell timeout=120 cmd>ls</shell>')])
    out = runtime(prj, llm).agent("coder").run([{"role": "user", "content": "go"}])
    assert out == "done!" and any("could not be parsed" in c for c in llm.calls)
    # the repair limit counts consecutive bad replies: a long run with an
    # occasional broken tag keeps going
    state = {"n": 0}

    def step(_):
        state["n"] += 1
        return '<search pattern="x' if state["n"] % 2 else '<list_files path="."/>'

    llm = FakeLLM([("tool_result", lambda m: "finished" if state["n"] >= 12 else step(m)),
                   ("could not be parsed", step), ("go", step)])
    out = runtime(prj, llm).agent("coder").run([{"role": "user", "content": "go"}])
    assert out == "finished", out
    # a tag inside a code fence in a final answer is not a call
    assert tools.malformed("Use it like this:\n```\n<shell>ls</shell>\n```") is None


def test_tools_and_workspace(tmp_path):
    ws = tools.Workspace(tmp_path)
    ctx = tools.Context(ws, tools.Approver("auto"))
    out = tools.run_calls(tools.parse_calls(
        '<write_file path="d/a.txt">\nhello world\n</write_file>'
        '<edit_file path="d/a.txt"><old>world</old><new>agge</new></edit_file>'
        '<read_file path="d/a.txt"/><search pattern="agge"/><list_files/>'
        '<read_file path="/etc/passwd"/><shell>echo hi</shell>'), ctx)
    assert (tmp_path / "d/a.txt").read_text() == "hello agge\n"
    assert out.count('status="ok"') == 6 and "outside the allowed" in out and "exit code 0\nhi" in out
    denied = tools.run_calls(tools.parse_calls("<shell>echo x</shell>"),
                             tools.Context(ws, tools.Approver("deny")))
    assert "denied" in denied
    attr_form = tools.run_calls(tools.parse_calls('<shell command="echo attr"/>'), ctx)
    assert "exit code 0\nattr" in attr_form


def test_agent_tool_loop(prj):
    llm = FakeLLM([("tool_result", "All done."), ("make", '<write_file path="x.py">print(1)</write_file>')])
    out = runtime(prj, llm).agent("coder").run([{"role": "user", "content": "make x"}])
    assert out == "All done." and (prj.root / "x.py").read_text() == "print(1)\n"


def test_delegation_parallel(prj):
    llm = FakeLLM([
        ("tool_result", "team finished"),
        ("plan it", '<delegate agent="architect">design A</delegate><delegate agent="coder">build B</delegate>'),
        ("design A", "design ok"), ("build B", "built ok")])
    rt = runtime(prj, llm)
    rt.team = "code"
    assert rt.agent("lead").run([{"role": "user", "content": "plan it"}]) == "team finished"
    result = [c for c in llm.calls if "tool_result" in c][0]
    assert "design ok" in result and "built ok" in result


def test_graph_map_and_loop(prj):
    review_count = {"n": 0}

    def review(_):
        review_count["n"] += 1
        return "VERDICT: REVISE fix it" if review_count["n"] == 1 else "VERDICT: APPROVED"

    llm = FakeLLM([
        ("Plan a book", 'outline\n```json\n[{"file":"c1.md","title":"One"},{"file":"c2.md","title":"Two"}]\n```'),
        ("Your chapter: One", "wrote one"), ("Your chapter: Two", "wrote two"),
        ("Review", review), ("Feedback from `review`", "fixed"),
    ])
    g = {"nodes": {
        "outline": {"agent": "outliner", "prompt": "Plan a book: {{task}}"},
        "chapters": {"agent": "writer", "map": "outline", "prompt": "Your chapter: {{item.title}} -> {{item.file}}"},
        "fix": {"agent": "coder", "prompt": "{{chapters}}\n{{feedback}}"},
        "review": {"agent": "reviewer", "prompt": "Review {{fix}}"}},
        "edges": [["chapters", "fix"], ["fix", "review"],
                  {"from": "review", "to": "fix", "when": "REVISE", "max": 2}]}
    assert graph.validate(g, prj.agents()) == []
    out = graph.Executor(g, runtime(prj, llm)).run("HLS")
    assert "wrote one" in out["chapters"] and "wrote two" in out["chapters"]
    assert out["fix"] == "fixed" and out["review"] == "VERDICT: APPROVED" and review_count["n"] == 2


def test_quota_error_detection():
    from agge.providers import FallbackLLM, LLMError, is_quota_error
    assert is_quota_error('已达到 Token Plan 用量上限：请升级')
    assert is_quota_error('insufficient credit')
    assert not is_quota_error('too many requests, slow down')

    class Boom:
        label = "a:boom"

        def chat(self, *a, **k):
            raise LLMError("配額用盡")

    class Ok:
        label = "b:ok"

        def chat(self, *a, **k):
            return "answer"

    assert FallbackLLM([Boom(), Ok()]).chat("s", []) == "answer"

    class Empty:
        label = "c:empty"

        def chat(self, *a, **k):
            return "  "

    assert FallbackLLM([Empty(), Ok()]).chat("s", []) == "answer"
    assert FallbackLLM([Empty(), Boom()]).chat("s", []) == ""


def test_runtime_fallback_chain(prj):
    rt = Runtime(prj, model="echo", fallback="glm")
    assert rt.llm(rt.model_for("writer")).label == "echo:echo,nvidia:z-ai/glm-5.3-flash"
    assert rt.llm("glm").label == "nvidia:z-ai/glm-5.3-flash"  # no duplicate


def test_graph_map_resume_and_tolerance(prj):
    (prj.root / "c1.md").write_text("already written")

    def boom(_):
        raise RuntimeError("quota exhausted")

    llm = FakeLLM([
        ("Plan", '```json\n[{"file":"c1.md","title":"One"},{"file":"c2.md","title":"Two"}]\n```'),
        ("Your chapter: Two", boom),
    ])
    g = {"nodes": {
        "outline": {"agent": "outliner", "prompt": "Plan {{task}}"},
        "chapters": {"agent": "writer", "map": "outline", "prompt": "Your chapter: {{item.title}}"},
    }, "edges": [["outline", "chapters"]]}
    out = graph.Executor(g, runtime(prj, llm), resume=True).run("book")
    assert "跳過已存在檔案 c1.md" in out["chapters"]
    assert "此項失敗: quota exhausted" in out["chapters"]
    assert not any("Your chapter: One" in c for c in llm.calls)


def test_validate_and_render(prj):
    bad = {"nodes": {"a": {"agent": "nobody"}, "b": {}}, "edges": [["a", "b"], ["b", "a"]]}
    errs = graph.validate(bad, prj.agents())
    assert any("nobody" in e for e in errs)
    assert graph.render("{{x.y}} {{missing}}", {"x": {"y": "ok"}}) == "ok {{missing}}"
    assert graph.extract_items("1. alpha\n2. beta") == ["alpha", "beta"]
    for name, g in prj.graphs().items():
        assert graph.validate(g, prj.agents()) == [], name
        assert graph.mermaid(g).startswith("flowchart")


def test_model_resolution(prj):
    rt = Runtime(prj, model="echo", model_map={"coder": "nvidia:z-ai/glm-5.3-flash"})
    assert rt.llm(rt.model_for("coder")).label == "nvidia:z-ai/glm-5.3-flash"
    assert rt.model_for("writer") == "echo"
    assert rt.llm("ollama:llama3:8b").model == "llama3:8b"


def test_cli_end_to_end(tmp_path):
    repo = str(Path(__file__).resolve().parent.parent)
    env = {"AGGE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1", "PYTHONPATH": repo}
    run = lambda *a: subprocess.run([sys.executable, "-m", "agge", *a], cwd=tmp_path, env=env,
                                    capture_output=True, text=True)
    assert run("init", "-m", "echo").returncode == 0
    r = run("run", "solo", "hello", "there")
    assert r.returncode == 0 and "[echo]" in r.stdout and "hello there" in r.stdout
    assert run("graph", "new", "mine", "--from", "code").returncode == 0
    r = run("graph", "add-node", "mine", "docs", "--agent", "writer", "--after", "implement")
    assert r.returncode == 0 and "docs" in r.stdout
    assert run("graph", "validate", "mine").returncode == 0
    r = run("code", "x", "-n", "-M", "review=nvidia:z-ai/glm-5.3-flash")
    assert "review" in r.stdout and "nvidia:z-ai/glm-5.3-flash" in r.stdout
    r = run("log")
    assert "solo" in r.stdout and "done" in r.stdout
    r = subprocess.run([sys.executable, "-m", "agge", "chat"], cwd=tmp_path, env=env, input="ping\n",
                       capture_output=True, text=True)
    assert "[echo] ping" in r.stdout
    assert json.loads((tmp_path / ".agge/config.json").read_text())["default_model"] == "echo"


def test_tool_calls_to_tags():
    from agge.providers import tool_calls_to_tags
    tags = tool_calls_to_tags([
        {"function": {"name": "delegate", "arguments": '{"agent": "coder", "task": "build it"}'}},
        {"function": {"name": "read_file", "arguments": '{"path": "a.py"}'}}])
    calls = tools.parse_calls(tags)
    assert [(c.name, c.attrs, c.body) for c in calls] == [
        ("delegate", {"agent": "coder"}, "build it"), ("read_file", {"path": "a.py"}, None)]


def test_nudge_when_action_announced_without_tag(prj):
    llm = FakeLLM([("tool_result", "done!"), ("Write the tool tag", '<list_files path="."/>'),
                   ("go", "Step 1: list the files:")])
    out = runtime(prj, llm).agent("coder").run([{"role": "user", "content": "go"}])
    assert out == "done!" and any("Write the tool tag" in c for c in llm.calls)


def test_empty_reply_is_retried(prj):
    replies = iter(["", "real answer"])
    llm = FakeLLM([("", lambda _: next(replies))])
    assert runtime(prj, llm).agent("writer").run([{"role": "user", "content": "x"}]) == "real answer"


def test_retry_waits():
    from agge.providers import retry_wait
    assert [retry_wait(429, i) for i in range(7)] == [10, 20, 40, 60, 60, 60, None]
    assert retry_wait(429, 0, "3") == 3.0 and retry_wait(429, 0, "soon") == 10
    assert [retry_wait(503, i) for i in range(4)] == [2, 4, 8, None]


def test_fallback_llm():
    from agge.providers import FallbackLLM, LLMError

    class Broken:
        label = "broken:x"

        def chat(self, *a, **k):
            raise LLMError("down")

    ok = FakeLLM([])
    assert FallbackLLM([Broken(), ok]).chat("", [{"role": "user", "content": "hi"}]) == "done"
    with pytest.raises(LLMError):
        FallbackLLM([Broken()]).chat("", [{"role": "user", "content": "hi"}])


def test_ask_and_map_without_project(tmp_path):
    repo = str(Path(__file__).resolve().parent.parent)
    env = {"AGGE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1", "PYTHONPATH": repo}
    run = lambda *a, inp=None: subprocess.run([sys.executable, "-m", "agge", *a], cwd=tmp_path, env=env,
                                              input=inp, capture_output=True, text=True)
    (tmp_path / "a.txt").write_text("alpha")
    (tmp_path / "b.txt").write_text("beta")
    r = run("ask", "-m", "echo", "--json", "-f", "a.txt", "sum up")
    rec = json.loads(r.stdout)
    assert rec["model"] == "echo:echo" and "alpha" in rec["answer"] and rec["files_changed"] == []
    r = run("ask", "-m", "echo", "--save", "out/ans.md", "hello")
    assert "saved" in r.stdout and (tmp_path / "out/ans.md").read_text().startswith("[echo] hello")
    r = run("map", "-m", "echo", "-d", "res", "describe {{name}}", "*.txt")
    assert r.returncode == 0 and "beta" in (tmp_path / "res/b.md").read_text()
    r = run("map", "-m", "echo", "--json", "--lines", "item {{item}}", inp="x\ny\n")
    assert [json.loads(l)["answer"] for l in r.stdout.splitlines()] == ["[echo] item x", "[echo] item y"]
    assert not (tmp_path / ".agge").exists()


ECHO_SERVER = str(Path(__file__).resolve().parent / "mcp_echo_server.py")


def test_agent_uses_skill_and_mcp(prj):
    from agge import skills
    skills.create(prj.dir / "skills", "haiku", "Write haiku poems")
    cfg = prj.raw_config
    cfg["mcp"] = {"calc": {"command": sys.executable, "args": [ECHO_SERVER]}}
    prj.save_config(cfg)
    llm = FakeLLM([
        ("Skill: haiku", '<mcp server="calc" tool="add">{"a": 2, "b": 40}</mcp>'),
        ("tool_result name=\"mcp\"", lambda last: "answer " + last.split("\n")[1]),
        ("go", '<skill name="haiku"/>')])
    rt = runtime(prj, llm)
    agent = rt.agent("assistant")
    system = agent.system_prompt()
    assert "- haiku: Write haiku poems" in system and "calc.add: Add two numbers. (args: a*, b*)" in system
    assert agent.run([{"role": "user", "content": "go"}]) == "answer 42"
    rt.mcp.close_all()


def test_cli_skill_and_mcp(tmp_path):
    repo = str(Path(__file__).resolve().parent.parent)
    env = {"AGGE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1", "PYTHONPATH": repo}
    run = lambda *a: subprocess.run([sys.executable, "-m", "agge", *a], cwd=tmp_path, env=env,
                                    capture_output=True, text=True)
    run("init", "-m", "echo")
    assert run("skill", "new", "review-style", "--description", "House review style").returncode == 0
    assert "review-style" in run("skill", "list").stdout
    assert run("mcp", "add", "calc", "--", sys.executable, ECHO_SERVER).returncode == 0
    assert "calc.echo" in run("mcp", "tools").stdout
    assert run("mcp", "call", "calc", "add", '{"a": 1, "b": 2}').stdout.strip() == "3"
    (tmp_path / "claude.json").write_text(json.dumps({"mcpServers": {"x": {"command": "x-server"}}}))
    assert "x" in run("mcp", "import", "claude.json").stdout
    assert set(json.loads((tmp_path / ".agge/config.json").read_text())["mcp"]) == {"calc", "x"}


def test_shell_timeout_kills_process_group(tmp_path):
    import time
    marker = tmp_path / "alive"
    # A child that outlives the shell would keep touching the marker.
    cmd = f"(while true; do touch {marker}; sleep 0.1; done) & sleep 30"
    code, out, timed_out = tools.run_shell(cmd, tmp_path, 1)
    assert timed_out
    marker.unlink(missing_ok=True)
    time.sleep(0.5)
    assert not marker.exists()


def test_shell_leftover_background_killed(tmp_path):
    import time
    marker = tmp_path / "bg"
    code, out, timed_out = tools.run_shell(f"(sleep 0.5; touch {marker}) & echo started", tmp_path, 10)
    assert code == 0 and "started" in out and not timed_out
    time.sleep(1)
    assert not marker.exists()


def test_shell_output_is_capped(tmp_path):
    code, out, timed_out = tools.run_shell("yes x | head -c 300000", tmp_path, 10, limit=1000)
    assert "output truncated: 300000 bytes" in out
    assert len(out) < 1200
