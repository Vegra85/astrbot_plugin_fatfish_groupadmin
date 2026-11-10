# -*- coding: utf-8 -*-
"""认话：群里这句是不是在让机器人安静 / 闭嘴 / 恢复，是不是在说「别接我的话」/「可以理我了」。不依赖 AstrBot。

规则先认（不花钱）：
  - 群管的话：要冲着它说（@ 它 / 引用它 / 叫它名字）。「你们安静点」没点它的名 → 不算（多半在说别人）
  - 普通群友让它安静 → 不改群的状态（只给模型一句事实：他不是群主 / 管理员，不算）
  - 「为什么要闭嘴」「谁让你安静了」「我先安静会儿」「让他们安静点」「不是让你闭嘴」→ 不是在下命令
  - 转述别人的话（「他刚说让你闭嘴」）、开玩笑（「别说了笑死我了」）、「我手机静音了」「谁把小王禁言了？」→ 不算
  - 拿不准的（问句：「？」结尾、「…吗」、「谁…了」；又让安静又让恢复）→ sure=False，配置开了才问一次模型
  - v0.1.1：先认「解除」再认「安静」（「取消安静」「不用再安静了」是解除，以前被认成再安静一次），见下面 _lift
「谁算管理员」由调用的地方按 QQ 身份和配置算好传进来（嘴上说「我是管理」不算）。
"""

import re
from collections import namedtuple

Verdict = namedtuple("Verdict", "kind sure why")
NONE = Verdict(None, True, "")

QUIET_NAME = {"quiet": "安静", "mute": "静音"}

# ───────── 解除（v0.1.1：先认「解除」再认「安静」）─────────
# v0.1.0 只认「别安静 / 不用安静」这种紧挨着的写法：「取消安静」「结束安静」「不用再安静了」对不上，剩下的「安静」被认成再安静一次。
# 现在按结构认，不靠短语表：
#   否定 + 安静类的词（别 / 不用 / 不必 … [再 / 继续 / 一直] 安静 / 闭嘴 / 静音 / 潜水 / 不说话）
#   取消类的词 + 安静类的词（取消 / 解除 / 结束 / 停止 / 关闭 / 退出 … 安静）；安静类的词 + 结束类的词（安静结束 / 静音取消 / 安静模式关了）
#   可以 / 能 + 说话类的词（可以说话了 / 可以正常发言了 / 可以聊了）；说话吧 / 继续聊吧 / 正常说话吧
#   单说的「恢复 / 解除 / 解禁 / 出来吧」：要在这句话的主干上（开头，或者前面只有「你 / 可以 / 现在 / 好了」这类词），「等我电脑恢复了」「怎么解除绑定」不算
# 每个认到的地方再看它前面：「别解除安静」「还不能说话」是否定 → 不算解除；「他可以说话」说的是别人 → 不算；
# 「十分钟后解除」「到点自己恢复」「等我说可以说话了」说的是以后 → 不算现在解除（前面有让它安静的话时，就是安静）
_SEP = "，,。.!！?？~～…；;:：、\n\r\t 　"
# 安静类的词（一种状态）
_S_STATE = "安静|静音|免打扰|勿扰|闭嘴|住嘴|闭麦|禁言|潜水|憋着|沉默|噤声|少说话?|少讲话?|少发言|少冒泡|低调|收敛|消停"
_S = r"(?:" + _S_STATE + r"|不(?:说话|讲话|出声|吭声|吱声|发言|开口|冒泡))(?:模式|状态)?"
# 「特别安静」「个别」里的「别」不是否定
_L_NEG = (
    r"(?:(?<![特个区分告辨识])别|甭|不用|不必|不要|不需要|没必要|无需|无须|用不着|不许|不准)"
    r"(?:再|继续|一直|老是|总是|老|总|这么|那么|太|给我|在(?:群里|这里|这儿|这个群|这)?|你|您)*" + _S
)
# 光一个「不」：只认「不安静了」「可以不闭嘴了」（在主干上、后面带「了」）；「还不闭嘴」「再不安静」「真不安静」是在催它安静
_L_NOT = r"不再?(?:" + _S_STATE + r")(?:模式|状态)?了"
_CANCEL_WORD = "取消|解除|结束|停止|终止|中止|关闭|关掉|关了|退出|撤销|撤掉|撤回|解开|去掉|停掉"
_L_CANCEL = (
    r"(?:" + _CANCEL_WORD + r"|(?<![了理])解(?=禁言))(?:了|掉|一下)?"
    r"(?:(?:你|它|我|刚才|刚刚|之前|现在|当前|本群|群里|这个群|上面|前面|这次|这个|那个)的?)?" + _S
)
# 「安静还没结束」「安静不用取消」：中间夹着否定 → 不是解除
_L_END = (
    _S + r"的?(?:时间|期间|时段)?(?:可以|就|都|已经|已|到此|现在|该|也|先)?(%s还没有?|没有?|尚未|未|不用|不能|别|不要|不会|不)?"
    r"(?:结束|取消|解除|解了|解开|关了|关掉|关闭|停了|停止|作废|撤了|撤销|撤掉|到此为止|完毕|免了|不用了|到点了|到时间了|时间到了)"
)
_L_PERMIT = (
    r"(?:可以|能|准你|允许你)\s*(?:继续|正常|随便|随意|重新|再|又|接着|开始|自由)*"
    r"(?:说话|讲话|开口|发言|出来|冒泡|出声|(?:聊天?|回来|继续|活跃|接话|回话)(?=了|啦|吧|咯|喽)|(?:说|讲)(?=了(?!算)|啦|吧))"
)
_L_URGE = (
    r"(?:说话|讲话|开口|发言|冒泡)吧|(?:继续|接着|正常|放开|尽管|随便|随意)了?(?:说话?|聊天?|讲话?|发言|水群?)(?:吧|了|啦)"
    r"|随便说|想说就说|畅所欲言|你自由了|活跃起来|恢复(?:正常)?(?:说话|发言|聊天|讲话|冒泡|活跃)"
)
_L_TAIL = r"(?=[了吧啦呀啊哦嘛哈呗咯喽]*(?:$|[" + _SEP + r"]))"
_L_BARE = (
    r"(?:恢复(?:正常|原样|原状|自由)?|解除|解禁|解封|放开(?:了?(?:说|聊))?|我?放你?出来"
    r"|出来(?:说话|说两句|说句话|聊天?|冒个?泡|水群|(?=吧|啦))|回来(?=吧)|(?<!好好)(?:倒是)?说(?:句|两句)?话)" + _L_TAIL
)
# (哪一类, 正则)：N / S = 话里带着安静类的词（认到了就要挖掉，不然剩下的「安静」会被当成让它安静）；T = 说话类的；
# NOT / BARE = 要在主干上才算
_LIFTS = tuple(
    (fam, re.compile(pat, re.I))
    for fam, pat in (
        ("N", _L_NEG), ("NOT", _L_NOT), ("S", _L_CANCEL), ("S", _L_END % "?P<neg>"),
        ("T", _L_PERMIT), ("T", _L_URGE), ("BARE", _L_BARE),
    )
)
_RESTORE = "|".join((_L_NEG, _L_NOT, _L_CANCEL, _L_END % "?:", _L_PERMIT, _L_URGE, _L_BARE))
# 主干上允许出现在前面的词（「你可以恢复了」「好了恢复吧」）
_L_HEAD = (
    r"你的?|您|可以|能|现在|这下|马上|立刻|立即|那就|就|那|好了|行了|好啦|行啦|好的?|行|ok|快点?|赶紧|赶快|给我|请|都|也|又|已经|终于|总算"
    r"|把|的|先|直接|来|嗯|哦|喂|诶|哎|自己|自动|自行|了|吧|啦|我说|我宣布|我批准|我同意|同意|批准|全部|统统|还是|要不|不如|该"
)
# 说的是以后（「十分钟后」「到点」「8 点」「明天」「再」「才」）
_L_DEFER = (
    r"(?:\d+(?:\.\d+)?|[零〇一二两俩三四五六七八九十百半几]+)\s*个?半?\s*(?:分钟|小时|钟头|天|点半|点钟|点|秒钟?|刻钟|分|h|min)(?:以后|之后|后)?"
    r"|到点|到时候|到时间|到时|届时|以后|之后|然后|等会儿?|待会儿?|过会儿?|过一会儿?|一会儿?|回头|晚点|晚些|稍后"
    r"|明天|明早|明晚|今晚|晚上|早上|下午|中午|后天|再|才"
)
_L_HEAD_RE = re.compile(_L_HEAD, re.I)
_L_DEFER_RE = re.compile(_L_DEFER, re.I)
_L_PRE_MAX = 40  # 只看它前面这么多个字（主干上的前缀本来就短；也免得一长串没标点的字拖慢）
_L_DEFER_PRE = re.compile(r"(?:" + _L_DEFER + r")(?:就|你|我|自己|自动|自行|会|可以|能|给你|让你|才|再|说)*$", re.I)
_L_NEG_PRE = re.compile(
    r"(?:不是|不|别|没有?|未|甭|勿|莫)(?:用|要|必|能|可以|许|准|让|给|想|会|该|说|叫|是|打算|需要|允许|急着|着急)*"
    r"(?:你|过|再|现在|马上|就|给我)*$"
)
_L_OTHER_PRE = re.compile(
    r"(?:他们?|她们?|它们|你们|我们?|咱们?|俺|大家|别人|谁|管理员?|群主|群友|群员|成员)(?:都|也|才|就|现在|已经|还|又|的)*$"
)
_L_CLAUSE_END = re.compile(_L_TAIL)
_L_REPEAT = re.compile(r"别|甭|不用|不必|不要")
_L_UNTIL = re.compile(r"了?(?:之前|以前|为止)")  # 「我说可以说话之前…」：说的是以后
_L_IF = re.compile(r"如果|要是|假如|万一")
_L_IF_AFTER = re.compile(r"了?的话")
_L_NOT_END = re.compile(r"[吧啦]?(?:$|[" + _SEP + r"])")  # 「不安静了」后面就完了才算命令；「不安静了是吧」「不闭嘴了啊」是反话 → 拿不准
# 兜底：让它安静的词旁边（同一小句里紧挨着）还有「取消 / 解除 / 撤销」这类词、上面又没认成解除的（「撤回那句安静」「安静的命令作废」）
# → 方向拿不准，不当成让它安静。「会议结束前安静」是在说时间、「停止刷屏安静点」「取消订单的事先别说话」取消的是别的事，不算
_NEAR_BEFORE = re.compile(
    r"(?:" + _CANCEL_WORD + r"|作废)(?:了|掉|一下|那句|那条|那个|这个|刚才|刚刚|之前|上面|前面|我说的|让你|叫你|的|你|它)*$"
)
_NEAR_AFTER = re.compile(
    r"(?:模式|状态|的|这个|那个|命令|指令|这事|这件事|那事|时间|可以|就|也|都|先|给我|帮我|请|了|吧)*(?:" + _CANCEL_WORD + r"|作废)"
)


def _near_cancel(t, m):
    if m.group(0) not in ("安静", "静音", "闭嘴", "住嘴", "闭麦", "禁言"):
        return False
    a, b = m.span()
    k = a
    while k > max(0, a - _L_PRE_MAX) and t[k - 1] not in _SEP:
        k -= 1
    return bool(_NEAR_BEFORE.search(t[k:a]) or _NEAR_AFTER.match(t, b))


def _trunk(pre):
    """它前面（同一小句里）是不是只有主干上的词 → None（不是）/ "now" / "later"（里面有「十分钟后」「到点」这类）。
    一个词一个词往后对，不回头（不用一条大正则：一长串「好了好了好了…」会把正则拖死）"""
    if len(pre) > _L_PRE_MAX:
        return None
    i, later = 0, False
    while i < len(pre):
        m = _L_DEFER_RE.match(pre, i)
        if m and m.end() > i:
            later = True
        else:
            m = _L_HEAD_RE.match(pre, i)
            if not m or m.end() == i:
                return None
        i = m.end()
    return "later" if later else "now"


def _lift(text):
    """找这句里「解除」的说法 → (把它们挖掉以后的字, 有没有现在就解除的, 有没有说以后才解除的, 有没有「如果解除」这种商量的)"""
    t = str(text or "")
    spans, now, later, unsure = [], False, False, False
    for fam, pat in _LIFTS:
        for m in pat.finditer(t):
            a, b = m.span()
            k, lo = a, max(0, a - _L_PRE_MAX - 1)
            while k > lo and t[k - 1] not in _SEP:
                k -= 1
            trunk = _trunk(t[k:a])
            on_trunk = trunk is not None
            pre = t[max(k, a - _L_PRE_MAX):a]  # 同一小句里、它前面的字
            if fam in ("NOT", "BARE"):
                if not on_trunk:
                    continue  # 不在主干上：不算（也不挖）
                flag = trunk
                if fam == "NOT" and not _L_NOT_END.match(t, b):
                    flag = "if"
            else:
                if fam == "N" and t[a] == "不" and a > 0 and t[a - 1] == t[a + 1] and t[max(0, a - 2)] != "不":
                    continue  # 「要不要闭嘴」「用不用安静」：不是否定（「不用不用安静了」是）
                chk = pre
                rp = _L_REPEAT.match(m.group(0)) if fam == "N" else None
                while rp and chk.endswith(rp.group(0)):
                    chk = chk[: -len(rp.group(0))]  # 「不用不用安静了」「别别别闭嘴」：说了两遍，不是双重否定
                # 「能不能取消安静」「可不可以说话了」的「不」不是否定
                if _L_NEG_PRE.search(re.sub(r"(.)不(?=\1)", "〇〇", chk + t[a])[:-1]) or m.groupdict().get("neg"):
                    flag = "neg"
                elif fam in ("T", "N") and _L_OTHER_PRE.search(pre) and not re.search("[你您]", m.group(0)):
                    flag = "skip"  # 「他可以说话」「只有管理员能说话」「他不用闭嘴」：说的是别人（「我不要你安静」不是）
                elif "禁言" in m.group(0) and not (on_trunk and _L_CLAUSE_END.match(t, b)):
                    flag = "skip"  # 「给小王解除禁言」「别禁言他」：说的是别人的禁言
                elif _L_IF.search(pre) or _L_IF_AFTER.match(t, b):
                    flag = "if"  # 「如果取消安静…」「取消安静的话…」：在商量，不是命令 → 拿不准
                elif _L_DEFER_PRE.search(pre) or "等" in pre or _L_UNTIL.match(t, b):
                    flag = "later"
                else:
                    flag = "now"
                if fam == "T" and flag in ("neg", "skip"):
                    continue  # 「先别说话吧」「不许说话」：留给下面认静音
            now, later, unsure = now or flag == "now", later or flag == "later", unsure or flag == "if"
            if fam != "BARE":
                mm = re.search(r"(?:可以|能)$", pre)  # 「可以不用安静了吗」的「可以」是这个说法自己的，不算客气
                spans.append((a - len(mm.group(0)) if mm else a, b))
    if spans:
        chars = list(t)
        for a, b in spans:
            chars[a:b] = "〇" * (b - a)
        t = "".join(chars)
    return t, now, later, unsure


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
# v0.1.1：「群里这么安静」「有点安静啊」「终于安静了」「安安静静」「安静点好」也是描述
_DESC = "".join(
    f"(?<!{w})"
    for w in ("这么", "那么", "多么", "有点", "有些", "突然", "忽然", "终于", "总算", "变得", "比较", "非常", "特别", "格外",
              "十分", "这样", "那样", "如此", "一片", "一下子")
)
_QUIET = (
    r"(?<![很好太真挺多安])" + _DESC + r"安静(?![的地静得]|了吗|吗|(?:一?点儿?|些)?(?:比较|更|最|才|也|就|还是)?好(?![吗嘛吧么不了]))"
    r"|少说|少讲|话少|少点话|少发言|少冒泡|别冒泡|不要冒泡|"
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
# v0.1.1：@ / 引用着它说「你们别吵了，听它说」「大家安静一下，看它发的这个」：拿不准包不包括它 → 不直接改
# （同一条里还 @ 了别人的「你们俩闭嘴」照旧算；「大家都睡了你安静点」中间有「你」，照旧算）
PLURAL = re.compile(r"(?:你们|大家|各位|诸位|所有人|全体)[^，,。你]{0,4}?(?:" + _MUTE + "|" + _QUIET + ")", re.I)
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
    """这句里有没有让它安静 / 静音 / 恢复的说法 → "quiet" / "mute" / "restore" / "mixed" / "later" / None（不看是谁说的、冲谁说的）
    先认解除、把解除的说法挖掉，再在剩下的字里找安静 / 静音。mixed = 又有现在解除、又有让它安静；
    later = 只说了以后才恢复（「十分钟后恢复」）。这两种都不改状态"""
    masked, rs, later, unsure = _lift(text)
    mu, qu = MUTE.search(masked), QUIET.search(masked)
    if unsure or (rs and (mu or qu)):
        return "mixed"
    if any(_near_cancel(masked, m) for m in (mu, qu) if m):
        return "mixed"
    if rs:
        return "restore"
    if mu:
        return "mute"
    if qu:
        return "quiet"
    if later:
        return "later"
    return None


def recognize(text, admin=False, addressed=False, direct=False, at_other=False,
              restricted=False, recent=False, hushed=False, names=()):
    """这句话要不要改状态 → Verdict(kind, sure, why)

    text：这条消息的字（去掉了 @ 它那一段）
    admin：说话的人算不算管理员（调用的地方按 QQ 身份 + 配置算）
    addressed：冲着它说的（@ 它 / 引用它 / 叫它名字）；direct：@ 它 / 引用它（不只是叫名字）
    at_other：@ 了别人；restricted：这个群现在在安静 / 静音；recent：它刚回过这个人；hushed：这个人说过别接他的话
    names：它的名字（v0.1.1：先从话里拿掉，「小鱼恢复」的「恢复」才在主干上）
    kind：quiet / mute / restore / self_off / self_on / not_admin / None；sure=False = 拿不准"""
    t = str(text or "")
    for n in names or ():
        n = str(n or "").strip()
        if len(n) >= 2:
            t = re.sub(re.escape(n), " ", t, flags=re.I)
    t = t.strip()
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
    asked = bool(QUESTION.search(t)) and not POLITE.search(_lift(rest)[0])

    # 群管让它安静 / 静音 / 恢复（群管说「别理我」只管他自己，下面再看）
    if words and talking_to_it and admin:
        v = _admin_verdict(rest, words, restricted, asked, at_other)
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


def _admin_verdict(t, words, restricted, asked, at_other=False):
    if SELF_SUBJ.search(t):
        return Verdict(None, True, "说的是他自己")
    if OTHERS.search(t):
        return Verdict(None, True, "说的是别人")
    if REPORT.search(t):
        return Verdict(None, True, "转述别人的话")
    if words in ("quiet", "mute") and JOKE.search(t):
        return Verdict(None, True, "开玩笑的语气")
    if ASK_WHY.search(t):
        return Verdict(words if words not in ("mixed", "later") else None, False, "在问为什么（拿不准是不是命令）")
    if words == "mixed":
        return Verdict(None, False, "又像让它安静又像让它恢复（拿不准）")
    if words in ("quiet", "mute") and not at_other and PLURAL.search(t):
        return Verdict(words, False, "说的是「你们 / 大家」（拿不准包不包括它）")
    if words in ("restore", "later") and not restricted:
        return Verdict(None, True, "这个群本来就没在安静")
    if words == "later":
        # 「十分钟后恢复」「到点自己恢复」：没让它现在恢复 → 不改（也不问模型：模型只会答「恢复」，那就成了马上恢复）
        return Verdict(None, True, "说的是以后才恢复（现在不改）")
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
