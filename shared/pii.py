"""
검색 결과(특히 VOC 이력)에 섞여 있는 개인·조직 식별 정보를 자리표시자로 바꾼다.

왜 코드에서 하나: VOC는 실제 문의 원문이라 계정·이메일·이름·부서가 그대로 들어 있다.
LLM에게 "쓰지 마라"고만 하면 원문이 프롬프트에 들어간 뒤라 유출 경로가 남는다.
그래서 **MCP가 결과를 돌려주기 전에** 먼저 지운다(에이전트는 마스킹된 텍스트만 본다).

한계: 외국 이름이나 흔치 않은 표기까지 정규식으로 다 잡을 수는 없다. 그래서 지시문에도
"식별 정보는 자리표시자로 바꿔 쓴다"는 규칙을 함께 둔다(코드가 1차, 지시문이 2차 방어).
이름·조직 패턴은 보수적으로 잡는다 - 멀쩡한 문장이 깨지면 답변 자체가 못 쓰게 된다.
**계정은 반대다**: 놓치면 남의 계정이 그대로 나가므로, 계정처럼 보이면 일단 가린다
(아래 `ACCOUNT_RE` 주석).
"""
import re

USER_ID = "{사용자 id}"
USER_NAME = "{사용자 이름}"

# 조직 접미사 -> 자리표시자. 긴 접미사부터 매칭해야 "사업부"가 "부"로 잘리지 않는다.
_ORG_SUFFIXES = ["사업부", "본부", "부문", "센터", "그룹", "파트", "모듈", "부서", "팀"]
# 접미사 뒤에 올 수 있는 것: 문장 끝 / 공백·기호 / **조사**.
# `\b`를 쓰면 안 된다 - 한국어 조사는 한글이라 단어 문자로 취급돼 경계가 생기지 않는다.
# 그래서 "DS부문 "은 가려지는데 "DX부문은"은 그대로 노출되는, 같은 문장 안에서도
# 들쭉날쭉한 마스킹이 나왔다(개인정보는 새고 문장은 깨지는 최악의 조합).
# 조사를 명시적으로 허용하되, "팀장"처럼 다른 단어로 이어지는 경우는 제외한다.
_ORG_TAIL = r"(?=$|[^A-Za-z0-9가-힣]|[은는이가을를의에도만과와로])"
# 조직명 앞부분에 **공백을 허용하지 않는다.** 예전에는 `[A-Za-z가-힣0-9\s]{0,15}?`라
# 단어 경계를 넘어 앞 문장을 통째로 먹었다:
#   "플랫폼팀이 처리했고 인프라팀은" -> "{팀명}{팀명}은"  ("이 처리했고"가 삭제됨)
# 실제로 VOC 답변에서 "…: OO을 통한 방화벽 신청"의 가운데가 사라지는 원인이었다.
# 조직명은 대개 한 토큰이므로("메모리사업부", "플랫폼팀", "DS부문") 공백 없이 잡는다.
# 띄어쓴 조직명("AI 개발팀")은 뒷 토큰만 가려지지만, 문장을 깨뜨리는 것보다 낫다.
_ORG_RE = re.compile(
    r"[A-Za-z가-힣0-9]{0,15}?(" + "|".join(_ORG_SUFFIXES) + r")" + _ORG_TAIL)

# 이메일 전체를 계정 자리표시자로 바꾼다(도메인도 조직 식별 정보라 남기지 않는다).
_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")

# 사내 계정 형태(`말.말`).
#
# 예전에는 **점 앞에 숫자를 요구**했다(`ops.user`). 파일명(`server.log`)을 계정으로 오인하지
# 않으려던 것인데, 그 대가로 `other.user`처럼 숫자가 없는 계정을 통째로 놓쳤다 —
# VOC 원문에 남의 계정이 그대로 실려 프롬프트에 들어갔다.
#
# 그래서 방향을 뒤집는다: **`말.말`이면 일단 계정으로 보고**, 계정일 리 없는 꼬리(파일
# 확장자·도메인·기술 토큰)만 예외로 둔다. 두 실패의 무게가 다르기 때문이다 —
# 과잉 마스킹은 문장 하나가 어색해지는 일이고, 누락은 **남의 계정이 노출되는** 일이다.
# 사용자 지시: "다른 사람 계정명·id·이메일·이름은 절대로 사용하면 안 됨."
# 앞뒤의 lookaround는 **점이 더 이어지는 것**을 계정으로 보지 않게 한다. 없으면
# `www.google.com`에서 `www.google`만 잡아 `{사용자 id}.com`이라는 괴상한 문장이 된다.
ACCOUNT_RE = re.compile(
    r"(?<![.\w-])[A-Za-z][A-Za-z0-9_-]{1,15}\.[A-Za-z][A-Za-z0-9_-]{1,30}(?!\.?[\w-])")
# 여기 없는 꼬리가 오면 멀쩡한 토큰 하나가 자리표시자로 바뀔 뿐이다. 반대로 여기에 계정
# 꼬리를 잘못 넣으면 그 계정은 영영 새어 나간다 — **의심스러우면 넣지 않는다.**
NOT_ACCOUNT_SUFFIX = {
    # 파일 확장자
    "py", "sh", "bash", "md", "yml", "yaml", "json", "log", "txt", "csv", "tsv",
    "sql", "html", "htm", "js", "jsx", "ts", "tsx", "css", "svg", "png", "jpg",
    "jpeg", "gif", "pdf", "xlsx", "xls", "doc", "docx", "ppt", "pptx", "zip",
    "tar", "gz", "bz2", "xz", "whl", "deb", "rpm", "img", "iso", "bin", "so",
    "out", "err", "lock", "toml", "xml", "cfg", "conf", "ini", "env", "example",
    "bak", "tmp", "old", "new", "sample", "template", "pem", "crt", "key", "pub",
    "service", "socket", "timer", "list", "path", "sys", "db", "dat", "dump",
    # 도메인 꼬리
    "com", "net", "org", "io", "kr", "co", "jp", "cn", "us", "eu", "ai", "app",
    "cloud", "gov", "edu", "ac", "info", "biz", "local", "internal", "dev",
    "test", "stage", "prod",
}


def is_masked_account(token: str) -> bool:
    """`말.말` 토큰이 **계정으로 취급되는가**. 마스킹과 노출 차단이 같은 판정을 쓰게 한다.

    두 곳에서 각자 정규식을 들고 있으면 언젠가 한쪽만 고쳐진다(실제로 그랬다 — 노출 차단은
    `other.user`을 잡는데 마스킹은 놓쳤다). 판정은 여기 하나뿐이다.
    """
    tok = (token or "").strip()
    if not ACCOUNT_RE.fullmatch(tok):
        return False
    return tok.rsplit(".", 1)[-1].lower() not in NOT_ACCOUNT_SUFFIX


def find_accounts(text: str | None) -> list[str]:
    """텍스트에 든 계정처럼 보이는 토큰(중복 제거, 등장 순서)."""
    return [t for t in dict.fromkeys(ACCOUNT_RE.findall(text or "")) if is_masked_account(t)]

# 한국 성씨(빈도순 상위). 성+이름 2~3자가 하나의 토큰으로 붙어 있을 때만 이름으로 본다.
_SURNAMES = (
    "김이박최정강조윤장임한오서신권황안송류전홍고문양손배백허유남심노하곽성차주우구"
    "라마원방변석선설성소신엄여염오용우육윤인장전정제조진차천최추탁태편표피하한함현형홍황"
)
# 뒤에 오는 호칭/직급으로 이름임을 확인한다. 한국어는 조사가 붙으므로("책임이 요청") 단어
# 경계(\b)를 쓰면 매칭되지 않는다 - 호칭 자체만 확인한다.
_NAME_RE = re.compile(rf"(?<![가-힣])[{_SURNAMES}][가-힣]{{1,2}}(?=\s*(님|씨|책임|선임|수석|프로|"
                      r"사원|주임|대리|과장|차장|부장|팀장|파트장|그룹장|매니저|연구원))")
# 직급 없이 단독으로 쓰인 이름은 오탐이 크므로, '담당자: 홍길동'처럼 라벨이 붙은 경우만 잡는다.
# 이름 뒤 공백까지 먹어 문장이 붙지 않도록 토큰 단위로만 잡는다("예림 최", "John Smith" 포함).
_LABELED_NAME_RE = re.compile(
    r"(담당자|요청자|의뢰자|작성자|신청자|문의자|처리자|승인자|성명|이름)\s*[:：]\s*"
    r"[A-Za-z가-힣]{1,12}(?:\s+[A-Za-z가-힣]{1,12})?")


def _org_repl(m: re.Match) -> str:
    return "{" + m.group(1) + "명}"


def _account_repl(m: re.Match) -> str:
    return USER_ID if is_masked_account(m.group(0)) else m.group(0)


def mask_accounts(text: str | None) -> str | None:
    """**계정과 이메일만** 가린다(이름·조직은 그대로).

    매뉴얼용이다. 매뉴얼은 공식 문서라 조직명·직급이 절차의 일부다("OO팀에 신청") —
    그것까지 자리표시자로 바꾸면 사용자가 어디에 신청해야 할지 알 수 없게 된다.
    반면 매뉴얼에 남아 있는 **남의 계정·이메일**은 어떤 절차에도 필요하지 않다.
    """
    if not text:
        return text
    return ACCOUNT_RE.sub(_account_repl, _EMAIL_RE.sub(USER_ID, text))


def mask_pii(text: str | None) -> str | None:
    """사람·조직 식별 정보를 자리표시자로 바꾼 문자열을 돌려준다(None은 그대로)."""
    if not text:
        return text
    out = mask_accounts(text)
    out = _LABELED_NAME_RE.sub(lambda m: f"{m.group(1)}: {USER_NAME}", out)
    out = _NAME_RE.sub(USER_NAME, out)
    out = _ORG_RE.sub(_org_repl, out)
    return out


def mask_record(record: dict, fields: tuple[str, ...]) -> dict:
    """dict의 지정 필드만 마스킹한 새 dict를 돌려준다."""
    masked = dict(record)
    for f in fields:
        if isinstance(masked.get(f), str):
            masked[f] = mask_pii(masked[f])
    return masked
