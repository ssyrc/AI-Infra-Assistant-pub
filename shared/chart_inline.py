"""
차트를 **답변 안에 그대로 박아** 보낸다(data URI). 설정도, 열어 둘 포트도 필요 없다.

왜 이렇게 하나:
  차트 이미지는 결국 **사용자 브라우저**가 가져가야 한다. 예전 방식은 Chart MCP가
  `http://<배포호스트>:8509/charts/<id>.svg` 주소를 돌려주고 브라우저가 그 주소로 받아가는
  것이었다. 폐쇄망 안에서만 도는 구조지만(사외망과 무관하다), 두 가지가 걸린다.
    1) 컨테이너는 자기 외부 주소를 모른다 -> 관리자가 `chart_public_base_url`을 손으로 넣어야 하고,
       틀리면 조용히 깨진 이미지가 된다.
    2) 이미지용 호스트 포트를 하나 더 열어야 한다.

  그래서 Chart MCP는 짧은 표시자(`chart://<id>`)만 돌려주고, **Agent Server가 답변을 내보낼 때**
  그 자리를 `data:image/svg+xml;base64,...`로 바꾼다. SVG는 도커 내부망으로 가져오므로
  브라우저는 Open WebUI(8502) 하나만 알면 된다.

왜 MCP가 처음부터 data URI를 돌려주지 않나:
  MCP 툴 결과는 **그대로 다음 요청 프롬프트에 실린다**(#110). base64를 돌려주면 32768 컨텍스트가
  한 번에 날아간다. 표시자는 40자 남짓이라 예산에 영향이 없고, 치환은 LLM이 보지 않는
  '내보내는 텍스트'에서만 일어난다. 대화 이력에도 표시자가 그대로 남는다.

스트리밍이라 표시자가 두 델타에 걸쳐 쪼개져 올 수 있다. 그래서 '아직 표시자로 자랄 수 있는
꼬리'만 붙들고 나머지를 흘린다(_holdback).
"""
import base64
import re

MARKER_SCHEME = "chart://"
_ID_RE = "[0-9a-f]{16,64}"
_TOKEN_RE = re.compile(MARKER_SCHEME + f"({_ID_RE})")
_HEX = set("0123456789abcdef")
MAX_ID_LEN = 64


def marker_for(chart_id: str) -> str:
    return f"{MARKER_SCHEME}{chart_id}"


def _holdback(buf: str) -> int:
    """buf에서 '지금 흘려보내도 안전한' 길이를 돌려준다.

    두 경우를 붙들어야 한다.
      1) 꼬리가 `chart://`의 일부인 경우(`...chart:/`) - 다음 델타에서 완성될 수 있다.
      2) `chart://`는 왔는데 id가 아직 이어지는 중인 경우.
    """
    for n in range(len(MARKER_SCHEME) - 1, 0, -1):
        if buf.endswith(MARKER_SCHEME[:n]):
            return len(buf) - n
    idx = buf.rfind(MARKER_SCHEME)
    if idx != -1:
        tail = buf[idx + len(MARKER_SCHEME):]
        if len(tail) < MAX_ID_LEN and all(c in _HEX for c in tail):
            return idx
    return len(buf)


def svg_to_data_uri(svg: str) -> str:
    b64 = base64.b64encode(svg.encode("utf-8")).decode("ascii")
    return f"data:image/svg+xml;base64,{b64}"


class ChartInliner:
    """`chart://<id>` 표시자를 data URI로 바꾸는 스트리밍 치환기.

    fetch_svg(chart_id) -> str|None 를 주입받는다(테스트에서 갈아끼울 수 있게).
    같은 차트가 여러 번 나와도 한 번만 가져온다.
    """

    def __init__(self, fetch_svg):
        self._fetch = fetch_svg
        self._buf = ""
        self._cache: dict[str, str] = {}
        self.replaced = 0
        self.failed = 0

    async def _data_uri(self, chart_id: str) -> str | None:
        if chart_id in self._cache:
            return self._cache[chart_id]
        try:
            svg = await self._fetch(chart_id)
        except Exception as e:  # noqa: BLE001
            print(f"[chart] SVG를 가져오지 못했습니다({chart_id}): {type(e).__name__}: {e}")
            svg = None
        if not svg:
            return None
        uri = svg_to_data_uri(svg)
        # 같은 답변에 같은 차트가 두 번 나오면 다시 가져오지 않는다.
        self._cache[chart_id] = uri
        return uri

    async def _substitute(self, text: str) -> str:
        out, last = [], 0
        for m in _TOKEN_RE.finditer(text):
            out.append(text[last:m.start()])
            uri = await self._data_uri(m.group(1))
            if uri:
                out.append(uri)
                self.replaced += 1
            else:
                # 깨진 이미지를 조용히 남기지 않는다. 사용자가 이유를 알 수 있게 적는다.
                out.append("")
                self.failed += 1
            last = m.end()
        out.append(text[last:])
        result = "".join(out)
        if self.failed and "차트 이미지를 불러오지 못했습니다" not in result:
            result += "\n\n(차트 이미지를 불러오지 못했습니다 - chart-mcp 상태를 확인하세요.)"
        return result

    async def feed(self, delta: str) -> str:
        """스트리밍 델타를 받아 '지금 내보낼 텍스트'를 돌려준다."""
        if not delta:
            return ""
        self._buf += delta
        cut = _holdback(self._buf)
        emit, self._buf = self._buf[:cut], self._buf[cut:]
        return await self._substitute(emit) if emit else ""

    async def flush(self) -> str:
        """스트림이 끝났을 때 붙들고 있던 꼬리를 마무리한다."""
        emit, self._buf = self._buf, ""
        return await self._substitute(emit) if emit else ""

    async def whole(self, text: str) -> str:
        """스트리밍이 아닌 경로(완성 응답)용."""
        return await self._substitute(text or "")


def charts_base_url(chart_mcp_url: str) -> str:
    """`http://chart-mcp:8005/mcp` -> `http://chart-mcp:8005`.
    도커 내부망 주소라 방화벽·외부 주소와 무관하다."""
    url = (chart_mcp_url or "").strip()
    if not url:
        return ""
    return url.rsplit("/mcp", 1)[0].rstrip("/") if "/mcp" in url else url.rstrip("/")
