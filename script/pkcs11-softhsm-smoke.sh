#!/usr/bin/env bash
# PKCS#11 SoftHSM2 integration test: real token, real signatures, mock everything else.
# Nothing here touches a live chain, an HSM, or real funds. SoftHSM2 is a
# software-backed behavior double: it exercises the PKCS#11 path, not hardware isolation.
set -euo pipefail
cd "$(dirname "$0")/.."
WORKDIR="$(mktemp -d)"
cleanup() { rm -rf "$WORKDIR"; }
trap cleanup EXIT
mkdir -p "$WORKDIR/tokens"
cat > "$WORKDIR/softhsm2.conf" <<EOF
directories.tokendir = $WORKDIR/tokens
objectstore.backend = file
EOF
SOFTHSM_BIN="$(nix build --print-out-paths nixpkgs#softhsm --no-link)/bin/softhsm2-util"
OPENSC_BIN="$(nix build --print-out-paths nixpkgs#opensc --no-link)/bin"
SOFTHSM_LIB_DIR="$(nix build --print-out-paths nixpkgs#softhsm --no-link)/lib/softhsm"
PKCS11_LIB="$SOFTHSM_LIB_DIR/libsofthsm2.so"
export SOFTHSM2_CONF="$WORKDIR/softhsm2.conf"
"$SOFTHSM_BIN" --init-token --slot 0 --label tameion-test --so-pin 123456 --pin 2345 >/dev/null
export TAMEION_TEST_PKCS11=1
export TAMEION_SOFTHSM_CONF="$WORKDIR/softhsm2.conf"
export TAMEION_SOFTHSM_UTIL="$SOFTHSM_BIN"
export TAMEION_PKCS11_LIB="$PKCS11_LIB"
export TAMEION_PKCS11_TOOL="$OPENSC_BIN/pkcs11-tool"
PATH="$OPENSC_BIN:$PATH" uv run --frozen pytest -q tests/test_pkcs11_signer.py
echo "PKCS#11 SoftHSM2 integration tests passed (behavioral double only; not hardware assurance)."
