# -*- coding: utf-8 -*-
"""规则拿不准的时候问一次模型（配置「规则拿不准时问一次模型」开着才问，默认关）。不依赖 AstrBot。

模型只回答「这句是不是冲它说的、让它怎样、说了多久」；改不改、谁有权改，还是代码按 QQ 身份定（不靠模型说了算）。
"""

import json
import re

JUDGE_SYSTEM = """你在帮 QQ 群里的一个聊天机器人看一条消息：这条是不是在要求这个机器人改变说话的多少（安静 / 静音 / 恢复），或者是不是在让它别理（或重新理）说话的这个人本人。
只输出一行 JSON：{"target": "me|others|none", "action": "quiet|mute|restore|self_off|self_on|none", "minutes": 数字或null, "confidence": 0到100}

target：
- me：明确是冲着这个机器人说的（@了它、叫了它的名字、引用了它的话）
- others：冲着别人或者别的机器人说的
- none：不是在要求什么（闲聊、提问、开玩笑、转述别人的话、讨论怎么设置）
action：
- quiet：安静点、少说两句、别插嘴（有人 @ 它还能回）
- mute：闭嘴、别说话、一句都别说
- restore：可以说话了、恢复、解除
- self_off：只是让它别理 / 别接 / 别 @ 说话的这个人自己
- self_on：让它重新理说话的这个人
- none：都不是
minutes：话里说了多久才填（「一小时」填 60），没说填 null；「以后都别说了」填 -1。
confidence：拿不准就写低于 60。反问、开玩笑、转述别人的话都算 none。"""

ACTIONS = ("quiet", "mute", "restore", "self_off", "self_on", "none")
MIN_CONF = 70


def judge_body(name, role, text, flags):
    """给模型看的那条（只有事实：谁说的、什么身份、@ 了谁、原话）"""
    marks = []
    if flags.get("at_me"):
        marks.append("这条 @ 了这个机器人")
    if flags.get("quoted_me"):
        marks.append("这条引用了这个机器人的话")
    if flags.get("called"):
        marks.append("这条叫了这个机器人的名字")
    if flags.get("at_other"):
        marks.append("这条还 @ 了：" + "、".join(str(x) for x in flags["at_other"][:4]))
    return (
        f"说话的人：{name or '某人'}（{role or '群友'}）\n"
        + ("；".join(marks) + "\n" if marks else "")
        + f"要判断的这一条：{str(text or '')[:200]}"
    )


def parse_judge(out):
    """模型回的 → {"target", "action", "minutes", "confidence"}；看不懂 → None"""
    m = re.search(r"\{.*\}", out or "", re.S)
    if not m:
        return None
    raw = m.group(0)
    try:
        j = json.loads(raw)
    except Exception:  # noqa: BLE001  模型回的 JSON 常常不规整：退回一项一项抠
        j = {}
        for k in ("target", "action"):
            mm = re.search(r'"%s"\s*:\s*"([^"]*)"' % k, raw)
            if mm:
                j[k] = mm.group(1)
        for k in ("minutes", "confidence"):
            mm = re.search(r'"%s"\s*:\s*"?(-?\d+(?:\.\d+)?)' % k, raw)
            if mm:
                j[k] = float(mm.group(1))
    if not isinstance(j, dict):
        return None
    tgt = str(j.get("target") or "none")
    act = str(j.get("action") or "none")
    try:
        mins = j.get("minutes")
        mins = None if mins in (None, "", "null") else float(mins)
    except (TypeError, ValueError):
        mins = None
    try:
        conf = int(float(j.get("confidence") or 0))
    except (TypeError, ValueError):
        conf = 0
    return {
        "target": tgt if tgt in ("me", "others", "none") else "none",
        "action": act if act in ACTIONS else "none",
        "minutes": mins,
        "confidence": max(0, min(100, conf)),
    }


def judge_kind(res, admin=False, restricted=False, hushed=False, min_conf=MIN_CONF):
    """模型的判断 → 要改成什么（kind，同 rules.recognize）；权限在这里按身份卡，不听模型的"""
    if not res or res.get("target") != "me" or int(res.get("confidence") or 0) < min_conf:
        return None
    act = res.get("action")
    if act in ("quiet", "mute"):
        return act if admin else "not_admin"
    if act == "restore":
        if not restricted:
            return None
        return "restore" if admin else "not_admin"
    if act == "self_off":
        return "self_off"
    if act == "self_on":
        return "self_on" if hushed else None
    return None


def judge_span(res):
    """模型说的时长 → 秒（没说 = None；「以后都」= -1）"""
    mins = (res or {}).get("minutes")
    if mins is None:
        return None
    if mins < 0:
        return -1
    return float(mins) * 60 if mins > 0 else None
