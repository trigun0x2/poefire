"""Checks that an update was built by this project's release workflow, in public CI.

Every release zip comes with a Sigstore build attestation (`<zip>.sigstore.json`), signed
keylessly by GitHub Actions: the signing certificate names the workflow file and the branch it
ran on, and the signed statement names the zip's SHA-256. Checking it proves that the zip was
built by `.github/workflows/release.yml` of trigun0x2/poefire on `master` (or `dev`, for the
beta) and wasn't changed afterwards. Someone who could only upload files to a release (a
leaked token, say) can't make a matching attestation.

It's the same check as `gh attestation verify <zip> --repo trigun0x2/poefire`.
"""

from __future__ import annotations

import json
import logging

log = logging.getLogger(__name__)

REPO = "trigun0x2/poefire"
WORKFLOW = f"https://github.com/{REPO}/.github/workflows/release.yml"
ISSUER = "https://token.actions.githubusercontent.com"
PUBLIC_BRANCH = "master"
BETA_BRANCH = "dev"


class ProvenanceError(Exception):
    pass


def identity(branch: str) -> str:
    """The signer an attestation must name: this repo's release workflow on `branch`."""
    return f"{WORKFLOW}@refs/heads/{branch}"


def verify(bundle_json: str | bytes, sha256: str, name: str, *, beta: bool,
           offline: bool = False) -> None:  # fmt: skip
    """Raises ProvenanceError unless `bundle_json` is a valid attestation for a file `name`
    with this `sha256`, signed by the release workflow on master (or dev, for a beta).
    `offline` uses the Sigstore trust root shipped with the library
    instead of fetching the current one (it's fetched, and falls back to that, by default)."""
    from sigstore.errors import Error as SigstoreError
    from sigstore.models import Bundle
    from sigstore.verify import Verifier
    from sigstore.verify.policy import AnyOf, Identity

    try:
        bundle = Bundle.from_json(bundle_json)
    except Exception as e:
        raise ProvenanceError(f"the attestation can't be read: {e}") from e
    verifier = None
    if not offline:
        try:
            verifier = Verifier.production()
        except Exception as e:
            log.warning("couldn't refresh the Sigstore trust root (%s): using the built-in one", e)
    if verifier is None:
        verifier = Verifier.production(offline=True)
    branches = [PUBLIC_BRANCH, BETA_BRANCH] if beta else [PUBLIC_BRANCH]
    policy = AnyOf([Identity(identity=identity(b), issuer=ISSUER) for b in branches])
    try:
        payload_type, payload = verifier.verify_dsse(bundle, policy)
    except (SigstoreError, ValueError) as e:
        why = str(e)
        if "polic" in why:  # a valid signature, by someone else (or the other channel)
            where = " or ".join(branches)
            why = f"it wasn't signed by poefire's release workflow on {where} ({why})"
        raise ProvenanceError(f"the attestation doesn't check out: {why}") from e
    if payload_type != "application/vnd.in-toto+json":
        raise ProvenanceError(f"unexpected attestation type {payload_type}")
    try:
        subjects = json.loads(payload).get("subject") or []
    except ValueError as e:
        raise ProvenanceError("the attestation's statement can't be read") from e
    for s in subjects:
        if s.get("name") == name and str((s.get("digest") or {}).get("sha256", "")).lower() == (
            sha256.lower()
        ):
            log.info("build attestation verified for %s", name)
            return
    raise ProvenanceError(f"the attestation is for a different file than {name}")
