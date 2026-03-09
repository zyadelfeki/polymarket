import data_feeds.polymarket_client_v2 as client_module


class _StubAccount:
    @staticmethod
    def from_key(_private_key):
        class _Account:
            address = "0x1234567890abcdef1234567890abcdef12345678"

        return _Account()


class _AltCredsClobClient:
    def __init__(self, host, key=None, chain_id=None, creds=None):
        self.host = host
        self.key = key
        self.chain_id = chain_id
        self.creds = creds
        self.set_creds_calls = []

    def create_or_derive_api_creds(self):
        return {
            "api_key": "test-api-key",
            "secret": "test-secret",
            "passphrase": "test-passphrase",
        }

    def set_api_creds(self, creds):
        self.creds = creds
        self.set_creds_calls.append(creds)


def test_force_authentication_uses_alt_sdk_credential_method(monkeypatch):
    monkeypatch.setattr(client_module, "POLYMARKET_AVAILABLE", True)
    monkeypatch.setattr(client_module, "ClobClient", _AltCredsClobClient)
    monkeypatch.setattr(client_module, "Account", _StubAccount)

    client = client_module.PolymarketClientV2(
        private_key="0x" + "1" * 64,
        paper_trading=False,
        retry_backoff_base=0,
    )

    assert client.authenticated is True
    assert client.can_trade is True
    assert isinstance(client.client, _AltCredsClobClient)
    assert client.client.creds["api_key"] == "test-api-key"
    assert len(client.client.set_creds_calls) == 1