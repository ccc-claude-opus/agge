"""agge — a git-style Swiss-army CLI for orchestrating LLM agent teams.

    agge                     開啟互動對話 (同 agge chat)
    agge <op> <args...>      執行子指令,見 agge help
"""
import argparse
import json
import os
import sys
from pathlib import Path

from . import __version__, builtins, repl, ui
from .graph import Executor, GraphError, describe, mermaid, validate
from .project import Project, ProjectError
from .providers import PROVIDERS, LLM, LLMError
from .runtime import Runtime
from .tools import Approver, ToolError, Workspace

EPILOG = """常用範例:
  agge init                                 在目前目錄建立 .agge/
  agge chat                                 互動對話 (/help 看指令)
  agge chat --team code                     與 lead 對話,lead 會分派工作給團隊
  agge ask "解釋 main.py 在做什麼"           單次提問
  agge code "寫一個 todo CLI" -o todo        設計→實作→測試→審查 流程
  agge book "EDA HLS 的歷史與現況" -o hlsBook --chapters 8
  agge run brainstorm "如何設計快取層" -M a=glm -M merge=claude
  agge graph show code / agge graph mermaid code
  agge model add claude anthropic claude-sonnet-5-5
  agge model add glm nvidia z-ai/glm-5.3-flash --temperature 0.5
  agge log                                  檢視執行紀錄
"""


# ---------------------------------------------------------------- helpers
def need_project(args):
    prj = Project.find()
    if prj is None:
        raise ProjectError("目前目錄不是 agge 專案,請先執行 `agge init`")
    return prj


def parse_kv(items):
    out = {}
    for it in items or []:
        if "=" not in it:
            raise ProjectError(f"格式錯誤 '{it}',應為 name=model")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def make_runtime(prj, args, on_event=None):
    cfg = prj.config
    root = Path(args.out).expanduser().resolve() if getattr(args, "out", None) else prj.root
    root.mkdir(parents=True, exist_ok=True)
    dirs = list(cfg.get("dirs", [])) + [str(prj.root)] + list(getattr(args, "add_dir", None) or [])
    approval = "auto" if getattr(args, "yes", False) else cfg.get("approval", "ask")
    return Runtime(prj, workspace=Workspace(root, dirs), approver=Approver(approval),
                   model=getattr(args, "model", None), model_map=parse_kv(getattr(args, "assign", None)),
                   on_event=on_event, team=getattr(args, "team", None),
                   language=getattr(args, "lang", None), max_steps=getattr(args, "max_steps", None))


def read_task(words):
    task = " ".join(words).strip()
    if task == "-" or (not task and not sys.stdin.isatty()):
        task = sys.stdin.read().strip()
    if not task:
        raise ProjectError("請提供任務描述(或用 - 從 stdin 讀取)")
    if task.startswith("@") and Path(task[1:]).is_file():
        task = Path(task[1:]).read_text(encoding="utf-8")
    return task


# ---------------------------------------------------------------- commands
def cmd_init(args):
    prj = Project.init(args.dir or os.getcwd(), model=args.model)
    ui.out(f"已初始化 agge 專案: {prj.dir}")
    ui.out(f"預設模型: {prj.config['default_model']}  (用 `agge model default <name>` 修改)")


def cmd_chat(args):
    prj = Project.find() or Project.init(os.getcwd())
    rt = make_runtime(prj, args)
    resume = (args.resume or True) if args.resume is not None else None
    session = None
    if args.agent or args.team:
        session = prj.load_session(None if resume is True else resume) if resume else prj.new_session()
        if args.team:
            teams = prj.teams()
            if args.team not in teams:
                raise ProjectError(f"沒有團隊 '{args.team}'(可用: {', '.join(teams)})")
            session["team"], session["speaker"] = args.team, teams[args.team]["lead"]
        if args.agent:
            rt.agent(args.agent)
            session["speaker"] = args.agent
        prj.save_session(session)
        resume = session["id"]
    repl.start(prj, rt, resume)


def cmd_ask(args):
    prj = need_project(args)
    rt = make_runtime(prj, args, on_event=ui.EventPrinter(args.verbose))
    task = read_task(args.prompt)
    agent = rt.agent(args.agent or ("lead" if args.team else "assistant"))
    stream = sys.stdout.isatty() or args.stream
    reply = agent.run([{"role": "user", "content": task}], on_token=(lambda t: ui.out(t, end="")) if stream else None)
    ui.out("" if stream else reply)


def run_graph(prj, args, graph_name, graph, task):
    errs = validate(graph, prj.agents())
    if errs:
        raise ProjectError("graph 有錯誤:\n  " + "\n  ".join(errs))
    log = prj.new_run(graph_name, task)
    rt = make_runtime(prj, args, on_event=ui.EventPrinter(args.verbose, log=log))
    ui.out(ui.bold(f"▶ 執行 graph '{graph_name}'") + ui.dim(f"  工作目錄 {rt.workspace.root} · 紀錄 {log.id}"))
    if args.dry_run:
        ui.out(describe(graph))
        for n, node in graph["nodes"].items():
            agent = node.get("agent", n)
            m = rt.model_map.get(n) or rt.model_map.get(agent) or node.get("model") or rt.model_for(agent)
            ui.out(f"  {n:<12} → {m}")
        log.finish("dry-run")
        return
    try:
        outputs = Executor(graph, rt, log=log, workers=args.workers).run(task)
    except KeyboardInterrupt:
        log.finish("interrupted")
        raise
    except Exception as e:
        log.finish("failed", error=str(e))
        raise
    log.finish("done")
    last = list(outputs.values())[-1]
    ui.out(ui.bold("\n═══ 最終輸出 ═══\n") + last)
    ui.out(ui.dim(f"\n完整紀錄: agge log {log.id}"))


def cmd_run(args):
    prj = need_project(args)
    graph = prj.load_graph(args.graph)
    run_graph(prj, args, Path(args.graph).stem, graph, read_task(args.task))


def cmd_code(args):
    prj = need_project(args)
    task = read_task(args.task)
    if args.lang_hint:
        task += f"\n\nProgramming language / stack: {args.lang_hint}"
    run_graph(prj, args, "code", prj.load_graph(args.graph or "code"), task)


def cmd_book(args):
    prj = need_project(args)
    task = read_task(args.topic)
    extra = []
    if args.chapters:
        extra.append(f"The book should have about {args.chapters} chapters.")
    if args.lang:
        extra.append(f"Write the whole book in {args.lang}.")
    if args.audience:
        extra.append(f"Target audience: {args.audience}.")
    if args.style:
        extra.append(f"Style / format requirements: {args.style}")
    if extra:
        task += "\n\n" + "\n".join(extra)
    run_graph(prj, args, "book", prj.load_graph(args.graph or "book"), task)


def cmd_graph(args):
    prj = need_project(args)
    op = args.op
    if op == "list":
        for n, g in prj.graphs().items():
            builtin = n in builtins.GRAPHS and not (prj.dir / "graphs" / f"{n}.json").exists()
            ui.out(f"  {ui.cyan(n):<20} {len(g.get('nodes', {}))} nodes  {ui.dim('(內建) ' if builtin else '')}"
                   f"{g.get('description', '')}")
        return
    if not args.name:
        raise ProjectError(f"用法: agge graph {op} <name>")
    if op == "new":
        if args.name in prj.graphs() and not args.force:
            raise ProjectError(f"graph '{args.name}' 已存在(--force 覆寫)")
        g = prj.load_graph(args.source) if args.source else {"description": "", "nodes": {}, "edges": []}
        prj.save_graph(args.name, g)
        ui.out(f"已建立 {prj.dir / 'graphs' / (args.name + '.json')}")
        return
    if op == "rm":
        prj.delete_graph(args.name)
        ui.out(f"已刪除 {args.name}")
        return
    g = prj.load_graph(args.name)
    if op == "show":
        ui.out(json.dumps(g, ensure_ascii=False, indent=2) if args.json else describe(g))
    elif op == "mermaid":
        ui.out(mermaid(g))
    elif op == "validate":
        errs = validate(g, prj.agents())
        ui.out("\n".join(ui.red("✘ " + e) for e in errs) if errs else ui.green("✔ graph 正確"))
        if errs:
            sys.exit(1)
    elif op in ("add-node", "add-edge", "rm-node"):
        rest = args.rest
        if op == "add-node":
            if not rest:
                raise ProjectError("用法: agge graph add-node <graph> <node> [--agent A] [--prompt P] [--map SRC] "
                                   "[--node-model M] [--after X ...]")
            node = {"agent": args.agent or rest[0], "prompt": args.prompt or "{{task}}\n\n{{deps}}\n\n{{feedback}}"}
            if args.map:
                node["map"] = args.map
            if args.node_model:
                node["model"] = args.node_model
            g.setdefault("nodes", {})[rest[0]] = node
            for a in args.after or []:
                g.setdefault("edges", []).append([a, rest[0]])
        elif op == "rm-node":
            g["nodes"].pop(rest[0], None)
            g["edges"] = [e for e in g.get("edges", [])
                          if rest[0] not in ((e[0], e[1]) if isinstance(e, list) else (e["from"], e["to"]))]
        else:
            if len(rest) != 2:
                raise ProjectError("用法: agge graph add-edge <graph> <from> <to> [--when REGEX --max N]")
            e = {"from": rest[0], "to": rest[1], "when": args.when, "max": args.max} if args.when else rest
            g.setdefault("edges", []).append(e)
        errs = validate(g, prj.agents())
        prj.save_graph(args.name, g)
        ui.out(describe(g) if g.get("nodes") else "(空 graph)")
        for e in errs:
            ui.out(ui.yellow("! " + e))


def cmd_agent(args):
    prj = need_project(args)
    agents = prj.agents()
    if args.op == "list":
        rt = Runtime(prj)
        for n, spec in agents.items():
            ui.out(f"  {ui.cyan(n):<22} {ui.dim(rt.model_for(n)):<30} {spec.get('description', '')}")
    elif args.op == "show":
        ui.out(json.dumps(agents[args.name], ensure_ascii=False, indent=2))
    elif args.op == "add":
        base = dict(agents.get(args.base or args.name, agents["assistant"]))
        for k in ("description", "system", "model"):
            v = getattr(args, k)
            if v:
                base[k] = v
        if args.system_file:
            base["system"] = Path(args.system_file).read_text(encoding="utf-8")
        if args.tools is not None:
            base["tools"] = [t for t in args.tools.split(",") if t]
        prj.save_agent(args.name, base)
        ui.out(f"已儲存 agent '{args.name}'")
    elif args.op == "rm":
        prj.save_agent(args.name, None)
        ui.out(f"已移除專案 agent '{args.name}'(內建同名 agent 仍存在)")


def cmd_team(args):
    prj = need_project(args)
    if args.op == "list":
        for n, t in prj.teams().items():
            ui.out(f"  {ui.cyan(n):<16} lead={t['lead']}  members={', '.join(t['members'])}  "
                   f"{ui.dim(t.get('description', ''))}")
    elif args.op == "add":
        cfg = prj.raw_config
        cfg.setdefault("teams", {})[args.name] = {"lead": args.lead, "members": args.members,
                                                  "description": args.description or ""}
        prj.save_config(cfg)
        ui.out(f"已建立團隊 '{args.name}'")
    elif args.op == "rm":
        cfg = prj.raw_config
        cfg.get("teams", {}).pop(args.name, None)
        prj.save_config(cfg)
        ui.out(f"已移除團隊 '{args.name}'")


def cmd_model(args):
    prj = need_project(args)
    cfg = prj.raw_config
    models = cfg.setdefault("models", {})
    if args.op == "list":
        merged = prj.config
        for n, m in merged["models"].items():
            mark = "*" if n == merged.get("default_model") else " "
            p = PROVIDERS.get(m.get("provider"), {})
            env = m.get("api_key_env", p.get("env"))
            key = "" if not env else (ui.green("key ✔") if os.environ.get(env) else ui.red(f"缺 {env}"))
            ui.out(f"{mark} {ui.cyan(n):<18} {m.get('provider')}:{m.get('model'):<36} {key}")
    elif args.op == "add":
        if not args.provider or not args.model_id:
            raise ProjectError("用法: agge model add <alias> <provider> <model-id> [--base-url ...]")
        m = {"provider": args.provider, "model": args.model_id}
        for k in ("temperature", "top_p", "max_tokens", "base_url", "api_key_env"):
            v = getattr(args, k)
            if v is not None:
                m[k] = v
        if args.provider not in PROVIDERS and "base_url" not in m:
            raise ProjectError(f"未知 provider '{args.provider}',請加 --base-url(OpenAI 相容端點)")
        models[args.name] = m
        prj.save_config(cfg)
        ui.out(f"已新增模型 {args.name} = {args.provider}:{args.model_id}")
    elif args.op == "rm":
        models.pop(args.name, None)
        prj.save_config(cfg)
        ui.out(f"已移除 {args.name}")
    elif args.op == "default":
        prj.resolve_model(args.name)
        cfg["default_model"] = args.name
        prj.save_config(cfg)
        ui.out(f"預設模型: {args.name}")
    elif args.op == "test":
        name = args.name or prj.config["default_model"]
        llm = LLM(prj.resolve_model(name))
        ui.out(ui.dim(f"測試 {name} ({llm.label}) ..."))
        llm.chat("You are a helpful assistant.", [{"role": "user", "content": args.prompt}],
                 on_token=lambda t: ui.out(t, end=""), on_think=lambda t: ui.out(ui.dim(t), end=""))
        ui.out()


def cmd_provider(args):
    for n, p in PROVIDERS.items():
        env = p.get("env")
        status = "" if not env else (ui.green("✔") if os.environ.get(env) else ui.dim("✘"))
        ui.out(f"  {ui.cyan(n):<20} {p['kind']:<10} {p.get('base_url', ''):<55} {env or ''} {status}")


def cmd_dir(args):
    prj = need_project(args)
    cfg = prj.raw_config
    dirs = cfg.setdefault("dirs", [])
    if args.op == "add":
        p = str(Path(args.path).expanduser().resolve())
        if p not in dirs:
            dirs.append(p)
        prj.save_config(cfg)
        ui.out(f"已允許 agent 存取 {p}")
    elif args.op == "rm":
        p = str(Path(args.path).expanduser().resolve())
        cfg["dirs"] = [d for d in dirs if d != p]
        prj.save_config(cfg)
        ui.out(f"已移除 {p}")
    else:
        ui.out(f"  {prj.root} (專案)")
        for d in prj.config["dirs"]:
            ui.out(f"  {d}")


def cmd_config(args):
    prj = need_project(args)
    cfg = prj.raw_config
    if args.key is None:
        ui.out(json.dumps(prj.config, ensure_ascii=False, indent=2))
    elif args.value is None:
        ui.out(json.dumps(prj.config.get(args.key), ensure_ascii=False, indent=2))
    else:
        try:
            cfg[args.key] = json.loads(args.value)
        except json.JSONDecodeError:
            cfg[args.key] = args.value
        prj.save_config(cfg)
        ui.out(f"{args.key} = {cfg[args.key]!r}")


def cmd_log(args):
    prj = need_project(args)
    runs = prj.runs()
    if not args.run:
        for rid in runs[-args.n:]:
            meta = json.loads((prj.dir / "runs" / rid / "meta.json").read_text(encoding="utf-8"))
            color = {"done": ui.green, "failed": ui.red}.get(meta.get("status"), ui.yellow)
            ui.out(f"  {ui.cyan(rid):<40} {color(meta.get('status', '?')):<10} {ui.short(meta.get('task', ''), 60)}")
        return
    rid = args.run if args.run in runs else next((r for r in reversed(runs) if r.startswith(args.run)), None)
    if args.run == "last" and runs:
        rid = runs[-1]
    if not rid:
        raise ProjectError(f"找不到執行紀錄 {args.run}")
    d = prj.dir / "runs" / rid
    if args.node:
        files = sorted(d.glob(f"{args.node}*.md"))
        if not files:
            raise ProjectError(f"{rid} 沒有節點 {args.node} 的輸出")
        for f in files:
            ui.out(ui.bold(f"── {f.name} ──"))
            ui.out(f.read_text(encoding="utf-8"))
        return
    ui.out(json.dumps(json.loads((d / "meta.json").read_text(encoding="utf-8")), ensure_ascii=False, indent=2))
    ui.out(ui.bold("輸出:"))
    for f in sorted(d.glob("*.md")):
        ui.out(f"  {f.stem:<24} {f.stat().st_size} bytes")
    ui.out(ui.dim(f"用 agge log {rid} <node> 查看節點輸出"))


def cmd_session(args):
    prj = need_project(args)
    if args.op == "list":
        for sid in prj.sessions():
            ui.out("  " + sid)
    else:
        s = prj.load_session(args.id)
        for agent, hist in s["histories"].items():
            ui.out(ui.bold(f"── {agent} ──"))
            for m in hist:
                who = ui.cyan("you") if m["role"] == "user" else ui.green(agent)
                ui.out(f"{who}: {m['content']}\n")


def cmd_status(args):
    prj = Project.find()
    ui.out(f"agge {__version__}")
    if not prj:
        ui.out("目前不在 agge 專案中(agge init 建立)")
        return
    cfg = prj.config
    ui.out(f"專案:     {prj.root}")
    ui.out(f"預設模型: {cfg['default_model']}")
    ui.out(f"模型:     {', '.join(cfg['models'])}")
    ui.out(f"Agents:   {', '.join(prj.agents())}")
    ui.out(f"Teams:    {', '.join(prj.teams())}")
    ui.out(f"Graphs:   {', '.join(prj.graphs())}")
    ui.out(f"核准模式: {cfg.get('approval', 'ask')}   額外目錄: {', '.join(cfg['dirs']) or '(無)'}")
    ui.out(f"Sessions: {len(prj.sessions())}   Runs: {len(prj.runs())}")


# ---------------------------------------------------------------- parser
def build_parser():
    p = argparse.ArgumentParser(prog="agge", description="agge — 組織 LLM agent 團隊寫程式與寫書的瑞士刀 CLI",
                                epilog=EPILOG, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("-V", "--version", action="version", version=f"agge {__version__}")
    sub = p.add_subparsers(dest="cmd", metavar="<op>")

    def runtime_opts(sp, out=True):
        sp.add_argument("-m", "--model", help="預設模型(別名或 provider:model)")
        sp.add_argument("-M", "--assign", action="append", metavar="AGENT=MODEL",
                        help="指定某 agent/節點使用的模型,可重複")
        sp.add_argument("-y", "--yes", action="store_true", help="自動核准 shell 指令")
        sp.add_argument("--add-dir", action="append", metavar="DIR", help="允許存取的額外目錄")
        sp.add_argument("--lang", help="輸出語言,如 zh-TW、English")
        sp.add_argument("--max-steps", type=int, help="每個 agent 最多工具回合數")
        sp.add_argument("-v", "--verbose", action="store_true", help="顯示每個節點的輸出")
        if out:
            sp.add_argument("-o", "--out", metavar="DIR", help="工作目錄(輸出檔案寫到這裡)")

    def graph_opts(sp):
        runtime_opts(sp)
        sp.add_argument("-g", "--graph", help="改用其他 graph")
        sp.add_argument("-w", "--workers", type=int, default=4, help="平行節點數")
        sp.add_argument("-n", "--dry-run", action="store_true", help="只顯示計畫與模型分配")

    sp = sub.add_parser("init", help="建立 .agge/ 專案")
    sp.add_argument("dir", nargs="?")
    sp.add_argument("-m", "--model", help="預設模型")
    sp.set_defaults(func=cmd_init)

    sp = sub.add_parser("chat", help="互動對話(預設)")
    runtime_opts(sp)
    sp.add_argument("-a", "--agent", help="對話的 agent")
    sp.add_argument("-t", "--team", help="團隊模式:與 lead 對話,lead 分派給成員")
    sp.add_argument("-r", "--resume", nargs="?", const="", default=None, metavar="ID", help="接續 session")
    sp.set_defaults(func=cmd_chat)

    sp = sub.add_parser("ask", help="單次提問 / 執行任務")
    runtime_opts(sp)
    sp.add_argument("prompt", nargs="*")
    sp.add_argument("-a", "--agent")
    sp.add_argument("-t", "--team")
    sp.add_argument("--stream", action="store_true")
    sp.set_defaults(func=cmd_ask)

    sp = sub.add_parser("run", help="執行 graph 工作流程")
    sp.add_argument("graph", help="graph 名稱或 .json 檔")
    sp.add_argument("task", nargs="*", help="任務(- 讀 stdin,@file 讀檔)")
    graph_opts(sp)
    sp.set_defaults(func=cmd_run)

    sp = sub.add_parser("code", help="用開發團隊寫程式 (graph: code)")
    sp.add_argument("task", nargs="*")
    sp.add_argument("--stack", dest="lang_hint", help="程式語言 / 技術棧")
    graph_opts(sp)
    sp.set_defaults(func=cmd_code)

    sp = sub.add_parser("book", help="用寫作團隊寫書 (graph: book)")
    sp.add_argument("topic", nargs="*")
    sp.add_argument("-c", "--chapters", type=int)
    sp.add_argument("--audience")
    sp.add_argument("--style", help="格式/風格要求(可用 @file)")
    graph_opts(sp)
    sp.set_defaults(func=cmd_book)

    sp = sub.add_parser("graph", help="管理 graph: list|show|mermaid|validate|new|rm|add-node|add-edge|rm-node")
    sp.add_argument("op", choices=["list", "show", "mermaid", "validate", "new", "rm", "add-node", "add-edge", "rm-node"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("rest", nargs="*")
    sp.add_argument("--from", dest="source", help="new: 從既有 graph 複製")
    sp.add_argument("--force", action="store_true")
    sp.add_argument("--json", action="store_true")
    sp.add_argument("--agent")
    sp.add_argument("--prompt")
    sp.add_argument("--map")
    sp.add_argument("--node-model")
    sp.add_argument("--after", action="append")
    sp.add_argument("--when")
    sp.add_argument("--max", type=int, default=2)
    sp.set_defaults(func=cmd_graph)

    sp = sub.add_parser("agent", help="管理 agent: list|show|add|rm")
    sp.add_argument("op", choices=["list", "show", "add", "rm"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--base", help="add: 以哪個 agent 為範本")
    sp.add_argument("--description")
    sp.add_argument("--system")
    sp.add_argument("--system-file")
    sp.add_argument("--model")
    sp.add_argument("--tools", help="逗號分隔: read_file,write_file,edit_file,list_files,search,shell,delegate")
    sp.set_defaults(func=cmd_agent)

    sp = sub.add_parser("team", help="管理團隊: list|add|rm")
    sp.add_argument("op", choices=["list", "add", "rm"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("--lead", default="lead")
    sp.add_argument("--members", nargs="+", default=[])
    sp.add_argument("--description")
    sp.set_defaults(func=cmd_team)

    sp = sub.add_parser("model", help="管理模型: list|add|rm|default|test")
    sp.add_argument("op", choices=["list", "add", "rm", "default", "test"])
    sp.add_argument("name", nargs="?")
    sp.add_argument("provider", nargs="?")
    sp.add_argument("model_id", nargs="?")
    sp.add_argument("--temperature", type=float)
    sp.add_argument("--top-p", type=float)
    sp.add_argument("--max-tokens", type=int)
    sp.add_argument("--base-url")
    sp.add_argument("--api-key-env")
    sp.add_argument("--prompt", default="用一句話介紹你自己。")
    sp.set_defaults(func=cmd_model)

    sp = sub.add_parser("provider", help="列出支援的 provider")
    sp.set_defaults(func=cmd_provider)

    sp = sub.add_parser("dir", help="管理 agent 可存取目錄: list|add|rm")
    sp.add_argument("op", choices=["list", "add", "rm"])
    sp.add_argument("path", nargs="?")
    sp.set_defaults(func=cmd_dir)

    sp = sub.add_parser("config", help="讀寫設定: agge config [key [value]]")
    sp.add_argument("key", nargs="?")
    sp.add_argument("value", nargs="?")
    sp.set_defaults(func=cmd_config)

    sp = sub.add_parser("log", help="執行紀錄: agge log [run|last] [node]")
    sp.add_argument("run", nargs="?")
    sp.add_argument("node", nargs="?")
    sp.add_argument("-n", type=int, default=20)
    sp.set_defaults(func=cmd_log)

    sp = sub.add_parser("session", help="對話 session: list|show [id]")
    sp.add_argument("op", choices=["list", "show"])
    sp.add_argument("id", nargs="?")
    sp.set_defaults(func=cmd_session)

    sp = sub.add_parser("status", help="顯示專案狀態")
    sp.set_defaults(func=cmd_status)

    sp = sub.add_parser("help", help="顯示說明")
    sp.set_defaults(func=lambda a: p.print_help())
    return p


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    parser = build_parser()
    args = parser.parse_args(argv or ["chat"])
    try:
        args.func(args)
    except KeyboardInterrupt:
        ui.err("已中斷")
        sys.exit(130)
    except (ProjectError, LLMError, ToolError, GraphError, KeyError, ValueError) as e:
        ui.err(e.args[0] if isinstance(e, KeyError) and e.args else e)
        sys.exit(1)


if __name__ == "__main__":
    main()
