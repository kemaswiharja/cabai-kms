"""Cabai KMS — single FastAPI app serving all five features on Vercel."""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from fastapi import Body, Depends, FastAPI, File, Form, Header, HTTPException, UploadFile  # noqa: E402
from fastapi.responses import JSONResponse, Response, StreamingResponse  # noqa: E402

from lib import chat, documents, drive, ontology  # noqa: E402
from lib.core import ADMIN_PASSWORD, LLM_BASE_URL, LLM_MODEL, db_status, llm_available, require_admin  # noqa: E402
from lib.specimens import parse_table, rows_from_csv, rows_from_xlsx  # noqa: E402

app = FastAPI(title="Cabai KMS API", docs_url="/api/docs-ui", openapi_url="/api/openapi.json")
MAX_UPLOAD = 4_400_000  # Vercel request body limit is 4.5 MB
SSE_HEADERS = {"Cache-Control": "no-cache", "X-Accel-Buffering": "no"}


def sse(obj) -> str:
    return f"data: {json.dumps(obj, ensure_ascii=False, default=str)}\n\n"


async def read_upload(f: UploadFile) -> bytes:
    data = await f.read()
    if len(data) > MAX_UPLOAD:
        raise HTTPException(413, "File terlalu besar (maks ±4,4 MB di Vercel). Kompres atau pecah file-nya.")
    return data


# ── general ───────────────────────────────────────────────────────────────────
@app.get("/api/health")
def health():
    return {"status": "ok", "mongodb": db_status(), "llm_configured": llm_available(),
            "llm_model": LLM_MODEL, "llm_endpoint": LLM_BASE_URL.split("/")[2] if "//" in LLM_BASE_URL else LLM_BASE_URL,
            "drive_configured": drive.configured(), "admin_password_set": bool(ADMIN_PASSWORD)}


@app.get("/api/llm-check")
def llm_check():
    """One tiny LLM call: shows which model actually answers for this API key."""
    from lib import core
    try:
        out = core.llm_json([{"role": "user", "content": 'Balas JSON {"ok": true}'}], fast=True)
        return {"ok": bool(out.get("ok")), "model_used": core.LAST_MODEL_USED,
                "unavailable_models": sorted(core._dead_models)}
    except core.LLMError as e:
        return {"ok": False, "error": str(e), "unavailable_models": sorted(core._dead_models)}


@app.post("/api/auth/check")
def auth_check(_=Depends(require_admin)):
    return {"ok": True}


# ── 1. RAG chatbot (public) ───────────────────────────────────────────────────
@app.post("/api/chat")
def chat_endpoint(payload: dict = Body(...)):
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "Pertanyaan kosong.")

    def gen():
        try:
            for ev in chat.run(question):
                yield sse(ev)
        except HTTPException as e:
            yield sse({"step": "final", "answer": f"Error: {e.detail}", "source": "error", "images": []})
        except Exception as e:  # noqa: BLE001
            yield sse({"step": "final", "answer": f"Error: {e}", "source": "error", "images": []})
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── 2. Document importer (admin) ──────────────────────────────────────────────
@app.post("/api/docs/upload")
async def docs_upload(file: UploadFile = File(...), _=Depends(require_admin)):
    return documents.upload(file.filename or "dokumen.pdf", await read_upload(file))


@app.get("/api/docs/documents")
def docs_list():
    return documents.list_documents()


@app.get("/api/docs/documents/{doc_id}/download")
def docs_download(doc_id: str):
    name, data = documents.get_file(doc_id)
    return Response(data, media_type="application/pdf",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


@app.delete("/api/docs/documents/{doc_id}")
def docs_delete(doc_id: str, _=Depends(require_admin)):
    return documents.delete(doc_id)


@app.post("/api/docs/query")
def docs_query(payload: dict = Body(...), _=Depends(require_admin)):
    return {"results": documents.search(payload.get("question", ""), int(payload.get("top_k", 3)))}


@app.post("/api/docs/ask")
def docs_ask(payload: dict = Body(...), _=Depends(require_admin)):
    return documents.ask(payload.get("question", ""), int(payload.get("top_k", 5)))


# ── 3. Drive database sync (admin) ────────────────────────────────────────────
@app.get("/api/drive/auth/status")
def drive_auth_status(x_admin_password: str | None = Header(default=None)):
    return {"authenticated": bool(ADMIN_PASSWORD) and x_admin_password == ADMIN_PASSWORD,
            "drive_configured": drive.configured()}


@app.post("/api/drive/auth/logout")
def drive_logout():
    return {"ok": True}


@app.post("/api/drive/process/run")
def drive_run(payload: dict = Body(default={}), _=Depends(require_admin)):
    if not drive.configured():
        raise HTTPException(503, "Google Drive belum dikonfigurasi (GOOGLE_SERVICE_ACCOUNT_JSON, DRIVE_EXCEL_FOLDER_ID).")
    return {"job_id": drive.start_job(dry_run=bool(payload.get("dry_run")))}


@app.get("/api/drive/process/status/{job_id}")
def drive_status(job_id: str, _=Depends(require_admin)):
    return drive.job_status(job_id)


@app.get("/api/specimens")
def specimens_list():
    from lib.core import get_db
    return {"specimens": list(get_db().specimens.find({}, {"_id": 0, "updated_at": 0}).limit(1000))}


# ── 4. Ontology updater (admin) ───────────────────────────────────────────────
@app.get("/api/updater/ontology/status")
def updater_status():
    return ontology.default_status()


@app.get("/api/updater/ontology/download-default")
def updater_download_default():
    return Response(ontology.default_owl(), media_type="application/rdf+xml",
                    headers={"Content-Disposition": 'attachment; filename="cabai_default.owl"'})


@app.post("/api/updater/update-ontology")
async def updater_run(csv_file: UploadFile = File(...), owl_file: UploadFile | None = File(None),
                      enable_recommendations: str = Form("true"), _=Depends(require_admin)):
    csv_bytes = await read_upload(csv_file)
    owl_bytes = await read_upload(owl_file) if owl_file else None
    mode = "user" if owl_bytes else "default"
    fname = (owl_file.filename.rsplit(".", 1)[0] + "_updated.owl") if owl_file else "cabai_default_updated.owl"

    def gen():
        logs = []
        yield sse({"type": "meta", "mode": mode, "owl_filename": fname})
        try:
            is_xlsx = (csv_file.filename or "").lower().endswith(".xlsx")
            rows = rows_from_xlsx(csv_bytes) if is_xlsx else rows_from_csv(csv_bytes)
            specimens = parse_table(rows)
            yield sse({"type": "log", "msg": f"CSV dibaca: {len(specimens)} spesimen, "
                                              f"{len({k for s in specimens for k in s['attributes']})} kolom atribut"})
            if not specimens:
                raise ValueError("CSV kosong atau formatnya tidak dikenali.")
            owl = owl_bytes.decode("utf-8", errors="replace") if owl_bytes else ontology.default_owl()
            g, stats, recs = ontology.update_ontology(specimens, owl, logs.append,
                                                      enable_recommendations.lower() == "true")
            for line in logs:
                yield sse({"type": "log", "msg": line})
            out = ontology.serialize(g)
            if mode == "default":
                ontology.save_default(out)
                yield sse({"type": "log", "msg": "💾 Ontologi default disimpan di MongoDB"})
                from lib.core import get_db
                from lib.specimens import upsert
                n = upsert(get_db(), specimens, f"Ontology Updater: {csv_file.filename}")
                yield sse({"type": "log", "msg": f"💾 {len(specimens)} spesimen juga disimpan ke database chatbot ({n} baru)"})
            yield sse({"type": "done", "stats": stats, "recommendations": recs})
            import base64
            b64 = base64.b64encode(out.encode()).decode()
            step = 60000
            for i in range(0, len(b64), step):
                yield sse({"type": "file_chunk", "chunk": b64[i:i + step], "done": i + step >= len(b64)})
        except Exception as e:  # noqa: BLE001
            for line in logs:
                yield sse({"type": "log", "msg": line})
            yield sse({"type": "error", "msg": getattr(e, "detail", None) or str(e)})
    return StreamingResponse(gen(), media_type="text/event-stream", headers=SSE_HEADERS)


# ── 5. Ontology refiner (admin) ───────────────────────────────────────────────
@app.post("/api/refiner/parse")
async def refiner_parse(file: UploadFile = File(...), _=Depends(require_admin)):
    return ontology.describe(ontology.load_graph(await read_upload(file)))


@app.post("/api/refiner/mutate")
def refiner_mutate(payload: dict = Body(...), _=Depends(require_admin)):
    return ontology.mutate(payload.get("owl_b64", ""), payload.get("operations", []))


@app.post("/api/refiner/download")
def refiner_download(payload: dict = Body(...), _=Depends(require_admin)):
    g = ontology.graph_from_b64(payload.get("owl_b64", ""))
    return Response(ontology.serialize(g), media_type="application/rdf+xml",
                    headers={"Content-Disposition": 'attachment; filename="ontology_refined.owl"'})


@app.post("/api/refiner/check-consistency")
def refiner_consistency(payload: dict = Body(...), _=Depends(require_admin)):
    return ontology.check_consistency(payload.get("owl_b64", ""))


@app.exception_handler(Exception)
async def unhandled(_, exc):
    return JSONResponse({"detail": f"Server error: {exc}"}, status_code=500)
