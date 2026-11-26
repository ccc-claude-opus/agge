"""Text-protocol tools.

Agents call tools by writing XML-like tags in their reply, which works with every model
(no native function-calling needed):

    <read_file path="a.py"/>
    <write_file path="a.py">...</write_file>
    <edit_file path="a.py"><old>x</old><new>y</new></edit_file>
    <list_files path="." depth="2"/>
    <search pattern="def main" path="." glob="*.py"/>
    <shell timeout="60">pytest -q</shell>
    <delegate agent="coder">task</delegate>

Results are returned as <tool_result name=... status="ok|error">...</tool_result>.
"""
import fnmatch
import html
import json
import os
import re
import subprocess
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path

from . import ui

TOOL_DOCS = {
    "read_file": '<read_file path="src/app.py"/> — read a file (optional start="1" end="200" for a line range)',
    "write_file": '<write_file path="docs/a.md">full content</write_file> — create/overwrite a file; '
                  'append="true" appends instead',
    "edit_file": '<edit_file path="src/app.py"><old>exact existing text</old><new>replacement</new></edit_file> '
                 '— each <old> must match exactly once; several <old>/<new> pairs allowed',
    "list_files": '<list_files path="." depth="2"/> — show a directory tree',
    "search": '<search pattern="regex" path="." glob="*.py"/> — grep file contents',
    "shell": '<shell timeout="120">command</shell> — run a shell command in the workspace root '
             '(the user may refuse)',
    "delegate": '<delegate agent="name">complete, self-contained brief</delegate> — hand a subtask to a team '
                'member and get its final report; several delegate tags in one reply run in parallel',
    "skill": '<skill name="skill-name"/> — load the full instructions of a skill listed under "Skills" '
             'before doing a task it covers',
    "mcp": '<mcp server="server" tool="tool">{"arg": "value"}</mcp> — call a tool listed under "MCP tools" '
           'with JSON arguments',
}
TOOL_NAMES = tuple(TOOL_DOCS)
SKIP_DIRS = {".git", ".agge", "node_modules", "__pycache__", ".venv", "venv", ".mypy_cache",
             ".pytest_cache", "dist", "build", ".idea", ".DS_Store"}
MAX_RESULT = 30000

_TAG = re.compile(r'<(%s)((?:\s+[\w-]+\s*=\s*"[^"]*")*)\s*(/?)>' % "|".join(TOOL_NAMES))
_NAMES = "|".join(TOOL_NAMES)
ATTR_NAMES = ("path", "pattern", "glob", "timeout", "depth", "start", "end", "append", "name", "agent", "server", "tool")
# Damage some models (MiniMax-M3.1) do to the tags in long contexts, repaired before parsing:
#   ]<]minimax[>[  (or only ]<]minimax[)    a leaked marker of the model's native tool-call format
#   <shell timeout>120">ls</shell>          `="` written as `>`
#   <search pattern>a|b</pattern> path="."/>  an attribute written as a child element
#   shell timeout="120">ls</shell>          the opening `<` missing (also list_files path="."/>)
_GARBAGE = re.compile(r"\]<\][\w-]*\[(?:>\[)?")
_BROKEN_EQ = re.compile(r'(<(?:%s)(?:\s+[\w-]+\s*=\s*"[^"]*")*\s+[\w-]+)>([^"<>\n]*)"' % _NAMES)
_CHILD_ATTR = re.compile(r'(<(?:%s)(?:\s+[\w-]+\s*=\s*"[^"]*")*)\s+(%s)>(.*?)</\2>' % (_NAMES, "|".join(ATTR_NAMES)), re.S)
_MISSING_LT = re.compile(r'(?<![<\w/])(%s)((?:\s+[\w-]+\s*=\s*"[^"]*")*\s*>.*?</\1>)' % _NAMES, re.S)
_MISSING_LT_SELF = re.compile(r'(?<![<\w/])(%s)((?:\s+[\w-]+\s*=\s*"[^"]*")+\s*/>)' % _NAMES)
_OPEN = re.compile(r"<(%s)\b" % _NAMES)
_FENCE = re.compile(r"```.*?(```|\Z)", re.S)
_ATTR = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')
_PAIR = re.compile(r"<old>(.*?)</old>\s*<new>(.*?)</new>", re.S)


class ToolError(Exception):
    pass


@dataclass
class Call:
    name: str
    attrs: dict
    body: str = None
    closed: bool = True


def repair(text):
    """Undo the known kinds of tag damage (see _GARBAGE, _BROKEN_EQ, _CHILD_ATTR)."""
    text = _GARBAGE.sub("", text)
    text = _MISSING_LT.sub(r"<\1\2", text)
    text = _MISSING_LT_SELF.sub(r"<\1\2", text)
    for _ in range(16):
        new = _CHILD_ATTR.sub(lambda m: f'{m.group(1)} {m.group(2)}="{html.escape(m.group(3), quote=True)}"', text)
        new = _BROKEN_EQ.sub(r'\1="\2"', new)
        if new == text:
            return text
        text = new
    return text


def malformed(text):
    """A tool tag outside code fences that parse_calls could not read: the first 200 characters from it."""
    bare = _FENCE.sub("", text)
    m = _OPEN.search(bare)
    return bare[m.start():m.start() + 200] if m else None


def parse_calls(text):
    text = repair(text)
    calls, pos = [], 0
    while True:
        m = _TAG.search(text, pos)
        if not m:
            return calls
        name = m.group(1)
        attrs = {k: html.unescape(v) for k, v in _ATTR.findall(m.group(2))}
        if m.group(3) == "/":
            calls.append(Call(name, attrs))
            pos = m.end()
            continue
        close = f"</{name}>"
        end = text.find(close, m.end())
        if end < 0:
            calls.append(Call(name, attrs, text[m.end():], closed=False))
            return calls
        calls.append(Call(name, attrs, text[m.end():end]))
        pos = end + len(close)


def strip_tool_tags(text):
    """Remove tool tags from a reply (for display / summaries)."""
    text = re.sub(r"<(%s)\b[^>]*/>" % "|".join(TOOL_NAMES), "", text)
    return re.sub(r"<(%s)\b[^>]*>.*?</\1>" % "|".join(TOOL_NAMES), "", text, flags=re.S).strip()


def _body(call, *keys):
    """Tag body, or a fallback attribute (some models write <shell command="ls"/>)."""
    if call.body is not None:
        return call.body
    return next((call.attrs[k] for k in keys if k in call.attrs), "")


def _trim_nl(s):
    if s.startswith("\r\n"):
        s = s[2:]
    elif s.startswith("\n"):
        s = s[1:]
    return s


class Workspace:
    def __init__(self, root, dirs=()):
        self.root = Path(root).expanduser().resolve()
        self.dirs = [self.root]
        for d in dirs:
            self.add(d)

    def add(self, d):
        p = Path(d).expanduser()
        p = (p if p.is_absolute() else self.root / p).resolve()
        if p not in self.dirs:
            self.dirs.append(p)
        return p

    def resolve(self, path):
        if not path:
            raise ToolError("missing path attribute")
        p = Path(path).expanduser()
        p = (p if p.is_absolute() else self.root / p).resolve()
        if not any(p == d or p.is_relative_to(d) for d in self.dirs):
            raise ToolError(f"path {path} is outside the allowed directories: "
                            + ", ".join(map(str, self.dirs)) + " (the user can allow more with /add-dir)")
        return p

    def rel(self, p):
        try:
            return str(Path(p).relative_to(self.root))
        except ValueError:
            return str(p)


class Approver:
    """Asks the user before risky actions. mode: ask | auto | deny"""

    def __init__(self, mode="ask"):
        self.mode = mode
        self.lock = threading.Lock()

    def confirm(self, question):
        if self.mode == "auto":
            return True
        if self.mode == "deny" or not sys.stdin.isatty():
            return False
        with self.lock, ui.LOCK:
            if self.mode == "auto":
                return True
            ans = input(ui.yellow(f"\n? {question}\n  允許? [y]es / [N]o / [a]lways: ")).strip().lower()
        if ans in ("a", "always"):
            self.mode = "auto"
            return True
        return ans in ("y", "yes")


@dataclass
class Context:
    workspace: Workspace
    approver: Approver
    agent: str = "agent"
    allowed: set = field(default_factory=lambda: set(TOOL_NAMES))
    on_event: callable = lambda *a, **k: None
    delegate: callable = None
    changed: set = None
    skills: dict = None
    mcp: object = None

    def touch(self, path):
        if self.changed is not None:
            self.changed.add(self.workspace.rel(path))


def run_calls(calls, ctx):
    """Execute calls in order; delegate calls run in parallel after the others."""
    results = [None] * len(calls)
    deleg = []
    for i, c in enumerate(calls):
        if c.name == "delegate":
            deleg.append(i)
        else:
            results[i] = execute(c, ctx)
    if deleg:
        with ThreadPoolExecutor(max_workers=len(deleg)) as ex:
            futs = {i: ex.submit(execute, calls[i], ctx) for i in deleg}
            for i, f in futs.items():
                results[i] = f.result()
    return "\n\n".join(results)


def execute(call, ctx):
    attrs = "".join(f' {k}="{html.escape(v)}"' for k, v in call.attrs.items())
    try:
        if call.name not in ctx.allowed:
            raise ToolError(f"agent '{ctx.agent}' is not allowed to use {call.name}")
        if not call.closed:
            raise ToolError(f"<{call.name}> was not closed (reply truncated?). "
                            "Write long content in smaller parts with append=\"true\".")
        detail = call.attrs.get("path") or call.attrs.get("agent") or (call.body or "").strip()
        ctx.on_event("tool", agent=ctx.agent, name=call.name, detail=detail)
        out, status = globals()["_" + call.name](call, ctx), "ok"
    except ToolError as e:
        out, status = str(e), "error"
    except Exception as e:  # tool failures are reported back to the model
        out, status = f"{type(e).__name__}: {e}", "error"
    if len(out) > MAX_RESULT:
        out = out[:MAX_RESULT] + f"\n... [truncated, {len(out) - MAX_RESULT} more chars]"
    return f'<tool_result name="{call.name}"{attrs} status="{status}">\n{out}\n</tool_result>'


def _read_file(call, ctx):
    p = ctx.workspace.resolve(call.attrs.get("path"))
    if p.is_dir():
        raise ToolError(f"{call.attrs['path']} is a directory; use list_files")
    if not p.exists():
        raise ToolError(f"{call.attrs['path']} does not exist")
    lines = p.read_text(encoding="utf-8", errors="replace").splitlines(keepends=True)
    start = int(call.attrs.get("start", 1))
    end = int(call.attrs.get("end", len(lines)))
    body = "".join(lines[start - 1:end])
    if start > 1 or end < len(lines):
        return f"(lines {start}-{min(end, len(lines))} of {len(lines)})\n{body}"
    return body


def _write_file(call, ctx):
    p = ctx.workspace.resolve(call.attrs.get("path"))
    content = _trim_nl(_body(call, "content"))
    if content and not content.endswith("\n"):
        content += "\n"
    p.parent.mkdir(parents=True, exist_ok=True)
    append = call.attrs.get("append", "").lower() == "true"
    with open(p, "a" if append else "w", encoding="utf-8") as f:
        f.write(content)
    ctx.touch(p)
    return f"{'appended' if append else 'wrote'} {len(content)} chars to {ctx.workspace.rel(p)}"


def _edit_file(call, ctx):
    p = ctx.workspace.resolve(call.attrs.get("path"))
    if not p.exists():
        raise ToolError(f"{call.attrs['path']} does not exist; use write_file to create it")
    text = p.read_text(encoding="utf-8")
    pairs = _PAIR.findall(call.body or "")
    if not pairs:
        raise ToolError("edit_file needs <old>...</old><new>...</new> pairs")
    for old, new in pairs:
        old, new = _trim_nl(old), _trim_nl(new)
        if old.endswith("\n") and new.endswith("\n"):
            old, new = old[:-1], new[:-1]
        n = text.count(old)
        if n == 0:
            raise ToolError(f"<old> text not found in {call.attrs['path']} (no change made); "
                            f"read the file and copy the text exactly:\n{old[:300]}")
        if n > 1:
            raise ToolError(f"<old> text occurs {n} times in {call.attrs['path']}; include more context")
        text = text.replace(old, new, 1)
    p.write_text(text, encoding="utf-8")
    ctx.touch(p)
    return f"applied {len(pairs)} edit(s) to {ctx.workspace.rel(p)}"


def _walk(root):
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIRS)
        for f in sorted(filenames):
            if f not in SKIP_DIRS:
                yield Path(dirpath) / f


def _list_files(call, ctx):
    root = ctx.workspace.resolve(call.attrs.get("path", "."))
    if not root.is_dir():
        raise ToolError(f"{call.attrs.get('path')} is not a directory")
    depth = int(call.attrs.get("depth", 2))
    lines = []

    def rec(d, level):
        for e in sorted(d.iterdir(), key=lambda x: (x.is_file(), x.name)):
            if e.name in SKIP_DIRS:
                continue
            if len(lines) >= 500:
                return
            lines.append("  " * level + e.name + ("/" if e.is_dir() else ""))
            if e.is_dir() and level + 1 < depth:
                rec(e, level + 1)

    rec(root, 0)
    return "\n".join(lines) or "(empty directory)"


def _search(call, ctx):
    try:
        rx = re.compile(call.attrs.get("pattern", ""))
    except re.error as e:
        raise ToolError(f"bad regex: {e}")
    root = ctx.workspace.resolve(call.attrs.get("path", "."))
    glob = call.attrs.get("glob")
    hits = []
    for f in ([root] if root.is_file() else _walk(root)):
        if glob and not fnmatch.fnmatch(f.name, glob):
            continue
        try:
            text = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if rx.search(line):
                hits.append(f"{ctx.workspace.rel(f)}:{i}: {line.strip()[:200]}")
                if len(hits) >= 200:
                    return "\n".join(hits) + "\n... (more matches omitted)"
    return "\n".join(hits) or "no matches"


def _shell(call, ctx):
    cmd = _body(call, "command", "cmd").strip()
    if not cmd:
        raise ToolError("empty command")
    if not ctx.approver.confirm(f"{ctx.agent} 要執行 shell(在 {ctx.workspace.root}):\n  {cmd}"):
        raise ToolError("the user denied this command")
    code, out, timed_out = run_shell(cmd, ctx.workspace.root, int(call.attrs.get("timeout", 120)))
    if timed_out:
        raise ToolError("command timed out (its processes were killed)\n" + out)
    return f"exit code {code}\n{out}".rstrip()


SHELL_OUTPUT_LIMIT = 100_000


def run_shell(cmd, cwd, timeout, limit=SHELL_OUTPUT_LIMIT):
    """Runs a shell command in its own process group.

    On timeout the whole group is killed (subprocess.run would only kill the
    shell, leaving e.g. a runaway test binary running and eating memory), and
    any background processes it left behind are killed when it finishes.
    Output goes to a temporary file so a flood of output cannot fill memory;
    only the first `limit` bytes are returned. Returns (code, output, timed_out).
    """
    import signal
    import tempfile

    with tempfile.TemporaryFile() as buf:
        p = subprocess.Popen(cmd, shell=True, cwd=cwd, stdout=buf, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True)
        timed_out = False
        try:
            code = p.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            code = None
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            pass
        if code is None:
            code = p.wait()
        size = buf.seek(0, os.SEEK_END)
        buf.seek(0)
        data = buf.read(limit).decode("utf-8", "replace")
        if size > limit:
            data += f"\n... (output truncated: {size} bytes, showing the first {limit})"
    return code, data, timed_out


def _skill(call, ctx):
    name = call.attrs.get("name") or (call.body or "").strip()
    skill = (ctx.skills or {}).get(name)
    if not skill:
        raise ToolError(f"unknown skill '{name}'; available: {', '.join(sorted(ctx.skills or {})) or 'none'}")
    return skill.render()


def _mcp(call, ctx):
    from .mcp import MCPError
    if not ctx.mcp:
        raise ToolError("no MCP servers are configured")
    server, tool = call.attrs.get("server", ""), call.attrs.get("tool", "")
    raw = (call.body or "").strip() or call.attrs.get("arguments", "") or "{}"
    try:
        args = json.loads(raw)
    except json.JSONDecodeError as e:
        raise ToolError(f"arguments must be a JSON object: {e}")
    try:
        return ctx.mcp.call(server, tool, args)
    except MCPError as e:
        raise ToolError(str(e))


def _delegate(call, ctx):
    if not ctx.delegate:
        raise ToolError("delegation is not available here")
    task = _body(call, "task", "content").strip()
    if not task:
        raise ToolError("empty delegate task")
    return ctx.delegate(call.attrs.get("agent", ""), task)
