# -*- coding: utf-8 -*-
"""认话：群里这句是不是在让机器人安静 / 闭嘴 / 恢复，是不是在说「别接我的话」/「可以理我了」。不依赖 AstrBot。

规则先认（不花钱）：
  - 群管的话：要冲着它说（@ 它 / 引用它 / 叫它名字）。「你们安静点」没点它的名 → 不算（多半在说别人）
  - 普通群友让它安静 → 不改群的状态（只给模型一句事实：他不是群主 / 管理员，不算）
  - 「为什么要闭嘴」「谁让你安静了」「我先安静会儿」「让他们安静点」「不是让你闭嘴」→ 不是在下命令
  - 转述别人的话（「他刚说让你闭嘴」）、开玩笑（「别说了笑死我了」）、「我手机静音了」「谁把小王禁言了？」→ 不算
  - 拿不准的（问句：「？」结尾、「…吗」、「谁…了」；又让安静又让恢复）→ sure=False，配置开了才问一次模型
「谁算管理员」由调用的地方按 QQ 身份和配置算好传进来（嘴上说「我是管理」不算）。
"""

import re
from collections import namedtuple

Verdict = namedtuple("Verdict", "kind sure why")
NONE = Verdict(None, True, "")

QUIET_NAME = {"quiet": "安静", "mute": "静音"}

# 恢复先认（「别安静了」「不用闭嘴」里也有安静 / 闭嘴，先把它们挖掉再找安静 / 静音）
_RESTORE = (
    r"(?:可以|能|准你|允许你)\s*(?:说话|讲话|开口|发言|出来|冒泡)|说话吧|讲话吧|开口吧|"
    r"恢复|解除|解禁|解封|放(?:你)?出来|放开|出来吧|"
    r"别安静|不用安静|不要安静|不必安静|别闭嘴|不用闭嘴|不要闭嘴|不必闭嘴|别少说|不用少说|"
    r"别憋着|随便说|想说就说|你自由了|活跃起来"
)
# 「静音」「禁言」只认冲它下命令的用法（开头 /「你」「给我」「先」后面，后面跟着结尾、时长、「吧」）：
# 「我手机静音了」「谁把小王禁言了？」「帮我禁言他」都不是让它静音
_CMD_HEAD = r"(?:^|(?<=[\s，,。.!！~～:：])|(?<=你)|(?<=给我)|(?<=自己)|(?<=先)|(?<=就)|(?<=开))"
_CMD_TAIL = r"(?=\s*(?:$|[，,。.!！~～?？]|吧|一下|一会|起来|[0-9零〇一二两三四五六七八九十半]|到|直到|至))"
_MUTE = (
    r"闭嘴|住嘴|闭麦|别说话|不要说话|不许说话|不准说话|别讲话|不要讲话|不许讲话|"
    r"别出声|不要出声|别吭声|不许吭声|别发言|不要发言|不许发言|别开口|不要开口|"
    r"别再说话|" + _CMD_HEAD + r"(?:静音|禁言)" + _CMD_TAIL + r"|一句(?:话)?都别说|一个字都别说|shut\s*up"
)
# 「安静」只认让它安静的用法：「好安静啊」「安静的夜晚」「太安静了」是在描述，不是命令
# 「别说了」多半是「行了别说了」，按轻的一档（安静）算；「别闹了」「话痨」「你在潜水」不算
_QUIET = (
    r"(?<![很好太真挺多])安静(?![的地]|了吗|吗)|少说|少讲|话少|少点话|少发言|少冒泡|别冒泡|不要冒泡|"
    r"别插嘴|不要插嘴|别插话|不要插话|别刷屏|不要刷屏|消停(?!了)|收敛(?:点|些|一点|一下)|低调点|"
    r"别吵(?!架)|不要吵|话太多|话好多|话也太多|去潜水|潜水去|潜水吧|别那么活跃|别太活跃|"
    r"别说了|不要说了|别再说了"
)
RESTORE = re.compile(_RESTORE, re.I)
MUTE = re.compile(_MUTE, re.I)
QUIET = re.compile(_QUIET, re.I)
_VERBS = f"(?:{_MUTE}|{_QUIET}|{_RESTORE})"

# 不是在下命令
NOT_ME = re.compile(r"不是(?:让|叫|说|在说)你|(?:又)?没(?:让|叫|说)你|不是说你|没说你|别误会")
ASK_WHY = re.compile(
    r"(?:为什么|为啥|干嘛|干吗|凭什么|怎么|咋|谁让你|谁叫你|谁说)[^，,。.!！?？\n]{0,6}?" + _VERBS, re.I
)
SELF_SUBJ = re.compile(
    r"(?:^|[，,。.!！?？\s])(?:我|俺|咱)(?:要|先|得|想|就|也|还是|准备)?(?:安静|闭嘴|少说|不说话|潜水|闭麦)|"
    r"让我(?:安静|静静|闭嘴|少说)"
)
OTHERS = re.compile(
    r"(?:让|叫|把|给|帮我)(?:他们|她们|他|她|大家|别人|这个人|那个人|这群人|它们|群友)[^，,。]{0,3}?" + _VERBS
    + r"|禁言\s*(?:他|她|这个|那个|他们|她们|@)",
    re.I,
)
POLITE = re.compile(r"能不能|能否|可不可以|可以.{0,8}吗|能.{0,8}吗|行不行|好不好|好吗|行吗|麻烦|请")
# 问句（「？」结尾、「…吗 / 呢」、「谁…了」）：拿不准是不是在下命令 → 不直接改（配置开了才问模型）
QUESTION = re.compile(r"[?？]\s*$|(?:吗|呢|么)\s*[?？。!！~～]*\s*$|谁[^，,。.!！?？\n]{0,12}了")
# 转述别人的话（「他说让你闭嘴」「刚说…」「让你安静的是群主」）：不是他在下命令
REPORT = re.compile(
    r"(?:他们|她们|他|她|群主|管理员|管理|别人|有人|大家|老板|那人|这人)(?:刚才|刚刚|刚|之前|方才|也|都|还)?(?:说|讲)"
    r"|(?<!我)(?:刚才|刚刚|方才|刚)(?:说|讲)"
    r"|让你.{0,10}的是(?:他|她|群主|管理|别人|有人)"
)
# 开玩笑的语气（「别说了笑死我了」「安静点哈哈哈」）：不当真
JOKE = re.compile(r"笑死|哈哈|嘿嘿|h{3,}|2333|笑不活|笑疯|xswl|绷不住|蚌埠住|笑喷|好好笑|太好笑|乐死|😂|🤣", re.I)

# 群友只管自己：「别理我」「别接我的话」「别 @ 我」/「可以理我了」
_ME = r"(?:我|俺|人家)"
SELF_OFF = re.compile(
    r"(?:别|不要|甭|莫|勿|少)\s*(?:再)?\s*(?:来)?\s*(?:理|搭理|理会|接|回|回复|@|＠|艾特|at|cue|烦|找|管|插|吵)\s*"
    + _ME
    + r"(?!们|朋|同|家|哥|姐|弟|妹|爸|妈|老|爷|奶|室|男|女|对象|的(?!话))"
    r"|(?:别|不要)\s*(?:跟|和|找|对)\s*我\s*(?:说话|聊天|聊|讲话|搭话)"
    r"|离我远[点些]|别来烦我|不想(?:跟|和)你(?:说话|聊)",
    re.I,
)
SELF_ON = re.compile(
    r"(?:可以|能|又可以|又能|继续|恢复|重新)\s*(?:理|搭理|接|回|回复|@|＠|艾特|at|cue|找)\s*"
    + _ME
    + r"(?:的话)?"
    r"|理理我|理我一下|理一下我|别不理我|不要不理我|(?:可以|能)\s*(?:跟|和)\s*我\s*(?:说话|聊天|聊)",
    re.I,
)


def _mask(text, pat):
    """把匹配到的地方换成占位（后面的查找就看不到它们了）→ (换完的字, 有没有匹配到)"""
    hit = [False]

    def rep(m):
        hit[0] = True
        return "〇" * len(m.group(0))

    return pat.sub(rep, text), hit[0]


def admin_words(text):
    """这句里有没有让它安静 / 静音 / 恢复的说法 → "quiet" / "mute" / "restore" / "mixed" / None（不看是谁说的、冲谁说的）"""
    t = str(text or "")
    masked, rs = _mask(t, RESTORE)
    mu, qu = bool(MUTE.search(masked)), bool(QUIET.search(masked))
    if rs and (mu or qu):
        return "mixed"
    if rs:
        return "restore"
    if mu:
        return "mute"
    if qu:
        return "quiet"
    return None


def recognize(text, admin=False, addressed=False, direct=False, at_other=False,
              restricted=False, recent=False, hushed=False):
    """这句话要不要改状态 → Verdict(kind, sure, why)

    text：这条消息的字（去掉了 @ 它那一段）
    admin：说话的人算不算管理员（调用的地方按 QQ 身份 + 配置算）
    addressed：冲着它说的（@ 它 / 引用它 / 叫它名字）；direct：@ 它 / 引用它（不只是叫名字）
    at_other：@ 了别人；restricted：这个群现在在安静 / 静音；recent：它刚回过这个人；hushed：这个人说过别接他的话
    kind：quiet / mute / restore / self_off / self_on / not_admin / None；sure=False = 拿不准"""
    t = str(text or "").strip()
    if not t:
        return NONE
    if NOT_ME.search(t):
        return Verdict(None, True, "不是在说它")
    # @ 了别人、只叫了它的名字：多半是在跟那个人说
    talking_to_it = addressed and not (at_other and not direct)
    # 「别吵我」「别跟我说话」是在管他自己：先挖掉，剩下的才看是不是让它安静
    self_off, self_on = SELF_OFF.search(t), SELF_ON.search(t)
    rest, _ = _mask(t, SELF_OFF)
    rest, _ = _mask(rest, SELF_ON)
    words = admin_words(rest)
    # 「可以安静点吗」是客气的命令；「可以说话了吗」的「可以」是恢复那个说法自己的，不算客气 → 挖掉再看
    asked = bool(QUESTION.search(t)) and not POLITE.search(_mask(rest, RESTORE)[0])

    # 群管让它安静 / 静音 / 恢复（群管说「别理我」只管他自己，下面再看）
    if words and talking_to_it and admin:
        v = _admin_verdict(rest, words, restricted, asked)
        if v.kind or not v.sure:
            return v
    # 群友只管自己：冲着它说，或者它刚回过他（不是在 @ 别人）
    to_it = talking_to_it or (recent and not at_other)
    if self_on:
        if hushed and to_it:
            return Verdict("self_on", True, "让它又可以接他的话")
        return Verdict(None, True, "本来就没说过别接他的话" if not hushed else "没冲着它说")
    if self_off:
        if not to_it:
            return Verdict(None, True, "没冲着它说")
        if REPORT.search(t):
            return Verdict(None, True, "转述别人的话")
        if asked:
            return Verdict("self_off", False, "问句（拿不准是不是在让它别接）")
        return Verdict("self_off", True, "让它别主动接他的话")
    # 普通群友让它安静：不算，但给模型一句事实
    if words in ("quiet", "mute") and talking_to_it and not admin:
        v = _admin_verdict(rest, words, restricted, asked)
        if v.kind and v.sure:
            return Verdict("not_admin", True, f"不是群主 / 管理员，让它{QUIET_NAME[v.kind]}不算")
    if words == "restore" and talking_to_it and not admin and restricted:
        v = _admin_verdict(rest, words, restricted, asked)
        if v.kind and v.sure:
            return Verdict("not_admin", True, "不是群主 / 管理员，让它恢复不算")
    return NONE


def _admin_verdict(t, words, restricted, asked):
    if SELF_SUBJ.search(t):
        return Verdict(None, True, "说的是他自己")
    if OTHERS.search(t):
        return Verdict(None, True, "说的是别人")
    if REPORT.search(t):
        return Verdict(None, True, "转述别人的话")
    if words in ("quiet", "mute") and JOKE.search(t):
        return Verdict(None, True, "开玩笑的语气")
    if ASK_WHY.search(t):
        return Verdict(words if words != "mixed" else None, False, "在问为什么（拿不准是不是命令）")
    if words == "mixed":
        return Verdict(None, False, "又让安静又让恢复（拿不准）")
    if words == "restore" and not restricted:
        return Verdict(None, True, "这个群本来就没在安静")
    if asked:
        return Verdict(words, False, "问句（拿不准是不是命令）")
    why = {"quiet": "让它安静", "mute": "让它静音", "restore": "让它恢复"}[words]
    return Verdict(words, True, why)


def names_called(text, names):
    """这句里叫没叫它的名字（名字至少 2 个字，太短的会误认）"""
    t = str(text or "").lower()
    for n in names or ():
        n = str(n or "").strip().lower()
        if len(n) >= 2 and n in t:
            return True
    return False
