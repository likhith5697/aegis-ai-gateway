# Same image as the gateway, so tests run on the exact Python + LiteLLM we ship.
FROM ghcr.io/berriai/litellm:v1.105.0

# The image has no pip; ensurepip installs the copy bundled with Python, then we add pytest.
RUN python -m ensurepip && python -m pip install --no-cache-dir pytest==9.1.1
