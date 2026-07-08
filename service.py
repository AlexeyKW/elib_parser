import os
import json
from datetime import datetime, timezone
from pathlib import Path

from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from parse_elibrary_author import enrich_csv, parse_author_to_csv


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
def parse(
    authorid: str,
    force: int = Query(default=0, ge=0, le=1),
    enrich: int = Query(default=0, ge=0, le=1),
    enrich_force: int = Query(default=0, ge=0, le=1),
):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"author_{authorid}.csv"

    meta = _load_meta(authorid)
    cache_hit = out_path.exists() and not force and not enrich

    total_found = meta.get("total_found_on_site") if isinstance(meta, dict) else None
    saved = 0
    enriched = meta.get("enriched_count") if isinstance(meta, dict) else None
    enrich_skipped = None

    if cache_hit:
        saved = int(meta.get("saved_to_csv", _csv_saved_count(out_path)))
    else:
        if force or not out_path.exists():
            try:
                total_found, saved = parse_author_to_csv(
                    authorid,
                    str(out_path),
                    enrich=False,
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))

        if enrich:
            if not out_path.exists():
                raise HTTPException(status_code=404, detail=f"CSV not found: {out_path}")
            try:
                total_rows, enriched, enrich_skipped = enrich_csv(
                    str(out_path),
                    skip_fetched=not enrich_force,
                    force=bool(enrich_force),
                )
            except Exception as e:
                raise HTTPException(status_code=500, detail=str(e))
            if not saved:
                saved = total_rows

        _save_meta(
            authorid,
            {
                "author_id": str(authorid),
                "total_found_on_site": total_found,
                "saved_to_csv": saved or _csv_saved_count(out_path),
                "enriched_count": enriched,
                "enrich_skipped": enrich_skipped,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            },
        )
        if not saved:
            saved = _csv_saved_count(out_path)

    headers = {}
    if total_found is not None:
        headers["X-Total-Found-On-Site"] = str(total_found)
    headers["X-Saved-To-Csv"] = str(saved if saved else _csv_saved_count(out_path))
    if enriched is not None:
        headers["X-Enriched-Count"] = str(enriched)
    headers["X-Cache-Hit"] = "1" if cache_hit else "0"

    return FileResponse(
        path=str(out_path),
        media_type="text/csv; charset=utf-8",
        filename=out_path.name,
        headers=headers,
    )


@app.get("/enrich/{authorid}")
def enrich_only(
    authorid: str,
    enrich_force: int = Query(default=0, ge=0, le=1),
):
    """Enrich an existing CSV with keywords and abstract."""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"author_{authorid}.csv"
    if not out_path.exists():
        raise HTTPException(status_code=404, detail=f"CSV not found: {out_path}")

    meta = _load_meta(authorid)
    try:
        total, enriched, skipped = enrich_csv(
            str(out_path),
            skip_fetched=not enrich_force,
            force=bool(enrich_force),
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

    _save_meta(
        authorid,
        {
            **(meta or {}),
            "author_id": str(authorid),
            "saved_to_csv": total,
            "enriched_count": enriched,
            "enrich_skipped": skipped,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        },
    )

    headers = {
        "X-Saved-To-Csv": str(total),
        "X-Enriched-Count": str(enriched),
        "X-Enrich-Skipped": str(skipped),
    }
    return FileResponse(
        path=str(out_path),
        media_type="text/csv; charset=utf-8",
        filename=out_path.name,
        headers=headers,
    )


@app.get("/parse_json/{authorid}")
def parse_json(
    authorid: str,
    force: int = Query(default=0, ge=0, le=1),
    enrich: int = Query(default=0, ge=0, le=1),
    enrich_force: int = Query(default=0, ge=0, le=1),
):
    """
    Same as /parse/{authorid}, but returns JSON metadata (and stores CSV in DATA_DIR).
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out_path = DATA_DIR / f"author_{authorid}.csv"
    meta = _load_meta(authorid)
    cache_hit = out_path.exists() and not force and not enrich

    enriched = meta.get("enriched_count") if isinstance(meta, dict) else None
    enrich_skipped = None

    if cache_hit:
        saved = int(meta.get("saved_to_csv", _csv_saved_count(out_path)))
        total_found = meta.get("total_found_on_site")
        updated_at = meta.get("updated_at")
    else:
        total_found = meta.get("total_found_on_site") if isinstance(meta, dict) else None
        saved = 0
        updated_at = datetime.now(timezone.utc).isoformat()

        if force or not out_path.exists():
            try:
                total_found, saved = parse_author_to_csv(
                    authorid,
                    str(out_path),
                    enrich=False,
                )
            except Exception as e:
                return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})

        if enrich:
            if not out_path.exists():
                return JSONResponse(
                    status_code=404,
                    content={"ok": False, "error": f"CSV not found: {out_path}"},
                )
            try:
                total_rows, enriched, enrich_skipped = enrich_csv(
                    str(out_path),
                    skip_fetched=not enrich_force,
                    force=bool(enrich_force),
                )
            except Exception as e:
                return JSONResponse(status_code=500, content={"ok": False, "error": str(e)})
            if not saved:
                saved = total_rows

        _save_meta(
            authorid,
            {
                "author_id": str(authorid),
                "total_found_on_site": total_found,
                "saved_to_csv": saved or _csv_saved_count(out_path),
                "enriched_count": enriched,
                "enrich_skipped": enrich_skipped,
                "updated_at": updated_at,
            },
        )
        if not saved:
            saved = _csv_saved_count(out_path)

    return {
        "ok": True,
        "author_id": str(authorid),
        "total_found_on_site": total_found,
        "saved_to_csv": saved,
        "enriched_count": enriched,
        "enrich_skipped": enrich_skipped,
        "csv_path": str(out_path),
        "cache_hit": cache_hit,
        "updated_at": updated_at,
    }

