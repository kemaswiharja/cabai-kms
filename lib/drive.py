"""Google Drive → MongoDB specimen sync (replaces the student's DriveHandlerCabaiRAG).

Uses a Google *service account* (free) instead of per-user OAuth, so it works on
serverless hosting. Share both Drive folders with the service-account e-mail.
"""
import datetime as dt
import json
import os
import uuid

import httpx

from .core import get_db
from .specimens import match_image, parse_table, rows_from_csv, rows_from_xlsx, safe_key

DRIVE = "https://www.googleapis.com/drive/v3/files"
XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
GSHEET_MIME = "application/vnd.google-apps.spreadsheet"
FOLDER_MIME = "application/vnd.google-apps.folder"


def _token():
    raw = os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON", "")
    if not raw:
        raise RuntimeError("GOOGLE_SERVICE_ACCOUNT_JSON belum diset.")
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_info(
        json.loads(raw), scopes=["https://www.googleapis.com/auth/drive.readonly"]
    )
    creds.refresh(Request())
    return creds.token


def configured() -> bool:
    return bool(os.getenv("GOOGLE_SERVICE_ACCOUNT_JSON") and os.getenv("DRIVE_EXCEL_FOLDER_ID"))


class DriveClient:
    def __init__(self, token=None):
        self.h = {"Authorization": f"Bearer {token or _token()}"}

    def list(self, folder_id):
        files, page = [], None
        while True:
            params = {
                "q": f"'{folder_id}' in parents and trashed=false",
                "fields": "nextPageToken, files(id,name,mimeType,modifiedTime)",
                "pageSize": 1000,
                "supportsAllDrives": "true",
                "includeItemsFromAllDrives": "true",
            }
            if page:
                params["pageToken"] = page
            r = httpx.get(DRIVE, headers=self.h, params=params, timeout=30)
            r.raise_for_status()
            d = r.json()
            files += d.get("files", [])
            page = d.get("nextPageToken")
            if not page:
                return files

    def download(self, f):
        if f["mimeType"] == GSHEET_MIME:
            url, params = f"{DRIVE}/{f['id']}/export", {"mimeType": XLSX_MIME}
        else:
            url, params = f"{DRIVE}/{f['id']}", {"alt": "media", "supportsAllDrives": "true"}
        r = httpx.get(url, headers=self.h, params=params, timeout=60, follow_redirects=True)
        r.raise_for_status()
        return r.content


def run_sync(client=None, dry_run=False):
    """Rebuild the specimen collection from Drive. Returns (logs, result)."""
    logs = []
    log = logs.append
    db = get_db()
    excel_folder = os.getenv("DRIVE_EXCEL_FOLDER_ID")
    image_folder = os.getenv("DRIVE_IMAGE_FOLDER_ID")
    client = client or DriveClient()

    log("=" * 48)
    log("▶ PHASE 1 — Excel")
    files = client.list(excel_folder)
    sheets = [f for f in files if f["mimeType"] in (XLSX_MIME, GSHEET_MIME)
              or f["name"].lower().endswith((".xlsx", ".csv"))]
    if not image_folder:
        sub = [f for f in files if f["mimeType"] == FOLDER_MIME and f["name"].strip().lower() == "gambar"]
        image_folder = sub[0]["id"] if sub else None
    log(f"📁 {len(sheets)} file Excel ditemukan")

    specimens, seen_sources = {}, []
    for f in sheets:
        try:
            data = client.download(f)
            rows = rows_from_csv(data) if f["name"].lower().endswith(".csv") else rows_from_xlsx(data)
            parsed = parse_table(rows, orientation="columns")
            for s in parsed:
                key = s["name"].strip().lower()
                entry = specimens.setdefault(key, {"name": s["name"].strip(), "attributes": {}, "sources": []})
                entry["attributes"].update({safe_key(k): v for k, v in s["attributes"].items()})
                entry["sources"].append(f["name"])
            seen_sources.append(f["name"])
            log(f"✓ {f['name']}: {len(parsed)} spesimen")
        except Exception as e:  # noqa: BLE001
            log(f"❌ {f['name']}: {e}")

    log("▶ PHASE 2 — Gambar")
    images, matched_names, unmatched = [], set(), []
    if image_folder:
        queue, all_imgs = [image_folder], []
        while queue:
            for f in client.list(queue.pop()):
                if f["mimeType"] == FOLDER_MIME:
                    queue.append(f["id"])
                elif f["mimeType"].startswith("image/"):
                    all_imgs.append(f)
        names = [s["name"] for s in specimens.values()]
        rule = {f["name"]: match_image(f["name"], names) for f in all_imgs}
        llm_hits = _llm_match_images([n for n, m in rule.items() if not m], names, log)
        for f in all_imgs:
            m = rule[f["name"]] or llm_hits.get(f["name"])
            if m:
                specimens[m.lower()].setdefault("images", []).append(
                    f"https://drive.google.com/file/d/{f['id']}/view")
                matched_names.add(m)
            else:
                unmatched.append(f["name"])
        images = all_imgs
        log(f"🗂 {len(all_imgs)} gambar, {len(all_imgs) - len(unmatched)} cocok dengan spesimen")
        for u in unmatched[:20]:
            log(f"⏭ Tidak cocok: {u}")
    else:
        log("⏭ Folder gambar tidak ditemukan (set DRIVE_IMAGE_FOLDER_ID atau buat subfolder 'gambar')")

    existing = {d["name_lower"] for d in db.specimens.find({}, {"name_lower": 1})}
    new_count = len(set(specimens) - existing)
    removed = len(existing - set(specimens))
    if dry_run:
        log("⏭ Dry run — database tidak diubah")
    else:
        log("💾 Menulis ke MongoDB…")
        now = dt.datetime.now(dt.timezone.utc)
        for key, s in specimens.items():
            db.specimens.replace_one(
                {"name_lower": key},
                {"name": s["name"], "name_lower": key, "attributes": s["attributes"],
                 "images": s.get("images", []), "sources": s["sources"], "updated_at": now},
                upsert=True,
            )
        if specimens:  # never wipe the DB because Drive returned nothing
            db.specimens.delete_many({"name_lower": {"$nin": list(specimens)}})
        log(f"🧹 {removed} spesimen dihapus (tidak ada lagi di Drive)")
    log(f"✅ COMPLETE — {len(specimens)} spesimen, {new_count} baru")
    result = {
        "excel": {"files": len(seen_sources), "total": len(specimens), "new_count": new_count, "removed": removed},
        "images": {"total": len(images), "unique_matched": len(matched_names),
                   "updated": sum(1 for s in specimens.values() if s.get("images"))},
    }
    return logs, result


def _llm_match_images(image_names, specimen_names, log):
    """Fallback like the original DriveHandler: let the LLM match file names that
    don't follow the naming guide exactly (typos, abbreviations)."""
    from .core import LLMError, llm_available, llm_json
    if not image_names or not specimen_names or not llm_available():
        return {}
    out = {}
    lower = {n.lower(): n for n in specimen_names}
    for i in range(0, len(image_names), 60):
        batch = image_names[i:i + 60]
        try:
            r = llm_json([
                {"role": "system", "content":
                    "Cocokkan setiap nama file gambar dengan SATU nama spesimen cabai dari daftar, hanya jika jelas "
                    "merujuk spesimen itu (boleh salah ketik/singkatan). Jika ragu, null. "
                    'Balas JSON {"matches": {nama_file: nama_spesimen_atau_null}}.'},
                {"role": "user", "content": json.dumps({"files": batch, "specimens": specimen_names},
                                                       ensure_ascii=False)}], fast=True, max_tokens=2000)
        except LLMError as e:
            log(f"⚠ Pencocokan gambar via LLM dilewati: {e}")
            return out
        for f, n in (r.get("matches") or {}).items():
            if f in batch and n and str(n).lower() in lower:
                out[f] = lower[str(n).lower()]
    if out:
        log(f"🤖 {len(out)} gambar dicocokkan oleh LLM")
    return out


def start_job(dry_run=False):
    db = get_db()
    job_id = uuid.uuid4().hex[:12]
    try:
        logs, result = run_sync(dry_run=dry_run)
        doc = {"_id": job_id, "status": "done", "logs": logs, "result": result}
    except Exception as e:  # noqa: BLE001
        doc = {"_id": job_id, "status": "error", "logs": [f"❌ ERROR: {e}"], "result": {}}
    doc["created_at"] = dt.datetime.now(dt.timezone.utc)
    db.sync_jobs.insert_one(doc)
    return job_id


def job_status(job_id):
    d = get_db().sync_jobs.find_one({"_id": job_id})
    if not d:
        return {"status": "error", "logs": ["❌ Job tidak ditemukan"]}
    d.pop("_id"); d.pop("created_at", None)
    return d
