#!/usr/bin/env python3
"""Cross-device conformance for the Zotero AI tools (ai/spec).

Runs the same tool calls against two clients, diffs the JSON results, and checks that writes made
on one device show up on the other after a Zotero sync.

Drivers:
  android  - debug build's test provider over adb (content call ... --method tool / sync)
  desktop  - desktop plugin's dev endpoints on Zotero's local server (/zotero-ai/test/...)

  python3 ai/conformance/run.py --serial emulator-5554 --desktop http://127.0.0.1:23119
"""
import argparse
import base64
import json
import os
import random
import string
import subprocess
import sys
import urllib.request


class Android:
    name = "android"

    def __init__(self, serial, package="org.zotero.android.debug"):
        self.serial = serial
        self.uri = f"content://{package}.aitest"

    def _call(self, method, arg=None, args=None, timeout=300):
        cmd = ["adb", "-s", self.serial, "shell", "content", "call", "--uri", self.uri, "--method", method]
        if arg:
            cmd += ["--arg", arg]
        if args is not None:
            encoded = base64.b64encode(json.dumps(args).encode()).decode()
            cmd += ["--extra", f"args64:s:{encoded}"]
        out = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout).stdout.strip()
        prefix = "Result: Bundle[{result="
        if not out.startswith(prefix):
            raise RuntimeError(f"android {method} {arg}: {out[:300]}")
        return json.loads(out[len(prefix):-2])

    def tool(self, name, args):
        return self._call("tool", name, args)

    def sync(self):
        return self._call("sync")

    def describe(self, name, args):
        return self._call("describe", name, args)

    def notes(self, html):
        return self._call("notes", None, {"html": html})

    def markdown(self, text):
        return self._call("markdown", None, {"markdown": text})["html"]


class Desktop:
    name = "desktop"

    def __init__(self, base, token_file):
        self.base = base.rstrip("/")
        with open(os.path.expanduser(token_file)) as f:
            self.token = f.read().strip()

    def _post(self, path, body, timeout=300):
        req = urllib.request.Request(
            self.base + path, data=json.dumps(body).encode(),
            headers={"Content-Type": "application/json", "X-Zotero-AI-Test-Token": self.token}, method="POST")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode())

    def tool(self, name, args):
        return self._post("/zotero-ai/test/tool", {"name": name, "args": args})

    def sync(self):
        return self._post("/zotero-ai/test/sync", {})

    def describe(self, name, args):
        return self._post("/zotero-ai/test/describe", {"name": name, "args": args})

    def notes(self, html):
        return self._post("/zotero-ai/test/notes", {"html": html})

    def markdown(self, text):
        return self._post("/zotero-ai/test/markdown", {"markdown": text})["html"]

    def chat_notes(self):
        """HTML of the real conversation notes in the library, to read back on both devices."""
        code = ("let items = await Zotero.Items.getAll(Zotero.Libraries.userLibraryID, false, false);"
                "return items.filter(i => i.isNote() && !i.deleted && ZoteroAI.Notes.isChatNote(i.getNoteTitle(), i.getNote()))"
                ".map(i => ({ name: 'real note ' + i.key, html: i.getNote() }));")
        return self._post("/zotero-ai/test/eval", {"code": code})["result"]


def normalize(value):
    """Collapse whitespace in long free text (HTML→text conversion differs slightly by platform)."""
    if isinstance(value, dict):
        return {k: normalize(v) for k, v in value.items()}
    if isinstance(value, list):
        return [normalize(v) for v in value]
    if isinstance(value, str):
        return " ".join(value.split())
    return value


def diff(a, b, path=""):
    out = []
    if type(a) != type(b):
        return [f"{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}"]
    if isinstance(a, dict):
        for k in sorted(set(a) | set(b)):
            if k not in a or k not in b:
                out.append(f"{path}.{k}: only on {'android' if k in a else 'desktop'}")
            else:
                out += diff(a[k], b[k], f"{path}.{k}")
    elif isinstance(a, list):
        if len(a) != len(b):
            out.append(f"{path}: length {len(a)} != {len(b)}")
        for i, (x, y) in enumerate(zip(a, b)):
            out += diff(x, y, f"{path}[{i}]")
    elif a != b:
        out.append(f"{path}: {json.dumps(a)[:120]} != {json.dumps(b)[:120]}")
    return out


READ_CALLS = [
    ("zotero_search_items", {"query": "attention", "tag": None, "collection_key": None, "limit": None}),
    ("zotero_search_items", {"query": "reasoning 2025", "tag": None, "collection_key": None, "limit": 10}),
    ("zotero_search_items", {"query": "", "tag": "harness", "collection_key": None, "limit": 50}),
    ("zotero_get_item_metadata", {"item_key": "2MIWXEWJ"}),
    ("zotero_get_item_metadata", {"item_key": "4WS8XRLA"}),
    ("zotero_get_item_metadata", {"item_key": "NOPE0000"}),
    ("zotero_get_item_fulltext", {"item_key": "2MIWXEWJ", "max_chars": 2000}),
    ("zotero_get_collections", {}),
    ("zotero_get_tags", {"limit": 30}),
    ("zotero_get_recent", {"limit": 5}),
    ("zotero_get_notes", {"item_key": "4WS8XRLA", "query": None, "limit": None}),
    ("zotero_get_notes", {"item_key": None, "query": "transformer", "limit": 5}),
    ("zotero_get_annotations", {"item_key": "4WS8XRLA"}),
]


def describe_calls(collection_key):
    """Write calls whose approval text must read the same on both devices."""
    five = ["2MIWXEWJ", "7KLQ74ZI", "4GX3L9K7", "GITEQ5QS", "W74L85JR"]
    return [
        ("zotero_batch_update_tags", {"item_keys": five[:1], "add_tags": ["to-read"], "remove_tags": []}),
        ("zotero_batch_update_tags", {"item_keys": five[:2], "add_tags": [], "remove_tags": ["to-read"]}),
        ("zotero_batch_update_tags", {"item_keys": five, "add_tags": ["a", "b"], "remove_tags": ["c"]}),
        ("zotero_set_item_collections", {"item_keys": five[:1], "add_to": [collection_key], "remove_from": []}),
        ("zotero_set_item_collections", {"item_keys": five[:1], "add_to": [], "remove_from": [collection_key]}),
        ("zotero_create_collection", {"name": "Transformers", "parent_collection_key": None}),
        ("zotero_create_collection", {"name": "Transformers", "parent_collection_key": collection_key}),
        ("zotero_create_note", {"parent_item_key": None, "title": "Reading list", "content": "x", "tags": []}),
        ("zotero_create_note", {"parent_item_key": "2MIWXEWJ", "title": "Summary", "content": "x", "tags": []}),
        ("zotero_add_item_by_identifier", {"identifier": "10.48550/arXiv.1706.03762", "collection_key": None}),
        ("zotero_add_item_by_identifier", {"identifier": "arXiv:1810.04805", "collection_key": collection_key}),
        ("zotero_trash_items", {"item_keys": five[:4]}),
        ("zotero_trash_items", {"item_keys": ["NOPE0000"]}),
    ]


MATH_SAMPLES = [
    ("inline and money", "Scale by $1/\\sqrt{d_k}$ and $x_i * y_i$, costs $5 and $10."),
    ("display block", "Before.\n\n$$\\mathrm{softmax}\\left(\\frac{QK^\\top}{\\sqrt{d_k}}\\right)V$$\n\nAfter."),
    ("bracket forms", "Inline \\(a<b\\) and\n\n\\[ E = mc^2 \\]"),
    ("code is left alone", "`$not math$` and\n\n```\n$also_not$\n```\n\nbut $y$ is."),
    ("math in a table", "| a | b |\n|---|---|\n| $[0.73,\\,0.27]$ | x |"),
]


def check(results, label, ok, detail=""):
    results.append((label, ok, detail))
    print(("PASS " if ok else "FAIL ") + label + (f"  — {detail}" if detail and not ok else ""))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--serial", default="emulator-5554")
    ap.add_argument("--desktop", default="http://127.0.0.1:23119")
    ap.add_argument("--desktop-token-file", default="~/.zotero-ai-test/test-token",
                    help="per-launch secret written by desktop-plugin/scripts/dev-zotero.sh")
    ap.add_argument("--skip-writes", action="store_true")
    opts = ap.parse_args()
    android, desktop = Android(opts.serial), Desktop(opts.desktop, opts.desktop_token_file)
    results = []

    print("== sync both")
    check(results, "android sync", android.sync().get("synced") is True)
    check(results, "desktop sync", "error" not in desktop.sync())

    print("== read parity")
    collections = android.tool("zotero_get_collections", {})["collections"]
    calls = list(READ_CALLS)
    for c in collections[:3]:
        calls.append(("zotero_get_collection_items", {"collection_key": c["key"], "limit": None}))
    for name, args in calls:
        a, d = normalize(android.tool(name, args)), normalize(desktop.tool(name, args))
        problems = diff(a, d)
        check(results, f"{name} {json.dumps(args)}", not problems, "; ".join(problems[:5]))

    print("== approval text parity")
    for name, args in describe_calls(collections[0]["key"]):
        a, d = android.describe(name, args), desktop.describe(name, args)
        check(results, f"describe {name} {json.dumps(args)[:80]}", a == d, f"{a} != {d}")

    print("== conversation notes read back the same")
    with open(__file__.rsplit("/", 1)[0] + "/notes.json") as f:
        fixtures = json.load(f)
    for fixture in fixtures + desktop.chat_notes():
        a, d = android.notes(fixture["html"]), desktop.notes(fixture["html"])
        problems = diff(a, d)
        check(results, f"notes: {fixture['name']}", not problems, "; ".join(problems[:3]))
    expected = {f["name"]: f for f in fixtures}
    a = android.notes(expected["library note, attribute stripped by note editor"]["html"])
    check(results, "stripped library note still a library chat", a["isLibraryChat"] is True)
    a = android.notes(expected["ordinary note"]["html"])
    check(results, "ordinary note isn't a chat", a["isLibraryChat"] is False and a["turns"] == [], json.dumps(a))
    a = android.notes(expected["reader note"]["html"])
    table = [t for t in a["turns"] if t["role"] == "assistant"][0]["text"]
    check(results, "table rows stay together, paragraph after it doesn't",
          "| With scaling | $[2,1]$ | $[0.73, 0.27]$ |\n\nThe output" in table and "|---|---|---|\n| Without" in table, table[:300])
    check(results, "code fence lines stay together", "```python\nscores = q @ k.T" in table, table[-300:])

    print("== saved answers: math markup")
    # The two Markdown renderers differ in whitespace, so compare the math and the code they keep.
    import re
    marks = lambda html: re.findall(r'<(?:span|pre) class="math">.*?</(?:span|pre)>|<code>.*?</code>', html, re.S)
    for name, text in MATH_SAMPLES:
        a, d = marks(android.markdown(text)), marks(desktop.markdown(text))
        check(results, f"math: {name}", a == d and len(a) > 0, f"{a} != {d}")

    if not opts.skip_writes:
        suffix = "".join(random.choices(string.ascii_lowercase, k=5))
        tag_a, tag_d = f"zz-ai-conf-{suffix}-android", f"zz-ai-conf-{suffix}-desktop"
        # A throwaway item created for these tests (BERT, tagged zz-ai-test).
        item = android.tool("zotero_search_items", {"query": "", "tag": "zz-ai-test", "collection_key": None, "limit": 5})["items"]
        if not item:
            print("no zz-ai-test item to write to; skipping write checks")
        else:
            key = item[0]["key"]
            print(f"== writes on {key}")
            coll = android.tool("zotero_create_collection", {"name": f"zz-ai-conf {suffix}", "parent_collection_key": None})
            check(results, "android create_collection", "key" in coll, json.dumps(coll))
            r = android.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [tag_a], "remove_tags": []})
            check(results, "android add tag", r == {"updated": [key]}, json.dumps(r))
            r = android.tool("zotero_set_item_collections", {"item_keys": [key], "add_to": [coll["key"]], "remove_from": []})
            check(results, "android add to collection", r == {"updated": [key]}, json.dumps(r))
            note = android.tool("zotero_create_note", {"parent_item_key": key, "title": f"zz-ai-conf note {suffix}",
                                                       "content": "Made on **Android**.\n\n- point one\n- point two", "tags": [tag_a]})
            check(results, "android create_note", "key" in note, json.dumps(note))
            android.sync(); desktop.sync()
            meta = desktop.tool("zotero_get_item_metadata", {"item_key": key})
            check(results, "desktop sees android tag", tag_a in meta.get("tags", []), json.dumps(meta.get("tags")))
            check(results, "desktop sees android collection",
                  any(c["key"] == coll.get("key") for c in meta.get("collections", [])), json.dumps(meta.get("collections")))
            notes = desktop.tool("zotero_get_notes", {"item_key": key, "query": None, "limit": None})["notes"]
            mine = [n for n in notes if n["key"] == note.get("key")]
            check(results, "desktop sees android note title",
                  bool(mine) and mine[0]["title"] == f"zz-ai-conf note {suffix}", json.dumps(mine)[:200])

            r = desktop.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [tag_d], "remove_tags": [tag_a]})
            check(results, "desktop swap tags", r == {"updated": [key]}, json.dumps(r))
            r = desktop.tool("zotero_set_item_collections", {"item_keys": [key], "add_to": [], "remove_from": [coll["key"]]})
            check(results, "desktop remove from collection", r == {"updated": [key]}, json.dumps(r))
            dnote = desktop.tool("zotero_create_note", {"parent_item_key": key, "title": f"zz-ai-conf desktop note {suffix}",
                                                        "content": "Made on *desktop*.", "tags": []})
            check(results, "desktop create_note", "key" in dnote, json.dumps(dnote))
            desktop.sync(); android.sync()
            meta = android.tool("zotero_get_item_metadata", {"item_key": key})
            check(results, "android sees desktop tag swap",
                  tag_d in meta.get("tags", []) and tag_a not in meta.get("tags", []), json.dumps(meta.get("tags")))
            check(results, "android sees desktop collection removal",
                  all(c["key"] != coll.get("key") for c in meta.get("collections", [])), json.dumps(meta.get("collections")))
            notes = android.tool("zotero_get_notes", {"item_key": key, "query": None, "limit": None})["notes"]
            check(results, "android sees desktop note", any(n["key"] == dnote.get("key") for n in notes))

            a = normalize(android.tool("zotero_get_item_metadata", {"item_key": key}))
            d = normalize(desktop.tool("zotero_get_item_metadata", {"item_key": key}))
            problems = diff(a, d)
            check(results, "item identical on both after writes", not problems, "; ".join(problems[:5]))

            print("== cleanup")
            desktop.tool("zotero_batch_update_tags", {"item_keys": [key], "add_tags": [], "remove_tags": [tag_d]})
            android.tool("zotero_trash_items", {"item_keys": [note["key"], dnote["key"]]})
            android.sync(); desktop.sync()
            print(f"(leave collection {coll.get('key')} for manual deletion)")

    failed = [r for r in results if not r[1]]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
