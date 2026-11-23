# Cabai Knowledge Management System — versi Vercel (open-source LLM)

Tiruan dari Cabai KMS (zentilax.github.io/CabaiRAGHUB) dalam **satu proyek Vercel**:
frontend asli buatan mahasiswa (dengan alamat API diganti) + backend Python baru yang
mengikuti kontrak API yang sama.

| Halaman | Fungsi | Akses |
|---|---|---|
| `/` | Hub utama + panduan format data | publik |
| `/chat/` | RAG Chatbot (database → dokumen → web) | publik |
| `/docs/` | Document Importer (PDF) | admin |
| `/drive/` | Drive Database Sync (Excel + gambar → MongoDB) | admin |
| `/updater/` | Ontology Updater (CSV → OWL) | admin |
| `/refiner/` | Ontology Refiner (edit OWL + cek konsistensi) | admin |

## Komponen (semua gratis / open-source)

| Kebutuhan | Dipakai | Biaya |
|---|---|---|
| Hosting | Vercel Hobby | gratis |
| LLM | **GPT-OSS 120B** (jawaban) + **GPT-OSS 20B** (langkah kecil), open-weight Apache 2.0, lewat Groq; otomatis pindah model bila satu tidak tersedia/kuota habis | tier gratis |
| Database | MongoDB Atlas M0 (512 MB) | gratis |
| Pencarian dokumen | BM25 (tanpa API embedding) | — |
| Web search | DuckDuckGo (`ddgs`), atau Tavily bila `TAVILY_API_KEY` diisi | gratis |
| Google Drive | Service account Google Cloud | gratis |
| Ontologi | rdflib (Python murni) | — |

LLM bisa diganti ke penyedia lain yang kompatibel OpenAI (OpenRouter, Together, Hugging Face,
atau Ollama di server lab) cukup dengan mengubah `LLM_BASE_URL`, `LLM_API_KEY`, `LLM_MODEL`.

## Langkah deploy

### 1. MongoDB Atlas
1. Daftar di https://www.mongodb.com/cloud/atlas → buat cluster **M0 (Free)**.
2. *Database Access* → buat user + password.
3. *Network Access* → **Add IP Address → Allow access from anywhere (0.0.0.0/0)** (IP Vercel berubah-ubah).
4. *Connect → Drivers* → salin connection string `mongodb+srv://...` → ini `MONGODB_URI`.

### 2. Groq (LLM open-source)
1. Daftar di https://console.groq.com → *API Keys* → *Create API Key* → ini `LLM_API_KEY`.

### 3. Google Drive (untuk fitur Drive Sync)
1. https://console.cloud.google.com → buat project → *APIs & Services → Library* → aktifkan **Google Drive API**.
2. *IAM & Admin → Service Accounts* → buat service account → *Keys → Add key → JSON* → unduh file.
3. Isi seluruh isi file JSON itu ke `GOOGLE_SERVICE_ACCOUNT_JSON`.
4. Di Google Drive, **Share** folder Excel (dan folder gambar) ke e-mail service account (`...@...iam.gserviceaccount.com`) sebagai *Viewer*.
5. ID folder = bagian URL setelah `/folders/` → `DRIVE_EXCEL_FOLDER_ID` (dan `DRIVE_IMAGE_FOLDER_ID`, atau buat subfolder bernama `gambar` di folder Excel).
6. Agar gambar tampil di chatbot, folder gambar perlu di-share **"Anyone with the link – Viewer"**.

### 4. Vercel
1. Upload folder ini ke repo GitHub (mis. `kemaswiharja/cabai-kms`).
2. https://vercel.com/new → *Import* repo tersebut → Framework Preset: **Other** → *Deploy*.
3. *Settings → Environment Variables*, isi:

| Variabel | Wajib | Contoh |
|---|---|---|
| `MONGODB_URI` | ya | `mongodb+srv://user:pass@cluster0.xxx.mongodb.net/` |
| `LLM_API_KEY` | ya | `gsk_...` |
| `ADMIN_PASSWORD` | ya | password baru (jangan pakai yang lama, sudah terekspos di GitHub) |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | untuk Drive | isi file JSON |
| `DRIVE_EXCEL_FOLDER_ID` | untuk Drive | `1AbC...` |
| `DRIVE_IMAGE_FOLDER_ID` | opsional | `1XyZ...` |
| `TAVILY_API_KEY` | opsional | web search lebih andal |
| `LLM_BASE_URL` / `LLM_MODEL` / `LLM_FAST_MODEL` | opsional | ganti penyedia/model |
| `MONGODB_DB` | opsional | default `cabai_kms` |

4. *Deployments → Redeploy* agar variabel terbaca.
5. Cek `https://<proyek>.vercel.app/api/health` — harus `"mongodb": "connected"` dan `"llm_configured": true`.

### 5. Isi data
1. `/drive/` → login admin → **Perbarui Data MongoDB** (memuat spesimen dari Excel di Drive).
2. `/docs/` → unggah PDF penelitian.
3. `/updater/` → unggah CSV → ontologi default diperbarui & tersimpan di MongoDB.
4. Tanya di `/chat/`.

## Data awal

- `lib/default_ontology.owl` = **ontologyCabarver13.owl** (2 kelas `Cabai` & `DaerahAsal`, 17 aksesi,
  50+ data property). Disalin ke MongoDB saat pertama dipakai; setelah itu versi di MongoDB yang berlaku.
- `tests/sample_karakter_tahap2.xlsx` = *Lampiran Karakter aksesi cabai tahap II* (7 aksesi × 55 karakter).
  Bisa diunggah langsung ke Ontology Updater (CSV maupun .xlsx diterima) atau ditaruh di folder Drive.
- Baris judul di atas tabel Excel dideteksi otomatis (tabel tidak harus mulai di A1).
- Nama karakter dicocokkan ke property ontologi tanpa peduli spasi/huruf besar
  (`tinggi tanaman` → `TinggiTanaman`); sisanya dipetakan LLM, dan yang benar-benar baru dibuat sebagai property baru.

**Catatan kualitas data di ontologi v13** (perlu dirapikan di Ontology Refiner):
nilai yang berubah jadi tanggal oleh Excel (`05-Jun`, `02-May`, `Jul-19` seharusnya `5-6`, `2-5`, `7-19`),
karakter `Ð` yang seharusnya tanda pisah (`10Ð15 cm`), individu ganda beda huruf (`Kopay`/`kopay`,
`Katokkon`/`katokon`), `manado` tanpa kelas, dan property salah ketik `teksurDaun` di samping `teksturDaun`.

## Perbedaan dengan versi asli

- **Password admin dicek di server** (`ADMIN_PASSWORD`). Di versi asli password tertulis di kode halaman
  sehingga bisa dibaca siapa saja.
- **Drive Sync** memakai service account, bukan login Google per pengguna.
- **Pencarian dokumen** memakai BM25 (kata kunci), bukan embedding OpenAI. Cukup baik untuk koleksi
  ratusan PDF berbahasa Indonesia/Inggris; bisa ditingkatkan ke embedding open-source nanti.
- **Cek konsistensi** di Refiner adalah pemeriksaan struktural OWL (disjoint class, functional property,
  tipe data range, object/data property). Reasoner HermiT penuh butuh Java, yang tidak ada di Vercel.
- **Ontologi default** disimpan permanen di MongoDB (versi asli hilang setiap redeploy).
- Batas unggah **±4,4 MB per file** (batas Vercel); PDF hasil scan perlu OCR dulu.
- Tier gratis Groq: GPT-OSS 120B dan 20B masing-masing ±8K token/menit dan ±200K token/hari (kuota terpisah per model).
  Satu pertanyaan memakai beberapa ribu token, jadi cukup untuk puluhan pertanyaan/hari. Bila kena batas per menit,
  aplikasi menunggu sebentar atau pindah model. Cek kuota di `/api/llm-check` dan console.groq.com/settings/limits.
  Untuk satu kelas, naik ke paket Developer Groq (bayar per token) atau server lab sendiri.

## Pengembangan lokal

```bash
pip install -r requirements.txt fastapi uvicorn mongomock pytest playwright
CABAI_MOCK_DB=1 CABAI_FAKE_LLM=1 ADMIN_PASSWORD=test python -m pytest -q tests/test_all.py
CABAI_MOCK_DB=1 CABAI_FAKE_LLM=1 ADMIN_PASSWORD=test uvicorn tests.devserver:app --port 8765
# buka http://localhost:8765
```

Frontend: karya asli mahasiswa (Zentilax). Backend ditulis ulang untuk Vercel.
