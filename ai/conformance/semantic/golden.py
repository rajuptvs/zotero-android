"""The Python reference for semantic search (ai/spec/semantic.md): builds the golden index from
public arXiv abstracts and records, for each query, the token ids, the embedding and the ranked rows.

    pip install onnxruntime tokenizers numpy
    python3 golden.py <model dir with onnx/ and tokenizer.json> <corpus.json from arXiv>

Writes index.zip and golden.json next to this file.
"""
import json
import os
import re
import sys
import unicodedata
import zipfile

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = os.path.dirname(os.path.abspath(__file__))
REVISION = "daa72c51243991dfcaf9f9137d2c573d8f7790c0"
SHA256 = {
    "onnx/model_quantized.onnx": "d06edd601f851c633a2519304cbeb8dc6170d7ceb61b436625c17fb9b6e74953",
    "onnx/model_quantized.onnx_data": "278a7ff1248c3618e4bd11a607fc54f7bdc7778854230f3956d3f86bd9db4f3b",
    "tokenizer.json": "4d777ef5bdc1aa36227abdfb77c3e49e7b9c892d16e1b6bda41c393504828be4",
}
DIM = 512


def normalize(text):
    return re.sub(r"\s+", " ", unicodedata.normalize("NFC", text)).strip()


class Embedder:
    def __init__(self, model_dir):
        self.tok = Tokenizer.from_file(os.path.join(model_dir, "tokenizer.json"))
        self.tok.enable_truncation(2048)
        self.sess = ort.InferenceSession(os.path.join(model_dir, "onnx/model_quantized.onnx"), providers=["CPUExecutionProvider"])
        self.empty = {k: np.zeros((0, 512), np.float32) for k in ("image_features", "video_features", "audio_features")}

    def ids(self, text):
        return self.tok.encode(text).ids

    def embed(self, text):
        ids = np.array([self.ids(text)], np.int64)
        out = self.sess.run(["sentence_embedding"], {"input_ids": ids, "attention_mask": np.ones_like(ids), **self.empty})[0][0]
        return out.astype(np.float64)


def query_text(q):
    return f"task: search result | query: {normalize(q)}"


def doc_text(title, text):
    return f"title: {normalize(title) or 'none'} | text: {normalize(text)}"


def row(vec):
    v = vec[:DIM] / np.linalg.norm(vec[:DIM])
    x = v * 127
    return (np.sign(x) * np.floor(np.abs(x) + 0.5)).astype(np.int8)


def rank(q768, rows):
    q = q768[:DIM] / np.linalg.norm(q768[:DIM])
    r = rows.astype(np.float64)
    scores = (r @ q) / np.linalg.norm(r, axis=1)
    order = sorted(range(len(scores)), key=lambda i: (-scores[i], i))
    return [(i, float(scores[i])) for i in order]


def main():
    model_dir, corpus_path = sys.argv[1], sys.argv[2]
    corpus = json.load(open(corpus_path))
    passages = [p for p in corpus["passages"] if p.get("kind", "abstract") == "abstract"]
    e = Embedder(model_dir)
    chunks, rows = [], []
    for n, p in enumerate(passages):
        key = f"SEM{n:05d}"
        chunks.append({"itemKey": key, "source": "metadata", "key": None, "text": p["text"]})
        rows.append(row(e.embed(doc_text(p["title"], p["text"]))))
    rows = np.stack(rows)
    manifest = {"format": "zotero-ai-index", "version": 1,
                "model": {"id": "onnx-community/embeddinggemma-2-ONNX", "revision": REVISION, "sha256": SHA256},
                "dim": DIM, "count": len(chunks), "items": len(chunks), "builtAt": "2026-11-11T00:00:00Z",
                "libraryVersion": 0, "builder": {"host": "golden", "version": "1"}}
    with zipfile.ZipFile(os.path.join(HERE, "index.zip"), "w") as z:
        z.writestr("manifest.json", json.dumps(manifest, indent=1), zipfile.ZIP_STORED)
        z.writestr("chunks.jsonl", "".join(json.dumps(c, ensure_ascii=False) + "\n" for c in chunks), zipfile.ZIP_DEFLATED)
        z.writestr("vectors.i8", rows.tobytes(), zipfile.ZIP_STORED)
    queries = []
    for q in corpus["queries"] + ["  Attention mechanisms   for long   documents ", "CRISPR", "ﬁne-tuning café"]:
        text = query_text(q)
        vec = e.embed(text)
        queries.append({"query": q, "text": text, "ids": e.ids(text), "embedding": [round(float(x), 7) for x in vec],
                        "rows": [{"row": i, "itemKey": chunks[i]["itemKey"], "score": round(s, 7)} for i, s in rank(vec, rows)[:20]]})
    # One query per line, so the file diffs sensibly.
    lines = ",\n".join(" " + json.dumps(q, ensure_ascii=False) for q in queries)
    with open(os.path.join(HERE, "golden.json"), "w") as f:
        f.write('{"revision": %s, "dim": %d, "queries": [\n%s\n]}\n' % (json.dumps(REVISION), DIM, lines))
    print(f"{len(chunks)} rows, {len(queries)} queries")


if __name__ == "__main__":
    main()
