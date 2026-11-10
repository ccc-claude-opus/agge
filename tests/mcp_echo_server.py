"""Tiny stdio MCP server used by the tests: tools `echo` and `add`."""
import json
import sys

TOOLS = [
    {"name": "echo", "description": "Echo the given text back.",
     "inputSchema": {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"]}},
    {"name": "add", "description": "Add two numbers.",
     "inputSchema": {"type": "object", "properties": {"a": {"type": "number"}, "b": {"type": "number"}},
                     "required": ["a", "b"]}},
]


def reply(id_, result=None, error=None):
    msg = {"jsonrpc": "2.0", "id": id_}
    msg.update({"error": error} if error else {"result": result})
    print(json.dumps(msg), flush=True)


for line in sys.stdin:
    req = json.loads(line)
    method, id_, params = req.get("method"), req.get("id"), req.get("params") or {}
    if id_ is None:
        continue  # notification
    if method == "initialize":
        reply(id_, {"protocolVersion": "2025-06-18", "capabilities": {"tools": {}},
                    "serverInfo": {"name": "echo", "version": "1"}})
    elif method == "tools/list":
        reply(id_, {"tools": TOOLS})
    elif method == "tools/call":
        args = params.get("arguments", {})
        if params["name"] == "echo":
            reply(id_, {"content": [{"type": "text", "text": args["text"]}]})
        elif params["name"] == "add":
            reply(id_, {"content": [{"type": "text", "text": str(args["a"] + args["b"])}]})
        else:
            reply(id_, {"content": [{"type": "text", "text": "unknown tool"}], "isError": True})
    else:
        reply(id_, error={"code": -32601, "message": "Method not found"})
