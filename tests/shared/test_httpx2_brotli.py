"""httpx2 decodes brotli responses with the installed Brotli.

httpx2 — the OpenAI client's transport — calls
``Decompressor.process(data, output_buffer_limit=...)``, which Brotli added in
1.2.0. With 1.1.0 every brotli-encoded OpenAI response raised ``TypeError:
process() takes no keyword arguments`` and the chat turn failed (2026-09-24).
"""

import brotli
from httpx2._decoders import BrotliDecoder


def test_httpx2_decodes_brotli_body():
    body = b'{"output": "hello"}' * 1000
    decoder = BrotliDecoder()
    decoded = b"".join(decoder.decode(brotli.compress(body)))
    decoded += b"".join(decoder.flush())
    assert decoded == body
