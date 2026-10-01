#!/usr/bin/env python3
"""Generate self-signed SSL/TLS certificates for local HTTPS and WebRTC media permissions."""

import datetime
import ipaddress
import os
import socket
from pathlib import Path
from cryptography import x509
from cryptography.hazmat.primitives import hashes
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from cryptography.x509.oid import NameOID

CERTS_DIR = Path(__file__).parent / "certs"
CERT_FILE = CERTS_DIR / "cert.pem"
KEY_FILE = CERTS_DIR / "key.pem"


def get_local_ips():
    ips = {"127.0.0.1"}
    try:
        # Connect to a public DNS IP to identify default egress interface IP
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.settimeout(0.5)
        s.connect(("8.8.8.8", 80))
        ips.add(s.getsockname()[0])
        s.close()
    except Exception:
        pass
    return list(ips)


def generate_certificates(cert_path=CERT_FILE, key_path=KEY_FILE, force=False):
    CERTS_DIR.mkdir(parents=True, exist_ok=True)
    if cert_path.exists() and key_path.exists() and not force:
        print(f"Certificates already exist in {CERTS_DIR}")
        return cert_path, key_path

    print("Generating self-signed certificate for local HTTPS...")
    key = rsa.generate_private_key(
        public_exponent=65537,
        key_size=2048,
    )

    local_ips = get_local_ips()
    san_entries = [
        x509.DNSName("localhost"),
        x509.DNSName("dalek.local"),
        x509.DNSName("raspberrypi.local"),
    ]
    for ip_str in local_ips:
        try:
            san_entries.append(x509.IPAddress(ipaddress.ip_address(ip_str)))
        except ValueError:
            pass

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COUNTRY_NAME, "GB"),
        x509.NameAttribute(NameOID.ORGANIZATION_NAME, "Dalek Robotics"),
        x509.NameAttribute(NameOID.COMMON_NAME, "dalek.local"),
    ])

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(san_entries),
            critical=False,
        )
        .add_extension(
            x509.BasicConstraints(ca=True, path_length=None),
            critical=True,
        )
        .sign(key, hashes.SHA256())
    )

    with open(key_path, "wb") as f:
        f.write(
            key.private_bytes(
                encoding=serialization.Encoding.PEM,
                format=serialization.PrivateFormat.TraditionalOpenSSL,
                encryption_algorithm=serialization.NoEncryption(),
            )
        )

    with open(cert_path, "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Generated SSL cert: {cert_path}")
    print(f"Generated SSL key:  {key_path}")
    print(f"SAN IPs included:   {local_ips}")
    return cert_path, key_path


if __name__ == "__main__":
    generate_certificates()
