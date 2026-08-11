ARG PYTHON_IMAGE=python:3.11-slim-bullseye
FROM ${PYTHON_IMAGE}
# 폐쇄망 빌드: pip 미러/프록시 (기본값=공개 PyPI 직접). compose가 .env에서 전달한다.
ARG PIP_INDEX_URL=https://pypi.org/simple
ARG PIP_TRUSTED_HOST=
ARG BUILD_PROXY=
ARG NO_PROXY=
ARG APT_MIRROR=
ARG APT_HTTP_TIMEOUT=10
ENV PIP_INDEX_URL=${PIP_INDEX_URL} PIP_TRUSTED_HOST=${PIP_TRUSTED_HOST} PIP_DISABLE_PIP_VERSION_CHECK=1 \
    http_proxy=${BUILD_PROXY} https_proxy=${BUILD_PROXY} HTTP_PROXY=${BUILD_PROXY} HTTPS_PROXY=${BUILD_PROXY} \
    no_proxy=${NO_PROXY} NO_PROXY=${NO_PROXY}
WORKDIR /app
# 폐쇄망 사내 미러는 최신 pip의 JSON Simple API(PEP 691)를 지원하지 않아, 내장 최신 pip(24.x)가
# 인덱스 응답을 json.loads()하다 JSONDecodeError로 빌드가 깨진다. HTML API만 쓰는 pip<22.3으로
# 내려야 하는데 최신 pip로는 미러에서 그 pip조차 못 받으므로(닭-달걀), vendor/에 미리 받아둔 pip
# 휠을 오프라인으로 먼저 설치한다. (vendor 휠이 없으면 건너뜀 = 공개 PyPI 등 정상 미러용)
COPY vendor/ /tmp/vendor/
RUN mkdir -p /tmp/vendor-wheels; cp /tmp/vendor/*.whl /tmp/vendor-wheels/ 2>/dev/null || true
RUN if ls /tmp/vendor/pip-*.whl >/dev/null 2>&1; then \
      pip install --no-index --no-cache-dir /tmp/vendor/pip-*.whl ; \
    fi
# vendor/에 다른 whl(미러에 없거나 불안정한 패키지)이 있으면 패키지 본체만 오프라인으로
# 먼저 설치한다. 의존성은 이후 requirements.txt 단계에서 평소처럼 미러에서 받는다.
RUN for w in /tmp/vendor/*.whl; do \
      [ -e "$w" ] || continue; \
      case "$(basename "$w")" in pip-*) continue ;; esac; \
      pip install --no-index --no-cache-dir --no-deps "$w"; \
    done
# openssh-client: System/Command MCP가 대상 서버로 ssh 실행할 때 필요.
RUN set -eux; \
    if ls /tmp/vendor/deb/*.deb >/dev/null 2>&1; then \
      if [ -f /tmp/vendor/deb/SHA256SUMS ]; then \
        (cd /tmp/vendor/deb && sha256sum -c SHA256SUMS); \
      fi; \
      DEBIAN_FRONTEND=noninteractive dpkg --unpack /tmp/vendor/deb/*.deb; \
      DEBIAN_FRONTEND=noninteractive dpkg --configure -a; \
      ssh -V; \
    else \
      if [ -n "$APT_MIRROR" ]; then \
        . /etc/os-release; \
        rm -f /etc/apt/sources.list.d/*.list /etc/apt/sources.list.d/*.sources; \
        printf 'deb %s %s main\n' "$APT_MIRROR" "$VERSION_CODENAME" > /etc/apt/sources.list; \
      fi; \
      apt-get \
        -o Acquire::Retries=0 \
        -o Acquire::http::Timeout="$APT_HTTP_TIMEOUT" \
        -o Acquire::https::Timeout="$APT_HTTP_TIMEOUT" \
        -o APT::Update::Error-Mode=any \
        update; \
      DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends openssh-client; \
    fi; \
    rm -rf /var/lib/apt/lists/* /tmp/vendor/deb
COPY mcp_servers/requirements.txt .
RUN pip install --no-cache-dir --find-links /tmp/vendor-wheels -r requirements.txt
# 빌드 프록시를 런타임 컨테이너에 남기지 않는다(내부 서비스 통신이 프록시로 새지 않도록).
ENV http_proxy= https_proxy= HTTP_PROXY= HTTPS_PROXY= no_proxy= NO_PROXY=
COPY shared/ ./shared/
COPY mcp_servers/ ./mcp_servers/
EXPOSE 8001
# 실제 실행할 서버는 docker-compose의 command로 지정 (예: python mcp_servers/manual_mcp/server.py)
