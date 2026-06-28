#!/usr/bin/env python3
"""Prepare encrypted local no-TEE benchmark state."""

from __future__ import annotations

import argparse
import base64
import json
import os
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.x509.oid import NameOID


STATIC_ARTIFACTS = {
    "alice_private_model": "alice_private_model.tar",
    "alice_private_serving_patch": "alice_private_serving_patch.diff",
    "bob_private_eval_code": "bob_private_eval_code.py",
    "bob_private_eval_data": "bob_private_eval_data.jsonl",
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def _sha256(payload: bytes) -> str:
    import hashlib

    return f"sha256:{hashlib.sha256(payload).hexdigest()}"


def _write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _encrypt_file(key: bytes, source: Path, target: Path) -> dict[str, object]:
    nonce = os.urandom(12)
    plaintext = source.read_bytes()
    ciphertext = AESGCM(key).encrypt(nonce, plaintext, None)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(ciphertext)
    return {
        "ciphertext_hash": _sha256(ciphertext),
        "ciphertext_path": target.relative_to(target.parents[2]).as_posix(),
        "kind": "static",
        "nonce_b64": base64.b64encode(nonce).decode("ascii"),
        "plaintext_hash": _sha256(plaintext),
        "source_filename": source.name,
        "updated_at": _now_iso(),
    }


def _write_local_keypair(state_root: Path) -> None:
    key_dir = state_root / "secrets/ratls_key"
    key_dir.mkdir(parents=True, exist_ok=True)
    private_path = key_dir / "private.pem"
    cert_path = key_dir / "certificate.pem"
    if private_path.exists() and cert_path.exists():
        return

    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    subject = issuer = x509.Name(
        [x509.NameAttribute(NameOID.COMMON_NAME, "cove-local-no-tee-model-deployment")]
    )
    now = datetime.now(timezone.utc)
    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(public_key)
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .sign(private_key, algorithm=None)
    )
    cert_path.write_bytes(cert.public_bytes(serialization.Encoding.PEM))


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--state-root", type=Path, required=True)
    parser.add_argument(
        "--inputs-root",
        type=Path,
        default=Path("demos/attested_confidential_benchmark__vllm_gpu/runtime_inputs"),
    )
    parser.add_argument("--overwrite", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    state_root = args.state_root.resolve()
    inputs_root = args.inputs_root.resolve()
    state_root.mkdir(parents=True, exist_ok=True)

    key_path = state_root / "secrets/artifact_key.json"
    if key_path.exists() and not args.overwrite:
        key = base64.b64decode(json.loads(key_path.read_text(encoding="utf-8"))["key_b64"])
    else:
        key = AESGCM.generate_key(bit_length=256)
        _write_json(
            key_path,
            {
                "created_at": _now_iso(),
                "key_b64": base64.b64encode(key).decode("ascii"),
                "warning": "local no-TEE artifact key; not a security boundary",
            },
        )

    artifacts: dict[str, object] = {}
    for name, filename in STATIC_ARTIFACTS.items():
        source = inputs_root / filename
        if not source.is_file():
            raise SystemExit(f"missing input artifact: {source}")
        target = state_root / "artifacts/encrypted" / f"{name}.bin"
        artifacts[name] = _encrypt_file(key, source, target)

    _write_local_keypair(state_root)
    _write_json(
        state_root / "artifacts/manifest.json",
        {
            "artifacts": artifacts,
            "created_at": _now_iso(),
            "record_version": "cove_local_no_tee_artifacts_v1",
        },
    )
    _write_json(
        state_root / "state.json",
        {
            "created_at": _now_iso(),
            "state_root": str(state_root),
            "tee": False,
            "attestation": {"type": "none"},
        },
    )
    print(f"prepared local no-TEE state at {state_root}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
