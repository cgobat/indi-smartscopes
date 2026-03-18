#!/bin/bash

SCRIPT_PATH="$(realpath -- "${BASH_SOURCE[0]}")"
DRIVER_DIR="$(dirname "$SCRIPT_PATH")"
PROTO_DIR="$DRIVER_DIR/dwarfAlp/src/dwarf_alpaca/proto"
PROTOC="$(command -v protoc)"
if [ -z "$PROTOC" ]; then
    echo "ERROR: protoc not found. Is protobuf installed?"
    exit 1
fi

"$PROTOC" -I "$PROTO_DIR" --python_out="$PROTO_DIR" "$PROTO_DIR"/*.proto
