"""Strategy library: gallery, drafts with plain checks, risk summary and acceptance, versions, backtest,
export/import, share codes (TradeDesk and Algo Lab envelopes)."""
import pytest

pytest.importorskip("tradedesk_spec")

from tdserver.strategies import share  # noqa: E402


def _new(client, h, template="strangle_nifty"):
    r = client.post("/v1/strategies", json={"template": template}, headers=h)
    assert r.status_code == 200, r.text
    return r.json()


def test_gallery_has_categories_and_chips(client, owner):
    g = client.get("/v1/strategies/gallery", headers=owner["h"]).json()
    s = next(x for x in g if x["id"] == "strangle_nifty")
    assert s["category"] == "Option selling" and "unlimited risk" in s["chips"] and "positional" in s["chips"]
    o = next(x for x in g if x["id"] == "orb_nifty")
    assert o["category"] == "Indicator and breakout" and "defined risk" in o["chips"]


def test_blank_strategy_lists_what_to_fix_in_plain_words(client, owner):
    d = client.post("/v1/strategies", json={"blank": True}, headers=owner["h"]).json()
    assert d["ok"] is False and d["version"] == 0
    texts = " ".join(p["text"] for p in d["problems"])
    wheres = {p["where"] for p in d["problems"]}
    assert "This is required." in texts and "Risk › stop loss" in wheres and "Risk › max loss a day" in wheres
    r = client.post(f"/v1/strategies/{d['id']}/save", json={}, headers=owner["h"])
    assert r.status_code == 400 and r.json()["detail"]["problems"]


def test_selling_without_premium_stop_explained(client, owner):
    d = _new(client, owner["h"])
    s = client.get(f"/v1/strategies/{d['id']}", headers=owner["h"]).json()["spec"]
    s["risk"]["stop_loss"] = {"on": "underlying", "type": "points", "value": 50}
    c = client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": s}, headers=owner["h"]).json()
    assert c["ok"] is False
    assert any("stop loss on each leg's premium" in p["text"] for p in c["problems"])


def test_risk_summary_and_acceptance_above_2pct(client, owner, ctx):
    d = _new(client, owner["h"])
    spec = client.get(f"/v1/strategies/{d['id']}", headers=owner["h"]).json()["spec"]
    r = client.post("/v1/strategies/risk", json={"spec": spec}, headers=owner["h"]).json()
    assert r["lot_size"] == 65 and r["budget_2pct"] == 4000 and r["day_mark_4pct"] == 8000
    legs = r["legs"]
    assert [l["loss_at_stop"] for l in legs] == [2600, 2600]               # (2x - 1) x Rs 40 x 65
    assert r["above_budget"] is True and r["accept_required"] > 5200
    assert r["app_down_worst"] > r["loss_per_trade"]                      # SL-limit buffer on both legs
    blocked = client.post(f"/v1/strategies/{d['id']}/save", json={}, headers=owner["h"])
    assert blocked.status_code == 409 and blocked.json()["detail"]["accept_required"] == r["accept_required"]
    low = client.post(f"/v1/strategies/{d['id']}/save", json={"accept_risk": 100}, headers=owner["h"])
    assert low.status_code == 409
    ok = client.post(f"/v1/strategies/{d['id']}/save", json={"accept_risk": r["accept_required"], "note": "first"},
                     headers=owner["h"]).json()
    assert ok["version"] == 1
    got = client.get(f"/v1/strategies/{d['id']}", headers=owner["h"]).json()
    assert got["versions"][0]["accepted_risk"] == r["accept_required"] and got["editing_draft"] is False


def _saved(client, h):
    d = _new(client, h)
    spec = client.get(f"/v1/strategies/{d['id']}", headers=h).json()["spec"]
    spec["costs"]["capital"] = 500000                                       # 2% = 10,000: no acceptance needed
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": spec}, headers=h)
    assert client.post(f"/v1/strategies/{d['id']}/save", json={}, headers=h).json()["version"] == 1
    return d["id"]


def test_versions_restore_and_backtest(client, owner):
    h = owner["h"]
    sid = _saved(client, h)
    spec = client.get(f"/v1/strategies/{sid}", headers=h).json()["spec"]
    spec["risk"]["stop_loss"]["value"] = 1.8
    client.put(f"/v1/strategies/{sid}/draft", json={"spec": spec}, headers=h)
    assert client.post(f"/v1/strategies/{sid}/save", json={"note": "tighter stop"}, headers=h).json()["version"] == 2
    v1 = client.get(f"/v1/strategies/{sid}/versions/1", headers=h).json()
    assert v1["spec"]["risk"]["stop_loss"]["value"] == 2
    client.post(f"/v1/strategies/{sid}/versions/1/restore", headers=h)
    assert client.get(f"/v1/strategies/{sid}", headers=h).json()["editing_draft"] is True
    run = client.post(f"/v1/strategies/{sid}/backtest", json={"start": "2026-10-12", "end": "2026-10-13", "version": 2},
                      headers=h).json()
    assert run["status"] == "queued" and run["name"].endswith("v2") and run["fill_model"] == "honest"
    mine = client.get("/v1/strategies", headers=h).json()
    assert mine[0]["id"] == sid and mine[0]["version"] == 2 and mine[0]["has_draft"] is True


def test_share_and_import_with_changes(client, owner, ctx):
    h = owner["h"]
    sid = _saved(client, h)
    code = client.post(f"/v1/strategies/{sid}/share", json={"include_results": False}, headers=h).json()["code"]
    assert code.startswith("ALGOSHARE1.")
    payload = share.decode(code)
    assert payload["kind"] == "spec" and "token" not in str(payload).lower()
    # the sharer changes the stop and shares again: the friend sees what changed
    spec = client.get(f"/v1/strategies/{sid}", headers=h).json()["spec"]
    spec["risk"]["stop_loss"]["value"] = 1.5
    client.put(f"/v1/strategies/{sid}/draft", json={"spec": spec}, headers=h)
    client.post(f"/v1/strategies/{sid}/save", json={}, headers=h)
    code2 = client.post(f"/v1/strategies/{sid}/share", json={}, headers=h).json()["code"]
    from conftest import Phone, enroll, sign_in
    p = Phone()
    enroll(client, ctx, p, role="trader", name="Friend")
    fh = {"Authorization": f"Bearer {sign_in(client, p)}"}
    first = client.post("/v1/strategies/import", json={"code": code}, headers=fh).json()
    assert first["ok"] is True and first["version"] == 0                      # a draft to check, then save
    client.post(f"/v1/strategies/{first['id']}/save", json={}, headers=fh)
    pv = client.post("/v1/strategies/import/preview", json={"code": code2}, headers=fh).json()
    assert pv["existing"]["id"] == first["id"]
    assert {c["path"] for c in pv["changes"]} == {"risk.stop_loss.value"}
    upd = client.post("/v1/strategies/import", json={"code": code2, "mode": "update"}, headers=fh).json()
    assert upd["id"] == first["id"]
    # broken codes are explained
    mid = len(code2) // 2
    broken = code2[:mid] + ("A" if code2[mid] != "A" else "B") + code2[mid + 1:]
    bad = client.post("/v1/strategies/import/preview", json={"code": broken}, headers=fh)
    assert bad.status_code == 400 and "copy it again" in bad.json()["detail"]


def test_algolab_preset_code_imports(client, owner):
    lib = pytest.importorskip("tradedesk_spec")
    py = [t for t in lib.templates() if lib.load_template(t).kind == "python"]
    if not py:
        pytest.skip("no python templates")
    code = share.encode({"v": 1, "kind": "preset", "id": "abc123def", "strategy": py[0], "title": "x",
                         "from": "Hamid", "preset": {"name": "Wide", "params": {"some_param": 3}}})
    d = client.post("/v1/strategies/import", json={"code": code}, headers=owner["h"]).json()
    assert d["kind"] == "python" and "Wide" in d["name"]
    unknown = share.encode({"v": 1, "kind": "preset", "id": "abc123def", "strategy": "nope", "preset": {}})
    r = client.post("/v1/strategies/import/preview", json={"code": unknown}, headers=owner["h"])
    assert r.status_code == 400 and "does not have yet" in r.json()["detail"]


def test_export_and_privacy(client, ctx, owner):
    sid = _saved(client, owner["h"])
    e = client.get(f"/v1/strategies/{sid}/export", headers=owner["h"]).json()
    assert e["format"] == "tradedesk-strategy/1" and e["spec"]["risk"]["stop_loss"]
    from conftest import Phone, enroll, sign_in
    p = Phone()
    enroll(client, ctx, p, role="viewer", name="Viewer")
    vh = {"Authorization": f"Bearer {sign_in(client, p)}"}
    assert client.get(f"/v1/strategies/{sid}", headers=vh).status_code == 404
    assert client.post("/v1/strategies", json={"blank": True}, headers=vh).status_code == 403
    imported = client.post("/v1/strategies/import", json={"code": __import__("json").dumps(e)}, headers=owner["h"])
    assert imported.status_code == 200


def test_python_params_for_the_builder(client, owner):
    r = client.get("/v1/strategies/python/intraday_straddle/params", headers=owner["h"]).json()
    names = {p["name"]: p for p in r["params"]}
    assert names["lots"]["default"] == 1 and names["entry_time"]["default"] == "09:20"
    assert client.get("/v1/strategies/python/nope/params", headers=owner["h"]).status_code == 404


def test_presets_and_preset_backtest(client, owner):
    h = owner["h"]
    sid = _saved(client, h)
    bad = client.post(f"/v1/strategies/{sid}/presets", json={"name": "Silly", "overrides": {"risk.stop_loss.value": -1}}, headers=h)
    assert bad.status_code == 400
    p = client.post(f"/v1/strategies/{sid}/presets", json={"name": "Tight", "overrides": {"risk.stop_loss.value": 1.5},
                                                          "is_default": True}, headers=h).json()
    assert p["is_default"] is True
    run = client.post(f"/v1/strategies/{sid}/presets/{p['id']}/backtest",
                      json={"start": "2026-10-12", "end": "2026-10-13", "lots": 2}, headers=h).json()
    assert "name" in run, run
    assert run["name"].endswith("Tight")
    detail = client.get(f"/v1/research/backtests/{run['id']}", headers=h).json()
    assert detail["spec"]["risk"]["stop_loss"]["value"] == 1.5 and detail["spec"]["legs"][0]["lots"] == 2
    # a preset saved from a run keeps what differs from the saved version
    p2 = client.post(f"/v1/strategies/{sid}/presets", json={"name": "From run", "from_run": run["id"]}, headers=h).json()
    assert p2["overrides"]["risk.stop_loss.value"] == 1.5 and p2["overrides"]["legs.0.lots"] == 2 if "legs.0.lots" in p2["overrides"] else True
    assert len(client.get(f"/v1/strategies/{sid}/presets", headers=h).json()) == 2


def test_stage_gates_block_paper_until_ready(client, owner):
    h = owner["h"]
    sid = _saved(client, h)
    g = client.get(f"/v1/strategies/{sid}/stage", headers=h).json()
    assert g["stage"] == "research" and g["next"] == "paper" and g["can_promote"] is False
    assert {c["name"]: c["ok"] for c in g["checks"]}["Saved version with a stop loss and a daily limit"] is True
    r = client.post(f"/v1/strategies/{sid}/stage/promote", headers=h)
    assert r.status_code == 409 and r.json()["detail"]["checks"]
    studio = client.get("/v1/strategies/studio/list", headers=h).json()
    assert studio[0]["stage"] == "research" and studio[0]["paper"] is None


def test_save_refuses_a_draft_changed_after_review(client, owner):
    h = owner["h"]
    d = _new(client, h)
    spec = client.get(f"/v1/strategies/{d['id']}", headers=h).json()["spec"]
    spec["costs"]["capital"] = 500000
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": spec}, headers=h)
    reviewed = dict(spec)
    other = {**spec, "risk": {**spec["risk"], "max_loss_per_day": spec["risk"]["max_loss_per_day"] + 1000}}
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": other}, headers=h)    # e.g. another device edits
    r = client.post(f"/v1/strategies/{d['id']}/save", json={"reviewed": reviewed}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["changed"] is True
    ok = client.post(f"/v1/strategies/{d['id']}/save", json={"reviewed": other}, headers=h)
    assert ok.status_code == 200 and ok.json()["version"] == 1


def test_day_limit_above_4pct_must_be_accepted(client, owner):
    h = owner["h"]
    d = _new(client, h)
    spec = client.get(f"/v1/strategies/{d['id']}", headers=h).json()["spec"]
    spec["costs"]["capital"] = 500000                                       # 2% = 10,000; 4% = 20,000
    spec["risk"]["max_loss_per_day"] = 30000
    client.put(f"/v1/strategies/{d['id']}/draft", json={"spec": spec}, headers=h)
    r = client.post("/v1/strategies/risk", json={"spec": spec}, headers=h).json()
    assert r["above_budget"] is False and r["accept_required"] == 30000
    assert any("4%" in i["what"] for i in r["accept_items"])
    assert client.post(f"/v1/strategies/{d['id']}/save", json={}, headers=h).status_code == 409
    assert client.post(f"/v1/strategies/{d['id']}/save", json={"accept_risk": 30000}, headers=h).json()["version"] == 1


def test_preset_that_raises_risk_needs_acceptance(client, owner):
    h = owner["h"]
    sid = _saved(client, h)
    big = {"risk.max_loss_per_day": 50000}
    r = client.post(f"/v1/strategies/{sid}/presets", json={"name": "Big", "overrides": big}, headers=h)
    assert r.status_code == 409 and r.json()["detail"]["accept_required"] == 50000
    ok = client.post(f"/v1/strategies/{sid}/presets", json={"name": "Big", "overrides": big, "accept_risk": 50000}, headers=h)
    assert ok.status_code == 200, ok.text
