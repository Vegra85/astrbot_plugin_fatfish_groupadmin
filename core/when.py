# -*- coding: utf-8 -*-
"""时长：从一句话里认出「多久」（「一小时」「半小时」「到晚上」「到明天早上」「到 8 点」），不依赖 AstrBot。

几点几分按 AstrBot 配置里的时区算（拿不到就按北京时间）：用户多半在国内，服务器的时区不一定。
认不出来 → None，由调用的地方用默认时长（宁可短一点，到点自己恢复，别一句话把它关一整天）。
"""

import re
from datetime import datetime, timedelta, timezone

# 拿不到 AstrBot 配置里的时区时按北京时间算（用户多半在国内；服务器的时区常常是 UTC）
BEIJING = timezone(timedelta(hours=8), "北京时间")


def resolve_tz(name):
    """AstrBot 配置里的时区名 → tzinfo；空的、写错的、这台机器没有时区数据的 → 北京时间"""
    name = str(name or "").strip()
    if name:
        try:
            from zoneinfo import ZoneInfo

            return ZoneInfo(name)
        except Exception:  # noqa: BLE001  Windows 没装 tzdata、名字写错都退回北京时间
            pass
    return BEIJING

_CN = {
    "零": 0, "〇": 0, "一": 1, "二": 2, "两": 2, "俩": 2, "三": 3, "四": 4,
    "五": 5, "六": 6, "七": 7, "八": 8, "九": 9,
}
_CN_CHARS = "零〇一二两俩三四五六七八九十百"


def cn_num(s):
    """「十五」「二十」「两」「3」「1.5」「半」→ 数字；认不出 → None"""
    s = (s or "").strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(?:\.\d+)?", s):
        return float(s)
    if s == "半":
        return 0.5
    if any(c not in _CN_CHARS for c in s):
        return None
    total, num = 0, 0
    for c in s:
        if c in _CN:
            num = _CN[c]
        elif c == "十":
            total += (num or 1) * 10
            num = 0
        elif c == "百":
            total += (num or 1) * 100
            num = 0
    return float(total + num)


_NUM = r"(\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百]+|半)"
# 「分」只认阿拉伯数字后面的（「十分安静」的十分是「很」）
_UNITS = (
    (r"秒钟?|s(?:ec(?:onds?)?)?", 1),
    (r"分钟|mins?|minutes?", 60),
    (r"小时|个?钟头|h(?:ours?|rs?)?", 3600),
    (r"天|日", 86400),
    (r"周|星期|礼拜", 7 * 86400),
)
_DUR = re.compile(
    _NUM
    + r"\s*(个半|个)?\s*("
    + "|".join(u for u, _ in _UNITS)
    + r")(?![a-zA-Z])",
    re.I,
)
_DUR_FEN = re.compile(r"(\d+)\s*分(?!钟|之)")  # 「10分」
_QUARTER = re.compile(r"(一|1)\s*刻钟")
_HALF_DAY = re.compile(r"半天")
# 「一直」单说多半是「你一直在刷屏」，只认后面紧跟着「别 / 不要 / 闭嘴 / 安静」的
_FOREVER = re.compile(r"永远|以后都|再也(?:别|不要|不许)|永久|从此|一直(?:别|不要|不许|闭嘴|安静)")
_VAGUE = re.compile(r"一会儿?|一下下?|片刻|一阵子?|会儿")

_DAY_WORDS = {"今天": 0, "今日": 0, "今晚": 0, "明天": 1, "明早": 1, "明晚": 1, "后天": 2}
_PERIODS = "早上|早晨|上午|中午|下午|傍晚|晚上|夜里|半夜|凌晨"
# 只说了时段（没说几点）时按这个点算
_PERIOD_HOUR = {
    "早上": (8, 0), "早晨": (8, 0), "上午": (9, 0), "中午": (12, 0), "下午": (14, 0),
    "傍晚": (17, 30), "晚上": (18, 0), "夜里": (23, 0), "半夜": (0, 0), "凌晨": (5, 0),
}
_TO = r"(?:到|直到|等到|至|until\s*)"
_HOUR = r"(\d{1,2}|[零〇一二两三四五六七八九十]+)\s*(?:点|:|：|时)\s*(半|一刻|三刻|\d{1,2}\s*分?|[零〇一二三四五六七八九十]+分)?"
# 「到 8 点」「到下午 3 点半」「到明天早上 8 点」
_CLOCK = re.compile(
    _TO + r"\s*(今天|今日|今晚|明天|明早|明晚|后天)?\s*(" + _PERIODS + r")?\s*" + _HOUR
)
# 「8 点以后再说话」「晚上 8 点前别说话」
_CLOCK_AFTER = re.compile(
    r"(今天|今日|今晚|明天|明早|明晚|后天)?\s*(" + _PERIODS + r")?\s*" + _HOUR
    + r"\s*(?:以后|之后|后|以前|之前|前|再)"
)
# 「到晚上」「到明天」「到明天早上」
_TO_PERIOD = re.compile(
    _TO + r"\s*(今天|今日|今晚|明天|明早|明晚|后天)?\s*(" + _PERIODS + r")?"
)
# 不带「到」的范围：「今天别说话了」「今晚安静点」「一晚上」
_SCOPE_TONIGHT = re.compile(r"今晚|今天晚上|一晚上|一晚|这一晚|这个晚上")
_SCOPE_TODAY = re.compile(r"今天|今日|这一天|一整天")


def _minute_of(s):
    if not s:
        return 0
    s = s.replace(" ", "").rstrip("分")
    if s == "半":
        return 30
    if s == "一刻":
        return 15
    if s == "三刻":
        return 45
    v = cn_num(s)
    return int(v) if v is not None and 0 <= v < 60 else 0


def _dt(now_dt, days, hour, minute):
    d = now_dt + timedelta(days=days)
    return d.replace(hour=hour % 24, minute=minute, second=0, microsecond=0)


def _clock_target(now_dt, day_word, period, hour, minute):
    """几点几分 → 将来的那个时刻（datetime）；没法算 → None"""
    days = _DAY_WORDS.get(day_word or "", 0)
    h = int(hour)
    if h > 24 or minute >= 60:
        return None
    night = period in ("晚上", "夜里", "半夜", "凌晨") or day_word in ("今晚", "明晚")
    # 晚上 / 夜里说的 12 点、24 点：是那天过完的半夜，不是中午（以前「到晚上 12 点」算成了第二天中午）
    if h == 24 or (h == 12 and night):
        t = _dt(now_dt, days + 1, 0, minute)
        return t if t > now_dt else None
    if h == 0:
        # 零点：下一个半夜（00:30 说「到零点」是今晚的半夜，不是今天中午）
        t = _dt(now_dt, days + 1 if night else max(days, 1), 0, minute)
        return t if t > now_dt else None
    if h == 12 and not period and not day_word:
        # 只说「到 12 点」：中午还没到就是中午；过了就是今晚的半夜（23 点说的不是第二天中午）
        noon = _dt(now_dt, 0, 12, minute)
        return noon if noon > now_dt else _dt(now_dt, 1, 0, minute)
    if period in ("下午", "傍晚", "晚上", "夜里") and h < 12:
        h += 12
    elif period == "中午" and h < 11:
        h += 12
    if day_word in ("今晚", "明晚") and h < 12:
        h += 12
    if not day_word and not period and h < 12:
        # 没说上午下午：取最近的一个将来时刻（说「到 8 点」：现在 15 点 → 20 点；现在 7 点 → 8 点）
        for cand in (_dt(now_dt, 0, h, minute), _dt(now_dt, 0, h + 12, minute),
                     _dt(now_dt, 1, h, minute)):
            if cand > now_dt:
                return cand
        return None
    t = _dt(now_dt, days, h, minute)
    if t <= now_dt and not day_word:
        t += timedelta(days=1)
    return t if t > now_dt else None


def _period_target(now_dt, day_word, period):
    """「到晚上」「到明天」「到明天早上」→ 时刻；没法算 → None"""
    if not day_word and not period:
        return None
    if day_word in ("明天", "明早", "后天") and not period:
        period = "早上"
    if day_word in ("今晚", "明晚") and not period:
        period = "晚上"
    days = _DAY_WORDS.get(day_word or "", 0)
    if day_word == "今天" and not period:
        return _dt(now_dt, 1, 0, 0)  # 到今天结束
    h, m = _PERIOD_HOUR.get(period, (8, 0))
    if period == "半夜" and days == 0:
        days = 1
    t = _dt(now_dt, days, h, m)
    if t > now_dt:
        return t
    if day_word:
        return None
    if period == "晚上":
        # 已经是晚上了还说「到晚上」：算到 23 点；再晚就认不出（用默认）
        t = _dt(now_dt, 0, 23, 0)
        return t if t > now_dt else None
    return t + timedelta(days=1)


def parse_span(text, now_ts, tz=None):
    """这句话里说了多久 → (秒数 或 None, 怎么认的)。
    None = 没说 / 认不出（调用的地方用默认时长）；-1 = 说了「一直 / 以后都」（调用的地方按上限）"""
    s = str(text or "")
    if not s:
        return None, ""
    try:
        now_dt = datetime.fromtimestamp(float(now_ts), tz)
    except Exception:  # noqa: BLE001  时区坏了也不能连累认指令
        now_dt = datetime.fromtimestamp(float(now_ts))
    m = _CLOCK.search(s) or _CLOCK_AFTER.search(s)
    if m:
        day_word, period, hour, minute = m.group(1), m.group(2), m.group(3), m.group(4)
        hv = cn_num(hour)
        if hv is not None:
            t = _clock_target(now_dt, day_word, period, int(hv), _minute_of(minute))
            if t is not None:
                return (t - now_dt).total_seconds(), "到 " + t.strftime("%m-%d %H:%M")
    for m in _TO_PERIOD.finditer(s):  # 第一个「到」后面不一定跟着时段（「收到！安静到晚上」）
        if not (m.group(1) or m.group(2)):
            continue
        t = _period_target(now_dt, m.group(1), m.group(2))
        if t is not None:
            return (t - now_dt).total_seconds(), "到 " + t.strftime("%m-%d %H:%M")
        break
    if _FOREVER.search(s):
        return -1, "一直"
    if _QUARTER.search(s):
        return 15 * 60.0, "一刻钟"
    if _HALF_DAY.search(s):
        return 12 * 3600.0, "半天"
    total, hit = 0.0, []
    for m in _DUR.finditer(s):
        n = cn_num(m.group(1))
        if n is None:
            continue
        if m.group(2) == "个半":
            n += 0.5
        unit = m.group(3).lower()
        sec = next((v for pat, v in _UNITS if re.fullmatch(pat, unit, re.I)), None)
        if sec is None:
            continue
        total += n * sec
        hit.append(m.group(0).strip())
    if not hit:
        for m in _DUR_FEN.finditer(s):
            total += float(m.group(1)) * 60
            hit.append(m.group(0))
    if hit and total > 0:
        return total, "、".join(hit)
    if _SCOPE_TONIGHT.search(s):
        t = _dt(now_dt, 1, 7, 0)
        return (t - now_dt).total_seconds(), "今晚（到明早 07:00）"
    if _SCOPE_TODAY.search(s):
        t = _dt(now_dt, 1, 0, 0)
        return (t - now_dt).total_seconds(), "今天（到 24 点）"
    if _VAGUE.search(s):
        return None, "一会儿（用默认）"
    return None, ""


def clamp_span(sec, default_sec, max_sec, min_sec=60.0):
    """说的时长 → 实际用的秒数：没说用默认；「一直」按上限；太短按 1 分钟；太长按上限"""
    if sec is None:
        sec = default_sec
    elif sec < 0:
        sec = max_sec
    return float(max(min_sec, min(float(sec), float(max_sec))))


def hhmm(ts, now_ts=None, tz=None):
    """时间戳 → 「14:32」；不是今天的带上「明天」/日期（按同一个时区比）"""
    try:
        t = datetime.fromtimestamp(float(ts), tz)
        n = datetime.fromtimestamp(float(now_ts if now_ts is not None else ts), tz)
    except Exception:  # noqa: BLE001
        return "?"
    d = (t.date() - n.date()).days
    if d == 0:
        return t.strftime("%H:%M")
    if d == 1:
        return "明天 " + t.strftime("%H:%M")
    if d == -1:
        return "昨天 " + t.strftime("%H:%M")
    return t.strftime("%m-%d %H:%M")


def span_text(sec):
    """秒数 → 「18 分钟」「2 小时 5 分钟」「3 天」"""
    sec = max(0, int(sec))
    if sec < 60:
        return "不到 1 分钟"
    m = (sec + 59) // 60 if sec < 3600 else sec // 60
    if m < 60:
        return f"{m} 分钟"
    h, mm = divmod(m, 60)
    if h < 48:
        return f"{h} 小时" + (f" {mm} 分钟" if mm else "")
    return f"{h // 24} 天" + (f" {h % 24} 小时" if h % 24 else "")
