"""PDF knowledge base (replaces doc-pepper-rag-backend).

Text is chunked and ranked with BM25 instead of paid embeddings; the raw PDF is
kept in MongoDB so it can be downloaded again.
"""
import datetime as dt
import io
import re
import uuid

from bson import Binary
from fastapi import HTTPException

from .core import bm25_rank, get_db, llm_chat

CHUNK_WORDS = 220
OVERLAP_WORDS = 40


def extract_pdf_text(data: bytes) -> list[tuple[int, str]]:
    from pypdf import PdfReader
    try:
        reader = PdfReader(io.BytesIO(data))
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, f"PDF tidak bisa dibaca: {e}") from e
    pages = []
    for i, p in enumerate(reader.pages, 1):
        t = re.sub(r"[ \t]+", " ", p.extract_text() or "").strip()
        if t:
            pages.append((i, t))
    return pages


def chunk_pages(pages):
    chunks = []
    for page_no, text in pages:
        words = text.split()
        step = CHUNK_WORDS - OVERLAP_WORDS
        for start in range(0, max(len(words), 1), step):
            piece = " ".join(words[start:start + CHUNK_WORDS])
            if len(piece) > 40:
                chunks.append({"page": page_no, "text": piece})
            if start + CHUNK_WORDS >= len(words):
                break
    return chunks


def upload(filename: str, data: bytes):
    if not filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Hanya file PDF yang didukung.")
    pages = extract_pdf_text(data)
    if not pages:
        raise HTTPException(400, "PDF tidak berisi teks (mungkin hasil scan — perlu OCR dulu).")
    chunks = chunk_pages(pages)
    db = get_db()
    doc_id = uuid.uuid4().hex[:16]
    db.doc_files.insert_one({"_id": doc_id, "filename": filename, "data": Binary(data),
                             "chunks": len(chunks), "uploaded_at": dt.datetime.now(dt.timezone.utc)})
    db.doc_chunks.insert_many([
        {"doc_id": doc_id, "filename": filename, "chunk_id": i, "page": c["page"], "text": c["text"]}
        for i, c in enumerate(chunks)
    ])
    return {"message": f"{filename} berhasil diproses", "doc_id": doc_id, "chunks": len(chunks)}


def list_documents():
    db = get_db()
    docs = [{"doc_id": d["_id"], "filename": d["filename"], "chunks": d.get("chunks", 0)}
            for d in db.doc_files.find({}, {"data": 0}).sort("uploaded_at", -1)]
    return {"documents": docs, "total_documents": len(docs),
            "total_chunks": sum(d["chunks"] for d in docs)}


def get_file(doc_id):
    d = get_db().doc_files.find_one({"_id": doc_id})
    if not d:
        raise HTTPException(404, "Dokumen tidak ditemukan.")
    return d["filename"], bytes(d["data"])


def delete(doc_id):
    db = get_db()
    d = db.doc_files.find_one({"_id": doc_id}, {"filename": 1})
    if not d:
        raise HTTPException(404, "Dokumen tidak ditemukan.")
    removed = db.doc_chunks.delete_many({"doc_id": doc_id}).deleted_count
    db.doc_files.delete_one({"_id": doc_id})
    return {"message": f"{d['filename']} dihapus", "chunks_removed": removed}


def search(question: str, top_k=5, max_chunks=20000):
    chunks = list(get_db().doc_chunks.find({}, {"_id": 0}).limit(max_chunks))
    ranked, seen = [], set()
    for i, s in bm25_rank(question, [c["text"] for c in chunks]):
        if chunks[i]["text"] in seen:  # same PDF uploaded twice → don't cite it twice
            continue
        seen.add(chunks[i]["text"])
        ranked.append((i, s))
        if len(ranked) >= top_k:
            break
    if not ranked:
        return []
    best = ranked[0][1]
    return [{**chunks[i], "similarity": round(s / best, 3) if best else 0} for i, s in ranked]


def ask(question: str, top_k=5):
    sources = search(question, top_k)
    if not sources:
        return {"answer": "Tidak ada bagian dokumen yang relevan dengan pertanyaan ini.", "sources": []}
    ctx = "\n\n".join(f"[{i + 1}] ({s['filename']}, hlm {s['page']}) {s['text']}" for i, s in enumerate(sources))
    answer = llm_chat([
        {"role": "system", "content": "Jawab hanya berdasarkan kutipan dokumen. Cantumkan nomor kutipan [n]. "
                                      "Jawab dalam bahasa yang sama dengan pertanyaan."},
        {"role": "user", "content": f"Kutipan:\n{ctx}\n\nPertanyaan: {question}"},
    ])
    return {"answer": answer, "sources": sources}
