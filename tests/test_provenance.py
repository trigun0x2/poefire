"""The build attestation check, on a real attestation of a beta build (the zip itself isn't
kept: only its name and SHA-256 matter)."""

from __future__ import annotations

import base64
import json
from pathlib import Path

import pytest

from poefire import provenance

DATA = Path(__file__).parent / "data"
NAME = "poefire-0.21.0-dev.88-windows-x64.zip"
SHA = "17a703dce83784b6ff87b6f7683a34aea906b9543203e70b4d4d6d8078884772"
BUNDLE = (DATA / f"{NAME}.sigstore.json").read_bytes()


def test_a_real_beta_attestation_checks_out() -> None:
    provenance.verify(BUNDLE, SHA, NAME, beta=True, offline=True)
    provenance.verify(BUNDLE, SHA.upper(), NAME, beta=True, offline=True)


def test_a_beta_build_is_not_a_public_release() -> None:
    with pytest.raises(provenance.ProvenanceError, match="doesn't check out"):
        provenance.verify(BUNDLE, SHA, NAME, beta=False, offline=True)


@pytest.mark.parametrize(
    ("sha", "name"), [("0" * 64, NAME), (SHA, "poefire-9.9.9-windows-x64.zip")]
)
def test_another_file_is_refused(sha: str, name: str) -> None:
    with pytest.raises(provenance.ProvenanceError, match="different file"):
        provenance.verify(BUNDLE, sha, name, beta=True, offline=True)


def test_a_broken_or_altered_attestation_is_refused() -> None:
    with pytest.raises(provenance.ProvenanceError, match="can't be read"):
        provenance.verify(b"not json", SHA, NAME, beta=True, offline=True)
    doc = json.loads(BUNDLE)
    sig = bytearray(base64.b64decode(doc["dsseEnvelope"]["signatures"][0]["sig"]))
    sig[-1] ^= 1  # one bit of the signature
    doc["dsseEnvelope"]["signatures"][0]["sig"] = base64.b64encode(sig).decode()
    altered = json.dumps(doc)
    with pytest.raises(provenance.ProvenanceError):
        provenance.verify(altered, SHA, NAME, beta=True, offline=True)
