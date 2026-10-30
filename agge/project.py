"""Project state stored in .agge/ (like .git/).

.agge/
  config.json        default_model, models, teams, dirs, approval, language
  agents.json        project agents (override / extend built-ins)
  graphs/<name>.json project graphs   (graph.json = legacy graph named "default")
  sessions/          chat sessions (LAST points at the latest)
  runs/<id>/         graph run logs: meta.json, events.jsonl, <node>.md
  history            readline history

User-wide settings live in ~/.agge/config.json (or $AGGE_HOME) and are merged under the project.
"""
import json
import os
import time
from pathlib import Path

from . import builtins
from .providers import PROVIDERS, parse_spec

DIRNAME = ".agge"
GLOBAL_HOME = Path(os.environ.get("AGGE_HOME", Path.home() / ".agge"))


def default_config():
    cfg = {
        "default_model": "glm" if os.environ.get("NVIDIA_API_KEY") else "echo",
        "models": {
            "echo": {"provider": "echo", "model": "echo"},
            "glm": {"provider": "nvidia", "model": "z-ai/glm-5.3-flash",
                    "temperature": 0.3, "top_p": 1.0, "max_tokens": 8192},
        },
        "approval": "ask",
        "dirs": [],
    }
    return cfg


def _read(path, default):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _write(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


class ProjectError(Exception):
    pass


class Project:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.dir = self.root / DIRNAME

    # ---------- discovery ----------
    @classmethod
    def find(cls, start=None):
        p = Path(start or os.getcwd()).resolve()
        for d in [p, *p.parents]:
            if (d / DIRNAME).is_dir() and (d / DIRNAME).resolve() != GLOBAL_HOME.resolve():
                return cls(d)
        return None

    @classmethod
    def init(cls, root, model=None):
        prj = cls(root)
        if not (prj.dir / "config.json").exists():
            cfg = default_config()
            if model:
                cfg["default_model"] = model
            _write(prj.dir / "config.json", cfg)
        if not (prj.dir / "agents.json").exists():
            _write(prj.dir / "agents.json", {})
        (prj.dir / "graphs").mkdir(parents=True, exist_ok=True)
        (prj.dir / "sessions").mkdir(exist_ok=True)
        (prj.dir / "runs").mkdir(exist_ok=True)
        return prj

    # ---------- config ----------
    @property
    def raw_config(self):
        return _read(self.dir / "config.json", {})

    def save_config(self, cfg):
        _write(self.dir / "config.json", cfg)

    @property
    def config(self):
        g = _read(GLOBAL_HOME / "config.json", {})
        p = self.raw_config
        cfg = {**g, **p}
        cfg["models"] = {**g.get("models", {}), **p.get("models", {})}
        cfg["teams"] = {**g.get("teams", {}), **p.get("teams", {})}
        cfg["dirs"] = list(dict.fromkeys(g.get("dirs", []) + p.get("dirs", [])))
        cfg.setdefault("default_model", "echo")
        return cfg

    def resolve_model(self, name):
        models = self.config["models"]
        if name in models:
            return dict(models[name])
        spec = parse_spec(name)
        if spec:
            return spec
        raise ProjectError(f"未知的模型 '{name}'。用 `agge model add` 新增,或用 provider:model 格式"
                           f"(provider: {', '.join(PROVIDERS)})")

    # ---------- agents / graphs / teams ----------
    def agents(self):
        return {**builtins.AGENTS, **_read(self.dir / "agents.json", {})}

    def save_agent(self, name, spec):
        data = _read(self.dir / "agents.json", {})
        if spec is None:
            data.pop(name, None)
        else:
            data[name] = spec
        _write(self.dir / "agents.json", data)

    def graphs(self):
        out = dict(builtins.GRAPHS)
        legacy = _read(self.dir / "graph.json", {})
        if legacy.get("nodes"):
            out["default"] = legacy
        for f in sorted((self.dir / "graphs").glob("*.json")):
            out[f.stem] = _read(f, {})
        return out

    def load_graph(self, name):
        if name.endswith(".json") and Path(name).exists():
            return _read(name, {})
        graphs = self.graphs()
        if name not in graphs:
            raise ProjectError(f"找不到 graph '{name}'(可用: {', '.join(graphs)})")
        return json.loads(json.dumps(graphs[name]))

    def save_graph(self, name, graph):
        _write(self.dir / "graphs" / f"{name}.json", graph)

    def delete_graph(self, name):
        f = self.dir / "graphs" / f"{name}.json"
        if not f.exists():
            raise ProjectError(f"'{name}' 不是專案 graph(內建 graph 不能刪)")
        f.unlink()

    def teams(self):
        return {**builtins.TEAMS, **self.config.get("teams", {})}

    # ---------- sessions ----------
    def new_session(self):
        sid = time.strftime("%Y%m%d-%H%M%S")
        return {"id": sid, "team": None, "speaker": "assistant", "histories": {}}

    def save_session(self, s):
        _write(self.dir / "sessions" / f"{s['id']}.json", s)
        (self.dir / "sessions" / "LAST").write_text(s["id"])

    def load_session(self, sid=None):
        if not sid:
            try:
                sid = (self.dir / "sessions" / "LAST").read_text().strip()
            except FileNotFoundError:
                raise ProjectError("沒有之前的 session")
        s = _read(self.dir / "sessions" / f"{sid}.json", None)
        if s is None:
            raise ProjectError(f"找不到 session {sid}")
        return s

    def sessions(self):
        return sorted(f.stem for f in (self.dir / "sessions").glob("*.json"))

    # ---------- runs ----------
    def new_run(self, graph_name, task):
        rid = time.strftime("%Y%m%d-%H%M%S") + f"-{graph_name}"
        return RunLog(self.dir / "runs" / rid, graph_name, task)

    def runs(self):
        d = self.dir / "runs"
        return sorted(p.name for p in d.iterdir() if p.is_dir()) if d.exists() else []


class RunLog:
    def __init__(self, path, graph_name, task):
        self.path = Path(path)
        self.id = self.path.name
        self.path.mkdir(parents=True, exist_ok=True)
        self.meta = {"id": self.id, "graph": graph_name, "task": task,
                     "started": time.strftime("%Y-%m-%d %H:%M:%S"), "status": "running"}
        _write(self.path / "meta.json", self.meta)

    def event(self, kind, **kw):
        kw = {k: v for k, v in kw.items() if k != "output"}
        with open(self.path / "events.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps({"t": time.time(), "kind": kind, **kw}, ensure_ascii=False) + "\n")

    def save_output(self, label, text):
        safe = label.replace("/", "_")
        f = self.path / f"{safe}.md"
        n = 1
        while f.exists():
            n += 1
            f = self.path / f"{safe}.{n}.md"
        f.write_text(text, encoding="utf-8")

    def finish(self, status, **extra):
        self.meta.update(status=status, finished=time.strftime("%Y-%m-%d %H:%M:%S"), **extra)
        _write(self.path / "meta.json", self.meta)
