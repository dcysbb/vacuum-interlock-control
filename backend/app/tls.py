from __future__ import annotations

import ipaddress
import socket
from datetime import datetime, timedelta, timezone
from pathlib import Path


def ensure_certificate(data_dir: Path) -> tuple[Path, Path]:
    """Create a host-only self-signed HTTPS cert for optional LAN access."""
    cert_path, key_path = data_dir / "lan-cert.pem", data_dir / "lan-key.pem"
    if cert_path.exists() and key_path.exists():
        return cert_path, key_path
    try:
        from cryptography import x509
        from cryptography.hazmat.primitives import hashes, serialization
        from cryptography.hazmat.primitives.asymmetric import rsa
        from cryptography.x509.oid import NameOID
    except ImportError as exc:
        raise RuntimeError("局域网 HTTPS 需要 cryptography 依赖") from exc
    hostname = socket.gethostname()
    san: list = [x509.DNSName("localhost"), x509.DNSName(hostname)]
    for item in socket.getaddrinfo(hostname, None):
        candidate = item[4][0].split("%", 1)[0]
        try:
            address = ipaddress.ip_address(candidate)
            if not address.is_loopback and address not in [entry.value for entry in san if isinstance(entry, x509.IPAddress)]:
                san.append(x509.IPAddress(address))
        except ValueError:
            pass
    san.extend([x509.IPAddress(ipaddress.ip_address("127.0.0.1")), x509.IPAddress(ipaddress.ip_address("::1"))])
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    subject = issuer = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, hostname)])
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.now(timezone.utc) - timedelta(minutes=5))
        .not_valid_after(datetime.now(timezone.utc) + timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    try:
        key_path.chmod(0o600)
    except OSError:
        pass
    return cert_path, key_path
