import io
import os
import shutil
import subprocess
import tempfile
import threading
import zipfile
from pathlib import Path, PurePosixPath

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

# ---- Batas keamanan & sumber daya (silakan sesuaikan) ----
MAX_UPLOAD_MB = int(os.getenv("MAX_UPLOAD_MB", 20))        # ukuran zip
MAX_UNZIPPED_MB = int(os.getenv("MAX_UNZIPPED_MB", 100))   # total setelah diekstrak
MAX_FILES = int(os.getenv("MAX_FILES", 2000))              # jumlah file
TIMEOUT_SEC = int(os.getenv("TIMEOUT_SEC", 240))           # waktu maksimum graphify
MAX_PARALLEL_JOBS = int(os.getenv("MAX_PARALLEL_JOBS", 1)) # proses bersamaan

SKIP_DIRS = {
    ".git", "node_modules", "venv", ".venv", "__pycache__",
    "dist", "build", ".next", ".idea", ".vscode", "graphify-out",
}
OUTPUT_FILES = ["graph.json", "GRAPH_REPORT.md"]

app = FastAPI(title="Graph Generator")
job_slots = threading.BoundedSemaphore(MAX_PARALLEL_JOBS)


def safe_extract(zip_bytes: bytes, dest: Path) -> int:
    """Ekstrak zip dengan perlindungan zip-slip, symlink, dan zip-bomb."""
    try:
        zf = zipfile.ZipFile(io.BytesIO(zip_bytes))
    except zipfile.BadZipFile:
        raise HTTPException(400, "File bukan zip yang valid.")

    total, count = 0, 0
    dest = dest.resolve()
    for info in zf.infolist():
        if info.is_dir():
            continue
        parts = PurePosixPath(info.filename).parts
        if info.filename.startswith(("/", "\\")) or ".." in parts:
            raise HTTPException(400, "Zip berisi path berbahaya.")
        if any(p in SKIP_DIRS for p in parts):
            continue
        if (info.external_attr >> 16) & 0o170000 == 0o120000:  # symlink
            continue

        count += 1
        total += info.file_size
        if count > MAX_FILES:
            raise HTTPException(413, f"Terlalu banyak file (maks {MAX_FILES}).")
        if total > MAX_UNZIPPED_MB * 1024 * 1024:
            raise HTTPException(413, f"Isi zip terlalu besar (maks {MAX_UNZIPPED_MB} MB).")

        target = (dest / Path(*parts)).resolve()
        if dest not in target.parents:
            raise HTTPException(400, "Zip berisi path berbahaya.")
        target.parent.mkdir(parents=True, exist_ok=True)
        with zf.open(info) as src, open(target, "wb") as out:
            shutil.copyfileobj(src, out)

    if count == 0:
        raise HTTPException(400, "Zip kosong atau hanya berisi folder yang diabaikan.")
    return count


def build_graph(zip_bytes: bytes) -> bytes:
    workdir = Path(tempfile.mkdtemp(prefix="graphgen_"))
    try:
        project = workdir / "project"
        project.mkdir()
        safe_extract(zip_bytes, project)

        try:
            proc = subprocess.run(
                ["graphify", "update", "."],
                cwd=project, capture_output=True, text=True, timeout=TIMEOUT_SEC,
            )
        except subprocess.TimeoutExpired:
            raise HTTPException(504, "Proses terlalu lama. Coba upload project yang lebih kecil.")

        out_dir = project / "graphify-out"
        if proc.returncode != 0 or not (out_dir / "graph.json").exists():
            raise HTTPException(
                422,
                "Graphify gagal membuat graph. Pastikan zip berisi file kode "
                "(.py, .js, .ts, .java, dst).\n" + (proc.stderr or proc.stdout)[-400:],
            )

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            for name in OUTPUT_FILES:
                f = out_dir / name
                if f.exists():
                    z.write(f, name)
        return buf.getvalue()
    finally:
        shutil.rmtree(workdir, ignore_errors=True)  # selalu hapus kode user


@app.post("/api/generate")
async def generate(file: UploadFile = File(...)):
    data = await file.read(MAX_UPLOAD_MB * 1024 * 1024 + 1)
    if len(data) > MAX_UPLOAD_MB * 1024 * 1024:
        raise HTTPException(413, f"Ukuran zip maksimal {MAX_UPLOAD_MB} MB.")

    if not job_slots.acquire(blocking=False):
        raise HTTPException(429, "Server sedang sibuk. Coba lagi sebentar.")
    try:
        result = await run_in_threadpool(build_graph, data)
    finally:
        job_slots.release()

    return StreamingResponse(
        io.BytesIO(result),
        media_type="application/zip",
        headers={"Content-Disposition": 'attachment; filename="graphify-out.zip"'},
    )


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/")
def index():
    return FileResponse("static/index.html")


app.mount("/static", StaticFiles(directory="static"), name="static")
