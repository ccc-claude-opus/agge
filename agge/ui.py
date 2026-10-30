"""Terminal output helpers (thread-safe, colour aware)."""
import os
import sys
import threading

LOCK = threading.RLock()
COLOR = sys.stdout.isatty() and not os.environ.get("NO_COLOR")


def _c(code):
    return lambda s: f"\033[{code}m{s}\033[0m" if COLOR else str(s)


dim, bold, red, green, yellow, blue, magenta, cyan = (
    _c("2"), _c("1"), _c("31"), _c("32"), _c("33"), _c("34"), _c("35"), _c("36"))


def out(*args, end="\n"):
    with LOCK:
        print(*args, end=end, flush=True)


def err(msg):
    with LOCK:
        print(red(f"錯誤: {msg}"), file=sys.stderr, flush=True)


def short(text, n=80):
    text = " ".join(str(text).split())
    return text if len(text) <= n else text[: n - 1] + "…"


class EventPrinter:
    """Renders runtime events (graph nodes, tools, delegation) as progress lines."""

    def __init__(self, verbose=False, log=None):
        self.verbose = verbose
        self.log = log

    def __call__(self, kind, **kw):
        if self.log:
            self.log.event(kind, **kw)
        a = kw.get("agent", "")
        if kind == "node_start":
            out(cyan(f"▶ {kw['node']}") + dim(f"  ({a} · {kw.get('model', '')})"))
        elif kind == "node_done":
            out(green(f"✔ {kw['node']}") + dim(f"  {kw['secs']:.1f}s · {kw['chars']} chars"))
            if self.verbose and kw.get("output"):
                out(dim(kw["output"]))
        elif kind == "tool":
            out(dim(f"  ⚙ {a}: {kw['name']} {short(kw.get('detail', ''), 70)}"))
        elif kind == "delegate":
            out(magenta(f"  ↪ {a} → {kw['target']}: ") + dim(short(kw.get("task", ""), 70)))
        elif kind == "delegate_done":
            out(magenta(f"  ↩ {kw['target']} 完成") + dim(f"  ({kw['chars']} chars)"))
        elif kind == "loop":
            out(yellow(f"↺ {kw['src']} → {kw['dst']}  (第 {kw['round']}/{kw['max']} 輪)"))
        elif kind == "warn":
            out(yellow(f"! {kw.get('msg', '')}"))
        elif kind == "llm" and self.verbose:
            out(dim(f"  … {a} 呼叫 {kw.get('model')} (step {kw.get('step')})"))
