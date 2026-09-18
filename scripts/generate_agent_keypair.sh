#!/usr/bin/env bash
# Generates an RSA key pair for the Bookly agent's Snowflake service user
# (BOOKLY_AGENT_SVC) and prints the public key in the form Snowflake expects
# for the RSA_PUBLIC_KEY value in snowflake/04_agent_access.sql.
#
# Usage: ./scripts/generate_agent_keypair.sh [output-dir]
# Defaults to ./secrets/ (gitignored) if no output dir is given.

set -euo pipefail

OUT_DIR="${1:-secrets}"
mkdir -p "$OUT_DIR"

PRIVATE_KEY="$OUT_DIR/bookly_agent_rsa_key.p8"
PUBLIC_KEY="$OUT_DIR/bookly_agent_rsa_key.pub"

if [ -f "$PRIVATE_KEY" ]; then
  echo "Refusing to overwrite existing $PRIVATE_KEY -- move or delete it first if you want a new key." >&2
  exit 1
fi

echo "Generating an encrypted private key -- you'll be prompted for a passphrase."
echo "(Use that same passphrase for SNOWFLAKE_PRIVATE_KEY_PASSPHRASE in your .env.)"
echo
openssl genrsa 2048 | openssl pkcs8 -topk8 -v2 des3 -inform PEM -out "$PRIVATE_KEY"
chmod 600 "$PRIVATE_KEY"

openssl rsa -in "$PRIVATE_KEY" -pubout -out "$PUBLIC_KEY"

echo
echo "Private key: $PRIVATE_KEY   (keep secret -- already gitignored)"
echo "Public key:  $PUBLIC_KEY"
echo
echo "Paste this into snowflake/04_agent_access.sql, replacing <PASTE_PUBLIC_KEY_HERE>"
echo "(Snowflake wants just the base64 body -- no BEGIN/END lines, no newlines):"
echo
grep -v -- '-----' "$PUBLIC_KEY" | tr -d '\n'
echo
echo
echo "Then in your .env, set:"
echo "  SNOWFLAKE_PRIVATE_KEY_PATH=$(cd "$OUT_DIR" && pwd)/bookly_agent_rsa_key.p8"
echo "  SNOWFLAKE_PRIVATE_KEY_PASSPHRASE=<the passphrase you entered above>"
