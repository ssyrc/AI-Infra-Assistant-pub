"""
차트를 **순수 파이썬으로 SVG 문자열**로 그린다. 외부 라이브러리도, 외부 렌더 서버도 쓰지 않는다.

왜 matplotlib이 아닌가(폐쇄망 제약):
  1) 새 pip 패키지는 이미지 재빌드를 부른다(반영 절차 B). SVG는 표준 라이브러리만 쓰므로
     코드만 rsync하고 재시작하면 끝난다(절차 A).
  2) slim 이미지에는 **한글 폰트가 없다.** matplotlib으로 그리면 축 라벨이 전부 두부(□□□)가
     된다. SVG는 글자를 그대로 담고 브라우저 폰트로 그려지므로 한글이 깨지지 않는다.
  3) PNG는 바이트가 커서 결과를 프롬프트에 실을 수 없다. SVG는 파일로 저장하고
     표시자만 돌려주면 되므로 컨텍스트 예산에 영향이 없다.

antvis/mcp-server-chart를 그대로 쓰지 않은 이유는 docs/HISTORY.md #110 참고
(기본값이 외부 렌더 서버 호출이라 폐쇄망에서 동작하지 않는다).

## 디자인 근거 (#159)

색·마크 규격은 눈으로 고르지 않았다. 팔레트는 여섯 가지 검사(밝기 대역·채도 하한·색각이상
분리도·정상시야 하한·표면 대비·문서화)를 **검증기로 돌려서** 통과한 것만 쓴다.
결과는 docs/HISTORY.md #159에 붙여 뒀다. 색을 바꾸고 싶으면 **먼저 검증기를 돌린다.**

- 계열 색은 **고정 순서로 배정하고 절대 재활용하지 않는다.** 9번째 계열에 색을 만들어 내면
  색각이상에서 기존 색과 구분되지 않는다 → `_MAX_SERIES`에서 '기타'로 접는다.
- **글자는 계열 색을 입지 않는다.** 값·라벨·범례 글자는 전부 잉크 토큰이고, 정체성은 글자
  옆의 색칠된 마크가 나른다. 밝은 계열색(노랑·청록)은 표면 위에서 글자로 읽히지 않는다.
- 밝은 모드에서 세 색(청록·노랑·자홍)은 표면 대비 3:1 미만이다. 검증기가 요구하는 완화가
  **직접 값 라벨**이라 막대·선 끝에 값을 찍는다. 이 라벨은 장식이 아니라 접근성 요건이다.
- 마크는 얇게, 격자는 뒤로. 막대는 24px를 넘지 않고 값 쪽 끝만 둥글다.
- **툴팁이 없다.** SVG가 `<img>`(data URI)로 박히므로 hover가 동작하지 않는다. 그래서
  값을 읽을 경로가 화면에 보이는 것뿐이고, 직접 라벨을 더 적극적으로 쓴다.
"""
import html
import math

# 계열(categorical) 팔레트 — 8슬롯 고정 순서. 밝은/어두운 모드는 같은 여덟 색을 각 표면에
# 맞춰 다시 뽑은 것이다(뒤집은 것이 아니다). 순서 자체가 색각이상 안전장치라 바꾸지 않는다.
SERIES_LIGHT = ["#2a78d6", "#eb6834", "#1baf7a", "#eda100",
                "#e87ba4", "#008300", "#4a3aa7", "#e34948"]
SERIES_DARK = ["#3987e5", "#d95926", "#199e70", "#c98500",
               "#d55181", "#008300", "#9085e9", "#e66767"]

# 흩뿌림(scatter)은 어느 두 점이든 나란히 놓일 수 있어 **모든 쌍**이 검사 대상이다.
# 그 조건에서 여덟 색은 통과하지 못하고 앞 세 슬롯만 통과한다(검증기 확인). 색을 더 만드는
# 대신 계열 수를 줄이는 것이 정답이라, 그 이상은 잘라 낸다.
_MAX_SERIES = 8
_MAX_SERIES_SCATTER = 3

_LIGHT = {
    "surface": "#fcfcfb", "ink": "#0b0b0b", "ink2": "#52514e", "muted": "#898781",
    "grid": "#e1e0d9", "axis": "#c3c2b7", "edge": "rgba(11,11,11,0.10)",
}
_DARK = {
    "surface": "#1a1a19", "ink": "#ffffff", "ink2": "#c3c2b7", "muted": "#898781",
    "grid": "#2c2c2a", "axis": "#383835", "edge": "rgba(255,255,255,0.10)",
}

CHART_TYPES = ("line", "area", "bar", "hbar", "stacked", "pie", "donut", "scatter")

_FONT = ("system-ui,-apple-system,'Segoe UI','Malgun Gothic',"
         "'Noto Sans KR','Apple SD Gothic Neo',sans-serif")

_W, _H = 760, 440
_BAR_MAX = 24.0        # 막대 두께 상한. 슬롯을 꽉 채우지 않고 남는 만큼을 여백으로 둔다
_BAR_R = 4.0           # 값 쪽 끝 모서리 반경(바닥은 각지게)
_SURFACE_GAP = 2.0     # 맞닿는 마크 사이의 '표면 색 틈'. 테두리를 그리지 않고 이걸로 가른다


def _esc(text) -> str:
    return html.escape(str(text if text is not None else ""), quote=True)


def _mode_vars(tokens: dict, series: list[str]) -> str:
    rows = [f"  .bg{{fill:{tokens['surface']}}}",
            f"  .edge{{stroke:{tokens['edge']}}}",
            f"  .grid{{stroke:{tokens['grid']}}}",
            f"  .axis{{stroke:{tokens['axis']}}}",
            f"  .ink{{fill:{tokens['ink']}}}",
            f"  .ink2{{fill:{tokens['ink2']}}}",
            f"  .mut{{fill:{tokens['muted']}}}",
            f"  .gap{{stroke:{tokens['surface']}}}"]
    for i, hex_ in enumerate(series, 1):
        rows.append(f"  .f{i}{{fill:{hex_}}} .k{i}{{stroke:{hex_}}}")
    return "\n".join(rows)


def _style() -> str:
    """밝은 값이 기본이고 어두운 모드만 덮어쓴다.

    `<img>`로 참조된 SVG에서도 최신 브라우저는 `prefers-color-scheme`를 따른다. 지원하지
    않는 환경에서는 밝은 쪽으로 그려져 그대로 읽힌다(색을 미디어 쿼리 안에서만 정의하지
    않는 이유다 — 그러면 구형 뷰어에서 색이 통째로 빈다).
    """
    return (f"\n  text{{font-family:{_FONT}}}\n"
            "  .ttl{font-size:16px;font-weight:600}\n"
            "  .lbl{font-size:11.5px}\n"
            "  .val{font-size:11px;font-weight:600}\n"
            "  .tick{font-size:11px;font-variant-numeric:tabular-nums}\n"
            + _mode_vars(_LIGHT, SERIES_LIGHT) + "\n"
            "  @media (prefers-color-scheme: dark) {\n"
            + _mode_vars(_DARK, SERIES_DARK) + "\n  }\n")


def _fmt(v: float) -> str:
    """값 표기. 정수는 천 단위 콤마로, 큰 수는 k/M로 줄인다."""
    if v is None:
        return ""
    a = abs(v)
    if a >= 1_000_000:
        return f"{v / 1_000_000:.1f}M".replace(".0M", "M")
    if a >= 10_000:
        return f"{v / 1000:.0f}k"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:.2f}".rstrip("0").rstrip(".")


def _fmt_value(v: float) -> str:
    """**읽으라고 찍는 값**의 표기. 축 눈금과 달리 반올림해 뭉개지 않는다.

    `_fmt`는 눈금용이라 15,630을 `16k`로 줄인다. 눈금은 원래 둥근 수라 괜찮지만, 막대 위에
    찍는 값까지 그러면 **화면에 틀린 수가 보인다**(사용자가 그 숫자를 그대로 옮겨 적는다).
    """
    if v is None:
        return ""
    if abs(v) >= 10_000_000:
        # 여기부터는 자리수가 라벨 폭을 넘는다. 유효숫자를 남기고 줄인다(15,630,000 -> 15.63M).
        return f"{v / 1_000_000:.2f}".rstrip("0").rstrip(".") + "M"
    if v == int(v):
        return f"{int(v):,}"
    return f"{v:,.2f}".rstrip("0").rstrip(".")


def _text_w(text: str, size: float) -> float:
    """글자 폭 어림. 한글·한자는 정사각(1.0em), 나머지는 0.55em으로 본다.

    라벨이 막대 밖으로 삐져나가거나 범례가 캔버스를 넘는 것을 **그리기 전에** 막으려면
    폭을 알아야 한다. 폰트 메트릭을 읽을 수 없으니(폐쇄망·표준 라이브러리만) 어림한다 —
    한글을 0.55em으로 보면 실제보다 훨씬 좁게 잡혀 라벨이 겹친다.
    """
    wide = sum(1 for ch in str(text)
               if "가" <= ch <= "힣" or "㄰" <= ch <= "㆏"
               or "一" <= ch <= "鿿" or "！" <= ch <= "｠")
    return (wide * 1.0 + (len(str(text)) - wide) * 0.55) * size


def _nice_ticks(lo: float, hi: float, count: int = 5) -> list[float]:
    """1/2/5 x 10^n 규칙으로 눈금을 고른다(0.1, 0.2, 0.5, 1, 2, 5, 10 ...)."""
    if hi <= lo:
        hi = lo + 1
    raw = (hi - lo) / max(1, count)
    mag = 10 ** math.floor(math.log10(raw)) if raw > 0 else 1
    step = mag
    for m in (1, 2, 5, 10):
        step = m * mag
        if raw <= step:
            break
    start = math.floor(lo / step) * step
    ticks, v = [], start
    while v <= hi + step * 0.5:
        # 부동소수 누적 오차로 -0.0이나 2.9999가 나오지 않게 스텝 단위로 반올림한다.
        ticks.append(round(v / step) * step)
        v += step
    return ticks


def _values(s: dict) -> list:
    return list(s.get("values") or [])


def _flat(series: list[dict]) -> list[float]:
    return [v for s in series for v in _values(s) if v is not None]


def _y_range(series: list[dict], include_zero: bool) -> tuple[float, float]:
    flat = _flat(series)
    if not flat:
        return 0.0, 1.0
    lo, hi = min(flat), max(flat)
    if include_zero:
        lo, hi = min(lo, 0.0), max(hi, 0.0)
    if lo == hi:
        # 값이 전부 같으면 선이 축에 붙어 안 보인다. 위아래로 여유를 준다.
        pad = abs(lo) * 0.1 or 1.0
        lo, hi = lo - pad, hi + pad
    return lo, hi


def _stack_totals(labels, series) -> list[float]:
    """누적 막대의 열별 합계. 음수는 0으로 본다(아래로 자라는 누적은 그리지 않는다)."""
    out = []
    for i in range(len(labels)):
        out.append(sum(max(0.0, (_values(s)[i] if i < len(_values(s)) else 0) or 0)
                       for s in series))
    return out


# --- 마크 -----------------------------------------------------------------------------

def _bar_v(x: float, w: float, y_base: float, y_val: float, r: float = _BAR_R) -> str:
    """세로 막대 경로. **값 쪽 끝만 둥글고 바닥은 각지다**(기준선에 붙어야 한다)."""
    top, bot = min(y_base, y_val), max(y_base, y_val)
    r = max(0.0, min(r, w / 2, bot - top))
    if y_val <= y_base:      # 위로 자란다 → 위쪽 두 모서리
        return (f"M{x:.1f},{bot:.1f} V{top + r:.1f} Q{x:.1f},{top:.1f} {x + r:.1f},{top:.1f} "
                f"H{x + w - r:.1f} Q{x + w:.1f},{top:.1f} {x + w:.1f},{top + r:.1f} "
                f"V{bot:.1f} Z")
    return (f"M{x:.1f},{top:.1f} V{bot - r:.1f} Q{x:.1f},{bot:.1f} {x + r:.1f},{bot:.1f} "
            f"H{x + w - r:.1f} Q{x + w:.1f},{bot:.1f} {x + w:.1f},{bot - r:.1f} "
            f"V{top:.1f} Z")


def _bar_h(y: float, h: float, x_base: float, x_val: float, r: float = _BAR_R) -> str:
    """가로 막대 경로. 값 쪽 끝만 둥글다."""
    lo, hi = min(x_base, x_val), max(x_base, x_val)
    r = max(0.0, min(r, h / 2, hi - lo))
    if x_val >= x_base:      # 오른쪽으로 자란다
        return (f"M{lo:.1f},{y:.1f} H{hi - r:.1f} Q{hi:.1f},{y:.1f} {hi:.1f},{y + r:.1f} "
                f"V{y + h - r:.1f} Q{hi:.1f},{y + h:.1f} {hi - r:.1f},{y + h:.1f} "
                f"H{lo:.1f} Z")
    return (f"M{hi:.1f},{y:.1f} H{lo + r:.1f} Q{lo:.1f},{y:.1f} {lo:.1f},{y + r:.1f} "
            f"V{y + h - r:.1f} Q{lo:.1f},{y + h:.1f} {lo + r:.1f},{y + h:.1f} "
            f"H{hi:.1f} Z")


def _dot(x: float, y: float, si: int, r: float = 4.0) -> str:
    """점 마커. **표면 색 2px 링**을 둘러 선·다른 점과 겹쳐도 읽히게 한다(테두리가 아니다)."""
    return (f'<circle class="f{si} gap" cx="{x:.1f}" cy="{y:.1f}" r="{r}" '
            f'stroke-width="2"/>')


def _value_text(x: float, y: float, text: str, anchor: str = "middle") -> str:
    return (f'<text class="ink2 val" x="{x:.1f}" y="{y:.1f}" '
            f'text-anchor="{anchor}">{_esc(text)}</text>')


# --- 뼈대 -----------------------------------------------------------------------------

def _legend_rows(series: list[dict], width: float) -> list[list[tuple[int, str]]]:
    """범례를 캔버스 폭에 맞춰 줄바꿈한다. 예전에는 한 줄에 밀어 넣어 **캔버스 밖으로
    흘러 나갔다**(계열 이름이 길면 그대로 잘렸다)."""
    rows, row, x = [], [], 0.0
    for i, s in enumerate(series):
        name = str(s.get("name") or f"계열 {i + 1}")
        w = 14 + _text_w(name, 11.5) + 18
        if row and x + w > width:
            rows.append(row)
            row, x = [], 0.0
        row.append((i, name))
        x += w
    if row:
        rows.append(row)
    return rows


def _legend(rows, left: float, y0: float) -> list[str]:
    out = []
    for r, row in enumerate(rows):
        x, y = left, y0 + r * 18
        for i, name in row:
            out.append(f'<rect class="f{i + 1}" x="{x:.1f}" y="{y - 8:.1f}" '
                       f'width="10" height="10" rx="2"/>')
            out.append(f'<text class="ink2 lbl" x="{x + 14:.1f}" y="{y + 1:.1f}">'
                       f'{_esc(name)}</text>')
            x += 14 + _text_w(name, 11.5) + 18
    return out


def _x_labels(labels: list[str], x_of, y: float, slot: float) -> list[str]:
    """x축 라벨. 개수가 많으면 건너뛰며 찍고, 슬롯보다 넓으면 기울여 겹치지 않게 한다."""
    n = len(labels)
    step = max(1, math.ceil(n / 12))
    tilt = any(_text_w(t, 11.5) > slot * step - 4 for t in labels)
    out = []
    for i, label in enumerate(labels):
        if i % step:
            continue
        cx, text = x_of(i), _esc(label)
        if tilt:
            out.append(f'<text class="mut lbl" x="{cx:.1f}" y="{y:.1f}" text-anchor="end" '
                       f'transform="rotate(-35 {cx:.1f} {y:.1f})">{text}</text>')
        else:
            out.append(f'<text class="mut lbl" x="{cx:.1f}" y="{y:.1f}" '
                       f'text-anchor="middle">{text}</text>')
    return out, tilt


def _grid_and_axes(L: dict, ticks, y_of, vertical: bool = True) -> list[str]:
    """격자와 축. **실선 헤어라인**으로 표면 한 단계 위에만 둔다(점선은 '임계값'으로 읽힌다)."""
    out = []
    for t in ticks:
        p = y_of(t)
        if vertical:
            out.append(f'<line class="grid" x1="{L["left"]:.1f}" y1="{p:.1f}" '
                       f'x2="{L["right"]:.1f}" y2="{p:.1f}"/>')
            out.append(f'<text class="mut tick" x="{L["left"] - 8:.1f}" y="{p + 4:.1f}" '
                       f'text-anchor="end">{_fmt(t)}</text>')
        else:
            out.append(f'<line class="grid" x1="{p:.1f}" y1="{L["top"]:.1f}" '
                       f'x2="{p:.1f}" y2="{L["bottom"]:.1f}"/>')
            out.append(f'<text class="mut tick" x="{p:.1f}" y="{L["bottom"] + 18:.1f}" '
                       f'text-anchor="middle">{_fmt(t)}</text>')
    out.append(f'<line class="axis" x1="{L["left"]:.1f}" y1="{L["top"]:.1f}" '
               f'x2="{L["left"]:.1f}" y2="{L["bottom"]:.1f}"/>')
    out.append(f'<line class="axis" x1="{L["left"]:.1f}" y1="{L["bottom"]:.1f}" '
               f'x2="{L["right"]:.1f}" y2="{L["bottom"]:.1f}"/>')
    return out


def _layout(kind: str, labels, series, title: str, y_label: str) -> dict:
    """제목·범례·축 라벨이 차지할 자리를 **그리기 전에** 잡는다.

    고정 여백으로 두면 계열 이름이 길거나 눈금이 큰 수일 때 글자가 겹치거나 잘린다.
    """
    legend_rows = []
    if kind not in ("pie", "donut") and len(series) >= 2:
        legend_rows = _legend_rows(series, _W - 48 - 24)

    top = 30 if title else 16
    legend_y = top + 22
    top = legend_y + len(legend_rows) * 18 + (6 if legend_rows else 0)
    if y_label:
        top += 16
    top += 14

    if kind == "hbar":
        widest = max((_text_w(t, 11.5) for t in labels), default=0)
        left = min(200.0, max(56.0, widest + 16))
    else:
        ticks_lo, ticks_hi = _y_range(series, kind in ("bar", "stacked", "area"))
        widest = max((_text_w(_fmt(t), 11) for t in _nice_ticks(ticks_lo, ticks_hi)),
                     default=0)
        left = max(48.0, widest + 20)

    # 제목·범례·단위는 **고정 여백**에 붙인다. 축 왼쪽(`left`)을 따라가게 두면 hbar처럼
    # 항목 이름이 긴 차트에서 제목이 화면 한가운데까지 밀려 들어간다.
    return {"left": left, "right": _W - 26, "top": top, "bottom": _H - 46,
            "text_left": min(left, 48.0),
            "legend_rows": legend_rows, "legend_y": legend_y}


def _frame(L: dict, title: str, y_label: str) -> list[str]:
    out = [f'<rect class="bg" x="0" y="0" width="{_W}" height="{_H}" rx="10"/>',
           f'<rect class="edge" x="0.5" y="0.5" width="{_W - 1}" height="{_H - 1}" '
           'rx="10" fill="none"/>']
    if title:
        out.append(f'<text class="ink ttl" x="{L["text_left"]:.1f}" y="30">'
                   f'{_esc(title)}</text>')
    if L["legend_rows"]:
        out += _legend(L["legend_rows"], L["text_left"], L["legend_y"])
    if y_label:
        out.append(f'<text class="mut lbl" x="{L["text_left"]:.1f}" y="{L["top"] - 8:.1f}">'
                   f'{_esc(y_label)}</text>')
    return out


# --- 종류별 그리기 --------------------------------------------------------------------

def _render_xy(L, labels, series, kind) -> list[str]:
    """line · area · scatter — 가로축이 순서, 세로축이 값."""
    n = len(labels)
    span = L["right"] - L["left"]

    def x_of(i):
        return L["left"] + (span * i / (n - 1) if n > 1 else span / 2)

    lo, hi = _y_range(series, include_zero=(kind == "area"))
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def y_of(v):
        return L["bottom"] - (v - lo) / (hi - lo) * (L["bottom"] - L["top"])

    body = _grid_and_axes(L, ticks, y_of)
    xl, _tilt = _x_labels(labels, x_of, L["bottom"] + 18, span / max(1, n - 1 or 1))
    body += xl

    for si, s in enumerate(series, 1):
        pts = [(x_of(i), y_of(v)) for i, v in enumerate(_values(s)) if v is not None]
        if not pts:
            continue
        if kind == "scatter":
            body += [_dot(x, y, si, 4.5) for x, y in pts]
            continue
        d = " ".join(f"{'M' if i == 0 else 'L'}{x:.1f},{y:.1f}"
                     for i, (x, y) in enumerate(pts))
        if kind == "area" and len(pts) > 1:
            base = y_of(max(lo, 0.0))
            body.append(f'<path class="f{si}" fill-opacity="0.10" stroke="none" d="{d} '
                        f'L{pts[-1][0]:.1f},{base:.1f} L{pts[0][0]:.1f},{base:.1f} Z"/>')
        if len(pts) > 1:
            body.append(f'<path class="k{si}" d="{d}" fill="none" stroke-width="2" '
                        'stroke-linejoin="round" stroke-linecap="round"/>')
        # 점이 많으면 마커가 선을 덮어 지저분해진다. 적을 때만 찍는다.
        if len(pts) <= 12:
            body += [_dot(x, y, si) for x, y in pts]
        else:
            body.append(_dot(pts[-1][0], pts[-1][1], si))
        # 값 라벨은 **끝점 하나만.** 모든 점에 숫자를 달면 읽히지 않는다.
        last_v = next((v for v in reversed(_values(s)) if v is not None), None)
        if last_v is not None:
            lx, ly = pts[-1]
            room = L["right"] - lx > _text_w(_fmt_value(last_v), 11) + 10
            body.append(_value_text(lx + (7 if room else -7), ly - 8, _fmt_value(last_v),
                                    "start" if room else "end"))
    return body


def _render_bar(L, labels, series) -> list[str]:
    """세로 막대(묶음). 계열이 여럿이면 나란히 세운다."""
    n, m = max(1, len(labels)), max(1, len(series))
    slot = (L["right"] - L["left"]) / n
    bw = min(_BAR_MAX, max(2.0, (slot * 0.78 - _SURFACE_GAP * (m - 1)) / m))
    group_w = bw * m + _SURFACE_GAP * (m - 1)

    def x_of(i):
        return L["left"] + slot * (i + 0.5)

    lo, hi = _y_range(series, include_zero=True)
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])

    def y_of(v):
        return L["bottom"] - (v - lo) / (hi - lo) * (L["bottom"] - L["top"])

    body = _grid_and_axes(L, ticks, y_of)
    xl, _tilt = _x_labels(labels, x_of, L["bottom"] + 18, slot)
    body += xl
    zero = y_of(0)
    label_all = n * m <= 24          # 마크가 많으면 숫자가 서로 겹친다
    # 라벨이 들어갈 폭. 계열이 하나면 막대가 24px여도 슬롯 전체를 쓸 수 있다 —
    # 막대 폭으로만 재면 `1,234` 같은 값이 자리가 있는데도 통째로 빠진다.
    room = slot * 0.85 if m == 1 else bw + _SURFACE_GAP

    for si, s in enumerate(series, 1):
        for i, v in enumerate(_values(s)):
            if v is None:
                continue
            x = x_of(i) - group_w / 2 + (bw + _SURFACE_GAP) * (si - 1)
            y = y_of(v)
            body.append(f'<path class="f{si}" d="{_bar_v(x, bw, zero, y)}"/>')
            if label_all and _text_w(_fmt_value(v), 11) <= room:
                above = y <= zero
                body.append(_value_text(x + bw / 2, y - 6 if above else y + 14, _fmt_value(v)))
    return body


def _render_hbar(L, labels, series) -> list[str]:
    """가로 막대. **항목 이름이 길 때** 세로 막대보다 훨씬 잘 읽힌다(기울일 필요가 없다)."""
    n, m = max(1, len(labels)), max(1, len(series))
    slot = (L["bottom"] - L["top"]) / n
    bh = min(_BAR_MAX, max(2.0, (slot * 0.74 - _SURFACE_GAP * (m - 1)) / m))
    group_h = bh * m + _SURFACE_GAP * (m - 1)

    lo, hi = _y_range(series, include_zero=True)
    ticks = _nice_ticks(lo, hi)
    lo, hi = min(lo, ticks[0]), max(hi, ticks[-1])
    # 값 라벨이 오른쪽 끝에 걸리지 않게 눈금 축을 조금 남겨 둔다.
    plot_right = L["right"] - 34

    def x_of(v):
        return L["left"] + (v - lo) / (hi - lo) * (plot_right - L["left"])

    body = []
    for t in ticks:
        px = x_of(t)
        body.append(f'<line class="grid" x1="{px:.1f}" y1="{L["top"]:.1f}" '
                    f'x2="{px:.1f}" y2="{L["bottom"]:.1f}"/>')
        body.append(f'<text class="mut tick" x="{px:.1f}" y="{L["bottom"] + 18:.1f}" '
                    f'text-anchor="middle">{_fmt(t)}</text>')
    body.append(f'<line class="axis" x1="{L["left"]:.1f}" y1="{L["top"]:.1f}" '
                f'x2="{L["left"]:.1f}" y2="{L["bottom"]:.1f}"/>')
    body.append(f'<line class="axis" x1="{L["left"]:.1f}" y1="{L["bottom"]:.1f}" '
                f'x2="{plot_right:.1f}" y2="{L["bottom"]:.1f}"/>')

    zero = x_of(0)
    for i, label in enumerate(labels):
        cy = L["top"] + slot * (i + 0.5)
        body.append(f'<text class="mut lbl" x="{L["left"] - 8:.1f}" y="{cy + 4:.1f}" '
                    f'text-anchor="end">{_esc(label)}</text>')
        for si, s in enumerate(series, 1):
            vals = _values(s)
            v = vals[i] if i < len(vals) else None
            if v is None:
                continue
            y = cy - group_h / 2 + (bh + _SURFACE_GAP) * (si - 1)
            x = x_of(v)
            body.append(f'<path class="f{si}" d="{_bar_h(y, bh, zero, x)}"/>')
            body.append(_value_text(x + (6 if v >= 0 else -6), y + bh / 2 + 4, _fmt_value(v),
                                    "start" if v >= 0 else "end"))
    return body


def _render_stacked(L, labels, series) -> list[str]:
    """누적 막대 — 구성(부분과 전체)을 볼 때.

    조각마다 숫자를 넣지 않고 **열 합계를 기둥 위에** 적는다. 조각 안 글자는 조각 색에 따라
    흰색/잉크를 갈라야 하는데, 그 색이 밝은/어두운 모드에서 서로 달라 한쪽이 반드시 깨진다.
    조각의 정체는 범례가 나른다.
    """
    n = max(1, len(labels))
    slot = (L["right"] - L["left"]) / n
    bw = min(_BAR_MAX, max(3.0, slot * 0.6))
    totals = _stack_totals(labels, series)

    def x_of(i):
        return L["left"] + slot * (i + 0.5)

    hi = max(totals) if totals else 1.0
    ticks = _nice_ticks(0.0, hi or 1.0)
    hi = max(hi, ticks[-1])

    def y_of(v):
        return L["bottom"] - (v / hi if hi else 0) * (L["bottom"] - L["top"])

    body = _grid_and_axes(L, ticks, y_of)
    xl, _tilt = _x_labels(labels, x_of, L["bottom"] + 18, slot)
    body += xl

    for i in range(n):
        base = 0.0
        for si, s in enumerate(series, 1):
            vals = _values(s)
            v = max(0.0, (vals[i] if i < len(vals) else 0) or 0)
            if v <= 0:
                continue
            y_top, y_bot = y_of(base + v), y_of(base)
            # 맞닿은 조각은 테두리가 아니라 **표면 색 틈**으로 가른다.
            if base > 0:
                y_bot -= _SURFACE_GAP
            if y_bot - y_top > 0.5:
                r = _BAR_R if abs(base + v - totals[i]) < 1e-9 else 0.0
                body.append(f'<path class="f{si}" '
                            f'd="{_bar_v(x_of(i) - bw / 2, bw, y_bot, y_top, r)}"/>')
            base += v
        if totals[i] > 0:
            body.append(_value_text(x_of(i), y_of(totals[i]) - 6, _fmt_value(totals[i])))
    return body


def _render_pie(L, labels, series, donut: bool) -> list[str]:
    """원/도넛 — 부분과 전체를 **한눈에** 볼 때만(조각 6개 이하).

    조각 비율은 조각 **바깥**에 적는다. 안쪽에 적으면 조각 색의 밝기에 따라 글자색을 흰색/
    잉크로 갈라야 하는데, 밝은 모드와 어두운 모드의 조각 색이 달라 한쪽이 반드시 깨진다.
    """
    values = [abs(v) for v in _values(series[0]) if v is not None]
    total = sum(values)
    cx, cy = L["left"] + 150, (L["top"] + L["bottom"]) / 2
    r = min(120.0, (L["bottom"] - L["top"]) / 2 - 14)
    if total <= 0 or r <= 0:
        return [f'<text class="mut lbl" x="{_W / 2}" y="{_H / 2}" text-anchor="middle">'
                '표시할 값이 없습니다</text>']

    body, angle = [], -math.pi / 2
    for i, v in enumerate(values):
        si = i + 1
        sweep = 2 * math.pi * v / total
        mid = angle + sweep / 2
        x1, y1 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        angle += sweep
        x2, y2 = cx + r * math.cos(angle), cy + r * math.sin(angle)
        if sweep >= 2 * math.pi - 1e-9:
            # 항목이 하나뿐이면 호(arc)로는 원을 못 그린다(시작=끝).
            body.append(f'<circle class="f{si}" cx="{cx:.1f}" cy="{cy:.1f}" r="{r:.1f}"/>')
        else:
            large = 1 if sweep > math.pi else 0
            body.append(f'<path class="f{si} gap" stroke-width="{_SURFACE_GAP}" '
                        f'd="M{cx:.1f},{cy:.1f} L{x1:.1f},{y1:.1f} '
                        f'A{r:.1f},{r:.1f} 0 {large} 1 {x2:.1f},{y2:.1f} Z"/>')
        # 18도보다 좁은 조각은 바깥 라벨끼리 겹친다 — 범례에만 남긴다.
        if sweep >= math.radians(18):
            lx, ly = cx + (r + 16) * math.cos(mid), cy + (r + 16) * math.sin(mid)
            anchor = "start" if math.cos(mid) >= 0 else "end"
            body.append(_value_text(lx, ly + 4, f"{v / total * 100:.0f}%", anchor))

    if donut:
        body.append(f'<circle class="bg" cx="{cx:.1f}" cy="{cy:.1f}" r="{r * 0.58:.1f}"/>')
        body.append(f'<text class="ink" x="{cx:.1f}" y="{cy + 2:.1f}" text-anchor="middle" '
                    f'font-size="19" font-weight="600">{_esc(_fmt_value(total))}</text>')
        body.append(f'<text class="mut lbl" x="{cx:.1f}" y="{cy + 20:.1f}" '
                    'text-anchor="middle">합계</text>')

    # 범례(이름 · 값). 조각이 색만으로 구분되지 않게 하는 **정체성 채널**이라 항상 붙인다.
    names = [labels[i] if i < len(labels) else f"항목 {i + 1}" for i in range(len(values))]
    lx, ly = cx + r + 66, L["top"] + 16
    # 값 열은 가장 긴 이름 바로 뒤에 세운다. 캔버스 오른쪽 끝에 붙이면 이름과 값 사이가
    # 휑하게 벌어져 어느 값이 어느 항목인지 눈으로 잇기 어렵다.
    val_x = min(_W - 26.0, lx + 16 + max((_text_w(n, 11.5) for n in names), default=0) + 40)
    for i, (name, v) in enumerate(zip(names, values)):
        if ly > L["bottom"]:
            break
        body.append(f'<rect class="f{i + 1}" x="{lx:.1f}" y="{ly - 9:.1f}" '
                    'width="10" height="10" rx="2"/>')
        body.append(f'<text class="ink2 lbl" x="{lx + 16:.1f}" y="{ly:.1f}">'
                    f'{_esc(name)}</text>')
        body.append(f'<text class="mut lbl" x="{val_x:.1f}" y="{ly:.1f}" text-anchor="end">'
                    f'{_fmt_value(v)}</text>')
        ly += 21
    return body


def cap_series(kind: str, series: list[dict]) -> list[dict]:
    """계열 수 상한. **색을 더 만들어 내지 않는다** — 넘치면 잘라 낸다.

    9번째 색은 만들어 봐야 색각이상에서 기존 색과 구분되지 않는다. 흩뿌림은 어느 두 점이든
    나란히 놓일 수 있어(모든 쌍 검사) 상한이 더 낮다.
    """
    limit = _MAX_SERIES_SCATTER if kind == "scatter" else _MAX_SERIES
    return series[:limit]


def render(chart_type: str, labels: list, series: list[dict],
           title: str = "", y_label: str = "") -> str:
    """차트 SVG 문자열을 만든다. 입력 검증은 호출부(server.py)에서 이미 끝난 상태를 가정한다."""
    kind = chart_type if chart_type in CHART_TYPES else "line"
    series = cap_series(kind, series)
    if kind in ("pie", "donut"):
        series = series[:1]

    L = _layout(kind, labels, series, title, y_label)
    body = _frame(L, title, y_label)
    if kind == "bar":
        body += _render_bar(L, labels, series)
    elif kind == "hbar":
        body += _render_hbar(L, labels, series)
    elif kind == "stacked":
        body += _render_stacked(L, labels, series)
    elif kind in ("pie", "donut"):
        body += _render_pie(L, labels, series, donut=(kind == "donut"))
    else:
        body += _render_xy(L, labels, series, kind)

    return (f'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 {_W} {_H}" '
            f'width="{_W}" height="{_H}" role="img">'
            f'<title>{_esc(title or "차트")}</title>'
            f'<style>{_style()}</style>' + "".join(body) + "</svg>")
