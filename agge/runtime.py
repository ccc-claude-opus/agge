"""Agents and the runtime that wires them to models, tools and each other."""
import threading
import time

from . import tools
from .providers import LLM

BASE_PROMPT = """You are "{name}", an agent in agge — a multi-agent environment for software engineering and long-form writing.

{system}

Workspace root: {root}
Allowed directories: {dirs}
Today: {date}
{lang}"""

TOOLS_PROMPT = """
## Tools
You act on the workspace by writing tool tags directly in your reply (never inside code fences).
After your reply, the environment executes every tag in order and sends back <tool_result> blocks.
Continue (inspect → change → verify) until the task is done, then give a final answer with NO tool tags.
{docs}
Paths are relative to the workspace root. Always write complete file contents."""


class Agent:
    def __init__(self, name, spec, rt, model=None, depth=0):
        self.name, self.spec, self.rt, self.depth = name, spec, rt, depth
        self.model_name = model or rt.model_for(name, spec)
        self.llm = rt.llm(self.model_name)
        self.tools = [t for t in spec.get("tools", []) if t in tools.TOOL_NAMES]
        if depth >= rt.max_depth and "delegate" in self.tools:
            self.tools.remove("delegate")

    def system_prompt(self):
        ws = self.rt.workspace
        lang = self.rt.language
        text = BASE_PROMPT.format(
            name=self.name, system=self.spec.get("system", ""), root=ws.root,
            dirs=", ".join(map(str, ws.dirs)), date=time.strftime("%Y-%m-%d"),
            lang=f"Write in {lang}." if lang else "Reply in the same language the user writes in.")
        if self.tools:
            text += TOOLS_PROMPT.format(docs="\n".join(f"- {tools.TOOL_DOCS[t]}" for t in self.tools))
        if "delegate" in self.tools:
            team = self.rt.delegates_for(self.name)
            text += "\n\n## Your team\n" + "\n".join(f"- {n}: {d}" for n, d in team.items())
        return text

    def context(self):
        return tools.Context(
            workspace=self.rt.workspace, approver=self.rt.approver, agent=self.name,
            allowed=set(self.tools), on_event=self.rt.event,
            delegate=lambda target, task: self.rt.delegate(self.name, target, task, self.depth))

    def run(self, messages, on_token=None, on_think=None):
        """Agent loop: call the model, execute tool tags, feed results back. Mutates `messages`."""
        system, reply = self.system_prompt(), ""
        for step in range(self.rt.max_steps):
            self.rt.event("llm", agent=self.name, model=self.llm.label, step=step)
            reply = self.llm.chat(system, messages, on_token=on_token, on_think=on_think) or "(empty reply)"
            messages.append({"role": "assistant", "content": reply})
            calls = tools.parse_calls(reply) if self.tools else []
            if not calls:
                return reply
            if on_token:
                on_token("\n")
            messages.append({"role": "user", "content": tools.run_calls(calls, self.context())})
        self.rt.event("warn", agent=self.name, msg=f"{self.name} 達到最大步數 {self.rt.max_steps}")
        return tools.strip_tool_tags(reply)


class Runtime:
    """Everything an agent needs: project config, workspace, approval policy, model choices."""

    def __init__(self, project, workspace=None, approver=None, model=None, model_map=None,
                 on_event=None, team=None, max_steps=None, language=None):
        cfg = project.config
        self.project = project
        self.workspace = workspace or tools.Workspace(project.root, cfg.get("dirs", []))
        self.approver = approver or tools.Approver(cfg.get("approval", "ask"))
        self.model = model
        self.model_map = dict(model_map or {})
        self.on_event = on_event
        self.team = team
        self.max_steps = max_steps or cfg.get("max_steps", 40)
        self.max_depth = cfg.get("max_depth", 2)
        self.language = language or cfg.get("language")
        self._llms = {}
        self._lock = threading.Lock()

    def event(self, kind, **kw):
        if self.on_event:
            self.on_event(kind, **kw)

    def agents(self):
        return self.project.agents()

    def model_for(self, name, spec=None):
        spec = spec if spec is not None else self.agents().get(name, {})
        return (self.model_map.get(name) or spec.get("model") or self.model
                or self.project.config.get("default_model") or "echo")

    def llm(self, model_name):
        with self._lock:
            if model_name not in self._llms:
                self._llms[model_name] = LLM(self.project.resolve_model(model_name))
            return self._llms[model_name]

    def agent(self, name, model=None, depth=0):
        specs = self.agents()
        if name not in specs:
            raise KeyError(f"沒有名為 '{name}' 的 agent(可用: {', '.join(specs)})")
        return Agent(name, specs[name], self, model=model, depth=depth)

    def delegates_for(self, name):
        specs = self.agents()
        teams = self.project.teams()
        if self.team in teams and teams[self.team].get("lead") == name:
            members = teams[self.team]["members"]
        else:
            members = [n for n in specs if n not in (name, "lead", "assistant")]
        return {m: specs[m].get("description", "") for m in members if m in specs}

    def delegate(self, frm, target, task, depth):
        if target not in self.agents():
            raise tools.ToolError(f"unknown agent '{target}'; team: {', '.join(self.delegates_for(frm))}")
        self.event("delegate", agent=frm, target=target, task=task)
        out = self.agent(target, depth=depth + 1).run([{"role": "user", "content": task}])
        self.event("delegate_done", agent=frm, target=target, chars=len(out))
        return out
