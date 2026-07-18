from __future__ import annotations

import keyring
import pytest

from data_agent.credentials import delete_saved_api_key, get_saved_api_key, save_api_key


def test_credential_round_trip_uses_keyring(monkeypatch):
    values: dict[tuple[str, str], str] = {}
    monkeypatch.setattr(keyring, "set_password", lambda service, account, value: values.__setitem__((service, account), value))
    monkeypatch.setattr(keyring, "get_password", lambda service, account: values.get((service, account)))
    monkeypatch.setattr(keyring, "delete_password", lambda service, account: values.pop((service, account)))

    save_api_key("  secret-value  ")
    assert get_saved_api_key() == "secret-value"
    delete_saved_api_key()
    assert get_saved_api_key() == ""


def test_rejects_empty_credential():
    with pytest.raises(ValueError, match="不能为空"):
        save_api_key("  ")
