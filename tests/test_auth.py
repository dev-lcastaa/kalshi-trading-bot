from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa

from kalshi_bot.auth import KalshiAuth, sign_pss_text


def _generate_keypair() -> rsa.RSAPrivateKey:
    return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def test_sign_pss_text_produces_verifiable_signature():
    private_key = _generate_keypair()
    message = "1700000000000GET/trade-api/v2/markets"

    signature_b64 = sign_pss_text(private_key, message)

    import base64

    signature = base64.b64decode(signature_b64)
    public_key = private_key.public_key()
    # Raises InvalidSignature if verification fails.
    public_key.verify(
        signature,
        message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )


def test_headers_contain_expected_keys_and_sign_path_without_query():
    private_key = _generate_keypair()
    auth = KalshiAuth(key_id="my-key-id", private_key=private_key)

    headers = auth.headers("GET", "/trade-api/v2/portfolio/orders?limit=5")

    assert headers["KALSHI-ACCESS-KEY"] == "my-key-id"
    assert "KALSHI-ACCESS-SIGNATURE" in headers
    assert headers["KALSHI-ACCESS-TIMESTAMP"].isdigit()

    # Verify the signature was computed over the path WITHOUT the query string.
    import base64

    signature = base64.b64decode(headers["KALSHI-ACCESS-SIGNATURE"])
    expected_message = (
        headers["KALSHI-ACCESS-TIMESTAMP"] + "GET" + "/trade-api/v2/portfolio/orders"
    )
    private_key.public_key().verify(
        signature,
        expected_message.encode("utf-8"),
        padding.PSS(mgf=padding.MGF1(hashes.SHA256()), salt_length=padding.PSS.DIGEST_LENGTH),
        hashes.SHA256(),
    )
