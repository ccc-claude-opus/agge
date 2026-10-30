"""Interactive chat (like codex / opencode): talk to one agent, a whole team, or launch graphs."""
import shlex
import sys
from pathlib import Path

from . import ui
from .graph import Executor, describe, validate
from .project import ProjectError
from .providers import LLMError

try:
    import readline
except ImportError:  # Windows
    readline = None

HELP = """指令:
  /help                    顯示說明
  /agent [name]            查看/切換對話的 agent      /agents   列出所有 agent
  @name 訊息               單次對某個 agent 說話
  /team [name|off]         讓 lead 帶領團隊(可 delegate 給成員)   /teams   列出團隊
  /model [name]            查看/設定目前 agent 的模型  /models   列出模型
  /run <graph> <任務>      執行 graph 工作流程        /graphs   列出 graph   /graph <name> 顯示結構
  /add-dir <path>          允許 agent 存取其他目錄     /dirs     列出可存取目錄
  /yes                     切換 shell 指令自動核准
  /compact                 把目前對話壓縮成摘要       /clear    清除目前 agent 的對話
  /new  /sessions  /resume [id]   session 管理
  /save <file>             把最後一則回覆存成檔案
  /exit                    離開(Ctrl-D 亦可)
多行輸入: 以 \"\"\" 開始與結束,或行尾加 \\"""

COMMANDS = ["/help", "/agent", "/agents", "/team", "/teams", "/model", "/models", "/run", "/graphs",
            "/graph", "/add-dir", "/dirs", "/yes", "/compact", "/clear", "/new", "/sessions", "/resume",
            "/save", "/exit", "/quit"]


class Chat:
    def __init__(self, project, rt, session):
        self.project, self.rt, self.s = project, rt, session
        self.rt.team = session.get("team")
        for d in session.get("dirs", []):
            rt.workspace.add(d)
        self.rt.model_map.update(session.get("models", {}))
        self.history_file = project.dir / "history"

    # ---------- helpers ----------
    @property
    def speaker(self):
        return self.s.get("speaker") or "assistant"

    def save(self):
        self.s["models"] = self.rt.model_map
        self.s["dirs"] = [str(d) for d in self.rt.workspace.dirs[1:]]
        self.project.save_session(self.s)

    def prompt(self):
        model = self.rt.model_for(self.speaker)
        team = f"[{self.s['team']}] " if self.s.get("team") else ""
        return f"{team}{self.speaker}@{model}> "

    def read_input(self):
        line = input(ui.bold(self.prompt()) if not readline else self.prompt())
        if line.strip() == '"""':
            buf = []
            while True:
                nxt = input("... ")
                if nxt.strip() == '"""':
                    return "\n".join(buf)
                buf.append(nxt)
        buf = [line]
        while buf[-1].endswith("\\"):
            buf[-1] = buf[-1][:-1]
            buf.append(input("... "))
        return "\n".join(buf)

    def setup_readline(self):
        if not readline:
            return
        try:
            readline.read_history_file(self.history_file)
        except (FileNotFoundError, OSError):
            pass
        words = COMMANDS + [f"@{a}" for a in self.rt.agents()]

        def complete(text, state):
            opts = [w for w in words if w.startswith(text)]
            return opts[state] if state < len(opts) else None

        readline.set_completer(complete)
        readline.set_completer_delims(" \t\n")
        readline.parse_and_bind("bind ^I rl_complete" if "libedit" in (readline.__doc__ or "")
                                else "tab: complete")

    # ---------- main loop ----------
    def loop(self):
        self.setup_readline()
        ui.out(ui.bold("agge chat") + ui.dim(f"  session {self.s['id']} · {self.project.root}"))
        ui.out(ui.dim("輸入 /help 查看指令,@agent 指定對象,/team code 啟用團隊模式"))
        try:
            while True:
                try:
                    text = self.read_input().strip()
                except KeyboardInterrupt:
                    ui.out()
                    continue
                except EOFError:
                    ui.out()
                    break
                if not text:
                    continue
                if text.startswith("/"):
                    if self.command(text) == "exit":
                        break
                    continue
                target = self.speaker
                if text.startswith("@"):
                    name, _, rest = text[1:].partition(" ")
                    if name not in self.rt.agents():
                        ui.err(f"沒有 agent '{name}'")
                        continue
                    target, text = name, rest.strip()
                self.send(target, text)
        finally:
            self.save()
            if readline:
                try:
                    readline.write_history_file(self.history_file)
                except OSError:
                    pass

    def send(self, target, text):
        hist = self.s["histories"].setdefault(target, [])
        hist.append({"role": "user", "content": text})
        thinking = {"shown": False}

        def on_think(_):
            if not thinking["shown"]:
                thinking["shown"] = True
                ui.out(ui.dim("(思考中…)"), end="")

        def on_token(t):
            if thinking["shown"]:
                thinking["shown"] = False
                ui.out()
            ui.out(t, end="")

        ui.out(ui.cyan(f"{target}") + ui.dim(f" ({self.rt.model_for(target)})"))
        try:
            self.rt.agent(target).run(hist, on_token=on_token, on_think=on_think)
        except KeyboardInterrupt:
            ui.out(ui.yellow("\n[已中斷]"))
            if hist and hist[-1]["role"] == "user":
                hist.append({"role": "assistant", "content": "(interrupted by user)"})
        except (LLMError, ProjectError, KeyError) as e:
            ui.err(e)
            if hist and hist[-1]["role"] == "user" and hist[-1]["content"] == text:
                hist.pop()
        ui.out("\n")
        self.save()

    # ---------- slash commands ----------
    def command(self, text):
        try:
            parts = shlex.split(text)
        except ValueError:
            parts = text.split()
        cmd, args = parts[0], parts[1:]
        rest = text[len(cmd):].strip()
        try:
            return self._command(cmd, args, rest)
        except (ProjectError, LLMError, KeyError, ValueError) as e:
            ui.err(e)

    def _command(self, cmd, args, rest):
        rt, s = self.rt, self.s
        if cmd in ("/exit", "/quit"):
            return "exit"
        if cmd == "/help":
            ui.out(HELP)
        elif cmd == "/agents":
            for n, spec in rt.agents().items():
                mark = "*" if n == self.speaker else " "
                ui.out(f"{mark} {ui.cyan(n):<22} {ui.dim(rt.model_for(n))}  {spec.get('description', '')}")
        elif cmd == "/agent":
            if args:
                rt.agent(args[0])
                s["speaker"] = args[0]
            ui.out(f"目前 agent: {self.speaker}")
        elif cmd == "/teams":
            for n, t in self.project.teams().items():
                ui.out(f"  {ui.cyan(n):<16} lead={t['lead']} members={', '.join(t['members'])}  "
                       f"{ui.dim(t.get('description', ''))}")
        elif cmd == "/team":
            if args and args[0] == "off":
                s["team"], rt.team, s["speaker"] = None, None, "assistant"
            elif args:
                teams = self.project.teams()
                if args[0] not in teams:
                    raise ValueError(f"沒有團隊 '{args[0]}'")
                s["team"] = rt.team = args[0]
                s["speaker"] = teams[args[0]]["lead"]
            ui.out(f"團隊: {s.get('team') or '(無)'},對話對象: {self.speaker}")
        elif cmd == "/models":
            cfg = self.project.config
            for n, m in cfg["models"].items():
                mark = "*" if n == cfg.get("default_model") else " "
                ui.out(f"{mark} {ui.cyan(n):<18} {m.get('provider')}:{m.get('model')}")
        elif cmd == "/model":
            if args:
                rt.llm(args[0])
                rt.model_map[self.speaker] = args[0]
            ui.out(f"{self.speaker} 使用模型: {rt.model_for(self.speaker)}")
        elif cmd == "/graphs":
            for n, g in self.project.graphs().items():
                ui.out(f"  {ui.cyan(n):<16} {g.get('description', '')}")
        elif cmd == "/graph":
            ui.out(describe(self.project.load_graph(args[0])))
        elif cmd == "/run":
            if len(args) < 2:
                raise ValueError("用法: /run <graph> <任務>")
            self.run_graph(args[0], rest[len(args[0]):].strip())
        elif cmd == "/add-dir":
            ui.out(f"已允許: {rt.workspace.add(rest)}")
        elif cmd == "/dirs":
            ui.out("\n".join(f"  {d}" for d in rt.workspace.dirs))
        elif cmd == "/yes":
            rt.approver.mode = "ask" if rt.approver.mode == "auto" else "auto"
            ui.out(f"shell 核准模式: {rt.approver.mode}")
        elif cmd == "/clear":
            s["histories"][self.speaker] = []
            ui.out(f"已清除 {self.speaker} 的對話")
        elif cmd == "/compact":
            self.compact()
        elif cmd == "/new":
            self.save()
            self.s = self.project.new_session()
            self.s["speaker"], self.s["team"] = s.get("speaker"), s.get("team")
            ui.out(f"新 session {self.s['id']}")
        elif cmd == "/sessions":
            for sid in self.project.sessions()[-20:]:
                ui.out(("* " if sid == s["id"] else "  ") + sid)
        elif cmd == "/resume":
            self.save()
            self.s = self.project.load_session(args[0] if args else None)
            rt.team = self.s.get("team")
            ui.out(f"已載入 session {self.s['id']}")
        elif cmd == "/save":
            hist = s["histories"].get(self.speaker, [])
            last = next((m["content"] for m in reversed(hist) if m["role"] == "assistant"), None)
            if not last or not rest:
                raise ValueError("用法: /save <file>(且需要有回覆)")
            p = rt.workspace.resolve(rest)
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(last, encoding="utf-8")
            ui.out(f"已寫入 {p}")
        else:
            ui.err(f"未知指令 {cmd},輸入 /help")

    def run_graph(self, name, task):
        graph = self.project.load_graph(name)
        errs = validate(graph, self.rt.agents())
        if errs:
            raise ValueError("graph 有錯誤:\n  " + "\n  ".join(errs))
        log = self.project.new_run(name, task)
        printer = ui.EventPrinter(log=log)
        old = self.rt.on_event
        self.rt.on_event = printer
        try:
            outputs = Executor(graph, self.rt, log=log).run(task)
            log.finish("done")
        except KeyboardInterrupt:
            log.finish("interrupted")
            ui.out(ui.yellow("[已中斷]"))
            return
        except Exception as e:
            log.finish("failed", error=str(e))
            raise ValueError(str(e))
        finally:
            self.rt.on_event = old
        last = list(outputs.values())[-1]
        ui.out(ui.bold("\n最終輸出:\n") + last + ui.dim(f"\n\n(紀錄: agge log {log.id})"))
        hist = self.s["histories"].setdefault(self.speaker, [])
        hist.append({"role": "user", "content": f"[I ran the '{name}' workflow with task: {task}]"})
        hist.append({"role": "assistant", "content": f"Workflow '{name}' finished. Final output:\n{last}"})
        self.save()

    def compact(self):
        hist = self.s["histories"].get(self.speaker, [])
        if len(hist) < 4:
            ui.out("對話太短,不需要壓縮")
            return
        transcript = "\n\n".join(f"{m['role'].upper()}: {m['content'][:4000]}" for m in hist)
        llm = self.rt.llm(self.rt.model_for(self.speaker))
        summary = llm.chat("Summarize this conversation for your own future reference: goals, decisions, "
                           "files touched, current state and open TODOs. Be dense and factual.",
                           [{"role": "user", "content": transcript}])
        self.s["histories"][self.speaker] = [
            {"role": "user", "content": "Summary of our conversation so far:\n" + summary},
            {"role": "assistant", "content": "Understood. I'll continue from this summary."}]
        ui.out(ui.dim(summary))
        ui.out(f"已壓縮 {len(hist)} 則訊息")


def start(project, rt, resume=None):
    session = project.load_session(resume if resume is not True else None) if resume else project.new_session()
    if not sys.stdin.isatty():
        # piped input: run each line as a message, no prompt decoration
        chat = Chat(project, rt, session)
        for line in sys.stdin:
            if line.strip():
                chat.send(chat.speaker, line.strip())
        return
    Chat(project, rt, session).loop()
