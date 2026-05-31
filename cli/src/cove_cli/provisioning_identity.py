from __future__ import annotations

import base64
import json
import ipaddress
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlparse
from urllib import request as urllib_request

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ed25519, rsa
from cryptography.hazmat.primitives.serialization import load_pem_private_key, load_pem_public_key
from cryptography.x509.oid import ExtendedKeyUsageOID, NameOID

from .common import canonical_json_bytes, sha256_literal
from .owner_identity import (
    OWNER_IDENTITY_VERSION,
    OwnerIdentityError,
    owner_identity_signature_payload,
    verify_owner_identity_document,
)

from .covehub import COVEHUB_USER_AGENT


DNS_LABEL_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$")
DEFAULT_COVEHUB_SERVER_URL = "https://api.covehub.io"


def ensure_owner_domain(value: str, label: str = "owner domain") -> str:
    normalized = value.strip().lower()
    labels = normalized.split(".")
    if len(labels) < 2 or any(DNS_LABEL_RE.fullmatch(label_part) is None for label_part in labels):
        raise ValueError(
            f"{label} must be a lowercase DNS hostname with at least one dot"
        )
    return normalized


def normalize_owner_server_url(
    value: str | None = None,
) -> str:
    if value is None or not value.strip():
        raise ValueError("owner_server_url must be configured explicitly")
    normalized = value.strip().rstrip("/")
    parsed = urlparse(normalized)
    if parsed.scheme != "https" or not parsed.netloc or parsed.path not in {"", "/"}:
        raise ValueError("owner_server_url must be an https origin URL")
    owner_domain_from_url(normalized)
    return normalized


def owner_domain_from_url(owner_url: str) -> str:
    parsed = urlparse(owner_url.strip())
    if parsed.scheme != "https" or not parsed.hostname:
        raise ValueError("owner_server_url must be an https origin URL")
    return ensure_owner_domain(parsed.hostname)


def certificate_matches_hostname(cert_path: Path, hostname: str) -> bool:
    hostname = hostname.strip().lower()
    if not hostname:
        return False
    try:
        dns_names, ip_addresses = certificate_subject_alt_names(cert_path)
    except ValueError:
        return False
    try:
        return str(ipaddress.ip_address(hostname)) in ip_addresses
    except ValueError:
        return hostname in dns_names


def certificate_subject_alt_names(cert_path: Path) -> tuple[set[str], set[str]]:
    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
    except Exception as exc:  # pragma: no cover - cryptography specifics vary
        raise ValueError(f"invalid PEM certificate at {cert_path}") from exc

    dns_names: set[str] = set()
    ip_addresses: set[str] = set()

    try:
        subject_alt_name = certificate.extensions.get_extension_for_class(
            x509.SubjectAlternativeName
        ).value
    except x509.ExtensionNotFound:
        subject_alt_name = None

    if subject_alt_name is not None:
        dns_names.update(name.lower() for name in subject_alt_name.get_values_for_type(x509.DNSName))
        ip_addresses.update(
            str(address) for address in subject_alt_name.get_values_for_type(x509.IPAddress)
        )

    if not dns_names and not ip_addresses:
        for attribute in certificate.subject.get_attributes_for_oid(x509.NameOID.COMMON_NAME):
            common_name = attribute.value.strip().lower()
            if not common_name:
                continue
            try:
                ip_addresses.add(str(ipaddress.ip_address(common_name)))
            except ValueError:
                dns_names.add(common_name)

    return dns_names, ip_addresses


def ensure_owner_signing_key_material(
    *,
    private_key_path: Path,
    public_key_path: Path,
) -> None:
    if private_key_path.exists() and public_key_path.exists():
        return
    private_key = ed25519.Ed25519PrivateKey.generate()
    public_key = private_key.public_key()
    private_key_path.parent.mkdir(parents=True, exist_ok=True)
    public_key_path.parent.mkdir(parents=True, exist_ok=True)
    private_key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    public_key_path.write_bytes(
        public_key.public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    private_key_path.chmod(0o600)
    public_key_path.chmod(0o644)


def _safe_common_name(value: str) -> str:
    normalized = value.strip()
    if len(normalized) <= 64:
        return normalized
    digest = sha256_literal(normalized.encode("utf-8")).removeprefix("sha256:")[:8]
    return f"{normalized[:55]}-{digest}"


def ensure_owner_tls_material(
    *,
    cert_path: Path,
    tls_key_path: Path,
    owner_private_key_path: Path,
    owner_public_key_path: Path,
    owner_url: str,
) -> None:
    owner_url = normalize_owner_server_url(owner_url)
    owner_domain = owner_domain_from_url(owner_url)
    ensure_owner_signing_key_material(
        private_key_path=owner_private_key_path,
        public_key_path=owner_public_key_path,
    )
    if cert_path.exists() and tls_key_path.exists():
        if _certificate_is_current(
            cert_path,
            owner_public_key_path=owner_public_key_path,
            server_url=owner_url,
        ):
            return
        cert_path.unlink(missing_ok=True)
        tls_key_path.unlink(missing_ok=True)

    owner_private_key = _load_owner_private_key(owner_private_key_path)
    tls_private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    hostname = _server_url_hostname(owner_url)
    san_entries: list[x509.GeneralName] = [
        x509.DNSName(hostname),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]
    subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _safe_common_name(hostname))])
    issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, _safe_common_name(f"cove-owner:{owner_domain}"))])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(tls_private_key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .add_extension(x509.BasicConstraints(ca=False, path_length=None), critical=True)
        .add_extension(
            x509.ExtendedKeyUsage([ExtendedKeyUsageOID.SERVER_AUTH]),
            critical=False,
        )
        .sign(owner_private_key, algorithm=None)
    )

    cert_path.parent.mkdir(parents=True, exist_ok=True)
    tls_key_path.parent.mkdir(parents=True, exist_ok=True)
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    tls_key_path.write_bytes(
        tls_private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_path.chmod(0o644)
    tls_key_path.chmod(0o600)


def build_owner_identity_document(
    *,
    owner_url: str,
    owner_private_key_path: Path,
    owner_public_key_path: Path,
) -> dict[str, object]:
    owner_private_key = _load_owner_private_key(owner_private_key_path)
    owner_public_key_pem = owner_public_key_path.read_text(encoding="utf-8")
    owner_url = normalize_owner_server_url(owner_url)
    owner_domain = owner_domain_from_url(owner_url)
    now = datetime.now(timezone.utc).replace(microsecond=0)
    document: dict[str, object] = {
        "version": OWNER_IDENTITY_VERSION,
        "owner_url": owner_url,
        "owner_domain": owner_domain,
        "owner_public_key_pem": owner_public_key_pem,
        "owner_public_key_sha256": sha256_literal(owner_public_key_pem.encode("utf-8")),
        "not_before": _format_utc(now - timedelta(minutes=1)),
        "not_after": _format_utc(now + timedelta(days=3650)),
        "signature_algorithm": "ed25519",
    }
    signature_payload = owner_identity_signature_payload(document)
    signature = owner_private_key.sign(canonical_json_bytes(signature_payload))
    document["signature"] = base64.b64encode(signature).decode("ascii")
    return document


def fetch_owner_identity_document(
    *,
    owner_url: str,
    expected_owner_domain: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    owner_url = normalize_owner_server_url(owner_url)
    identity_url = f"{owner_url.rstrip('/')}/identity"
    request = urllib_request.Request(
        identity_url,
        headers={
            "Accept": "application/json",
            "User-Agent": COVEHUB_USER_AGENT,
        },
        method="GET",
    )
    with urllib_request.urlopen(request, timeout=timeout) as response:
        payload = json.loads(response.read().decode("utf-8"))
    if not isinstance(payload, dict):
        raise OwnerIdentityError(f"owner identity endpoint returned a non-object payload: {identity_url}")
    return verify_owner_identity_document(
        payload,
        expected_owner_url=owner_url,
        expected_owner_domain=expected_owner_domain or owner_domain_from_url(owner_url),
    )


def fetch_owner_identity_for_write(
    *,
    owner_url: str,
    owner_private_key_path: Path,
    expected_owner_domain: str | None = None,
    timeout: float = 5.0,
) -> dict[str, object]:
    identity = fetch_owner_identity_document(
        owner_url=owner_url,
        expected_owner_domain=expected_owner_domain,
        timeout=timeout,
    )
    served_public_key_pem = identity.get("owner_public_key_pem")
    if not isinstance(served_public_key_pem, str) or not served_public_key_pem.strip():
        raise OwnerIdentityError("served owner identity public key must be a non-empty string")
    served_public_key = load_pem_public_key(served_public_key_pem.encode("utf-8"))
    if not isinstance(served_public_key, ed25519.Ed25519PublicKey):
        raise OwnerIdentityError("served owner identity public key must be Ed25519")
    local_public_key = _load_owner_private_key(owner_private_key_path).public_key()
    if _raw_owner_public_key(local_public_key) != _raw_owner_public_key(served_public_key):
        raise OwnerIdentityError(
            "served owner identity public key does not match the local owner signing key"
        )
    return identity


def _raw_owner_public_key(public_key: ed25519.Ed25519PublicKey) -> bytes:
    return public_key.public_bytes(
        encoding=serialization.Encoding.Raw,
        format=serialization.PublicFormat.Raw,
    )


def _load_owner_private_key(path: Path) -> ed25519.Ed25519PrivateKey:
    key = load_pem_private_key(path.read_bytes(), password=None)
    if not isinstance(key, ed25519.Ed25519PrivateKey):
        raise ValueError(f"owner private key at {path} must be Ed25519")
    return key


def _certificate_is_current(
    cert_path: Path,
    *,
    owner_public_key_path: Path,
    server_url: str,
) -> bool:
    hostname = _server_url_hostname(server_url)
    if not certificate_matches_hostname(cert_path, hostname):
        return False
    try:
        certificate = x509.load_pem_x509_certificate(cert_path.read_bytes())
        owner_public_key = load_pem_public_key(owner_public_key_path.read_bytes())
    except Exception:
        return False
    if not isinstance(owner_public_key, ed25519.Ed25519PublicKey):
        return False
    try:
        owner_public_key.verify(certificate.signature, certificate.tbs_certificate_bytes)
    except Exception:
        return False
    return True


def _server_url_hostname(server_url: str) -> str:
    parsed = urlparse(server_url)
    if parsed.hostname is None:
        raise ValueError(f"invalid owner server_url: {server_url}")
    return parsed.hostname


def _format_utc(value: datetime) -> str:
    return value.astimezone(timezone.utc).replace(microsecond=0).isoformat().replace("+00:00", "Z")
