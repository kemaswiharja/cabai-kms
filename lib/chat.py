"""PepperRAG chatbot pipeline (replaces pepper-rag-backend).

Order follows the user manual: relevance → specimen database → PDF documents →
web search (last fallback) → answer with numbered references.
Emits the same SSE events the original frontend expects.
"""
import json
import re

from . import documents
from .core import LLMError, get_db, llm_chat, llm_json
from .websearch import web_search

REFUSAL = ("Maaf, saya hanya bisa menjawab pertanyaan seputar cabai (Capsicum): "
           "spesimen, morfologi, budidaya, hama-penyakit, pascapanen, dan pemanfaatannya.")


def _ev(step, status, message):
    return {"step": step, "status": status, "message": message}


def _catalog(db):
    names, attrs = [], set()
    for d in db.specimens.find({}, {"name": 1, "attributes": 1}).limit(2000):
        names.append(d["name"])
        attrs.update(d.get("attributes", {}).keys())
    return names, sorted(attrs)


def _mentioned(question, names):
    q = " " + re.sub(r"[^a-z0-9]+", " ", question.lower()) + " "
    return [n for n in names if " " + re.sub(r"[^a-z0-9]+", " ", n.lower()).strip() + " " in q]


def run(question: str):
    question = (question or "").strip()[:1000]
    db = get_db()
    refs, context_blocks, used, images = [], [], [], []

    # 1 ─ relevance
    yield _ev("1", "processing", "Checking relevance...")
    try:
        rel = llm_json([
            {"role": "system", "content": 'Tentukan apakah pertanyaan berkaitan dengan cabai/paprika/Capsicum '
                                          '(tanaman, spesimen, budidaya, penyakit, kuliner, ekonomi, dll). '
                                          'Balas JSON {"relevant": true/false}.'},
            {"role": "user", "content": question}], fast=True, max_tokens=20)
        relevant = bool(rel.get("relevant", True))
    except LLMError as e:
        yield _ev("1", "complete", "LLM tidak tersedia")
        yield {"step": "final", "answer": f"Maaf, layanan LLM sedang tidak bisa dipakai. {e}", "source": "error", "images": []}
        return
    yield _ev("1", "complete", "Relevant ✓" if relevant else "Not about chili")
    if not relevant:
        yield {"step": "final", "answer": REFUSAL, "source": "none", "images": []}
        return

    # 2 ─ analyse database
    yield _ev("2", "processing", "Analyzing database...")
    try:
        from .ontology import seed_specimens_if_empty
        seed_specimens_if_empty()
    except Exception:  # noqa: BLE001 — seeding is best-effort
        pass
    names, attrs = _catalog(db)
    mentioned = _mentioned(question, names)
    yield _ev("2", "complete", f"{len(names)} specimens · {len(attrs)} attributes")

    # 3 ─ generate query
    yield _ev("3", "processing", "Generating query...")
    plan = {"use_database": bool(mentioned), "specimens": mentioned, "attributes": []}
    if names:
        try:
            plan = llm_json([
                {"role": "system", "content":
                    "Anda merencanakan pencarian di database spesimen cabai. Pilih spesimen dan atribut yang relevan "
                    "HANYA dari daftar yang diberikan. Balas JSON: "
                    '{"use_database": bool, "specimens": [nama persis], "attributes": [atribut persis], '
                    '"covers_all": bool}. covers_all=true HANYA jika SEMUA informasi yang ditanyakan ada di '
                    'atribut terpilih (mis. pertanyaan soal kepedasan tapi tidak ada atribut kepedasan → false). '
                    "Daftar atribut kosong berarti semua atribut. Jika pertanyaan umum (budidaya, resep) dan tidak "
                    "menyebut spesimen, use_database=false kecuali pertanyaan meminta perbandingan/daftar spesimen."},
                {"role": "user", "content": json.dumps({
                    "pertanyaan": question, "spesimen_tersedia": names[:250],
                    "atribut_tersedia": attrs[:150], "terdeteksi_di_pertanyaan": mentioned})}],
                fast=True, max_tokens=400)
        except LLMError:
            pass
    wanted = [n for n in plan.get("specimens", []) if n in names] or mentioned
    wanted_attrs = [a for a in plan.get("attributes", []) if a in attrs]
    use_db = bool(plan.get("use_database")) and bool(names)
    yield _ev("3", "complete", f"{len(wanted)} specimen(s) targeted" if use_db else "Database not needed")

    # 4 ─ execute
    yield _ev("4", "processing", "Executing query...")
    rows = []
    if use_db:
        flt = {"name": {"$in": wanted}} if wanted else {}
        for d in db.specimens.find(flt, {"_id": 0}).limit(25 if wanted else 60):
            a = d.get("attributes", {})
            if wanted_attrs:
                a = {k: v for k, v in a.items() if k in wanted_attrs}
            rows.append({"name": d["name"], "attributes": a, "images": d.get("images", [])})
    yield _ev("4", "complete", f"{len(rows)} record(s)")

    # 5 ─ evaluate database result
    yield _ev("5", "processing", "Evaluating results...")
    db_enough = False
    if rows:
        lines = [f"- {r['name']}: " + "; ".join(f"{k}: {v}" for k, v in r["attributes"].items()) for r in rows]
        block = "\n".join(lines)[:5000]
        refs.append("Database spesimen Cabai (MongoDB): " + ", ".join(r["name"] for r in rows[:15]))
        context_blocks.append(f"[{len(refs)}] DATABASE SPESIMEN\n{block}")
        used.append("mongodb")
        for r in rows:
            images += r["images"][:3]
        # Save an LLM call when the planner picked concrete DB attributes for the named specimens.
        found_all = bool(mentioned) and {r["name"].lower() for r in rows} >= {m.lower() for m in mentioned}
        db_enough = (found_all and bool(wanted_attrs) and plan.get("covers_all") is True) \
            or _sufficient(question, block)
    yield _ev("5", "complete", "Sufficient ✓" if db_enough else ("Need more sources" if rows else "No database data"))

    # 5a/5b ─ documents
    doc_enough = db_enough
    if not db_enough:
        yield _ev("5a", "processing", "Querying documents...")
        hits = documents.search(question, top_k=4)
        hits = [h for h in hits if h["similarity"] >= 0.35]
        yield _ev("5a", "complete", f"{len(hits)} passage(s)")
        yield _ev("5b", "processing", "Evaluating documents...")
        if hits:
            used.append("documents")
            texts, by_page = [], {}
            for h in hits:  # one reference per (file, page); passages of the same page share it
                label = f"{h['filename']}, halaman {h['page']}"
                if label not in by_page:
                    refs.append(label)
                    by_page[label] = len(refs)
                n = by_page[label]
                texts.append(f"[{n}] DOKUMEN {h['filename']} (hlm {h['page']})\n{h['text'][:1400]}")
            context_blocks += texts
            doc_enough = _sufficient(question, "\n".join(texts))
        yield _ev("5b", "complete", "Sufficient ✓" if doc_enough else "Need web search")

    # 6 ─ web search (last fallback)
    if not doc_enough:
        yield _ev("6", "processing", "Web search...")
        results = web_search(f"cabai {question}", 4)
        for w in results:
            refs.append(f"{w['title']} — {w['url']}")
            context_blocks.append(f"[{len(refs)}] WEB {w['title']}\n{w['snippet'][:700]}")
        if results:
            used.append("web")
        yield _ev("6", "complete", f"{len(results)} result(s)")

    # 7 ─ answer
    yield _ev("7", "processing", "Generating answer...")
    if not context_blocks:
        answer = "Maaf, saya tidak menemukan informasi yang cukup di database, dokumen, maupun web untuk menjawab."
    else:
        try:
            answer = llm_chat([
                {"role": "system", "content":
                    "Anda PepperRAG, asisten pengetahuan cabai. Jawab HANYA dari konteks bernomor. Sertakan rujukan "
                    "[n] setelah setiap klaim. Jika konteks tidak cukup, katakan dengan jujur. Jawab dalam bahasa "
                    "pertanyaan, ringkas dan terstruktur. Jangan tulis daftar referensi. JANGAN pakai markdown "
                    "(tabel |, **tebal**, # judul) karena tampilan tidak merendernya; pakai paragraf pendek dan "
                    "daftar dengan tanda '- '. Untuk perbandingan, tulis satu baris per aspek, mis. "
                    "'- Tinggi tanaman: Katokkon 60–155 cm; Kopay 60–89 cm [1]'."},
                {"role": "user", "content": "KONTEKS:\n" + "\n\n".join(context_blocks)
                                            + f"\n\nPERTANYAAN: {question}"}], max_tokens=900)
        except LLMError as e:
            answer = f"Gagal menyusun jawaban: {e}"
    answer += "\n\n---\n" + "\n".join(f"[{i}] {r}" for i, r in enumerate(refs, 1))
    yield _ev("7", "complete", "Done")
    source = {("mongodb",): "mongodb_only", ("documents",): "documents_only", ("web",): "web_only",
              ("mongodb", "documents"): "mongodb_and_documents", ("mongodb", "web"): "mongodb_and_web",
              ("documents", "web"): "documents_and_web",
              ("mongodb", "documents", "web"): "mongodb_documents_and_web"}.get(tuple(used), "none")
    yield {"step": "final", "answer": answer, "source": source, "images": images[:8]}


def _sufficient(question, context) -> bool:
    try:
        r = llm_json([
            {"role": "system", "content": 'Apakah konteks berikut sudah cukup untuk menjawab pertanyaan secara '
                                          'lengkap? Balas JSON {"sufficient": true/false}.'},
            {"role": "user", "content": f"PERTANYAAN: {question}\n\nKONTEKS:\n{context[:4000]}"}],
            fast=True, max_tokens=20)
        return bool(r.get("sufficient"))
    except LLMError:
        return True
