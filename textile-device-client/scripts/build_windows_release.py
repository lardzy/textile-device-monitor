from __future__ import annotations

import argparse
from pathlib import Path
import sys

from build_support import DEFAULT_SERVER_URL, DEFAULT_TIMESTAMP_URL
from build_windows_installer import build_installer, find_inno_setup_compiler
from build_windows_onedir import build as build_onedir


def main() -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Build a clean release onedir package, validate its manifest, and "
            "create the Windows installer."
        )
    )
    parser.add_argument(
        "--compiler",
        default="",
        help="Optional full path to ISCC.exe (or set ISCC_EXE).",
    )
    parser.add_argument(
        "--sign",
        action="store_true",
        help="Authenticode-sign both the client executable and installer.",
    )
    parser.add_argument(
        "--signtool",
        default="",
        help="Optional full path to signtool.exe (or set SIGNTOOL_EXE).",
    )
    parser.add_argument(
        "--certificate-thumbprint",
        default="",
        help=(
            "SHA-1 thumbprint of the code-signing certificate in the Windows "
            "certificate store (or set TDC_SIGN_CERT_THUMBPRINT)."
        ),
    )
    parser.add_argument(
        "--timestamp-url",
        default=DEFAULT_TIMESTAMP_URL,
        help="RFC 3161 timestamp server used when --sign is enabled.",
    )
    parser.add_argument(
        "--default-server-url",
        default=DEFAULT_SERVER_URL,
        help=(
            "Pure HTTP or HTTPS origin embedded into new installations "
            f"(default: {DEFAULT_SERVER_URL})."
        ),
    )
    parser.add_argument(
        "--tls-ca-bundle",
        default="",
        help=(
            "PEM root CA bundle packaged with the Windows client; required "
            "only when --default-server-url uses HTTPS."
        ),
    )
    args = parser.parse_args()

    root = Path(__file__).resolve().parents[1]
    requested_compiler = args.compiler.strip()
    if requested_compiler:
        compiler_path = Path(requested_compiler).expanduser()
        compiler = str(compiler_path) if compiler_path.is_file() else None
    else:
        compiler = find_inno_setup_compiler()
    if not compiler:
        print(
            "Inno Setup compiler not found. Install Inno Setup 6, pass "
            "--compiler, or set ISCC_EXE.",
            file=sys.stderr,
        )
        return 1

    signing_options = {
        "sign": args.sign,
        "signtool_path": args.signtool.strip() or None,
        "certificate_thumbprint": args.certificate_thumbprint.strip() or None,
        "timestamp_url": args.timestamp_url.strip(),
    }
    onedir_result = build_onedir(
        clean=True,
        console=False,
        bootloader_debug=False,
        default_server_url=args.default_server_url.strip(),
        tls_ca_bundle=args.tls_ca_bundle.strip() or None,
        **signing_options,
    )
    if onedir_result != 0:
        return onedir_result

    return build_installer(
        root=root,
        compiler_path=compiler,
        **signing_options,
    )


if __name__ == "__main__":
    raise SystemExit(main())
