"""Parsing specimen tables (Excel/CSV) and matching image file names to specimens.

Follows the student's naming guide (namingguide.pdf):
  * Excel: table starts at A1, one sheet; optional first column "No";
    the attribute column lists characters (tinggi, lebar daun, ...);
    every following column is one chili specimen.
  * Image names contain the specimen name, e.g. "kopay-buah.jpg",
    or a Latin name with the local name in brackets: "Lebar-daun-C.Annuum (kopay).jpg".
"""
import csv
import io
import re

NO_LIKE = re.compile(r"^(no\.?|nomor|#|nr\.?)$", re.I)
ATTR_LIKE = re.compile(r"^(karakter|atribut|attribute|parameter|variabel|sifat|ciri|nama karakter|character|trait|deskriptor)", re.I)


def _clean(v):
    if v is None:
        return ""
    if isinstance(v, float) and v.is_integer():
        v = int(v)
    return str(v).strip()


def _trim(rows):
    rows = [[_clean(c) for c in r] for r in rows]
    rows = [r for r in rows if any(r)]
    if not rows:
        return []
    width = max(max((i + 1 for i, c in enumerate(r) if c), default=0) for r in rows)
    return [(r + [""] * width)[:width] for r in rows]


def rows_from_xlsx(data: bytes):
    import openpyxl
    wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True)
    ws = wb.worksheets[0]
    return _trim(list(ws.iter_rows(values_only=True)))


def rows_from_csv(data: bytes):
    text = data.decode("utf-8-sig", errors="replace")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    return _trim(list(csv.reader(io.StringIO(text), dialect)))


def _from_header(rows):
    """Skip title rows above the table (e.g. 'Lampiran Karakter cabai ... tahap II')."""
    for i, r in enumerate(rows[:15]):
        filled = [c for c in r if c]
        if len(filled) >= 2 and any(NO_LIKE.match(c) or ATTR_LIKE.match(c) for c in r if c):
            return rows[i:]
    for i, r in enumerate(rows[:15]):
        if sum(1 for c in r if c) >= 3:
            return rows[i:]
    return rows


def parse_table(rows, orientation="auto"):
    """Return list of {"name": str, "attributes": {attr: value}}.

    orientation: "columns" = each column is a specimen (Excel format of the guide),
                 "rows"    = each row is a specimen, "auto" = detect.
    """
    rows = _from_header(rows)
    if not rows or len(rows) < 2:
        return []
    header = rows[0]
    skip_no = bool(NO_LIKE.match(header[0])) or (
        header[0] == "" and all(re.fullmatch(r"\d*", r[0] or "") for r in rows[1:])
    )
    first = 1 if skip_no else 0
    if orientation == "auto":
        if ATTR_LIKE.match(header[first] or ""):
            orientation = "columns"
        elif re.search(r"nama|name|spesimen|specimen|kultivar|varietas|aksesi", header[first] or "", re.I):
            orientation = "rows"
        else:
            # Guide format: few attributes as rows, specimens as columns → many rows, fewer columns
            orientation = "columns" if len(rows) > len(header) else "rows"

    specimens = []
    if orientation == "columns":
        names = header[first + 1:]
        for j, name in enumerate(names):
            if not name:
                continue
            attrs = {}
            for r in rows[1:]:
                key, val = r[first], r[first + 1 + j]
                if key and val:
                    attrs[key] = val
            specimens.append({"name": name, "attributes": attrs})
    else:
        keys = header[first + 1:]
        for r in rows[1:]:
            name = r[first]
            if not name:
                continue
            attrs = {k: v for k, v in zip(keys, r[first + 1:]) if k and v}
            specimens.append({"name": name, "attributes": attrs})
    return specimens


def safe_key(k: str) -> str:
    """MongoDB keys may not contain '.' or start with '$'."""
    return str(k).replace(".", "·").lstrip("$").strip() or "_"


_SEP = re.compile(r"[\s\-_]+")


def match_image(filename: str, specimen_names):
    """Return the specimen name an image belongs to, or None."""
    stem = re.sub(r"\.[a-z0-9]{2,5}$", "", filename, flags=re.I).lower()
    bracket = re.findall(r"\(([^)]+)\)", stem)
    candidates = [b.strip() for b in bracket] or [stem]
    by_len = sorted(specimen_names, key=lambda n: -len(n))
    for cand in candidates:
        norm = " " + _SEP.sub(" ", cand) + " "
        for name in by_len:
            n = _SEP.sub(" ", name.lower()).strip()
            if n and (" " + n + " ") in norm:
                return name
    return None


def upsert(db, specimens, source):
    """Merge specimens into MongoDB (keeps attributes/images from other sources)."""
    import datetime as dt
    n_new = 0
    for s in specimens:
        key = s["name"].strip().lower()
        if not key:
            continue
        attrs = {f"attributes.{safe_key(k)}": v for k, v in s["attributes"].items() if v}
        res = db.specimens.update_one(
            {"name_lower": key},
            {"$set": {"name": s["name"].strip(), "name_lower": key, **attrs,
                      "updated_at": dt.datetime.now(dt.timezone.utc)},
             "$addToSet": {"sources": source},
             "$setOnInsert": {"images": []}},
            upsert=True)
        n_new += 1 if res.upserted_id else 0
    return n_new
