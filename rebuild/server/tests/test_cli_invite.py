import base64

from tdserver import cli, devicekeys

from conftest import Phone, sign_in


def test_device_invite_from_the_server_enrols_a_reinstalled_phone_as_the_owner(client, ctx, owner, monkeypatch, capsys):
    monkeypatch.setattr(cli, "build_context", lambda s: ctx)
    monkeypatch.setattr(cli, "get_settings", lambda: None)
    assert cli.main(["create-owner-invite", "Hamid"]) == 1          # an owner exists already
    assert cli.main(["create-device-invite"]) == 0
    code = capsys.readouterr().out.split(": ")[1].split()[0]
    new = Phone()                                                   # the reinstalled app: a new device key
    r = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "iPhone (new)",
                                             "public_key": base64.b64encode(new.pub).decode(),
                                             "signature": new.sign(devicekeys.enroll_message(code))})
    assert r.status_code == 200, r.text
    h = {"Authorization": f"Bearer {sign_in(client, new)}"}
    me, before = client.get("/v1/me", headers=h).json(), client.get("/v1/me", headers=owner["h"]).json()
    assert me["id"] == before["id"] and me["role"] == "owner"       # same account, not a second owner
    again = client.post("/v1/auth/enroll", json={"invite_code": code, "device_name": "x",
                                                 "public_key": base64.b64encode(Phone().pub).decode(),
                                                 "signature": new.sign(devicekeys.enroll_message(code))})
    assert again.status_code == 400                                 # one use
