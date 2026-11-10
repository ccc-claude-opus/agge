"""Agents and the runtime that wires them to models, tools and each other."""
import threading
import time

from . import tools
from .providers import LLM, FallbackLLM

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


NUDGE = ("Your reply ended right before the action you announced, without any tool tag. "
         "Write the tool tag(s) now, or give your final answer if the task is complete.")
EMPTY_NUDGE = "Your previous reply was empty. Please continue with the task."


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
            allowed=set(self.tools), on_event=self.rt.event, changed=self.rt.changed,
            delegate=lambda target, task: self.rt.delegate(self.name, target, task, self.depth))

    def run(self, messages, on_token=None, on_think=None):
        """Agent loop: call the model, execute tool tags, feed results back. Mutates `messages`."""
        system, reply, nudges = self.system_prompt(), "", 0
        for step in range(self.rt.max_steps):
            self.rt.event("llm", agent=self.name, model=self.llm.label, step=step)
            reply = (self.llm.chat(system, messages, on_token=on_token, on_think=on_think) or "").strip()
            empty = not reply
            reply = reply or "(empty reply)"
            messages.append({"role": "assistant", "content": reply})
            calls = tools.parse_calls(reply) if self.tools else []
            if not calls:
                # Models sometimes return nothing, or announce an action ("Step 1: ask architect:")
                # and stop before writing the tag; nudge them to continue instead of finishing.
                stalled = self.tools and reply.rstrip("*").endswith((":", "："))
                if (empty or stalled) and nudges < 2:
                    nudges += 1
                    why = "回覆是空的" if empty else "宣告了動作但沒有寫出工具標籤"
                    self.rt.event("warn", agent=self.name, msg=f"{self.name} {why},提醒它繼續")
                    messages.append({"role": "user", "content": EMPTY_NUDGE if empty else NUDGE})
                    continue
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
        self.changed = set()  # files written or edited by any agent in this runtime

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
                names = [n.strip() for n in model_name.split(",") if n.strip()]
                llms = [LLM(self.project.resolve_model(n)) for n in names]
                self._llms[model_name] = llms[0] if len(llms) == 1 else FallbackLLM(llms)
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
