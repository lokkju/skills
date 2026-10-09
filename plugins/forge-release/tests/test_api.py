import base64
import json
import shutil
import subprocess

import pytest
from forge_release import api
from forge_release.manifest import FlowError

PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIfake\n-----END RSA PRIVATE KEY-----\n"
CONVERSION = {"id": 42, "slug": "acme-release", "client_id": "Iv23abc", "pem": PEM,
              "html_url": "https://github.com/apps/acme-release", "webhook_secret": None}


class FakeHttp:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, method, url, headers, body=None):
        self.calls.append((method, url, dict(headers), body))
        return self.responses.pop(0)


def b64json(part):
    return json.loads(base64.urlsafe_b64decode(part + "=" * (-len(part) % 4)))


def test_convert_posts_code_without_auth_and_parses():
    http = FakeHttp((201, CONVERSION))
    app = api.convert_manifest("c0de", http)
    method, url, headers, _body = http.calls[0]
    assert method == "POST" and url == "https://api.github.com/app-manifests/c0de/conversions"
    assert "Authorization" not in headers
    assert (app.app_id, app.slug, app.client_id, app.pem) == (42, "acme-release", "Iv23abc", PEM)


def test_credentials_repr_hides_pem():
    app = api.convert_manifest("c", FakeHttp((201, CONVERSION)))
    assert "MIIfake" not in repr(app) and "MIIfake" not in str(app)


def test_convert_error_raises_with_github_message():
    with pytest.raises(FlowError, match="404.*Not Found"):
        api.convert_manifest("c", FakeHttp((404, {"message": "Not Found"})))


def test_jwt_shape():
    signed = []

    def sign(data, pem):
        signed.append((data, pem))
        return b"sig"

    token = api.make_jwt("Iv23abc", PEM, now=1_000_000, sign=sign)
    header, claims, sig = token.split(".")
    assert b64json(header) == {"alg": "RS256", "typ": "JWT"}
    assert b64json(claims) == {"iat": 1_000_000 - 60, "exp": 1_000_000 + 540, "iss": "Iv23abc"}
    assert claims and (b64json(claims)["exp"] - 1_000_000) <= 600
    assert base64.urlsafe_b64decode(sig + "=" * (-len(sig) % 4)) == b"sig"
    assert "=" not in token
    assert signed == [(f"{header}.{claims}".encode(), PEM)]


def test_issuer_prefers_client_id():
    app = api.convert_manifest("c", FakeHttp((201, CONVERSION)))
    assert app.jwt_issuer == "Iv23abc"
    app2 = api.convert_manifest("c", FakeHttp((201, dict(CONVERSION, client_id=None))))
    assert app2.jwt_issuer == "42"


@pytest.mark.skipif(shutil.which("openssl") is None, reason="needs openssl")
def test_openssl_sign_verifies(tmp_path):
    key = tmp_path / "k.pem"
    subprocess.run(["openssl", "genrsa", "-out", str(key), "2048"], check=True, capture_output=True)
    subprocess.run(["openssl", "rsa", "-in", str(key), "-pubout", "-out", str(tmp_path / "p.pem")],
                   check=True, capture_output=True)
    sig = api.openssl_sign(b"hello", key.read_text())
    (tmp_path / "sig").write_bytes(sig)
    (tmp_path / "data").write_bytes(b"hello")
    out = subprocess.run(["openssl", "dgst", "-sha256", "-verify", str(tmp_path / "p.pem"),
                          "-signature", str(tmp_path / "sig"), str(tmp_path / "data")],
                         capture_output=True, text=True, check=False)
    assert "Verified OK" in out.stdout


def test_openssl_sign_failure_does_not_echo_key():
    def run(*a, **k):
        return subprocess.CompletedProcess(a, 1, b"", b"unable to load key")

    with pytest.raises(FlowError) as e:
        api.openssl_sign(b"x", PEM, run=run)
    assert "MIIfake" not in str(e.value) and "openssl" in str(e.value)


class Clock:
    def __init__(self):
        self.t = 0.0

    def now(self):
        return self.t

    def sleep(self, s):
        self.t += s


def test_poll_stops_at_first_installation():
    http = FakeHttp((200, []), (200, []), (200, [{"id": 777, "account": {"login": "acme"}}]),
                    (200, [{"id": 888}]))
    clock = Clock()
    tokens = iter(["t1", "t2", "t3", "t4"])
    got = api.wait_for_installation(lambda: next(tokens), http, timeout=600, interval=5,
                                    clock=clock.now, sleep=clock.sleep)
    assert got == 777
    assert len(http.calls) == 3
    method, url, headers, _ = http.calls[2]
    assert method == "GET" and url == "https://api.github.com/app/installations"
    assert headers["Authorization"] == "Bearer t3"
    assert clock.t == 10


def test_poll_times_out_cleanly():
    http = FakeHttp(*[(200, [])] * 200)
    clock = Clock()
    with pytest.raises(FlowError, match="timed out"):
        api.wait_for_installation(lambda: "t", http, timeout=30, interval=5,
                                  clock=clock.now, sleep=clock.sleep)
    assert clock.t <= 30 and len(http.calls) <= 8


def test_poll_api_error_raises():
    with pytest.raises(FlowError, match="401"):
        api.wait_for_installation(lambda: "t", FakeHttp((401, {"message": "Bad credentials"})),
                                  timeout=30, interval=5, clock=lambda: 0, sleep=lambda s: None)
