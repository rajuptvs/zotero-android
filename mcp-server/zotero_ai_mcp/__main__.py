"""python3 -m zotero_ai_mcp serve | set-admin-password | add-client | revoke | list"""
import argparse
import getpass
import os
import sys

from .server import Server, State

DEFAULT_DATA = os.path.join(os.path.expanduser("~"), ".config", "zotero-ai-mcp")


def api_key(args):
    if args.api_key_file:
        with open(os.path.expanduser(args.api_key_file), encoding="utf-8") as f:
            return f.read().strip()
    key = os.environ.get("ZOTERO_API_KEY")
    if not key:
        sys.exit("Set ZOTERO_API_KEY or pass --api-key-file (a Zotero API key with library and write access).")
    return key


def main():
    ap = argparse.ArgumentParser(prog="zotero_ai_mcp", description="Zotero AI MCP server on the Zotero Web API (works with Zotero closed).")
    ap.add_argument("--data-dir", default=os.environ.get("ZOTERO_AI_MCP_DATA", DEFAULT_DATA))
    sub = ap.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--allowed-host", action="append", default=[], help="extra Host header to accept, e.g. zotero.example.ts.net")
    serve.add_argument("--api-key-file")
    serve.add_argument("--test-hooks", action="store_true", help=argparse.SUPPRESS)
    sub.add_parser("set-admin-password", help="set the password for the approval page")
    add = sub.add_parser("add-client", help="add an agent and print its token (shown once)")
    add.add_argument("--name", required=True)
    add.add_argument("--scope", choices=["read", "write"], default="write")
    rev = sub.add_parser("revoke", help="revoke an agent")
    rev.add_argument("id")
    sub.add_parser("list", help="list agents")
    args = ap.parse_args()
    state = State(args.data_dir)

    if args.command == "set-admin-password":
        pw = getpass.getpass("New admin password: ")
        if len(pw) < 10 or pw != getpass.getpass("Again: "):
            sys.exit("Passwords must match and be at least 10 characters.")
        state.set_admin_password(pw)
        print("Saved.")
    elif args.command == "add-client":
        client, token = state.create_client(args.name, args.scope)
        print(f"Added {client['name']} ({client['id']}, {client['scope']}). Token, shown once:\n\n  {token}\n")
        print(f'  claude mcp add --transport http zotero http://127.0.0.1:8770/mcp --header "Authorization: Bearer {token}"')
    elif args.command == "revoke":
        state.revoke(args.id)
        print("Revoked.")
    elif args.command == "list":
        for c in state.clients():
            print(f"{c['id']}  {c['scope']:5}  auto-approve={c['autoApprove']}  {c['name']}")
    elif args.command == "serve":
        from .library import Library
        from .spec import Spec
        if not state.data.get("admin"):
            sys.exit("Set an admin password first: python3 -m zotero_ai_mcp set-admin-password")
        library = Library(api_key(args))
        library.sync(force=True)
        server = Server(library, Spec(), state, args.host, args.port, args.allowed_host, args.test_hooks)
        print(f"Zotero AI MCP: http://{args.host}:{args.port}/mcp  ·  approvals: http://{args.host}:{args.port}/admin", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
