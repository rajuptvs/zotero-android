"""python3 -m zotero_ai_mcp serve | set-admin-password | add-client | revoke | list"""
import argparse
import getpass
import os
import re
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


def webdav(args):
    if not args.webdav_url:
        return None
    if not args.webdav_username or not args.webdav_password_file:
        sys.exit("--webdav-url needs --webdav-username and --webdav-password-file.")
    with open(os.path.expanduser(args.webdav_password_file), encoding="utf-8") as f:
        password = f.read().strip()
    if args.webdav_url.lower().startswith("http://") and not re.match(r"^http://(127\.0\.0\.1|localhost|\[::1\])[:/]", args.webdav_url, re.I):
        print("Warning: the WebDAV address is plain http://, so the password is sent unencrypted.", file=sys.stderr)
    return {"url": args.webdav_url, "username": args.webdav_username, "password": password}


def read_password(path):
    with open(os.path.expanduser(path), encoding="utf-8") as f:
        pw = f.read().strip()
    if len(pw) < 10:
        sys.exit("The admin password must be at least 10 characters.")
    return pw


def main():
    ap = argparse.ArgumentParser(prog="zotero_ai_mcp", description="Zotero AI MCP server on the Zotero Web API (works with Zotero closed).")
    ap.add_argument("--data-dir", default=os.environ.get("ZOTERO_AI_MCP_DATA", DEFAULT_DATA))
    sub = ap.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the server")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8770)
    serve.add_argument("--allowed-host", action="append", default=[], help="extra Host header to accept, e.g. zotero.example.ts.net")
    serve.add_argument("--api-key-file")
    serve.add_argument("--webdav-url", help="your WebDAV address as entered in Zotero's sync settings, if Zotero keeps your files there")
    serve.add_argument("--webdav-username")
    serve.add_argument("--webdav-password-file")
    serve.add_argument("--public-url", help="the https address clients reach this server at (behind a proxy), e.g. https://zotero.example.com")
    serve.add_argument("--oauth", action="store_true", help="let remote MCP clients (claude.ai, ChatGPT, …) connect with OAuth; needs --public-url")
    serve.add_argument("--admin-password-file", help="set the admin password from this file at startup (for containers)")
    serve.add_argument("--semantic", action="store_true",
                       help="turn on zotero_semantic_search: downloads the ~346 MB model into the data directory (needs pip install onnxruntime tokenizers numpy)")
    serve.add_argument("--test-hooks", action="store_true", help=argparse.SUPPRESS)
    pw = sub.add_parser("set-admin-password", help="set the password for the approval page")
    pw.add_argument("--password-file", help="read it from this file instead of asking")
    add = sub.add_parser("add-client", help="add an agent and print its token (shown once)")
    add.add_argument("--name", required=True)
    add.add_argument("--scope", choices=["read", "write"], default="write")
    rev = sub.add_parser("revoke", help="revoke an agent")
    rev.add_argument("id")
    sub.add_parser("list", help="list agents")
    args = ap.parse_args()
    state = State(args.data_dir)

    if args.command == "set-admin-password":
        if args.password_file:
            pw = read_password(args.password_file)
        else:
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
            via = f"  (OAuth, to {c['oauth']['redirectHost']})" if c.get("oauth") else ""
            print(f"{c['id']}  {c['scope']:5}  auto-approve={c['autoApprove']}  {c['name']}{via}")
    elif args.command == "serve":
        from .library import Library
        from .oauth import normalize_public_url
        from .spec import Spec
        public_url = None
        if args.public_url:
            try:
                public_url = normalize_public_url(args.public_url)
            except ValueError as e:
                sys.exit(str(e))
        if args.oauth and not public_url:
            sys.exit("--oauth needs --public-url (the https address clients use).")
        if args.admin_password_file:
            pw = read_password(args.admin_password_file)
            if not state.check_admin_password(pw):
                state.set_admin_password(pw)
        if not state.data.get("admin"):
            sys.exit("Set an admin password first: python3 -m zotero_ai_mcp set-admin-password")
        from .pdf import Files
        from .semantic import Semantic
        library = Library(api_key(args))
        library.files = Files(library, os.path.join(args.data_dir, "files"), webdav(args))
        library.semantic = Semantic(args.data_dir, enabled=args.semantic)
        library.sync(force=True)
        server = Server(library, Spec(), state, args.host, args.port, args.allowed_host, args.test_hooks, public_url, args.oauth)
        print(f"Zotero AI MCP: http://{args.host}:{args.port}/mcp  ·  approvals: http://{args.host}:{args.port}/admin", flush=True)
        if args.oauth:
            print(f"OAuth on: remote clients connect to {public_url}/mcp", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
