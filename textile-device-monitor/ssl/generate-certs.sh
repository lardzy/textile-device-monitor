#!/usr/bin/env sh
set -eu

CERT_DIR="$(cd "$(dirname "$0")" && pwd)"
HOSTNAME="${1:-localhost}"
ALT_NAMES="${2:-localhost,127.0.0.1}"

KEY_FILE="${CERT_DIR}/localhost.key"
CERT_FILE="${CERT_DIR}/localhost.crt"
OPENSSL_CONFIG="${CERT_DIR}/openssl.cnf"

if ! command -v openssl >/dev/null 2>&1; then
  echo "openssl not found. Please install OpenSSL." >&2
  exit 1
fi

SAN_LIST=""
OLD_IFS="$IFS"
IFS=","
for name in $ALT_NAMES; do
  case "$name" in
    *[!0-9.]* )
      SAN_LIST="${SAN_LIST}DNS:${name},"
      ;;
    * )
      SAN_LIST="${SAN_LIST}IP:${name},"
      ;;
  esac
done
IFS="$OLD_IFS"
SAN_LIST="${SAN_LIST%,}"

cat > "$OPENSSL_CONFIG" <<EOF
[req]
default_bits = 2048
prompt = no
default_md = sha256
x509_extensions = v3_req
distinguished_name = dn

[dn]
CN = ${HOSTNAME}

[v3_req]
subjectAltName = ${SAN_LIST}
EOF

openssl req -x509 -nodes -days 825 -newkey rsa:2048 \
  -keyout "$KEY_FILE" \
  -out "$CERT_FILE" \
  -config "$OPENSSL_CONFIG"

echo "Generated:"
echo "  $CERT_FILE"
echo "  $KEY_FILE"
