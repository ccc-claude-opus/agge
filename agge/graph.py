"""Graph engineering: define agent workflows as graphs and execute them.

Graph JSON:
{
  "description": "...",
  "nodes": {
    "design":    {"agent": "architect", "prompt": "Task: {{task}}"},
    "implement": {"agent": "coder", "prompt": "{{design}}\n{{feedback}}", "model": "glm"},
    "chapters":  {"agent": "writer", "map": "outline", "prompt": "Write {{item.title}} to {{item.file}}"}
  },
  "edges": [
    ["design", "implement"],                                            # dependency
    {"from": "review", "to": "implement", "when": "REVISE", "max": 2}   # conditional loop-back
  ]
}

Template variables: {{task}}, {{<node name>}} (that node's output), {{deps}} (all upstream outputs),
{{feedback}} (output of the node that looped back here), {{item}}/{{item.field}}/{{index}} in map nodes.

Execution: nodes whose dependencies are done run in parallel. A "map" node runs its agent once per
item of the list found in its source node's output (```json block or bullet list), in parallel.
A conditional edge whose regex `when` matches the source output re-runs the target and everything
downstream of it, up to `max` times.
"""
import json
import re
import time
from collections import Counter
from concurrent import futures as cf

DEFAULT_PROMPT = "{{task}}\n\n{{deps}}\n\n{{feedback}}"


class GraphError(Exception):
    pass


def edges(graph):
    out = []
    for e in graph.get("edges", []):
        if isinstance(e, (list, tuple)):
            out.append({"from": e[0], "to": e[1]})
        else:
            out.append(dict(e))
    return out


def dependencies(graph):
    deps = {n: [] for n in graph.get("nodes", {})}
    for e in edges(graph):
        if not e.get("when") and e["to"] in deps and e["from"] not in deps[e["to"]]:
            deps[e["to"]].append(e["from"])
    for n, node in graph.get("nodes", {}).items():
        src = node.get("map")
        if src and src not in deps[n]:
            deps[n].append(src)
    return deps


def topo_order(graph):
    deps = dependencies(graph)
    order, state = [], {}

    def visit(n, path):
        if state.get(n) == 1:
            raise GraphError("依賴關係有循環: " + " → ".join(path + [n]) + "(迴圈請用帶 when 的條件邊)")
        if state.get(n) == 2:
            return
        state[n] = 1
        for d in deps.get(n, []):
            visit(d, path + [n])
        state[n] = 2
        order.append(n)

    for n in graph.get("nodes", {}):
        visit(n, [])
    return order


def descendants(graph, start):
    children = {}
    for n, ds in dependencies(graph).items():
        for d in ds:
            children.setdefault(d, []).append(n)
    seen, stack = set(), [start]
    while stack:
        for c in children.get(stack.pop(), []):
            if c not in seen:
                seen.add(c)
                stack.append(c)
    return seen


def validate(graph, agents):
    errors = []
    nodes = graph.get("nodes", {})
    if not nodes:
        errors.append("graph 沒有任何節點")
    for n, node in nodes.items():
        agent = node.get("agent", n)
        if agent not in agents:
            errors.append(f"節點 {n}: 未知的 agent '{agent}'")
        if node.get("map") and node["map"] not in nodes:
            errors.append(f"節點 {n}: map 來源 '{node['map']}' 不存在")
    for e in edges(graph):
        for k in ("from", "to"):
            if e.get(k) not in nodes:
                errors.append(f"邊 {e}: 節點 '{e.get(k)}' 不存在")
        if e.get("when"):
            try:
                re.compile(e["when"])
            except re.error as ex:
                errors.append(f"邊 {e}: when 正規表示式錯誤 {ex}")
    if not errors:
        try:
            topo_order(graph)
        except GraphError as ex:
            errors.append(str(ex))
    return errors


def render(template, variables):
    def lookup(key):
        cur = variables
        for part in key.split("."):
            if isinstance(cur, dict) and part in cur:
                cur = cur[part]
            else:
                return None
        return cur

    def rep(m):
        v = lookup(m.group(1))
        if v is None:
            return m.group(0)
        return v if isinstance(v, str) else json.dumps(v, ensure_ascii=False, indent=2)

    return re.sub(r"\{\{\s*([\w.\-]+)\s*\}\}", rep, template)


def extract_items(text):
    """Find a list in an agent's output: last ```json block, whole text as JSON, or a bullet list."""
    candidates = re.findall(r"```(?:json)?\s*\n(.*?)```", text, re.S)[::-1] + [text]
    for block in candidates:
        try:
            data = json.loads(block)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), None)
        if isinstance(data, list) and data:
            return data
    items = re.findall(r"^\s*(?:[-*]|\d+[.)])\s+(.+)$", text, re.M)
    return [i.strip() for i in items if i.strip()]


def mermaid(graph):
    lines = ["flowchart TD"]
    for n, node in graph.get("nodes", {}).items():
        label = f"{n}<br/>{node.get('agent', n)}" + (" ×map" if node.get("map") else "")
        lines.append(f'  {n}["{label}"]')
    for e in edges(graph):
        if e.get("when"):
            lines.append(f'  {e["from"]} -. "{e["when"]} (max {e.get("max", 3)})" .-> {e["to"]}')
        else:
            lines.append(f"  {e['from']} --> {e['to']}")
    for n, node in graph.get("nodes", {}).items():
        if node.get("map"):
            lines.append(f"  {node['map']} ==>|items| {n}")
    return "\n".join(lines)


def describe(graph):
    """Human-readable text rendering of a graph."""
    deps = dependencies(graph)
    out = []
    if graph.get("description"):
        out.append(graph["description"])
    for n in topo_order(graph):
        node = graph["nodes"][n]
        extra = []
        if node.get("map"):
            extra.append(f"map over {node['map']}")
        if node.get("model"):
            extra.append(f"model={node['model']}")
        after = f"  ← {', '.join(deps[n])}" if deps[n] else "  (start)"
        out.append(f"  [{n}] agent={node.get('agent', n)}{' ' + ' '.join(extra) if extra else ''}{after}")
    for e in edges(graph):
        if e.get("when"):
            out.append(f"  ↺ {e['from']} → {e['to']} when /{e['when']}/ (max {e.get('max', 3)})")
    return "\n".join(out)


class Executor:
    def __init__(self, graph, rt, log=None, workers=4):
        self.graph, self.rt, self.log, self.workers = graph, rt, log, workers
        self.nodes = graph.get("nodes", {})
        self.deps = dependencies(graph)
        self.order = topo_order(graph)
        self.loops = [e for e in edges(graph) if e.get("when")]
        self.outputs, self.feedback = {}, {}

    def _vars(self, name, task):
        v = {"task": task, "input": task, **self.outputs}
        v["deps"] = "\n\n".join(f"## Output of `{d}`\n{self.outputs[d]}" for d in self.deps[name])
        fb = self.feedback.get(name)
        v["feedback"] = (f"## Feedback from `{fb[0]}` (round {fb[2]}) — address all of it\n{fb[1]}\n"
                         if fb else "")
        return v

    def _run_agent(self, name, node, prompt, label):
        agent_name = node.get("agent", name)
        model = self.rt.model_map.get(name) or self.rt.model_map.get(agent_name) or node.get("model")
        agent = self.rt.agent(agent_name, model=model)
        self.rt.event("node_start", node=label, agent=agent_name, model=agent.llm.label)
        t = time.time()
        out = agent.run([{"role": "user", "content": prompt}])
        self.rt.event("node_done", node=label, agent=agent_name, secs=time.time() - t, chars=len(out), output=out)
        if self.log:
            self.log.save_output(label, f"<!-- agent: {agent_name} · model: {agent.llm.label} -->\n"
                                        f"# Prompt\n\n{prompt}\n\n# Output\n\n{out}\n")
        return out

    def _exec(self, name, variables):
        node = self.nodes[name]
        template = node.get("prompt") or DEFAULT_PROMPT
        src = node.get("map")
        if not src:
            return self._run_agent(name, node, render(template, variables), name)
        items = extract_items(self.outputs.get(src, ""))
        if not items:
            raise GraphError(f"節點 {name}: 無法從 '{src}' 的輸出取得清單(需要 ```json 陣列或條列清單)")
        items = items[: node.get("max_items", 100)]
        with cf.ThreadPoolExecutor(node.get("parallel", self.workers)) as ex:
            futs = [ex.submit(self._run_agent, name, node,
                              render(template, {**variables, "item": it, "index": str(i + 1)}), f"{name}[{i + 1}]")
                    for i, it in enumerate(items)]
            outs = [f.result() for f in futs]
        return "\n\n".join(f"### {name}[{i + 1}]\n{o}" for i, o in enumerate(outs))

    def run(self, task):
        done, gen, rounds, running = set(), Counter(), Counter(), {}
        pool = cf.ThreadPoolExecutor(self.workers)
        try:
            while True:
                busy = {n for n, _ in running.values()}
                for n in self.order:
                    if n not in done and n not in busy and all(d in done for d in self.deps[n]):
                        running[pool.submit(self._exec, n, self._vars(n, task))] = (n, gen[n])
                        busy.add(n)
                if not running:
                    break
                finished, _ = cf.wait(running, return_when=cf.FIRST_COMPLETED)
                for fut in finished:
                    n, g = running.pop(fut)
                    out = fut.result()
                    if g != gen[n]:
                        continue  # invalidated by a loop while running
                    self.outputs[n] = out
                    done.add(n)
                    self._check_loops(n, out, done, gen, rounds)
        except BaseException:
            pool.shutdown(wait=False, cancel_futures=True)
            raise
        pool.shutdown()
        missing = [n for n in self.nodes if n not in done]
        if missing:
            raise GraphError(f"以下節點無法執行: {', '.join(missing)}")
        return self.outputs

    def _check_loops(self, n, out, done, gen, rounds):
        for i, e in enumerate(self.loops):
            if e["from"] != n or not re.search(e["when"], out):
                continue
            limit = e.get("max", 3)
            if rounds[i] >= limit:
                self.rt.event("warn", msg=f"{n} → {e['to']} 已達最大迴圈次數 {limit},繼續往下")
                continue
            rounds[i] += 1
            self.feedback[e["to"]] = (n, out, rounds[i])
            self.rt.event("loop", src=n, dst=e["to"], round=rounds[i], max=limit)
            for m in {e["to"]} | descendants(self.graph, e["to"]):
                done.discard(m)
                gen[m] += 1
            return
