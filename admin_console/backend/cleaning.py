"""
청크 텍스트 정제(clean) 유틸.

설계상 가장 중요한 제약:
이 시스템은 인프라/커맨드 매뉴얼을 다루므로 `<user>`, `<host>`, `<namespace>`,
`<your-value>` 같은 placeholder를 절대 지우면 안 된다. 따라서 "꺾쇠로 감싼 모든 것"을
지우는 방식(<[^>]+>)을 쓰지 않고, **실제 HTML 태그 이름 화이트리스트**에 해당할 때만 제거한다.

또한 코드 블록(``` fenced, `inline`)은 정제 대상에서 제외해 명령어 원문을 보존한다.

옵션:
- strip_html:     실제 HTML 태그/엔티티 제거
- collapse_space: 연속 공백/탭/빈 줄 정리
- drop_urls:      노출된 URL 제거
- normalize:      유니코드 정규화 + 제어문자 제거
"""
import re
import html
import unicodedata
from dataclasses import dataclass

# 실제 HTML 태그 이름만 (인프라 placeholder와 구분하기 위한 화이트리스트)
_HTML_TAGS = (
    "html|head|body|title|meta|link|base|style|script|noscript|"
    "div|span|p|br|hr|pre|code|kbd|samp|var|blockquote|"
    "h1|h2|h3|h4|h5|h6|"
    "ul|ol|li|dl|dt|dd|"
    "table|thead|tbody|tfoot|tr|td|th|caption|colgroup|col|"
    "a|img|figure|figcaption|picture|source|"
    "b|i|u|s|strong|em|mark|small|sub|sup|del|ins|abbr|cite|q|"
    "form|input|button|select|option|optgroup|textarea|label|fieldset|legend|"
    "nav|header|footer|main|section|article|aside|details|summary|dialog|"
    "iframe|embed|object|param|video|audio|track|canvas|svg|path|g|"
    "font|center|big|strike|tt|o:p|xml"
)
# <tag ...>, </tag>, <tag/> 형태만 매칭 (대소문자 무시)
_HTML_TAG_RE = re.compile(rf"</?(?:{_HTML_TAGS})\b[^>]*/?>", re.IGNORECASE)
_SCRIPT_STYLE_RE = re.compile(r"<(script|style)\b[^>]*>.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
# HTML 주석
_COMMENT_RE = re.compile(r"<!--.*?-->", re.DOTALL)

# 코드 블록 보호용
_FENCED_RE = re.compile(r"```.*?```", re.DOTALL)
_INLINE_CODE_RE = re.compile(r"`[^`\n]+`")

_MD_IMAGE = re.compile(r"!\[[^\]]*\]\([^)]*\)")
_MD_LINK = re.compile(r"\[([^\]]+)\]\([^)]*\)")
_BARE_URL = re.compile(r"https?://\S+")
_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MULTISPACE = re.compile(r"[ \t]+")
_MULTINEWLINE = re.compile(r"\n{3,}")

# 정제 정책 버전. 규칙을 바꾸면 반드시 올린다(임베딩 캐시 키에 포함됨).
CLEAN_POLICY_VERSION = 2


@dataclass
class CleanOptions:
    strip_html: bool = True
    collapse_space: bool = True
    drop_urls: bool = False
    normalize: bool = True


def _protect_code(text: str) -> tuple[str, list[str]]:
    """코드 블록을 placeholder로 치환해 정제 대상에서 제외한다.
    마커는 유니코드 사용자 영역(U+E000/U+E001)을 쓴다. 제어문자 제거 정규식에
    걸리지 않고 NFKC 정규화에도 변형되지 않아 안전하게 복원된다."""
    stash: list[str] = []

    def _stash(m):
        stash.append(m.group(0))
        return f"\ue000CODE{len(stash) - 1}\ue001"

    text = _FENCED_RE.sub(_stash, text)
    text = _INLINE_CODE_RE.sub(_stash, text)
    return text, stash


def _restore_code(text: str, stash: list[str]) -> str:
    for i, code in enumerate(stash):
        text = text.replace(f"\ue000CODE{i}\ue001", code)
    return text


def clean_text(text: str, opts: CleanOptions | None = None) -> str:
    if text is None:
        return ""
    opts = opts or CleanOptions()

    # 코드 블록 보호 (제어문자 제거 전에 수행하되, placeholder는 \x00을 쓰므로 복원 후 정리)
    t, stash = _protect_code(text)

    if opts.normalize:
        t = unicodedata.normalize("NFKC", t)
        t = _CTRL.sub("", t)
        t = t.replace("\xa0", " ").replace("\u200b", "")

    if opts.strip_html:
        t = _COMMENT_RE.sub(" ", t)
        t = _SCRIPT_STYLE_RE.sub(" ", t)
        t = _HTML_TAG_RE.sub(" ", t)   # 실제 HTML 태그만 (placeholder는 보존)
        t = html.unescape(t)
        t = t.replace("\xa0", " ")

    t = _MD_IMAGE.sub("", t)
    t = _MD_LINK.sub(r"\1", t)

    if opts.drop_urls:
        t = _BARE_URL.sub("", t)

    if opts.collapse_space:
        t = _MULTISPACE.sub(" ", t)
        t = "\n".join(line.strip() for line in t.split("\n"))
        t = _MULTINEWLINE.sub("\n\n", t)

    t = _restore_code(t, stash)
    return t.strip()


def clean_options_from_dict(d: dict | None) -> CleanOptions:
    if not d:
        return CleanOptions()
    return CleanOptions(
        strip_html=bool(d.get("strip_html", True)),
        collapse_space=bool(d.get("collapse_space", True)),
        drop_urls=bool(d.get("drop_urls", False)),
        normalize=bool(d.get("normalize", True)),
    )
