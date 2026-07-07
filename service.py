import os
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from parse_elibrary_author import parse_author_to_csv


app = FastAPI(title="elib_parser", version="1.0.0")

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))

def _meta_path(authorid: str) -> Path:
    return DATA_DIR / f"author_{authorid}.meta.json"


def _load_meta(authorid: str) -> dict | None:
    p = _meta_path(authorid)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _save_meta(authorid: str, meta: dict) -> None:
    p = _meta_path(authorid)
    p.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


def _csv_saved_count(csv_path: Path) -> int:
    # Count rows excluding header. CSV is small enough for this.
    try:
        n_lines = sum(1 for _ in csv_path.open("r", encoding="utf-8-sig"))
        return max(0, n_lines - 1)
    except Exception:
        return 0


@app.get("/health")
def health():
    return {"ok": True}


@app.get("/parse/{authorid}")
def parse(authorid: str, force: int = Query(default=0, ge=0, le=1)):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"author_{authorid}.csv"

    meta = _load_meta(authorid)

    # Cache: if CSV exists and force=0, just return it.
    if out_path.exists() and not force:
        saved = int(meta.get("saved_to_csv")) if isinstance(meta, dict) and "saved_to_csv" in meta else _csv_saved_count(out_path)
        total_found = meta.get("total_found_on_site") if isinstance(meta, dict) else None
    else:
        try:
            total_found, saved = parse_author_to_csv(authorid, str(out_path))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))

        _save_meta(
            authorid,
            {
                "author_id": str(authorid),
                "total_found_on_site": total_found,
                "saved_to_csv": saved,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )

    headers = {}
    if total_found is not None:
        headers["X-Total-Found-On-Site"] = str(total_found)
    headers["X-Saved-To-Csv"] = str(saved)
    headers["X-Cache-Hit"] = "1" if (out_path.exists() and not force and meta is not None) else "0"

    return FileResponse(
        path=str(out_path),
        media_type="text/csv; charset=utf-8",
        filename=out_path.name,
        headers=headers,
    )


@app.get("/parse_json/{authorid}")
def parse_json(authorid: str, force: int = Query(default=0, ge=0, le=1)):
    """
    Same as /parse/{authorid}, but returns JSON metadata (and stores CSV in DATA_DIR).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"author_{authorid}.csv"
    meta = _load_meta(authorid)

    cache_hit = bool(out_path.exists() and not force and meta is not None)
    if cache_hit:
        saved = int(meta.get("saved_to_csv", _csv_saved_count(out_path)))
        total_found = meta.get("total_found_on_site")
        updated_at = meta.get("updated_at")
    else:
        try:
            total_found, saved = parse_author_to_csv(authorid, str(out_path))
        except Exception as e:
            return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
        updated_at = datetime.now(timezone.utc).isoformat()
        _save_meta(
            authorid,
            {
                "author_id": str(authorid),
                "total_found_on_site": total_found,
                "saved_to_csv": saved,
                "updated_at": updated_at,
            },
        )

    return {
        "ok": True,
        "author_id": str(authorid),
        "total_found_on_site": total_found,
        "saved_to_csv": saved,
        "csv_path": str(out_path),
        "cache_hit": cache_hit,
        "updated_at": updated_at,
    }

