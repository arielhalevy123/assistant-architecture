#!/usr/bin/env bash
# Downloads the multilingual "small" whisper.cpp model (~466 MB). "base" is not good enough for Hebrew.
set -euo pipefail
DIR="${ASSISTANT_STATE:-$HOME/.assistant}/models"
mkdir -p "$DIR"
if [ -s "$DIR/ggml-small.bin" ]; then echo "model already there"; exit 0; fi
curl -L --fail -o "$DIR/ggml-small.bin" \
  "https://huggingface.co/ggerganov/whisper.cpp/resolve/main/ggml-small.bin"
echo "saved $DIR/ggml-small.bin"
