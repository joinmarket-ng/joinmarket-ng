"""CLI entry point for the JoinMarket wallet daemon.

Usage::

    jmwalletd [--host HOST] [--port PORT] [--ws-port WS_PORT] [--data-dir DIR] [--no-tls]
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated

import typer

app = typer.Typer(
    name="jmwalletd",
    help="JoinMarket wallet daemon - JAM-compatible HTTP/WebSocket API.",
)


@app.command()
def serve(
    host: Annotated[str, typer.Option(envvar="JMWALLETD_HOST", help="Bind address")] = "127.0.0.1",
    port: Annotated[int, typer.Option(help="HTTPS/HTTP port")] = 28183,
    ws_port: Annotated[int, typer.Option(help="WebSocket port (0 = same as HTTP)")] = 0,
    data_dir: Annotated[
        Path | None,
        typer.Option(
            envvar="JOINMARKET_DATA_DIR",
            help="Data directory (default: ~/.joinmarket-ng or $JOINMARKET_DATA_DIR)",
        ),
    ] = None,
    config_file: Annotated[
        Path | None,
        typer.Option(
            "--config-file",
            envvar="JOINMARKET_CONFIG_FILE",
            help="Config file path (decoupled from data dir). Defaults to <data-dir>/config.toml",
        ),
    ] = None,
    no_tls: Annotated[
        bool, typer.Option(envvar="JMWALLETD_NO_TLS", help="Disable TLS (plain HTTP)")
    ] = False,
) -> None:
    """Start the wallet daemon HTTP/WebSocket server."""
    import os
    import ssl

    import uvicorn
    from loguru import logger

    from jmcore.paths import get_default_data_dir
    from jmcore.process_hardening import harden_current_process
    from jmwalletd.app import create_app

    # Disable core dumps and ptrace before loading wallet secrets.
    harden_current_process()

    # Honour an explicit --config-file so the daemon reads (and creates) its
    # config outside the data dir when requested (FHS deployments, issue #537).
    if config_file is not None:
        os.environ["JOINMARKET_CONFIG_FILE"] = str(Path(config_file).expanduser())

    resolved_data_dir = data_dir or get_default_data_dir()
    resolved_data_dir.mkdir(parents=True, exist_ok=True)

    fast_app = create_app(data_dir=resolved_data_dir)

    ssl_context: ssl.SSLContext | None = None
    if not no_tls:
        ssl_dir = resolved_data_dir / "ssl"
        cert_file = ssl_dir / "cert.pem"
        key_file = ssl_dir / "key.pem"

        if not cert_file.exists() or not key_file.exists():
            _generate_self_signed_cert(ssl_dir)

        ssl_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ssl_context.load_cert_chain(str(cert_file), str(key_file))

    scheme = "http" if no_tls else "https"
    logger.info("Starting jmwalletd on {}://{}:{}", scheme, host, port)

    uvicorn.run(
        fast_app,
        host=host,
        port=port,
        ssl_certfile=str(resolved_data_dir / "ssl" / "cert.pem") if not no_tls else None,
        ssl_keyfile=str(resolved_data_dir / "ssl" / "key.pem") if not no_tls else None,
        log_level="info",
    )


def _generate_self_signed_cert(ssl_dir: Path) -> None:
    """Generate a self-signed TLS certificate for the daemon."""
    from loguru import logger

    ssl_dir.mkdir(parents=True, exist_ok=True)
    cert_file = ssl_dir / "cert.pem"
    key_file = ssl_dir / "key.pem"

    logger.info("Generating self-signed TLS certificate in {}", ssl_dir)

    import datetime
    import ipaddress

    from cryptography import x509
    from cryptography.hazmat.primitives import hashes, serialization
    from cryptography.hazmat.primitives.asymmetric import rsa
    from cryptography.x509.oid import NameOID

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name(
        [
            x509.NameAttribute(NameOID.COMMON_NAME, "jmwalletd"),
            x509.NameAttribute(NameOID.ORGANIZATION_NAME, "JoinMarket-NG"),
        ]
    )

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.UTC))
        .not_valid_after(datetime.datetime.now(datetime.UTC) + datetime.timedelta(days=3650))
        .add_extension(
            x509.SubjectAlternativeName(
                [
                    x509.DNSName("localhost"),
                    x509.IPAddress(ipaddress.IPv4Address("127.0.0.1")),
                ]
            ),
            critical=False,
        )
        .sign(key, hashes.SHA256())
    )

    key_file.write_bytes(
        key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    cert_file.write_bytes(cert.public_bytes(serialization.Encoding.PEM))

    logger.info("TLS certificate generated: {}", cert_file)


def main() -> None:
    """Entry point for the ``jmwalletd`` console script."""
    app()


if __name__ == "__main__":
    main()
