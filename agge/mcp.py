"""Minimal MCP client over standard input/output for the agge project."""

import atexit
import json
import os
import queue
import subprocess
import threading


class MCPError(Exception):
    """Raised when an MCP operation fails."""


class MCPClient:
    """A small JSON-RPC client for one MCP server process."""

    def __init__(self, name, command, args=(), env=None, cwd=None, timeout=60):
        self.name = name
        self.command = command
        self.args = tuple(args or ())
        self.env = dict(env or {})
        self.cwd = cwd
        self.timeout = timeout
        self.server_info = {}
        self._process = None
        self._reader_thread = None
        self._next_id = 0
        self._write_lock = threading.Lock()
        self._queues_lock = threading.Lock()
        self._pending = {}
        self._tools_cache = None
        self._eof = False

    def start(self):
        """Start the server and complete the MCP initialization handshake."""
        if self._process is not None:
            if self._process.poll() is None:
                return self.server_info
            raise MCPError(f"{self.name}: server process is not running")

        try:
            process = subprocess.Popen(
                [self.command, *self.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                encoding="utf-8",
                bufsize=1,
                env={**os.environ, **(self.env or {})},
                cwd=self.cwd,
            )
        except OSError as exc:
            raise MCPError(f"{self.name}: failed to start server: {exc}") from exc

        self._process = process
        self._reader_thread = threading.Thread(
            target=self._read_loop,
            args=(process,),
            daemon=True,
        )
        self._reader_thread.start()

        try:
            result = self.request(
                "initialize",
                {
                    "protocolVersion": "2025-06-18",
                    "capabilities": {},
                    "clientInfo": {"name": "agge", "version": "0.3.0"},
                },
            )
            self.server_info = result
            self.notify("notifications/initialized")
        except Exception:
            self.close()
            raise
        return self.server_info

    def _read_loop(self, process):
        """Read server messages until stdout reaches EOF."""
        try:
            while True:
                line = process.stdout.readline()
                if line == "":
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    message = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(message, dict):
                    continue

                if (
                    "id" in message
                    and "method" not in message
                    and ("result" in message or "error" in message)
                ):
                    self._route_response(message)
                elif "method" in message and "id" in message:
                    self._answer_request(message)
        except (OSError, ValueError):
            pass
        finally:
            self._fail_pending()

    def _route_response(self, message):
        """Deliver a response to the request waiting for its id."""
        try:
            with self._queues_lock:
                pending = self._pending.pop(message["id"], None)
        except TypeError:
            return
        if pending is not None:
            pending.put(message)

    def _answer_request(self, message):
        """Answer the server requests supported by this minimal client."""
        if message["method"] == "ping":
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "result": {},
            }
        else:
            response = {
                "jsonrpc": "2.0",
                "id": message["id"],
                "error": {"code": -32601, "message": "Method not found"},
            }
        try:
            self._write(response)
        except MCPError:
            pass

    def _fail_pending(self):
        """Wake all pending requests after the connection closes."""
        with self._queues_lock:
            self._eof = True
            pending = list(self._pending.values())
            self._pending.clear()
        for request_queue in pending:
            request_queue.put(None)

    def _write_locked(self, message):
        """Write one message while the caller holds the write lock."""
        process = self._process
        if process is None or process.stdin is None:
            raise MCPError(f"{self.name}: not connected")
        try:
            process.stdin.write(json.dumps(message, ensure_ascii=False) + "\n")
            process.stdin.flush()
        except (OSError, ValueError) as exc:
            raise MCPError(f"{self.name}: failed to write to server: {exc}") from exc

    def _write(self, message):
        """Write one JSON-RPC message to the server."""
        with self._write_lock:
            self._write_locked(message)

    def request(self, method, params=None):
        """Send a JSON-RPC request and wait for its response."""
        request_queue = queue.Queue()
        with self._write_lock:
            with self._queues_lock:
                if self._eof or self._process is None or self._process.poll() is not None:
                    raise MCPError(f"{self.name}: not connected")
                self._next_id += 1
                request_id = self._next_id
                self._pending[request_id] = request_queue

            message = {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
            }
            if params is not None:
                message["params"] = params
            try:
                self._write_locked(message)
            except Exception:
                with self._queues_lock:
                    if self._pending.get(request_id) is request_queue:
                        self._pending.pop(request_id, None)
                request_queue.put(None)
                raise

        try:
            response = request_queue.get(timeout=self.timeout)
        except queue.Empty:
            with self._queues_lock:
                if self._pending.get(request_id) is request_queue:
                    self._pending.pop(request_id, None)
            raise MCPError(
                f"{self.name}: request timed out after {self.timeout} seconds"
            ) from None

        if response is None:
            raise MCPError(f"{self.name}: connection closed")
        if "error" in response:
            error = response["error"]
            message = error.get("message", str(error))
            raise MCPError(f"{self.name}: {message}")
        return response.get("result")

    def notify(self, method, params=None):
        """Send a JSON-RPC notification to the server."""
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self._write(message)

    def list_tools(self):
        """Return all tools exposed by the server, following pagination."""
        if self._tools_cache is not None:
            return self._tools_cache

        tools = []
        cursor = None
        while True:
            params = {"cursor": cursor} if cursor is not None else None
            result = self.request("tools/list", params)
            tools.extend(result.get("tools", []))
            cursor = result.get("nextCursor")
            if not cursor:
                break

        self._tools_cache = tools
        return tools

    def call_tool(self, name, arguments):
        """Call one server tool and return its textual result."""
        result = self.request(
            "tools/call",
            {"name": name, "arguments": arguments or {}},
        )
        content = result.get("content") or []
        parts = []
        has_text = False

        for item in content:
            item_type = item.get("type")
            if item_type == "text":
                has_text = True
                parts.append(item.get("text", ""))
            elif item_type in ("image", "audio"):
                parts.append(f"[{item_type} {item.get('mimeType', '')}]")
            elif item_type == "resource":
                resource = item.get("resource") or {}
                if "text" in resource:
                    parts.append(resource["text"])
                else:
                    parts.append(resource.get("uri", ""))
            else:
                parts.append(json.dumps(item, ensure_ascii=False))

        text = "\n".join(parts)
        if not has_text and "structuredContent" in result:
            text = json.dumps(result["structuredContent"], ensure_ascii=False)
        if result.get("isError"):
            raise MCPError(text)
        return text

    def close(self):
        """Stop the server process; calling this more than once is safe."""
        process = self._process
        if process is None:
            return
        if process.poll() is None:
            process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
        self._fail_pending()


class MCPManager:
    """Lazily manage clients for the configured MCP servers."""

    def __init__(self, servers, cwd=None):
        self.cwd = cwd
        self._servers = {
            name: config
            for name, config in (servers or {}).items()
            if not config.get("disabled", False)
        }
        self._clients = {}
        self._lock = threading.Lock()
        atexit.register(self.close_all)

    def names(self):
        """Return the names of enabled servers."""
        return list(self._servers)

    def get(self, name):
        """Return a started client for one server, starting it if needed."""
        with self._lock:
            if name not in self._servers:
                available = ", ".join(self.names()) or "none"
                raise MCPError(
                    f"unknown MCP server {name!r} (available: {available})"
                )
            client = self._clients.get(name)
            if client is None:
                config = self._servers[name]
                client = MCPClient(
                    name,
                    config.get("command"),
                    config.get("args", ()),
                    config.get("env"),
                    self.cwd,
                )
                self._clients[name] = client
            if client.server_info or client._process is not None:
                if client._process is not None and client._process.poll() is None:
                    return client
            try:
                client.start()
            except Exception:
                if self._clients.get(name) is client:
                    self._clients.pop(name, None)
                raise
            return client

    def call(self, server, tool, arguments):
        """Call a tool on one managed server."""
        return self.get(server).call_tool(tool, arguments)

    def summary(self, max_desc=100):
        """Describe all tools available from the enabled servers."""
        lines = []
        for server in self.names():
            try:
                tools = self.get(server).list_tools()
            except Exception as exc:
                lines.append(f"- {server}: (unavailable: {exc})")
                continue

            for tool in tools:
                description = tool.get("description") or ""
                first_line = description.splitlines()[0] if description else ""
                if max_desc is not None:
                    first_line = first_line[:max_desc]
                schema = tool.get("inputSchema") or {}
                properties = schema.get("properties") or {}
                required = set(schema.get("required") or [])
                args = []
                for arg_name in properties:
                    suffix = "*" if arg_name in required else ""
                    args.append(f"{arg_name}{suffix}")
                lines.append(
                    f"- {server}.{tool.get('name', '')}: {first_line} "
                    f"(args: {', '.join(args)})"
                )
        return "\n".join(lines)

    def close_all(self):
        """Close every client managed by this manager."""
        with self._lock:
            clients = list(self._clients.values())
            self._clients.clear()
        for client in clients:
            try:
                client.close()
            except Exception:
                pass


def load_mcp_json(path):
    """Load server definitions from a Claude-style MCP JSON configuration."""
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)

    loaded = {}
    for name, server in (config.get("mcpServers") or {}).items():
        if not isinstance(server, dict) or "command" not in server:
            continue
        loaded[name] = {
            key: server[key]
            for key in ("command", "args", "env")
            if key in server
        }
    return loaded
