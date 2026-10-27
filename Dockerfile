# Runs the web interface. Built for amd64, arm64 and arm/v7 by .github/workflows/release.yml.
FROM debian:trixie-slim

# numpy (needed by ezdxf) has no 32-bit ARM wheels on PyPI, so take Debian's build and pip-install the rest
# into a venv that can see it. This keeps the arm/v7 build from compiling numpy under emulation.
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-numpy \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv --system-site-packages /venv

WORKDIR /app
COPY requirements.txt .
RUN /venv/bin/pip install --no-cache-dir -r requirements.txt
COPY . .

# Saved presets live in the /data volume, so updating the image never touches them.
RUN useradd --system --no-create-home app && mkdir /data && chown app /data
VOLUME /data
USER app

ARG VERSION=dev
ENV CNC_RINGS_DATA=/data \
    CNC_RINGS_VERSION=$VERSION \
    PYTHONUNBUFFERED=1 \
    PATH=/venv/bin:$PATH

EXPOSE 8000
HEALTHCHECK --interval=1m --timeout=5s CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/presets')"
CMD ["waitress-serve", "--host=0.0.0.0", "--port=8000", "webapp:app"]
