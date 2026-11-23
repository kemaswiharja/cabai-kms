"""Ontology Updater + Ontology Refiner (replaces OntologyUpdater / OntologyRefiner).

Pure-Python (rdflib) so it runs on Vercel. The consistency check is a structural
OWL check (disjointness, functional properties, datatype ranges, property kinds)
rather than the full HermiT reasoner, which needs Java and is not available on
Vercel.
"""
import base64
import datetime as dt
import difflib
import json
import os
import re

from fastapi import HTTPException
from rdflib import BNode, Graph, Literal, Namespace, URIRef
from rdflib.namespace import OWL, RDF, RDFS, XSD

from .core import LLMError, get_db, llm_available, llm_json, norm_key

HERE = os.path.dirname(__file__)
XSD_BY_TYPE = {"str": XSD.string, "int": XSD.integer, "float": XSD.decimal, "bool": XSD.boolean}


# ── graph helpers ─────────────────────────────────────────────────────────────
def load_graph(data: bytes | str) -> Graph:
    if isinstance(data, str):
        data = data.encode()
    last = None
    for fmt in ("xml", "turtle", "json-ld", "nt"):
        g = Graph()
        try:
            g.parse(data=data, format=fmt)
            if len(g):
                return g
        except Exception as e:  # noqa: BLE001
            last = e
    raise HTTPException(400, f"File ontologi tidak bisa dibaca (RDF/XML, Turtle, JSON-LD): {last}")


def serialize(g: Graph) -> str:
    return g.serialize(format="pretty-xml")


def local(u) -> str:
    s = str(u)
    return s.rsplit("#", 1)[-1] if "#" in s else s.rstrip("/").rsplit("/", 1)[-1]


def base_ns(g: Graph) -> Namespace:
    for o in g.subjects(RDF.type, OWL.Ontology):
        if isinstance(o, URIRef):
            s = str(o)
            return Namespace(s if s.endswith(("#", "/")) else s + "#")
    for c in g.subjects(RDF.type, OWL.Class):
        if isinstance(c, URIRef) and "#" in str(c):
            return Namespace(str(c).rsplit("#", 1)[0] + "#")
    return Namespace("http://www.semanticweb.org/cabai#")


def safe_iri_name(s: str) -> str:
    s = re.sub(r"[^0-9A-Za-z_\-]+", "_", str(s).strip()).strip("_")
    return ("_" + s) if (not s or s[0].isdigit() or s[0] == "-") else s


def _entities(g, kind):
    return sorted({s for s in g.subjects(RDF.type, kind) if isinstance(s, URIRef)}, key=str)


def classes(g):
    return [c for c in _entities(g, OWL.Class) if c not in (OWL.Thing, OWL.Nothing)]


def individuals(g):
    ents = set(_entities(g, OWL.NamedIndividual))
    cls = set(classes(g))
    for s, o in g.subject_objects(RDF.type):
        if o in cls and isinstance(s, URIRef):
            ents.add(s)
    return sorted(ents, key=str)


def resolve(g, name, kinds):
    """Find an entity by local name or label (case-insensitive)."""
    n = str(name).strip().lower()
    pool = []
    for k in kinds:
        pool += individuals(g) if k == "individual" else _entities(g, k)
    for e in pool:
        if local(e).lower() == n:
            return e
    for e in pool:
        for lbl in g.objects(e, RDFS.label):
            if str(lbl).lower() == n:
                return e
    return None


def to_literal(value, value_type="auto"):
    if value_type == "auto":
        v = str(value).strip()
        if re.fullmatch(r"-?\d+", v):
            return Literal(int(v), datatype=XSD.integer)
        if re.fullmatch(r"-?\d+[.,]\d+", v):
            return Literal(v.replace(",", "."), datatype=XSD.decimal)
        return Literal(v, datatype=XSD.string)
    if value_type == "int":
        return Literal(int(float(value)), datatype=XSD.integer)
    if value_type == "float":
        return Literal(str(float(str(value).replace(",", "."))), datatype=XSD.decimal)
    if value_type == "bool":
        return Literal(str(value).strip().lower() in ("true", "1", "ya", "yes"), datatype=XSD.boolean)
    return Literal(str(value), datatype=XSD.string)


# ── parse (Refiner view) ──────────────────────────────────────────────────────
def describe(g: Graph) -> dict:
    def first(e, p):
        v = next(g.objects(e, p), None)
        return str(v) if v is not None else ""

    obj_props, data_props = _entities(g, OWL.ObjectProperty), _entities(g, OWL.DatatypeProperty)
    op_set, dp_set = set(obj_props), set(data_props)
    cls_set = set(classes(g))
    inds = []
    for i in individuals(g):
        o, d = {}, {}
        for p, v in g.predicate_objects(i):
            if p in op_set or (isinstance(v, URIRef) and p not in (RDF.type,) and p not in dp_set
                               and not str(p).startswith((str(RDFS), str(OWL), str(RDF)))):
                o.setdefault(local(p), []).append(local(v))
            elif p in dp_set or (isinstance(v, Literal) and p not in (RDFS.label, RDFS.comment)):
                d.setdefault(local(p), []).append(str(v))
        inds.append({"name": local(i), "types": [local(t) for t in g.objects(i, RDF.type) if t in cls_set],
                     "object_properties": o, "data_properties": d})
    prop = lambda p: {"name": local(p),  # noqa: E731
                      "domain": [local(x) for x in g.objects(p, RDFS.domain) if isinstance(x, URIRef)],
                      "range": [local(x) for x in g.objects(p, RDFS.range) if isinstance(x, URIRef)]}
    return {
        "owl_b64": base64.b64encode(serialize(g).encode()).decode(),
        "classes": [{"name": local(c), "label": first(c, RDFS.label), "comment": first(c, RDFS.comment),
                     "parents": [local(p) for p in g.objects(c, RDFS.subClassOf) if isinstance(p, URIRef)]}
                    for c in classes(g)],
        "individuals": inds,
        "object_properties": [prop(p) for p in obj_props],
        "data_properties": [prop(p) for p in data_props],
    }


def graph_from_b64(b64: str) -> Graph:
    try:
        raw = base64.b64decode(b64)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(400, "owl_b64 tidak valid") from e
    return load_graph(raw)


# ── mutate (Refiner edits) ────────────────────────────────────────────────────
def _rename(g, old: URIRef, new_name: str):
    new = URIRef(str(old).rsplit("#", 1)[0] + "#" + safe_iri_name(new_name)) if "#" in str(old) \
        else URIRef(str(old).rsplit("/", 1)[0] + "/" + safe_iri_name(new_name))
    if (new, None, None) in g or (None, None, new) in g:
        raise ValueError(f'"{new_name}" sudah ada')
    for s, p, o in list(g):
        if old in (s, p, o):
            g.remove((s, p, o))
            g.add((new if s == old else s, new if p == old else p, new if o == old else o))
    return new


def _need(g, name, kinds, what):
    e = resolve(g, name, kinds)
    if e is None:
        raise ValueError(f'{what} "{name}" tidak ditemukan')
    return e


PROPS = [OWL.ObjectProperty, OWL.DatatypeProperty, OWL.AnnotationProperty]


def apply_op(g: Graph, op: dict):
    kind = op.get("op")
    ns = base_ns(g)
    if kind == "rename_class":
        _rename(g, _need(g, op["target"], [OWL.Class], "Class"), op["new_name"])
    elif kind in ("set_class_label", "set_class_comment"):
        c = _need(g, op["target"], [OWL.Class], "Class")
        pred = RDFS.label if kind == "set_class_label" else RDFS.comment
        g.remove((c, pred, None))
        if op.get("value"):
            g.add((c, pred, Literal(op["value"])))
    elif kind == "rename_individual":
        _rename(g, _need(g, op["target"], ["individual"], "Individual"), op["new_name"])
    elif kind == "delete_individual":
        i = _need(g, op["target"], ["individual"], "Individual")
        g.remove((i, None, None)); g.remove((None, None, i))
    elif kind == "add_individual":
        c = _need(g, op["class_name"], [OWL.Class], "Class")
        i = ns[safe_iri_name(op["name"])]
        g.add((i, RDF.type, OWL.NamedIndividual)); g.add((i, RDF.type, c))
    elif kind in ("add_assertion", "delete_assertion"):
        s = _need(g, op["subject"], ["individual"], "Individual")
        p = _need(g, op["property"], [OWL.ObjectProperty], "Object property")
        o = _need(g, op["object"], ["individual"], "Individual")
        (g.add if kind == "add_assertion" else g.remove)((s, p, o))
    elif kind == "set_data_value":
        s = _need(g, op["subject"], ["individual"], "Individual")
        p = resolve(g, op["property"], [OWL.DatatypeProperty])
        if p is None:  # create it, like Protégé would
            p = ns[safe_iri_name(op["property"])]
            g.add((p, RDF.type, OWL.DatatypeProperty))
        g.remove((s, p, None))
        g.add((s, p, to_literal(op["value"], op.get("value_type", "str"))))
    elif kind == "delete_data_value":
        s = _need(g, op["subject"], ["individual"], "Individual")
        p = _need(g, op["property"], [OWL.DatatypeProperty], "Data property")
        g.remove((s, p, None))
    elif kind == "rename_property":
        _rename(g, _need(g, op["target"], PROPS, "Property"), op["new_name"])
    elif kind == "set_prop_domain":
        p = _need(g, op["target"], PROPS, "Property")
        g.remove((p, RDFS.domain, None)); g.add((p, RDFS.domain, _need(g, op["class_name"], [OWL.Class], "Class")))
    elif kind == "set_prop_range_class":
        p = _need(g, op["target"], [OWL.ObjectProperty], "Object property")
        g.remove((p, RDFS.range, None)); g.add((p, RDFS.range, _need(g, op["class_name"], [OWL.Class], "Class")))
    elif kind == "set_prop_range_type":
        p = _need(g, op["target"], [OWL.DatatypeProperty], "Data property")
        if op["range_type"] not in XSD_BY_TYPE:
            raise ValueError(f"range_type tidak dikenal: {op['range_type']}")
        g.remove((p, RDFS.range, None)); g.add((p, RDFS.range, XSD_BY_TYPE[op["range_type"]]))
    else:
        raise ValueError(f"Operasi tidak dikenal: {kind}")


def mutate(owl_b64: str, operations: list):
    g = graph_from_b64(owl_b64)
    applied, errors = 0, []
    for i, op in enumerate(operations or []):
        try:
            apply_op(g, op)
            applied += 1
        except (ValueError, KeyError) as e:
            errors.append({"op_index": i, "op": op.get("op"), "error": str(e).strip("'\"")})
    out = describe(g)
    out.update({"applied": applied, "errors": errors})
    return out


# ── consistency (structural) ──────────────────────────────────────────────────
def _superclasses(g, c, seen=None):
    seen = seen or set()
    for p in g.objects(c, RDFS.subClassOf):
        if isinstance(p, URIRef) and p not in seen:
            seen.add(p); _superclasses(g, p, seen)
    for p in g.objects(c, OWL.equivalentClass):
        if isinstance(p, URIRef) and p not in seen:
            seen.add(p); _superclasses(g, p, seen)
    return seen


def _disjoint_pairs(g):
    pairs = {frozenset((a, b)) for a, b in g.subject_objects(OWL.disjointWith)}
    for adc in g.subjects(RDF.type, OWL.AllDisjointClasses):
        for lst in g.objects(adc, OWL.members):
            members = list(g.items(lst))
            pairs |= {frozenset((a, b)) for a in members for b in members if a != b}
    return pairs


def _literal_ok(lit, rng):
    if not isinstance(lit, Literal):
        return False
    v = str(lit).strip()
    if rng in (XSD.integer, XSD.int, XSD.long, XSD.nonNegativeInteger, XSD.positiveInteger):
        return bool(re.fullmatch(r"[+-]?\d+", v)) and not (rng == XSD.nonNegativeInteger and v.startswith("-"))
    if rng in (XSD.decimal, XSD.float, XSD.double):
        return bool(re.fullmatch(r"[+-]?(\d+\.?\d*|\.\d+)([eE][+-]?\d+)?", v))
    if rng == XSD.boolean:
        return v.lower() in ("true", "false", "1", "0")
    if rng in (XSD.date, XSD.dateTime):
        return bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}.*", v))
    return True


def check_consistency(owl_b64: str):
    g = graph_from_b64(owl_b64)
    disjoint = _disjoint_pairs(g)
    unsat, bad_inds, issues = set(), set(), []
    for c in classes(g):
        sup = _superclasses(g, c) | {c}
        if OWL.Nothing in sup or any(p <= sup for p in disjoint):
            unsat.add(local(c))
    dprops = set(_entities(g, OWL.DatatypeProperty))
    oprops = set(_entities(g, OWL.ObjectProperty))
    functional = set(g.subjects(RDF.type, OWL.FunctionalProperty))
    for i in individuals(g):
        types = set()
        for t in g.objects(i, RDF.type):
            if isinstance(t, URIRef) and t != OWL.NamedIndividual:
                types |= _superclasses(g, t) | {t}
        if OWL.Nothing in types or any(p <= types for p in disjoint):
            bad_inds.add(local(i)); issues.append(f"{local(i)} berada di dua kelas yang disjoint")
        for p in dprops:
            vals = set(g.objects(i, p))
            if p in functional and len(vals) > 1:
                bad_inds.add(local(i)); issues.append(f"{local(i)}.{local(p)} functional tapi punya {len(vals)} nilai")
            for rng in g.objects(p, RDFS.range):
                for v in vals:
                    if not _literal_ok(v, rng):
                        bad_inds.add(local(i))
                        issues.append(f'{local(i)}.{local(p)} = "{v}" tidak sesuai range {local(rng)}')
        for p in oprops:
            vals = set(g.objects(i, p))
            if any(isinstance(v, Literal) for v in vals):
                bad_inds.add(local(i)); issues.append(f"{local(i)}.{local(p)} object property berisi literal")
            if p in functional and len(vals) > 1:
                bad_inds.add(local(i)); issues.append(f"{local(i)}.{local(p)} functional tapi punya {len(vals)} nilai")
    note = " (pemeriksaan struktural OWL; bukan reasoner HermiT penuh)"
    if unsat or bad_inds:
        return {"consistent": False, "message": "Ontologi TIDAK konsisten" + note,
                "details": {"unsatisfiable_classes": sorted(unsat), "inconsistent_individuals": sorted(bad_inds),
                            "issues": issues[:100]}}
    return {"consistent": True, "message": f"Ontologi konsisten — {len(classes(g))} kelas, "
                                           f"{len(individuals(g))} individu diperiksa{note}", "details": {}}


# ── default ontology store ────────────────────────────────────────────────────
def default_owl() -> str:
    db = get_db()
    d = db.ontology_store.find_one({"_id": "default"})
    if d:
        return d["owl"]
    with open(os.path.join(HERE, "default_ontology.owl"), encoding="utf-8") as f:
        owl = f.read()
    db.ontology_store.replace_one({"_id": "default"}, {"_id": "default", "owl": owl,
                                                        "updated_at": dt.datetime.now(dt.timezone.utc)}, upsert=True)
    return owl


def save_default(owl: str):
    get_db().ontology_store.replace_one({"_id": "default"}, {"_id": "default", "owl": owl,
                                                             "updated_at": dt.datetime.now(dt.timezone.utc)}, upsert=True)


def default_status():
    owl = default_owl()
    d = get_db().ontology_store.find_one({"_id": "default"}, {"updated_at": 1})
    g = load_graph(owl)
    return {"default_ontology_size_bytes": len(owl.encode()), "classes": len(classes(g)),
            "individuals": len(individuals(g)), "triples": len(g),
            "updated_at": d["updated_at"].isoformat() if d and d.get("updated_at") else None}


# ── ontology → specimen collection ───────────────────────────────────────────
def specimens_from_graph(g):
    """Individuals of the main chili class as {name, attributes} (for the chatbot DB)."""
    cls = [c for c in classes(g) if CHILI_CLASS.match(local(c))]
    out = []
    for i in individuals(g):
        if cls and not any((i, RDF.type, c) in g for c in cls):
            continue
        name = str(next(g.objects(i, RDFS.label), "") or local(i)).replace("_", " ")
        attrs = {}
        for p, v in g.predicate_objects(i):
            if p in (RDF.type, RDFS.label, RDFS.comment):
                continue
            key = str(next(g.objects(p, RDFS.label), "") or local(p))
            val = str(next(g.objects(v, RDFS.label), "") or local(v)) if isinstance(v, URIRef) else str(v)
            attrs[key] = f"{attrs[key]}; {val}" if key in attrs else val
        out.append({"name": name, "attributes": attrs})
    return out


def seed_specimens_if_empty():
    """First run: fill the chatbot's specimen DB from the default ontology."""
    from .specimens import upsert
    db = get_db()
    if db.specimens.estimated_document_count() > 0:
        return 0
    return upsert(db, specimens_from_graph(load_graph(default_owl())), "ontologi default")


# ── updater (CSV → OWL individuals) ───────────────────────────────────────────
CHILI_CLASS = re.compile(r"^(cabai|cabe|pepper|chili|chilli|capsicum|spesimen|specimen)", re.I)


def _main_class(g, ns):
    cls = classes(g)
    for c in cls:
        if local(c).lower() == "cabai":
            return c, False
    for c in cls:
        if CHILI_CLASS.match(local(c)):
            return c, False
    c = ns["Cabai"]
    g.add((c, RDF.type, OWL.Class)); g.add((c, RDFS.label, Literal("Cabai")))
    return c, True


def _compact(s):
    """'tinggi tanaman', 'TinggiTanaman', 'tinggi_tanaman' → 'tinggitanaman'."""
    return re.sub(r"[^0-9a-z]", "", str(s).lower())


def _match_property(col, props):
    key = _compact(col)
    by_key = {}
    for p, labels in props.items():
        for lab in labels:
            by_key.setdefault(_compact(lab), p)
    if key in by_key:
        return by_key[key]
    close = difflib.get_close_matches(key, list(by_key), n=1, cutoff=0.85)
    return by_key[close[0]] if close else None


def _llm_map_columns(cols, prop_names, sample, log):
    """Ask the LLM to map leftover columns to existing properties (semantic match)."""
    if not cols or not llm_available():
        return {}
    try:
        r = llm_json([
            {"role": "system", "content":
                "Petakan setiap kolom data cabai ke SATU data property ontologi yang maknanya SAMA PERSIS, "
                "atau null bila tidak ada. Jangan memetakan ke property yang hanya mirip (mis. panjang ≠ lebar, "
                "buah muda ≠ buah masak). Balas JSON {\"mapping\": {kolom: property_atau_null}}."},
            {"role": "user", "content": json.dumps({"kolom": {c: sample.get(c, [])[:3] for c in cols},
                                                    "properties": prop_names}, ensure_ascii=False)}],
            max_tokens=800)
        out = {c: p for c, p in (r.get("mapping") or {}).items() if c in cols and p in prop_names}
        for c, p in out.items():
            log(f"≈ Kolom '{c}' → {p} (dipetakan LLM)")
        return out
    except LLMError as e:
        log(f"⚠ Pemetaan LLM dilewati: {e}")
        return {}


def update_ontology(specimens: list, owl: str, log, enable_recommendations=True):
    g = load_graph(owl)
    ns = base_ns(g)
    before = len(g)
    main, created_cls = _main_class(g, ns)
    if created_cls:
        log(f"Kelas utama 'Cabai' dibuat di {ns}")
    else:
        log(f"Kelas utama: {local(main)}")

    props = {p: [local(p)] + [str(x) for x in g.objects(p, RDFS.label)] for p in _entities(g, OWL.DatatypeProperty)}
    columns = sorted({k for s in specimens for k in s["attributes"]})
    mapping, new_props = {}, []
    unmatched = [c for c in columns if _match_property(c, props) is None]
    sample = {c: list({s["attributes"][c] for s in specimens if c in s["attributes"]}) for c in unmatched}
    by_name = {local(p): p for p in props}
    llm_map = {c: by_name[n] for c, n in _llm_map_columns(unmatched, sorted(by_name), sample, log).items()}
    for col in columns:
        p = llm_map.get(col) or _match_property(col, props)
        if p is None:
            p = ns[safe_iri_name(norm_key(col))]
            if (p, RDF.type, OWL.DatatypeProperty) not in g:
                g.add((p, RDF.type, OWL.DatatypeProperty)); g.add((p, RDFS.label, Literal(col)))
                g.add((p, RDFS.domain, main))
                new_props.append(col)
            props[p] = [local(p), col]
            log(f"+ Properti baru: {local(p)}  ← kolom '{col}'")
        else:
            log(f"= Kolom '{col}' → {local(p)}")
        mapping[col] = p

    existing = {local(i).lower() for i in individuals(g)}
    stats = {"total_rows": len(specimens), "successful": 0, "new_cabai": 0, "total_properties_added": 0,
             "triples_added": 0, "new_properties_created": len(new_props)}
    for s in specimens:
        try:
            iname = safe_iri_name(s["name"])
            ind = ns[iname]
            if iname.lower() not in existing:
                stats["new_cabai"] += 1
                existing.add(iname.lower())
            g.add((ind, RDF.type, OWL.NamedIndividual)); g.add((ind, RDF.type, main))
            g.set((ind, RDFS.label, Literal(s["name"])))
            for col, val in s["attributes"].items():
                p = mapping[col]
                g.remove((ind, p, None)); g.add((ind, p, to_literal(val)))
                stats["total_properties_added"] += 1
            stats["successful"] += 1
        except Exception as e:  # noqa: BLE001
            log(f"✗ Baris '{s.get('name')}': {e}")
    stats["triples_added"] = len(g) - before
    log(f"✓ {stats['successful']}/{stats['total_rows']} baris diproses, {stats['new_cabai']} cabai baru")

    recs = {"new_classes": {}, "new_properties": {}, "new_object_properties": {},
            "summary": {"total_recommendations": 0}}
    if enable_recommendations:
        log("Menyusun rekomendasi struktur ontologi…")
        recs = recommendations(g, columns, specimens, new_props, log)
    return g, stats, recs


def recommendations(g, columns, specimens, new_props, log):
    sample = {c: list({s["attributes"][c] for s in specimens if c in s["attributes"]})[:5] for c in columns}
    recs = {"new_classes": {}, "new_properties": {}, "new_object_properties": {}}
    if llm_available():
        try:
            r = llm_json([
                {"role": "system", "content":
                    "Anda ahli ontologi OWL untuk keanekaragaman cabai. Berdasarkan struktur ontologi dan kolom data, "
                    "rekomendasikan perbaikan. Contoh: kolom bernilai kategori berulang (mis. warna buah) sebaiknya "
                    "menjadi kelas + object property. Balas JSON: {\"new_classes\": {Nama: {\"confidence\": 0-1, "
                    "\"reason\": str}}, \"new_properties\": {nama: {\"confidence\", \"range\": \"xsd:...\", "
                    "\"reason\"}}, \"new_object_properties\": {nama: {\"confidence\", \"domain\", \"range\", "
                    "\"reason\"}}}. Maksimal 15 rekomendasi."},
                {"role": "user", "content": json.dumps({
                    "kelas": [local(c) for c in classes(g)][:80],
                    "object_properties": [local(p) for p in _entities(g, OWL.ObjectProperty)][:80],
                    "data_properties": [local(p) for p in _entities(g, OWL.DatatypeProperty)][:150],
                    "kolom_csv_dan_contoh_nilai": sample}, ensure_ascii=False)[:12000]}], max_tokens=1500)
            for k in recs:
                recs[k] = {str(n): v for n, v in (r.get(k) or {}).items() if isinstance(v, dict)}
            log("✓ Rekomendasi dari LLM diterima")
        except LLMError as e:
            log(f"⚠ LLM tidak tersedia ({e}); memakai rekomendasi heuristik")
    if not any(recs.values()):
        for c in new_props:
            vals = sample.get(c, [])
            numeric = vals and all(re.fullmatch(r"-?\d+([.,]\d+)?", v) for v in vals)
            recs["new_properties"][norm_key(c)] = {"confidence": 0.6, "range": "xsd:decimal" if numeric else "xsd:string",
                                                   "reason": "Kolom CSV belum ada padanannya di ontologi"}
    recs["summary"] = {"total_recommendations": sum(len(v) for v in recs.values())}
    return recs
