"""Deterministic stand-in for the LLM used in local tests."""
import json


def respond(messages, json_mode):
    sys = messages[0]["content"]
    user = messages[-1]["content"]
    if '"relevant"' in sys:
        return json.dumps({"relevant": "cuaca" not in user.lower()})
    if '"use_database"' in sys:
        d = json.loads(user)
        return json.dumps({"use_database": bool(d["terdeteksi_di_pertanyaan"]),
                           "specimens": d["terdeteksi_di_pertanyaan"], "attributes": []})
    if '"sufficient"' in sys:
        return json.dumps({"sufficient": "budidaya" not in user.lower()})
    if "ontologi OWL" in sys:
        return json.dumps({"new_classes": {"WarnaBuah": {"confidence": 0.8, "reason": "kategori"}},
                           "new_properties": {}, "new_object_properties": {}})
    if "Petakan setiap kolom" in sys:
        d = json.loads(user)
        return json.dumps({"mapping": {c: ("jumlahBiji" if "biji" in c.lower() and "jumlahBiji" in d["properties"] else None)
                                       for c in d["kolom"]}})
    if "nama file gambar" in sys:
        d = json.loads(user)
        return json.dumps({"matches": {f: ("Gendot" if "gndot" in f.lower() else None) for f in d["files"]}})
    return "Jawaban uji berdasarkan konteks [1]."
