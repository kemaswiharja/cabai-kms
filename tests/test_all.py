"""End-to-end tests with an in-memory MongoDB and a fake LLM.
Run:  CABAI_MOCK_DB=1 CABAI_FAKE_LLM=1 ADMIN_PASSWORD=test python -m pytest -q tests
"""
import base64
import io
import json
import os

os.environ.setdefault("CABAI_MOCK_DB", "1")
os.environ.setdefault("CABAI_FAKE_LLM", "1")
os.environ.setdefault("ADMIN_PASSWORD", "test")
os.environ.setdefault("GOOGLE_SERVICE_ACCOUNT_JSON", "{}")
os.environ.setdefault("DRIVE_EXCEL_FOLDER_ID", "excel")

import openpyxl  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from api.index import app  # noqa: E402
from lib import drive, ontology  # noqa: E402
from lib.specimens import match_image, parse_table, rows_from_csv, rows_from_xlsx  # noqa: E402

c = TestClient(app)
ADMIN = {"X-Admin-Password": "test"}


def guide_xlsx():
    wb = openpyxl.Workbook()
    ws = wb.active
    for r in [["No", "Karakter", "Kopay", "Gendot", "Rawit"],
              [1, "Tinggi tanaman", "85 cm", "60 cm", "70 cm"],
              [2, "Bentuk buah", "Memanjang", "Bulat pipih", "Kecil lancip"],
              [3, "Warna buah", "Merah", "Merah", "Hijau"],
              [4, "Lebar daun", 4.5, 6, 2]]:
        ws.append(r)
    b = io.BytesIO(); wb.save(b)
    return b.getvalue()


def sse_events(text):
    return [json.loads(l[5:].strip()) for l in text.split("\n") if l.startswith("data:")]


def test_parse_guide_format():
    s = parse_table(rows_from_xlsx(guide_xlsx()))
    assert [x["name"] for x in s] == ["Kopay", "Gendot", "Rawit"]
    assert s[0]["attributes"]["Tinggi tanaman"] == "85 cm"
    assert s[0]["attributes"]["Lebar daun"] == "4.5"
    rows = rows_from_csv(b"Nama;Tinggi tanaman;Warna buah\nKopay;85 cm;Merah\nGendot;60 cm;Merah\n")
    assert parse_table(rows)[1] == {"name": "Gendot", "attributes": {"Tinggi tanaman": "60 cm", "Warna buah": "Merah"}}


def test_image_matching():
    names = ["Kopay", "Gendot", "Rawit", "Rawit Putih"]
    assert match_image("kopay-buah.jpg", names) == "Kopay"
    assert match_image("Lebar-daun-C.Annuum (kopay).png", names) == "Kopay"
    assert match_image("rawit_putih_daun.jpg", names) == "Rawit Putih"
    assert match_image("Leber-daun-C.Annuum.jpg", names) is None


def test_auth_required():
    assert c.post("/api/docs/upload", files={"file": ("a.pdf", b"x")}).status_code == 401
    assert c.post("/api/auth/check", headers={"X-Admin-Password": "salah"}).status_code == 401
    assert c.post("/api/auth/check", headers=ADMIN).json() == {"ok": True}


class FakeDrive:
    def list(self, folder):
        if folder == "excel":
            return [{"id": "x1", "name": "cabai.xlsx", "mimeType": drive.XLSX_MIME},
                    {"id": "g", "name": "gambar", "mimeType": drive.FOLDER_MIME}]
        return [{"id": "A" * 28, "name": "kopay-buah.jpg", "mimeType": "image/jpeg"},
                {"id": "B" * 28, "name": "Lebar-daun-C.Annuum (gendot).jpg", "mimeType": "image/jpeg"},
                {"id": "C" * 28, "name": "salah-nama.jpg", "mimeType": "image/jpeg"}]

    def download(self, f):
        return guide_xlsx()


def test_drive_sync(monkeypatch):
    monkeypatch.setattr(drive, "DriveClient", FakeDrive)
    assert c.get("/api/drive/auth/status", headers=ADMIN).json()["authenticated"] is True
    job = c.post("/api/drive/process/run", json={}, headers=ADMIN).json()["job_id"]
    st = c.get(f"/api/drive/process/status/{job}", headers=ADMIN).json()
    assert st["status"] == "done", st
    assert st["result"]["excel"]["new_count"] == 3
    assert st["result"]["images"] == {"total": 3, "unique_matched": 2, "updated": 2}
    sp = {s["name"]: s for s in c.get("/api/specimens").json()["specimens"]}
    assert sp["Kopay"]["images"][0].endswith("/view")


def make_pdf(text):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject
    w = PdfWriter()
    page = w.add_blank_page(612, 792)
    font = DictionaryObject({NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
                             NameObject("/BaseFont"): NameObject("/Helvetica")})
    page[NameObject("/Resources")] = DictionaryObject({NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
    s = DecodedStreamObject()
    s.set_data(("BT /F1 10 Tf 40 750 Td (" + text + ") Tj ET").encode())
    page[NameObject("/Contents")] = w._add_object(s)
    b = io.BytesIO(); w.write(b)
    return b.getvalue()


def test_documents_and_chat():
    pdf = make_pdf("Budidaya cabai keriting memerlukan tanah gembur dengan pH 6 sampai 7 dan penyiraman teratur setiap hari")
    r = c.post("/api/docs/upload", files={"file": ("budidaya.pdf", pdf, "application/pdf")}, headers=ADMIN)
    assert r.status_code == 200, r.text
    d = c.get("/api/docs/documents").json()
    assert d["total_documents"] == 1 and d["total_chunks"] >= 1
    q = c.post("/api/docs/query", json={"question": "pH tanah cabai keriting"}, headers=ADMIN).json()
    assert q["results"][0]["filename"] == "budidaya.pdf"
    assert c.get(f"/api/docs/documents/{d['documents'][0]['doc_id']}/download").content[:4] == b"%PDF"

    ev = sse_events(c.post("/api/chat", json={"question": "Bagaimana budidaya cabai keriting?"}).text)
    final = ev[-1]
    assert final["step"] == "final" and "documents" in final["source"], final
    assert "budidaya.pdf" in final["answer"]

    ev = sse_events(c.post("/api/chat", json={"question": "Apa bentuk buah cabai Kopay?"}).text)
    assert ev[-1]["source"] == "mongodb_only", ev[-1]
    assert "Kopay" in ev[-1]["answer"] and ev[-1]["images"]

    ev = sse_events(c.post("/api/chat", json={"question": "Bagaimana cuaca besok?"}).text)
    assert ev[-1]["source"] == "none"


def test_updater_default_and_user():
    csv = "Nama,Tinggi tanaman,Warna buah,Kadar vitamin C\nSigantung,85,Merah,120\nGenie,60,Merah,95\n".encode()
    r = c.post("/api/updater/update-ontology", files={"csv_file": ("d.csv", csv)},
               data={"enable_recommendations": "true"}, headers=ADMIN)
    ev = sse_events(r.text)
    assert ev[0]["type"] == "meta" and ev[0]["mode"] == "default"
    done = next(e for e in ev if e["type"] == "done")
    assert done["stats"]["new_cabai"] == 2 and done["stats"]["new_properties_created"] == 2, done
    assert "WarnaBuah" in done["recommendations"]["new_classes"]
    owl = base64.b64decode("".join(e["chunk"] for e in ev if e["type"] == "file_chunk")).decode()
    g = ontology.load_graph(owl)
    assert any(ontology.local(i) == "Sigantung" for i in ontology.individuals(g))
    # persisted as default
    assert "Sigantung" in c.get("/api/updater/ontology/download-default").text
    assert c.get("/api/updater/ontology/status").json()["individuals"] >= 20
    # user OWL mode does not touch default
    ev = sse_events(c.post("/api/updater/update-ontology", headers=ADMIN, data={"enable_recommendations": "false"},
                           files={"csv_file": ("d.csv", b"Nama,Tinggi\nZeta,1\n"),
                                  "owl_file": ("mine.owl", owlx())}).text)
    assert ev[0]["mode"] == "user" and "Zeta" not in c.get("/api/updater/ontology/download-default").text


def owlx():
    with open(os.path.join(os.path.dirname(__file__), "fixture_ontology.owl"), "rb") as f:
        return f.read()


def test_refiner():
    p = c.post("/api/refiner/parse", files={"file": ("o.owl", owlx())}, headers=ADMIN).json()
    assert {x["name"] for x in p["classes"]} >= {"Cabai", "Spesies", "Buah"}
    ops = [
        {"op": "add_individual", "class_name": "Cabai", "name": "Kopay"},
        {"op": "set_data_value", "subject": "Kopay", "property": "namaLokal", "value": "kopay", "value_type": "str"},
        {"op": "add_assertion", "subject": "Kopay", "property": "memilikiSpesies", "object": "Capsicum_annuum"},
        {"op": "rename_class", "target": "Lokasi", "new_name": "Daerah"},
        {"op": "set_class_label", "target": "Daerah", "value": "Daerah asal"},
        {"op": "set_prop_range_type", "target": "tinggiTanaman", "range_type": "int"},
        {"op": "set_data_value", "subject": "Kopay", "property": "tinggiTanaman", "value": "85 cm", "value_type": "str"},
        {"op": "rename_individual", "target": "TidakAda", "new_name": "X"},
    ]
    m = c.post("/api/refiner/mutate", json={"owl_b64": p["owl_b64"], "operations": ops}, headers=ADMIN).json()
    assert m["applied"] == 7 and m["errors"][0]["op_index"] == 7, m
    ind = next(i for i in m["individuals"] if i["name"] == "Kopay")
    assert ind["object_properties"]["memilikiSpesies"] == ["Capsicum_annuum"] and "Cabai" in ind["types"]
    assert any(x["name"] == "Daerah" and x["label"] == "Daerah asal" for x in m["classes"])
    cc = c.post("/api/refiner/check-consistency", json={"owl_b64": m["owl_b64"]}, headers=ADMIN).json()
    assert cc["consistent"] is False and "Kopay" in cc["details"]["inconsistent_individuals"], cc
    fix = c.post("/api/refiner/mutate", headers=ADMIN, json={"owl_b64": m["owl_b64"], "operations": [
        {"op": "set_data_value", "subject": "Kopay", "property": "tinggiTanaman", "value": "85", "value_type": "int"}]}).json()
    assert c.post("/api/refiner/check-consistency", json={"owl_b64": fix["owl_b64"]}, headers=ADMIN).json()["consistent"]
    # disjoint violation
    bad = c.post("/api/refiner/mutate", headers=ADMIN, json={"owl_b64": fix["owl_b64"], "operations": [
        {"op": "add_individual", "class_name": "Spesies", "name": "Kopay"}]}).json()
    assert c.post("/api/refiner/check-consistency", json={"owl_b64": bad["owl_b64"]}, headers=ADMIN).json()["consistent"] is False
    dl = c.post("/api/refiner/download", json={"owl_b64": fix["owl_b64"]}, headers=ADMIN)
    assert b"Kopay" in dl.content and b"rdf:RDF" in dl.content


def test_health():
    h = c.get("/api/health").json()
    assert h["mongodb"] == "connected" and h["admin_password_set"]


def test_real_excel_into_real_ontology():
    """Abang's Excel (title rows above the table) into the student's ontology v13."""
    xlsx = open(os.path.join(os.path.dirname(__file__), "sample_karakter_tahap2.xlsx"), "rb").read()
    sp = parse_table(rows_from_xlsx(xlsx))
    assert [s["name"] for s in sp] == ["Madiun", "Sigantung", "Genie", "Kawat", "Cakra putih", "Taruna", "Bara"]
    assert sp[0]["attributes"]["tinggi tanaman"] == "61 -79 cm" and len(sp[0]["attributes"]) == 55
    real = open(os.path.join(os.path.dirname(ontology.__file__), "default_ontology.owl"), "rb").read()
    logs = []
    g, stats, _ = ontology.update_ontology(sp, real.decode(), logs.append, enable_recommendations=False)
    assert stats["new_cabai"] == 7 and stats["successful"] == 7, stats
    text = "\n".join(logs)
    for col, prop in [("tinggi tanaman", "TinggiTanaman"), ("warna buah  masak", "warnaBuahMasak"),
                      ("bentuk ujung daun", "bentukUjungDaun"), ("jumlah biji/buah masak", "jumlahBiji")]:
        assert f"'{col}' → {prop}" in text, (col, text)
    # only genuinely new characters become new properties
    assert stats["new_properties_created"] <= 3, [l for l in logs if l.startswith("+")]
