"""
Chart MCP - 사용자가 추이/비교/비율을 "그래프로" 보고 싶어할 때 호출하는 차트 생성 MCP.

동작:
  create_chart(...)로 SVG를 만들어 파일로 저장하고, **짧은 표시자와 마크다운 한 줄만** 돌려준다.
  에이전트는 그 마크다운(`![제목](chart://<id>)`)을 답변에 그대로 넣고, Agent Server가 내보낼 때
  표시자를 data URI로 바꿔 넣는다(shared/chart_inline). 그래서 폐쇄망에서 **설정도, 열어 둘
  포트도 필요 없다** - 브라우저는 Open WebUI 하나만 알면 된다.

왜 이미지 바이트를 직접 안 돌려주나:
  MCP 툴 결과는 그대로 다음 요청 프롬프트에 실린다. base64를 돌려주면 컨텍스트 32768을
  한 번에 날려 먹는다. 표시자는 40자 남짓이라 예산에 영향이 없고, 치환은 LLM이 보지 않는
  '내보내는 텍스트'에서만 일어난다.

왜 antvis/mcp-server-chart를 그대로 쓰지 않았나:
  그 서버는 기본적으로 외부 렌더 서버(antv-studio.alipay.com)로 데이터를 보내 이미지를
  받아온다. 폐쇄망에서는 닿지 않고, 사내 데이터를 외부로 보내는 구조라 쓸 수도 없다.
  (사내에 GPT-Vis 렌더 서버를 띄운다면 그쪽으로 갈아탈 수 있다 - docs/HISTORY.md #110)

이 MCP는 실행/조회를 하지 않는다. 데이터는 **호출자가 준 것만** 그린다.
"""
import hashlib
import os
import re
import sys
import time

sys.path.append(os.path.join(os.path.dirname(__file__), "../../shared"))
sys.path.append(os.path.dirname(os.path.abspath(__file__)))
from config_store import get_config  # noqa: E402
from chart_inline import marker_for  # noqa: E402
from mcp_caller import CallerContextMiddleware  # noqa: E402
from svg_chart import CHART_TYPES, render  # noqa: E402

from mcp.server.fastmcp import FastMCP

mcp = FastMCP("chart-mcp", stateless_http=True, host="0.0.0.0")

CHART_DIR = os.environ.get("CHART_OUTPUT_DIR", "/app/charts")
_URL_PREFIX = "/charts/"
# 저장된 파일 이름은 우리가 만든 sha256 앞부분뿐이다. 그 외 이름은 서빙하지 않는다
# (경로 조작으로 컨테이너 안 다른 파일을 읽지 못하게 하는 마지막 방어선).
_SAFE_NAME = re.compile(r"^[0-9a-f]{16,64}\.svg$")

DEFAULT_MAX_POINTS = 200
DEFAULT_RETENTION_HOURS = 72


async def _config(key: str, default: str) -> str:
    """설정을 읽되, 설정 DB에 문제가 있어도 차트 생성을 막지 않는다(기본값으로 계속)."""
    try:
        value = await get_config(key, default)
    except Exception as e:  # noqa: BLE001
        print(f"[chart-mcp] 설정 '{key}'을 읽지 못해 기본값을 씁니다: {type(e).__name__}: {e}")
        return default
    return default if value is None else str(value)


async def _int_config(key: str, default: int) -> int:
    try:
        return int(await _config(key, str(default)))
    except (TypeError, ValueError):
        return default


def _cleanup(retention_hours: int) -> None:
    """오래된 차트 파일을 지운다. 차트를 만들 때마다 한 번씩 훑는다(파일 수가 적어 충분).

    실패해도 차트 생성을 막지 않는다 - 정리는 부수적인 일이다.
    """
    if retention_hours <= 0:
        return
    cutoff = time.time() - retention_hours * 3600
    try:
        with os.scandir(CHART_DIR) as it:
            for entry in it:
                try:
                    if entry.is_file() and entry.stat().st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except OSError:
        pass


def _normalize(labels, series, max_points: int) -> tuple[list, list[dict]]:
    """LLM이 준 데이터를 그릴 수 있는 형태로 맞춘다. 못 고칠 입력은 ValueError로 되돌린다."""
    labels = [str(v) for v in (labels or [])]
    if not labels:
        raise ValueError("labels(가로축 항목)가 비어 있습니다.")
    if len(labels) > max_points:
        raise ValueError(f"항목이 너무 많습니다({len(labels)}개, 최대 {max_points}개). "
                         "기간을 줄이거나 값을 미리 합쳐서 넘기세요.")
    if not series:
        raise ValueError("series(그릴 값)가 비어 있습니다.")

    out = []
    for i, s in enumerate(series):
        if not isinstance(s, dict):
            raise ValueError("series의 각 항목은 {\"name\": ..., \"values\": [...]} 형태여야 합니다.")
        raw = s.get("values")
        if raw is None:
            raise ValueError(f"series[{i}]에 values가 없습니다.")
        values = []
        for v in raw:
            if v is None or v == "":
                values.append(None)          # 값이 없는 구간은 선을 끊어 그린다
                continue
            try:
                values.append(float(v))
            except (TypeError, ValueError):
                raise ValueError(f"series[{i}]의 값에 숫자가 아닌 것이 있습니다: {v!r}")
        if len(values) != len(labels):
            raise ValueError(
                f"series[{i}]('{s.get('name') or ''}')의 값 개수({len(values)})가 "
                f"labels 개수({len(labels)})와 다릅니다. 두 목록의 길이를 맞춰서 넘기세요.")
        out.append({"name": str(s.get("name") or f"계열 {i + 1}"), "values": values})
    return labels, out


@mcp.tool()
async def create_chart(chart_type: str, labels: list, series: list,
                       title: str = "", y_label: str = "") -> dict:
    """수치 데이터를 차트 이미지로 만든다. 추이·비교·구성·분포를 그림으로 보여줄 때 쓴다.

    **읽는 사람이 무엇을 해야 하는가**로 종류를 고른다(막대가 기본값이 아니다):

      시간에 따른 변화       -> line (계열 1개면 area)
      항목끼리 크기 비교     -> bar. **항목 이름이 길거나 5개가 넘으면 hbar**(가로 막대)
      부분과 전체(구성)      -> stacked. 한 시점의 비율만 보여줄 때는 pie/donut
      두 값의 관계·분포      -> scatter
      값이 하나뿐            -> 차트를 만들지 않는다. 문장으로 답한다

    Args:
        chart_type: line | area | bar | hbar | stacked | pie | donut | scatter
        labels: 항목. 예: ["1월","2월","3월"] (hbar는 세로축, pie/donut은 조각 이름)
        series: [{"name": "계열 이름", "values": [12, 15, 9]}] - values 길이는 labels와 같아야 한다.
                pie/donut은 첫 번째 계열만 쓴다. 계열이 여러 개면 범례가 자동으로 붙는다.
        title: 차트 제목. 무엇을 그린 것인지 한 줄로.
        y_label: 값의 단위. 예: "GB", "%", "건"
    Returns:
        markdown(답변에 **그대로** 넣으면 그림이 표시된다), chart_id, chart_type, points.

    그림에는 마우스를 올려도 값이 뜨지 않는다(정적 이미지다). 막대·선 끝에 값이 찍히지만,
    **답변 본문에도 수치를 함께 적어라** - 그림을 못 보는 경우에도 답이 남아야 한다.
    """
    kind = (chart_type or "line").strip().lower()
    if kind not in CHART_TYPES:
        raise ValueError(f"chart_type은 {', '.join(CHART_TYPES)} 중 하나여야 합니다: {chart_type!r}")

    max_points = await _int_config("chart_max_points", DEFAULT_MAX_POINTS)
    labels, series = _normalize(labels, series, max_points)

    svg = render(kind, labels, series, title=title or "", y_label=y_label or "")
    # 내용 해시를 파일 이름으로 쓴다 - 같은 차트를 다시 요청해도 파일이 늘지 않고,
    # 이름을 추측해 남의 차트를 열어볼 수도 없다.
    name = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:32] + ".svg"
    os.makedirs(CHART_DIR, exist_ok=True)
    path = os.path.join(CHART_DIR, name)
    if not os.path.exists(path):
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(svg)
        os.replace(tmp, path)               # 반쯤 쓰인 파일이 서빙되지 않도록
    _cleanup(await _int_config("chart_retention_hours", DEFAULT_RETENTION_HOURS))

    chart_id = name[:-len(".svg")]
    # 기본은 **표시자**다. Agent Server가 답변을 내보낼 때 data URI로 바꿔 넣는다
    # (설정도 열어 둘 포트도 필요 없다 - shared/chart_inline 참고).
    # `chart_public_base_url`을 넣어 두면 그 주소를 직접 쓴다(이미지를 URL로 두고 싶을 때).
    base = (await _config("chart_public_base_url", "")).strip().rstrip("/")
    link = f"{base}{_URL_PREFIX}{name}" if base else marker_for(chart_id)
    return {
        "chart_type": kind,
        "points": len(labels),
        "chart_id": chart_id,
        "markdown": f"![{title or '차트'}]({link})",
    }


class ChartFiles:
    """생성된 SVG를 그대로 내려주는 최소 정적 서버. 나머지 요청은 MCP 앱으로 넘긴다.

    Open WebUI 화면(사용자 브라우저)이 직접 가져가는 경로라, MCP 프로토콜과 같은 포트에
    얹어 둔다. 읽기 전용이고 이름 규칙(_SAFE_NAME)에 맞는 파일만 연다.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith(_URL_PREFIX):
            await self.app(scope, receive, send)
            return

        name = scope["path"][len(_URL_PREFIX):]
        status, ctype, body = 404, b"text/plain; charset=utf-8", b"not found"
        if scope.get("method") not in ("GET", "HEAD"):
            status, body = 405, b"method not allowed"
        elif _SAFE_NAME.match(name):
            try:
                with open(os.path.join(CHART_DIR, name), "rb") as f:
                    status, ctype, body = 200, b"image/svg+xml; charset=utf-8", f.read()
            except OSError:
                pass

        headers = [(b"content-type", ctype), (b"content-length", str(len(body)).encode())]
        if status == 200:
            # 내용 해시가 곧 파일 이름이라 내용이 바뀌면 이름도 바뀐다 -> 길게 캐시해도 안전하다.
            headers.append((b"cache-control", b"public, max-age=86400, immutable"))
        await send({"type": "http.response.start", "status": status, "headers": headers})
        await send({"type": "http.response.body",
                    "body": b"" if scope.get("method") == "HEAD" else body})


if __name__ == "__main__":
    import uvicorn

    port = int(os.environ.get("MCP_PORT", 8005))
    os.makedirs(CHART_DIR, exist_ok=True)
    uvicorn.run(ChartFiles(CallerContextMiddleware(
                    mcp.streamable_http_app(),
                    secret_getter=lambda: get_config("mcp_shared_secret", ""))),
                host="0.0.0.0", port=port)
