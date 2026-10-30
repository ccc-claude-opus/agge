import json
import subprocess
import sys

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
    env = {"AGGE_HOME": str(tmp_path / "home"), "PATH": "/usr/bin:/bin", "NO_COLOR": "1"}
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
