"""LLM providers.

All providers expose the same interface:  LLM(cfg).chat(system, messages, on_token, on_think) -> str

- echo       : offline test provider, replies with the last user message
- openai     : any OpenAI-compatible /chat/completions endpoint
               (openai, nvidia, openrouter, deepseek, groq, gemini, ollama, lmstudio, custom base_url)
- anthropic  : Anthropic Messages API

Only the standard library is used (urllib), so agge has no hard dependencies.
"""
import json
import os
import time
import urllib.error
import urllib.request

PROVIDERS = {
    "echo":       {"kind": "echo"},
    "openai":     {"kind": "openai", "base_url": "https://api.openai.com/v1", "env": "OPENAI_API_KEY"},
    "nvidia":     {"kind": "openai", "base_url": "https://integrate.api.nvidia.com/v1", "env": "NVIDIA_API_KEY"},
    "openrouter": {"kind": "openai", "base_url": "https://openrouter.ai/api/v1", "env": "OPENROUTER_API_KEY"},
    "deepseek":   {"kind": "openai", "base_url": "https://api.deepseek.com/v1", "env": "DEEPSEEK_API_KEY"},
    "groq":       {"kind": "openai", "base_url": "https://api.groq.com/openai/v1", "env": "GROQ_API_KEY"},
    "gemini":     {"kind": "openai", "base_url": "https://generativelanguage.googleapis.com/v1beta/openai", "env": "GEMINI_API_KEY"},
    "ollama":     {"kind": "openai", "base_url": "http://localhost:11434/v1", "env": None},
    "lmstudio":   {"kind": "openai", "base_url": "http://localhost:1234/v1", "env": None},
    "anthropic":  {"kind": "anthropic", "base_url": "https://api.anthropic.com/v1", "env": "ANTHROPIC_API_KEY"},
}

RETRY_CODES = (408, 429, 500, 502, 503, 504)


class LLMError(RuntimeError):
    pass


class LLM:
    def __init__(self, cfg):
        self.cfg = dict(cfg)
        self.provider = cfg.get("provider", "openai")
        preset = PROVIDERS.get(self.provider, {})
        self.kind = cfg.get("kind") or preset.get("kind") or "openai"
        self.model = cfg.get("model", "")
        self.base_url = (cfg.get("base_url") or preset.get("base_url") or "").rstrip("/")
        self.env = cfg.get("api_key_env", preset.get("env"))
        self.api_key = cfg.get("api_key") or (os.environ.get(self.env) if self.env else None)
        self.timeout = cfg.get("timeout", 600)
        if self.kind != "echo" and not self.base_url:
            raise LLMError(f"provider '{self.provider}' 需要設定 base_url")

    @property
    def label(self):
        return f"{self.provider}:{self.model}"

    def chat(self, system, messages, on_token=None, on_think=None):
        if self.kind == "echo":
            text = "[echo] " + (messages[-1]["content"] if messages else "")
            if on_token:
                on_token(text)
            return text
        if self.env and not self.api_key:
            raise LLMError(f"{self.label}: 缺少 API key,請設定環境變數 {self.env}")
        fn = self._openai if self.kind == "openai" else self._anthropic
        for attempt in range(4):
            try:
                return fn(system, messages, on_token, on_think)
            except urllib.error.HTTPError as e:
                body = e.read().decode("utf-8", "replace")[:2000]
                if e.code in RETRY_CODES and attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise LLMError(f"{self.label} HTTP {e.code}: {body}") from None
            except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
                if attempt < 3:
                    time.sleep(2 ** attempt * 2)
                    continue
                raise LLMError(f"{self.label} 連線失敗: {e}") from None

    def _post(self, url, headers, body):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers, method="POST")
        return urllib.request.urlopen(req, timeout=self.timeout)

    @staticmethod
    def _sse(resp):
        for raw in resp:
            line = raw.decode("utf-8", "replace").strip()
            if not line.startswith("data:"):
                continue
            data = line[5:].strip()
            if data == "[DONE]":
                return
            try:
                yield json.loads(data)
            except json.JSONDecodeError:
                continue

    def _openai(self, system, messages, on_token, on_think):
        msgs = ([{"role": "system", "content": system}] if system else []) + messages
        body = {"model": self.model, "messages": msgs, "stream": bool(on_token or on_think)}
        for k in ("temperature", "top_p", "max_tokens"):
            if k in self.cfg:
                body[k] = self.cfg[k]
        body.update(self.cfg.get("extra", {}))
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        with self._post(f"{self.base_url}/chat/completions", headers, body) as resp:
            if not body["stream"]:
                data = json.load(resp)
                return (data["choices"][0]["message"].get("content") or "")
            parts = []
            for ev in self._sse(resp):
                choices = ev.get("choices") or []
                if not choices:
                    continue
                delta = choices[0].get("delta") or {}
                think = delta.get("reasoning_content") or delta.get("reasoning")
                if think and on_think:
                    on_think(think)
                text = delta.get("content")
                if text:
                    parts.append(text)
                    if on_token:
                        on_token(text)
            return "".join(parts)

    def _anthropic(self, system, messages, on_token, on_think):
        body = {"model": self.model, "messages": messages,
                "max_tokens": self.cfg.get("max_tokens", 8192), "stream": bool(on_token or on_think)}
        if system:
            body["system"] = system
        if "temperature" in self.cfg:
            body["temperature"] = self.cfg["temperature"]
        body.update(self.cfg.get("extra", {}))
        headers = {"Content-Type": "application/json", "x-api-key": self.api_key or "",
                   "anthropic-version": "2023-06-01"}
        with self._post(f"{self.base_url}/messages", headers, body) as resp:
            if not body["stream"]:
                data = json.load(resp)
                return "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
            parts = []
            for ev in self._sse(resp):
                if ev.get("type") == "error":
                    raise LLMError(f"{self.label}: {ev.get('error')}")
                if ev.get("type") != "content_block_delta":
                    continue
                delta = ev.get("delta", {})
                if delta.get("type") == "text_delta":
                    parts.append(delta["text"])
                    if on_token:
                        on_token(delta["text"])
                elif delta.get("type") == "thinking_delta" and on_think:
                    on_think(delta.get("thinking", ""))
            return "".join(parts)


def parse_spec(spec):
    """'nvidia:z-ai/glm-5.3-flash' -> {'provider': 'nvidia', 'model': 'z-ai/glm-5.3-flash'}"""
    provider, _, model = spec.partition(":")
    if provider not in PROVIDERS:
        return None
    return {"provider": provider, "model": model or provider}
