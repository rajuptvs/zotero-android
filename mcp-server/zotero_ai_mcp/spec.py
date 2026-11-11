"""ai/spec for the standalone server: tool definitions, the MCP catalog, argument checks, the approval
rule and approval texts. Same rules as desktop-plugin/src/content/spec.js and tools.js describeWrite.
"""
import copy
import json
import os

DEFAULT_SPEC_DIR = os.path.join(os.path.dirname(__file__), "..", "..", "ai", "spec")


class Spec:
    def __init__(self, spec_dir=None):
        spec_dir = spec_dir or os.environ.get("ZOTERO_AI_SPEC_DIR") or DEFAULT_SPEC_DIR
        with open(os.path.join(spec_dir, "tools.json"), encoding="utf-8") as f:
            self.tools = json.load(f)["tools"]
        with open(os.path.join(spec_dir, "prompts.json"), encoding="utf-8") as f:
            self.prompts = json.load(f)

    def tool(self, name):
        return next((t for t in self.tools if t["name"] == name), None)

    def is_write(self, name):
        return (self.tool(name) or {}).get("x-write") is True

    def should_ask(self, name, auto_approve):
        tool = self.tool(name)
        if not tool or tool.get("x-write") is not True:
            return False
        return tool.get("x-always-ask") is True or not auto_approve

    def mcp_tools(self, scope):
        out = []
        for t in self.tools:
            if scope != "write" and (t.get("x-write") or t.get("x-mcp-only")):
                continue
            schema = copy.deepcopy(t.get("parameters") or {"type": "object", "properties": {}})
            props = schema.get("properties", {})

            def nullable(k):
                ty = props.get(k, {}).get("type", [])
                return "null" in (ty if isinstance(ty, list) else [ty])

            schema["required"] = [k for k in schema.get("required", []) if not nullable(k)]
            out.append({"name": t["name"], "description": t["description"], "inputSchema": schema,
                        "annotations": {"readOnlyHint": t.get("x-write") is not True, "destructiveHint": t.get("x-always-ask") is True}})
        return out

    def validate_args(self, name, args):
        tool = self.tool(name)
        invalid = lambda message: {"error": "invalid_argument", "message": message}
        if not tool:
            return invalid(f"Unknown tool {name}")
        if not isinstance(args, dict):
            return invalid("Arguments must be an object.")
        schema = tool.get("parameters") or {"properties": {}}
        props = schema.get("properties", {})
        for key in args:
            if key not in props:
                return invalid(f"Unknown argument {key}.")
        out = {}
        for key, prop in props.items():
            types = prop.get("type", [])
            types = types if isinstance(types, list) else [types]
            if key not in args:
                if "null" in types:
                    out[key] = None
                    continue
                if key in schema.get("required", []):
                    return invalid(f"Missing {key}.")
                continue
            if not self._matches(args[key], types, prop):
                return invalid(f"{key} must be {' or '.join(types)}.")
            value = args[key]
            # JSON has one number type: 5.0 is 5, as in JavaScript.
            out[key] = int(value) if isinstance(value, float) and value.is_integer() else value
        return {"args": out}

    def _matches(self, value, types, prop):
        for t in types:
            if t == "null" and value is None:
                return True
            if t == "string" and isinstance(value, str):
                return True
            if t == "boolean" and isinstance(value, bool):
                return True
            if t == "integer" and isinstance(value, int) and not isinstance(value, bool):
                return True
            if t == "integer" and isinstance(value, float) and value.is_integer():
                return True
            if t == "number" and isinstance(value, (int, float)) and not isinstance(value, bool):
                return True
            if t == "object" and isinstance(value, dict):
                return True
            if t == "array" and isinstance(value, list):
                item_types = (prop.get("items") or {}).get("type", [])
                item_types = item_types if isinstance(item_types, list) else [item_types]
                if not item_types or all(self._matches(v, item_types, prop.get("items")) for v in value):
                    return True
        return False

    def describe_write(self, library, name, args):
        """One line describing a write, equal to the chat clients' approval text."""
        def titles(keys):
            names = []
            for key in keys or []:
                item = library.items.get(key)
                names.append(f"“{library.title(item)}”" if item else key)
            return ", ".join(names) if len(names) <= 3 else ", ".join(names[:3]) + f" and {len(names) - 3} more"

        def collections(keys):
            return ", ".join(f"“{library.collections[k]['name']}”" if k in library.collections else k for k in keys or [])

        def sentence(parts):
            t = " and ".join(parts)
            return t[:1].upper() + t[1:]

        library.sync()
        with library.lock:
            if name == "zotero_batch_update_tags":
                parts = []
                add, remove = args.get("add_tags") or [], args.get("remove_tags") or []
                if add:
                    parts.append(f"Add tag{'s' if len(add) > 1 else ''} {', '.join(add)}")
                if remove:
                    parts.append(f"remove {', '.join(remove)}")
                return sentence(parts) + f" on {titles(args.get('item_keys'))}"
            if name == "zotero_set_item_collections":
                parts = []
                if args.get("add_to"):
                    parts.append(f"Add to {collections(args['add_to'])}")
                if args.get("remove_from"):
                    parts.append(f"remove from {collections(args['remove_from'])}")
                return sentence(parts) + f": {titles(args.get('item_keys'))}"
            if name == "zotero_create_collection":
                parent = args.get("parent_collection_key")
                return f"Create collection “{args.get('name')}”" + (f" inside {collections([parent])}" if parent else "")
            if name == "zotero_create_note":
                parent = args.get("parent_item_key")
                return f"Create note “{args.get('title')}”" + (f" on {titles([parent])}" if parent else " (standalone)")
            if name == "zotero_add_item_by_identifier":
                coll = args.get("collection_key")
                return f"Add {args.get('identifier')} to the library" + (f" in {collections([coll])}" if coll else "")
            if name == "zotero_trash_items":
                return f"Move to trash: {titles(args.get('item_keys'))}"
        return name
