#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""认话的方向（v0.1.1，不用装 AstrBot）：python tests/test_words.py [插件目录 默认上一层]

v0.1.0 的毛病：群管想让它恢复说话，说「取消安静」「结束安静」「不用再安静了」→ 被认成再安静一次；「别再闭嘴了」→ 静音。
  A 解除的说法（69 种）：群在安静 / 静音，群管 @ 它说 → 全部认成恢复
  B 要安静的说法（47 种）：全部认成安静 / 静音（群正常时、群已经在安静时都一样）
  C 跟安静无关的日常话（27 种）：不改、也不算拿不准
  D 不许认反：「别解除安静」「还不能说话」不是恢复；方向拿不准的（又像安静又像解除）→ 拿不准，不改
  E 让它安静时顺口说了什么时候恢复 → 还是安静，时长按他说的；话里碰巧有「恢复 / 解除 / 放开」→ 不解除
  F 时长：「安静一点再吵…」「一天到晚」「说了半天了」「打 10 分」不是时长
  G 走真的 main.py（假的 AstrBot 桩）：安静 → 取消安静 → 真的恢复了；拿不准的不改状态
  H 问模型（默认关）：没问成不算问过，过一会儿同一句还能再问
"""

import asyncio
import importlib
import shutil
import sys
import tempfile
import types
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).resolve().parent.parent).resolve()
sys.path.insert(0, str(ROOT))
rules = importlib.import_module("core.rules")
when = importlib.import_module("core.when")
RES = []


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(("  [过]   " if cond else "  [没过] ") + msg + ("" if cond or not why else f"\n           {str(why)[:600]}"))
    return bool(cond)


def kind(text, **kw):
    try:
        v = rules.recognize(text, **kw)
    except TypeError as e:  # 老版本没有 names 这个参数
        return "!" + repr(e)[:60]
    return v.kind if v.sure else ("?" + str(v.kind))


ADM = dict(admin=True, addressed=True, direct=True)  # 群管 @ 了它
ON = dict(restricted=True, **ADM)  # 这个群正在安静 / 静音

print("[A 解除的说法：群在安静，群管 @ 它说 → 恢复]")
LIFT = [
    # 复核时定的 36 句（v0.1.0 认反 13 句、没认出 9 句）
    "取消安静", "结束安静", "不用再安静了", "可以说话了", "别安静了", "恢复吧", "解除安静", "不用安静了",
    "你可以说话了", "说话吧", "安静结束", "取消静音", "解除静音", "别闭嘴了", "不用闭嘴了，说吧",
    "可以不用安静了", "好了你继续聊吧", "出来吧", "安静模式关了", "行了，正常说话吧",
    "退出安静", "可以正常发言了", "解禁", "不用憋着了", "别潜水了", "开口吧", "安静取消", "不安静了", "可以聊了",
    "你可以回来了", "别再闭嘴了", "停止安静", "关闭安静模式", "可以不闭嘴了", "静音取消", "你不用再闭嘴了",
    # 另加的
    "别再安静了", "可以不安静了", "安静模式关掉", "安静解除", "停止静音", "取消闭嘴", "不用再闭嘴了",
    "解除禁言", "禁言解除了", "恢复", "恢复正常吧", "恢复说话", "放你出来", "不必安静了",
    "不用一直安静了", "别不说话了", "闭嘴时间结束了", "静音关掉吧", "你可以继续说话了", "可以开口了",
    "你不用在群里安静了", "你的禁言解除了", "不用不用安静了", "能不能别安静了", "可不可以说话了", "能不能取消安静", "解除一下安静", "把安静取消了", "我不要你安静", "没必要安静了", "你倒是说话啊", "撤销安静", "安静到此为止",
]
bad = [(t, kind(t, **ON)) for t in LIFT if kind(t, **ON) != "restore"]
for t in LIFT:
    k = kind(t, **ON)
    check(k == "restore", f"「{t}」→ 恢复", k)
rev = [x for x in bad if x[1] in ("quiet", "mute")]
check(len(LIFT) >= 30 and not rev, f"{len(LIFT)} 种解除的说法，没有一种被认成安静 / 静音", rev)
check(kind("小鱼 取消安静", admin=True, addressed=True, direct=False, restricted=True) == "restore", "群管只叫名字（没 @）说「取消安静」→ 也恢复")
check(kind("小鱼恢复", admin=True, addressed=True, direct=False, restricted=True, names=["小鱼"]) == "restore",
      "名字和「恢复」连着写（小鱼恢复）→ 也恢复", kind("小鱼恢复", admin=True, addressed=True, direct=False, restricted=True, names=["小鱼"]))
check(kind("取消安静", restricted=False, **ADM) is None, "群里本来就没在安静：「取消安静」什么都不改（以前会让它安静 30 分钟）", kind("取消安静", restricted=False, **ADM))
check(kind("取消安静", restricted=True, admin=False, addressed=True, direct=True) == "not_admin", "普通群友说「取消安静」→ 不算")

print("\n[B 要安静的说法：全部认成安静 / 静音]")
HUSH = [
    ("安静点", "quiet"), ("安静一会儿", "quiet"), ("闭嘴", "mute"), ("少说两句", "quiet"), ("别说话了", "mute"),
    ("你先别说话", "mute"), ("安静半小时", "quiet"), ("别插嘴", "quiet"), ("话太多了", "quiet"), ("消停会儿", "quiet"),
    ("别吵", "quiet"), ("静音", "mute"), ("你安静一下", "quiet"), ("先别出声", "mute"), ("少说点", "quiet"),
    ("别刷屏了", "quiet"), ("给我闭嘴一小时", "mute"), ("别发言了", "mute"), ("你去潜水吧", "quiet"), ("安静", "quiet"),
    ("安静！", "quiet"), ("闭麦", "mute"), ("不要说话", "mute"), ("能不能安静点", "quiet"), ("安静一下", "quiet"),
    ("别插嘴了", "quiet"), ("你话太多了，少说点", "quiet"), ("安静点好吗？", "quiet"), ("禁言一小时", "mute"), ("你静音吧", "mute"),
    ("行了别说了", "quiet"), ("安静一小时", "quiet"), ("一句话都别说", "mute"), ("闭嘴，安静点", "mute"), ("不许说话", "mute"),
    ("你还不闭嘴", "mute"), ("再不安静我就禁言你了", "quiet"), ("要不你安静会儿", "quiet"), ("你要不要闭嘴", "mute"),
    ("先别说话吧", "mute"), ("继续安静", "quiet"), ("你该安静了", "quiet"), ("我没说可以说话，闭嘴", "mute"),
    ("安静！别解除", "quiet"), ("在我说可以说话之前闭嘴", "mute"), ("停止刷屏安静点", "quiet"), ("会议结束前安静", "quiet"),
]
for t, want in HUSH:
    k1, k2 = kind(t, restricted=False, **ADM), kind(t, **ON)
    check(k1 == want and k2 == want, f"「{t}」→ {want}（群正常、群已经在安静时都一样）", (k1, k2))
check(len(HUSH) >= 30 and not [t for t, _ in HUSH if kind(t, **ON) in ("restore", "?restore")], "要安静的说法没有一种被认成恢复")
# 还不认得的几种说法（没认出 → 不改）：至少不能认反
for t in ["歇会儿吧", "停一下", "别bb了", "别回了", "别接话了", "休息一会"]:
    check(kind(t, **ON) in (None, "quiet", "mute"), f"「{t}」可以没认出，但不能认成恢复", kind(t, **ON))

print("\n[C 跟安静无关的日常话：群管 @ 着它说也不改、也不算拿不准]")
PLAIN = [
    "群里这么安静", "安静点好", "他一天到晚不说话", "今天群里有点安静啊", "群里突然安静了", "终于安静了",
    "安安静静的真好", "群里好安静啊",
    "安静的夜晚真舒服", "太安静了吧", "这个群平时挺安静的", "今天特别安静", "楼上装修吵了一天，现在总算安静了",
    "等我恢复了再聊", "等我电脑恢复了再找你", "我数据恢复不了了怎么办", "帮我看看这个怎么解除绑定",
    "你把手放开那个梗再说一遍", "我手机静音了没看到", "还是安静点比较好", "给小王解除禁言", "只有管理员能说话的群没意思",
    "帮我取消明天的提醒", "网络恢复了", "好好说话", "你出来一下", "放开那个女孩",
]
for t in PLAIN:
    k1, k2 = kind(t, restricted=False, **ADM), kind(t, **ON)
    check(k1 is None and k2 is None, f"「{t}」→ 不改（群正常时、群在安静时都是）", (k1, k2))
check(kind("群里这么安静，你出来说两句", restricted=False, **ADM) is None, "「群里这么安静，你出来说两句」→ 不是让它安静（以前会安静 30 分钟）",
      kind("群里这么安静，你出来说两句", restricted=False, **ADM))
check(kind("群里这么安静，你出来说两句", **ON) not in ("quiet", "mute"), "群在安静时说这句 → 也不会再安静一次", kind("群里这么安静，你出来说两句", **ON))

print("\n[D 不许认反；方向拿不准的不改]")
for t in ["别解除安静", "不用取消安静", "先别恢复", "你还不能说话", "安静还没结束呢", "没说可以说话", "不可以说话",
          "如果取消安静你又要刷屏", "他们不用安静", "你们可以说话了"]:
    check(kind(t, **ON) not in ("restore", "?restore"), f"群在安静：「{t}」不是恢复", kind(t, **ON))
for t in ["可以说话了，但少说点", "不用闭嘴了，安静点就行", "闭嘴够久了，出来吧", "撤回那句安静", "安静这事取消",
          "如果我取消安静你能少说点吗"]:
    check(kind(t, **ON) == "?None", f"「{t}」又像安静又像解除 → 拿不准（不改；开了问模型才问）", kind(t, **ON))
for t in ["十分钟后恢复", "到点自己恢复", "明天再解除"]:
    check(kind(t, **ON) is None, f"群在安静：「{t}」（没说现在就恢复）→ 不改", kind(t, **ON))
    check(kind(t, restricted=False, **ADM) is None, f"群正常：「{t}」→ 什么都没有", kind(t, restricted=False, **ADM))
for t in ["你们别吵了，听它说", "大家安静一下，看它发的这个", "你们少说两句"]:
    k1, k2 = kind(t, restricted=False, **ADM), kind(t, **ON)
    check(k1 == "?quiet" and k2 == "?quiet", f"只 @ / 引用了它说「{t}」→ 拿不准包不包括它，不改（以前安静 30 分钟）", (k1, k2))
check(kind("你们俩闭嘴", at_other=True, **ADM) == "mute" and kind("大家都睡了你安静点", **ADM) == "quiet",
      "同一条里还 @ 了别人的「你们俩闭嘴」、「大家都睡了你安静点」→ 照旧算", (kind("你们俩闭嘴", at_other=True, **ADM), kind("大家都睡了你安静点", **ADM)))
for t in ["不安静了是吧", "你不闭嘴了啊"]:
    check(kind(t, **ON) == "?None", f"「{t}」是反话还是命令拿不准 → 不改", kind(t, **ON))
check(kind("可以说话了吗", **ON) == "?restore" and kind("取消安静吗", **ON) == "?restore", "问句还是拿不准（不直接改）",
      (kind("可以说话了吗", **ON), kind("取消安静吗", **ON)))
check(kind("他不用闭嘴，你闭嘴", restricted=False, **ADM) == "mute", "「他不用闭嘴，你闭嘴」→ 静音（前半句说的是别人）", kind("他不用闭嘴，你闭嘴", restricted=False, **ADM))

import time as _time

_t0 = _time.time()
for t in ["好了" * 2000 + "电脑恢复", "安静结束" * 1500, "不用" * 3000 + "安静了", "十分钟后" * 1500 + "恢复", "你们" + "啊" * 6000 + "安静"]:
    kind(t, **ON)
    when.parse_span(t, 1794100000)
check(_time.time() - _t0 < 3, "几千字没标点的怪话：认得动（不会把正则拖死）", _time.time() - _t0)

print("\n[E 让它安静时顺口说了什么时候恢复 → 还是安静；话里碰巧有「恢复」→ 不解除]")
TZ = timezone(timedelta(hours=8))


def at(h, m=0):
    return datetime(2026, 11, 8, h, m, tzinfo=TZ).timestamp()


def span(text, now):
    return when.parse_span(text, now, TZ)[0]


for t, want, sec in [
    ("安静半小时，到点自己恢复", "quiet", 1800),
    ("闭嘴，十分钟后解除", "mute", 600),
    ("安静一小时再出来吧", "quiet", 3600),
    ("先闭嘴，等我说可以说话了再说", "mute", None),
    ("安静点，开完会放你出来", "quiet", None),
    ("安静点，到点恢复", "quiet", None),
]:
    k1, k2 = kind(t, restricted=False, **ADM), kind(t, **ON)
    check(k1 == want and k2 == want and span(t, at(14)) == sec, f"「{t}」→ {want}，时长 {sec or '默认'}", (k1, k2, span(t, at(14))))
sec = span("安静，8点恢复", at(15))
check(kind("安静，8点恢复", restricted=False, **ADM) == "quiet" and sec == 5 * 3600, "15 点说「安静，8点恢复」→ 安静到 20 点", (kind("安静，8点恢复", restricted=False, **ADM), sec))
for t in ["等我恢复了再聊", "等我电脑恢复了再找你", "你把手放开那个梗再说一遍", "我数据恢复不了了怎么办"]:
    check(kind(t, **ON) is None, f"群在安静，群管 @ 它「{t}」→ 不解除", kind(t, **ON))

print("\n[F 时长：随口一句不是时长]")
for t in ["安静一点再吵就禁言你", "安静一点后面的人在说正事", "别吵，一天到晚就你话多", "少说点，给你打10分", "少说点，给你打 10 分",
          "你都说了半天了，安静点", "这两天你话太多了，安静点", "前两天不是说了让你安静"]:
    check(span(t, at(14)) is None, f"14 点说「{t}」→ 没说多久（用默认）", when.parse_span(t, at(14), TZ))
for t, now, want in [
    ("一点以后再说话", at(22), 3 * 3600), ("安静到一点", at(22), 3 * 3600), ("下午一点后再说话", at(10), 3 * 3600),
    ("安静1点再说", at(22), 3 * 3600), ("安静一天", at(14), 86400), ("安静半天", at(14), 12 * 3600), ("禁言20分", at(14), 1200),
    ("8点以后再说话", at(15), 5 * 3600), ("过了一点再说话", at(22), 3 * 3600), ("这三天安静点", at(14), 3 * 86400),
    ("过两天再说话", at(14), 2 * 86400), ("这两天你安静点", at(14), 2 * 86400), ("安静大半天", at(14), 12 * 3600), ("闭嘴，十分钟后解除", at(14), 600),
]:
    check(span(t, now) == want, f"{datetime.fromtimestamp(now, TZ):%H:%M} 说「{t}」→ {want / 3600:g} 小时（真说了时间的照认）", when.parse_span(t, now, TZ))

# ───────── 下面走真的 main.py：给它一个假的 astrbot ─────────
print("\n[G 走真的 main.py：安静 → 取消安静]")


class _Deco:
    class EventMessageType:
        ALL = "all"

    class PermissionType:
        ADMIN = "admin"

    def __getattr__(self, name):
        def factory(*a, **k):
            def deco(fn):
                if name == "command_group":
                    return type("G", (), {"command": lambda self, *a, **k: (lambda f: f)})()
                return fn

            return deco

        return factory


class _Chain:
    def __init__(self, chain=None):
        self.chain = chain or []

    def message(self, t):
        self.chain.append(t)
        return self


class _Part:
    def __init__(self, text="", qq="", name="", id="", sender_id=""):
        self.text, self.qq, self.name, self.id, self.sender_id = text, qq, name, id, sender_id


TMP = Path(tempfile.mkdtemp(prefix="ga_words_"))


def _mod(name, **attrs):
    m = types.ModuleType(name)
    m.__dict__.update(attrs)
    sys.modules[name] = m
    return m


class _Log:
    def __getattr__(self, name):
        return lambda *a, **k: None


comp = _mod("astrbot.api.message_components", Plain=type("Plain", (_Part,), {}), At=type("At", (_Part,), {}),
            Reply=type("Reply", (_Part,), {}), Image=type("Image", (_Part,), {}))
_mod("astrbot")
_mod("astrbot.api", AstrBotConfig=dict, logger=_Log(), message_components=comp)
_mod("astrbot.api.event", AstrMessageEvent=object, MessageChain=_Chain, MessageEventResult=_Chain, filter=_Deco())
_mod("astrbot.api.provider", ProviderRequest=object)
_mod("astrbot.api.star", Context=object, Star=type("Star", (), {"__init__": lambda self, context=None: setattr(self, "context", context)}),
     StarTools=type("StarTools", (), {"get_data_dir": staticmethod(lambda name: TMP / name)}))
sys.path.insert(0, str(ROOT.parent))
M = importlib.import_module(ROOT.name + ".main")


class Ctx:
    calls, fail = 0, 0  # 模型被叫了几次；前几次让它超时

    def register_web_api(self, *a, **k):
        pass

    def get_config(self, **k):
        return {"admins_id": [], "wake_prefix": ["/"]}

    def get_registered_star(self, n):
        return None

    async def send_message(self, *a, **k):
        pass

    async def get_current_chat_provider_id(self, umo):
        return "p"

    async def llm_generate(self, **k):
        Ctx.calls += 1
        if Ctx.calls <= Ctx.fail:
            raise asyncio.TimeoutError()
        return types.SimpleNamespace(completion_text='{"target":"me","action":"restore","minutes":null,"confidence":95}')


class Ev:
    bot = None
    unified_msg_origin = "napcat:GroupMessage:555"

    def __init__(self, uid, text, role="member", at_me=True):
        self.uid, self.message_str, self.is_at_or_wake_command = uid, text, at_me
        self.message_obj = types.SimpleNamespace(
            message_str=text, raw_message={"post_type": "message", "sender": {"role": role, "card": "", "nickname": "n" + uid}})
        self._m = ([comp.At(qq="10000")] if at_me else []) + [comp.Plain(text=text)]
        self._x = {}

    def get_sender_id(self):
        return self.uid

    def get_self_id(self):
        return "10000"

    def get_group_id(self):
        return "555"

    def get_platform_id(self):
        return "napcat"

    def get_sender_name(self):
        return "阿木"

    def get_messages(self):
        return self._m

    def is_private_chat(self):
        return False

    def is_admin(self):
        return False

    def set_extra(self, k, v):
        self._x[k] = v

    def get_extra(self, k, d=None):
        return self._x.get(k, d)


async def e2e():
    GK, real = "napcat:555", M.time.time
    shift = [0.0]
    M.time.time = lambda: real() + shift[0]
    try:
        p = M.FatfishGroupAdmin(Ctx(), {"names": ["小鱼"]})  # 默认配置（问模型关着），只填了它的名字

        async def say(text, role="owner", at_me=True):
            e = Ev("1", text, role=role, at_me=at_me)
            await p._on_message(e)
            return e

        def left():
            m = p.store.mode(GK, M.time.time())
            return (m["mode"], round((m["until"] - M.time.time()) / 60)) if m else None

        await say("安静点")
        check(left() == ("quiet", 30), "群主 @ 它「安静点」→ 安静 30 分钟", left())
        shift[0] = 1200
        e = await say("取消安静")
        check(left() is None and (e.get_extra(M.EXTRA_ACK) or {}).get("kind") == "restore",
              "20 分钟后群主 @ 它「取消安静」→ 恢复了，给模型的事实是「恢复」（以前：又安静 30 分钟）", (left(), e.get_extra(M.EXTRA_ACK)))
        await say("闭嘴一小时")
        check(left() == ("mute", 60), "群主「闭嘴一小时」→ 静音 60 分钟", left())
        shift[0] += 600
        await say("可以说话了，但少说点")
        check(left() == ("mute", 50) and Ctx.calls == 0, "拿不准的那句（可以说话了，但少说点）→ 状态原样（还剩 50 分钟），默认也不问模型", (left(), Ctx.calls))
        await say("等我电脑恢复了再找你")
        check(left() == ("mute", 50), "「等我电脑恢复了再找你」→ 静音还在（以前被解除）", left())
        await say("别再闭嘴了")
        check(left() is None, "「别再闭嘴了」→ 恢复了（以前：再静音 30 分钟）", left())
        await say("安静两小时")
        await say("安静点，到点恢复", role="admin")
        check(left() == ("quiet", 120), "群主设的安静两小时，群管理员说「安静点，到点恢复」→ 还是群主那层（不缩短）", left())
        await say("不用再安静了")
        check(left() is None, "群主「不用再安静了」→ 恢复了", left())
        await say("小鱼闭嘴十分钟", at_me=False)
        m1 = left()
        e = await say("小鱼恢复", at_me=False)
        check(m1 == ("mute", 10) and left() is None and e.is_at_or_wake_command,
              "不 @、名字和命令连着写：「小鱼闭嘴十分钟」→ 静音，「小鱼恢复」→ 恢复（替 AstrBot 叫醒模型回这一句）", (m1, left()))
        await p.terminate()

        print("\n[H 问模型（默认关，这里打开）：没问成不算问过]")
        p = M.FatfishGroupAdmin(Ctx(), {"use_llm": True})
        p.store.put_mode(GK, "quiet", M.time.time() + 3600, 2, "1", "阿木", "群主", "安静一小时", M.time.time())
        Ctx.calls, Ctx.fail = 0, 1
        await p._on_message(Ev("1", "可以说话了吗", role="owner"))
        check(Ctx.calls == 1 and left() and left()[0] == "quiet", "第一次问模型超时 → 不改", (Ctx.calls, left()))
        await p._on_message(Ev("1", "可以说话了吗", role="owner"))
        check(Ctx.calls == 1, "紧接着同一句再说 → 先不问（没问成的也隔 1 分钟，免得模型挂着的时候被刷）", Ctx.calls)
        shift[0] += 90
        await p._on_message(Ev("1", "可以说话了吗", role="owner"))
        check(Ctx.calls == 2 and left() is None, "过了 1 分多钟同一句再说 → 又问了一次，这次问成了 → 恢复（以前：10 分钟内不再问）", (Ctx.calls, left()))
        p.store.put_mode(GK, "quiet", M.time.time() + 3600, 2, "1", "阿木", "群主", "安静一小时", M.time.time())
        shift[0] += 90
        await p._on_message(Ev("1", "可以说话了吗", role="owner"))
        check(Ctx.calls == 2 and left() and left()[0] == "quiet", "问成了的那一句：10 分钟内同一句不重复问（没变）", (Ctx.calls, left()))
        Ctx.calls, Ctx.fail = 0, 99
        for i in range(M.JUDGE_PER_HOUR + 5):
            await p._on_message(Ev("1", f"第{i}遍：可以说话了吗", role="owner"))
        check(Ctx.calls <= M.JUDGE_PER_HOUR, f"模型一直挂着：每个群每小时还是最多问 {M.JUDGE_PER_HOUR} 次（没问成的也占名额）", Ctx.calls)
        await p.terminate()
    finally:
        M.time.time = real


asyncio.run(e2e())
shutil.rmtree(TMP, ignore_errors=True)

ok = sum(1 for c, _ in RES if c)
bad = [m for c, m in RES if not c]
print("\n" + "=" * 60 + f"\n  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
for m in bad:
    print("    -", m)
print("=" * 60)
sys.exit(1 if bad else 0)
