"""Admin commands run on the server: `python -m tdserver.cli <command>`.

  create-owner-invite NAME   one-time code to enrol the owner's first iPhone (works only while no owner exists)
  new-master-key             print a fresh base64 master key for TD2_MASTER_KEY
  check                      check settings and database connection
  live-ordertest             the supervised 1-lot order test (market hours, owner watching; stop td2-live first)
  portfolio-import DB [ACTIONS.json ...]
                             copy the Algo Lab dashboard's portfolio history and corporate actions (safe to repeat)
"""
from __future__ import annotations

import base64
import os
import sys

from sqlalchemy import select, text

from .context import build_context
from .models import User
from .routes_auth import new_invite
from .settings import get_settings


def main(argv: list[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args:
        print(__doc__)
        return 2
    cmd = args[0]
    if cmd == "new-master-key":
        print(base64.b64encode(os.urandom(32)).decode())
        return 0
    ctx = build_context(get_settings())
    if cmd == "live-ordertest":
        from .live.ordertest import run
        return run(ctx)
    if cmd == "portfolio-import":
        from .portfolio.importer import import_actions, import_db
        if len(args) < 2:
            print("usage: portfolio-import /opt/trader/portfolio.db [corporate_actions.json ...]", file=sys.stderr)
            return 2
        with ctx.sessions() as db:
            n = import_db(db, args[1])
            acts = sum(import_actions(db, a) for a in args[2:])
            db.commit()
        print(f"imported: {n}; corporate actions: {acts}")
        return 0
    if cmd == "check":
        with ctx.sessions() as db:
            db.execute(text("select 1"))
        print(f"ok: env={ctx.settings.env} url={ctx.settings.public_base_url}")
        return 0
    if cmd == "create-owner-invite":
        name = " ".join(args[1:]).strip() or "Owner"
        with ctx.sessions() as db:
            if db.scalar(select(User).where(User.role == "owner")):
                print("An owner already exists. Create invites from the app (More › Family).", file=sys.stderr)
                return 1
            code = new_invite(db, ctx, name, "owner", None)
            db.commit()
        print(f"Owner invite for {name}: {code}  (valid 3 days, one use)")
        return 0
    print(__doc__)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
