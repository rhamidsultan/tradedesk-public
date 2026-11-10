"""Server version and Update now. The API only writes a request file; a root systemd path unit runs
ops/self_update.sh, which installs merged code from GitHub's main branch and rolls back if the server does
not come up healthy. GitHub is checked every 30 minutes by ops/check_update.sh (timer)."""
from __future__ import annotations

import json
import os
from pathlib import Path

from fastapi import APIRouter, Depends, HTTPException, Request

from . import __version__
from .context import AppContext
from .deps import Principal, audit, client_ip, current, get_ctx, get_db, owner

router = APIRouter(prefix="/v1/system")


def _read(p: Path) -> dict | None:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return None


@router.get("/version")
def version(p: Principal = Depends(current), ctx: AppContext = Depends(get_ctx)):
    d = Path(ctx.settings.data_dir)
    status = _read(d / "update-status.json")
    return {"version": __version__, "commit": os.environ.get("TD2_VERSION", "dev"),
            "available": _read(d / "update-available.json"), "last_update": status,
            "requested": (d / "update-request").exists()}


@router.post("/update")
def update(request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx), db=Depends(get_db)):
    d = Path(ctx.settings.data_dir)
    status = _read(d / "update-status.json") or {}
    if (d / "update-request").exists() or status.get("state") == "running":
        raise HTTPException(409, "An update is already running.")
    d.mkdir(parents=True, exist_ok=True)
    tmp = d / ".update-request.tmp"
    tmp.write_text(json.dumps({"by": p.user.name, "at": ctx.clock.now().isoformat()}))
    tmp.replace(d / "update-request")
    audit(db, ctx, p.user.id, "system.update", "", client_ip(request))
    return {"ok": True, "note": "The server pulls the merged code, restarts and checks itself. About a minute."}


@router.get("/share")
def share(request: Request, p: Principal = Depends(owner), ctx: AppContext = Depends(get_ctx), db=Depends(get_db)):
    """A clean copy of the running code for a friend: exactly what is committed (git archive), so no secrets, data,
    databases or tokens can be in it. The friend sets up their own server, keys and logins (docs/deploy.md)."""
    import subprocess

    from fastapi.responses import Response
    root = Path(__file__).resolve().parents[3]
    if not (root / ".git").exists():
        raise HTTPException(404, "This server's code is not a git checkout, so there is nothing clean to share.")
    try:
        out = subprocess.run(["git", "-C", str(root), "archive", "--format=zip", "--prefix=tradedesk/", "HEAD"],
                             capture_output=True, timeout=60, check=True).stdout
        rev = subprocess.run(["git", "-C", str(root), "rev-parse", "--short", "HEAD"], capture_output=True,
                             timeout=10, check=True, text=True).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise HTTPException(500, "Could not package the code on the server.") from None
    audit(db, ctx, p.user.id, "system.share", rev, client_ip(request), bytes=len(out))
    return Response(out, media_type="application/zip",
                    headers={"Content-Disposition": f"attachment; filename=tradedesk-{rev}.zip"})
