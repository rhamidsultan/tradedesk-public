import pytest
from cryptography.exceptions import InvalidTag

from tdserver.crypto import LinkSigner, LocalKeyProvider, Vault, hash_token


def test_vault_round_trip_and_context_binding():
    v = Vault(LocalKeyProvider(b"m" * 32))
    dk = v.new_data_key()
    sealed = v.seal(dk, "secret-api-key", "kite:api_key")
    assert b"secret-api-key" not in sealed
    assert v.open(dk, sealed, "kite:api_key") == "secret-api-key"
    with pytest.raises(InvalidTag):
        v.open(dk, sealed, "kite:api_secret")  # a ciphertext cannot be reused for another field


def test_wrong_master_key_cannot_open():
    dk = Vault(LocalKeyProvider(b"a" * 32)).new_data_key()
    with pytest.raises(InvalidTag):
        Vault(LocalKeyProvider(b"b" * 32)).provider.unwrap(dk)


def test_master_key_length():
    with pytest.raises(ValueError):
        LocalKeyProvider(b"short")


def test_link_signer():
    s = LinkSigner(b"x" * 32)
    t = s.sign("ack", "12:1", 1000)
    assert s.verify("ack", t, 999) == "12:1"
    assert s.verify("ack", t, 1001) is None            # expired
    assert s.verify("console", t, 999) is None         # other purpose
    assert s.verify("ack", t[:-2] + "AA", 999) is None # tampered
    assert s.verify("ack", "garbage", 999) is None


def test_hash_token_is_stable_and_not_plain():
    assert hash_token("abc") == hash_token("abc") != "abc"
