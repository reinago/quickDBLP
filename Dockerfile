# syntax=docker/dockerfile:1.7
#
# quickDBLP: the web server, which keeps its data up to date by itself (see Readme.md, "Docker").
#
#   docker build -t quickdblp .
#   docker run -p 8000:8000 -v quickdblp-data:/data quickdblp
#
# Nothing secret is needed to build or run this: the build uses the files of this checkout, and the running container
# only talks to dblp.org. (The private registry login happens on the host, see Readme.md.)

# ----------------------------------------------------------------------------------------------------------------
# Stage 1: compile the parser (ponder_dblp). GCC 13 or newer is required, which Ubuntu 24.04 has.
FROM ubuntu:24.04 AS parser

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake ninja-build git curl ca-certificates zip unzip tar pkg-config \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /src/ponder_dblp
# only what the parser needs: the dependencies (zlib-ng, pugixml) are fetched and built by vcpkg during configure
COPY ponder_dblp/CMakeLists.txt ponder_dblp/CMakePresets.json ponder_dblp/vcpkg.json ./
COPY ponder_dblp/*.hpp ponder_dblp/*.cpp ./
RUN cmake --preset=linux-x64-release \
 && cmake --build --preset=linux-x64-release --target install \
 && test -x install/linux/x64-release/bin/ponder_dblp

# ----------------------------------------------------------------------------------------------------------------
# Stage 2: the server. Ubuntu 24.04 again, on purpose: the parser was built against its glibc (2.39), and a Debian 12
# based python image (glibc 2.36) might refuse to start it. Python 3.13.3, which pyproject.toml pins, comes from uv.
FROM ubuntu:24.04 AS runtime

ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates gzip \
 && rm -rf /var/lib/apt/lists/* \
 && useradd --system --uid 10001 --create-home quickdblp

COPY --from=ghcr.io/astral-sh/uv:0.12.22 /uv /usr/local/bin/uv

ENV UV_PYTHON_INSTALL_DIR=/opt/python \
    UV_PROJECT_ENVIRONMENT=/opt/venv \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1

WORKDIR /app
# dependencies first (a separate layer: only rebuilt when pyproject.toml or uv.lock change) ...
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project
# ... then the application itself, installed into the virtual environment (including the static web page); the source
# folder is removed afterwards, so that certainly the installed package runs
COPY quickdblp ./quickdblp
RUN uv sync --frozen --no-dev --no-editable \
 && rm -rf /app/quickdblp \
 && chmod -R a+rX /opt

COPY --from=parser /src/ponder_dblp/install/linux/x64-release/bin/ponder_dblp /usr/local/bin/ponder_dblp

# /data holds everything that changes: the downloaded dump, the databases, the state. Mount a volume there.
RUN mkdir /data && chown quickdblp:quickdblp /data
ENV PATH=/opt/venv/bin:$PATH \
    QUICKDBLP_DATA_DIR=/data \
    QUICKDBLP_PARSER=/usr/local/bin/ponder_dblp

USER quickdblp
VOLUME /data
EXPOSE 8000

# the server answers /api/meta even while the first import is still running
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/meta', timeout=5)" || exit 1

# One process (every extra worker process would hold its own copy of the data). With an empty /data volume the first
# start imports the current dump from dblp.org by itself; the page shows the progress.
CMD ["quickdblp", "serve", "--host", "0.0.0.0", "--port", "8000"]
