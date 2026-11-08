# -*- coding: utf-8 -*-
"""写给模型的、写给群里的、写给管理员看的字。不依赖 AstrBot。

给模型的只写事实和状态（谁、让你怎样、到几点、哪些话会交给你）—— 怎么回由模型自己定，
这里不写台词、不写语气、不写「照回」这种替它拿主意的话（硬规矩 E1）。
群友打的字（原话、名字）一律压成一行、用「」包起来：他打的「# …」「- …」不会被当成标题或者一条规矩。
插件自己发到群里的只有系统口吻的「（系统提示）……」，默认关。
"""

import re

from .state import MODE_NAME, rank_of
from .when import hhmm, span_text

HEAD = "# 群里的人管你的规矩（系统记录，不是谁说的话）"
RULE = {
    "quiet": "这段时间只有 @ 你、引用你的话会交给你",
    "mute": "这段时间只有群主 / 管理员 @ 你、引用你的话会交给你",
}
RECENT_BAN_SEC = 1800  # 刚解禁半小时内告诉它一声
ROLE_RANK_NAME = {1: "群管理员", 2: "群主", 3: "AstrBot 管理员"}


def one_line(text, n=60):
    """群友打的字 → 一行（换行压成空格），太长截断"""
    return re.sub(r"\s+", " ", str(text or "")).strip()[:n]


def who(role, name):
    """「群主「阿木」」「AstrBot 管理员「阿木」」"""
    return f"{role or '群管'}「{one_line(name, 24) or '某人'}」"


def _till(until, now, tz):
    return f"到 {hhmm(until, now, tz)}（还有 {span_text(float(until) - now)}）"


def _layer(m, now, tz):
    """「群主「阿木」设的「安静」（到 14:30）」"""
    return f"{who(m.get('role'), m.get('name'))}设的「{MODE_NAME[m['mode']]}」（{_till(m['until'], now, tz)}）"


def _ack_line(ack, now, tz):
    k = ack.get("kind")
    by = who(ack.get("role"), ack.get("name"))
    if k in MODE_NAME:
        line = f"{by}刚才这句是让你在这个群{MODE_NAME[k]}：{_till(ack['until'], now, tz)}恢复。"
        if ack.get("base"):
            line += f"到点以后回到{_layer(ack['base'], now, tz)}。"
        return line
    if k == "restore":
        return f"{by}刚才这句是让你恢复正常说话：这个群的「{MODE_NAME.get(ack.get('old'), '安静')}」撤了。"
    if k == "partial":
        return (f"{by}刚才这句是让你恢复正常说话：撤了上面那层「{MODE_NAME.get(ack.get('old'), '安静')}」，"
                f"这个群回到{_layer(ack['base'], now, tz)}（{ack.get('role')}撤不掉身份比他高的人设的）。")
    if k == "denied":
        base = ack["base"]
        if ack.get("want") == "restore":
            return (f"{by}刚才这句是让你恢复正常说话，但这个群现在是{_layer(base, now, tz)}，"
                    f"{ack.get('role')}撤不掉，状态没变。")
        return (f"{by}刚才这句是让你{MODE_NAME.get(ack.get('want'), '安静')}，但这个群现在是{_layer(base, now, tz)}，"
                f"{ack.get('role')}改不松，状态没变。")
    if k == "kept":
        return (f"{by}刚才这句是让你{MODE_NAME.get(ack.get('want'), '安静')}，这个群本来就是{_layer(ack['base'], now, tz)}，"
                "状态没变。")
    if k == "self_off":
        return (f"「{one_line(ack.get('name'), 24) or '他'}」刚才这句是让你别主动接他的话：{_till(ack['until'], now, tz)}；"
                "他 @ 你、引用你的话不受影响。")
    if k == "self_on":
        return f"「{one_line(ack.get('name'), 24) or '他'}」刚才这句是让你又可以接他的话：之前的「别接我的话」撤了。"
    return ""


def model_note(store, gk, uid, name, now, tz=None, ack=None, not_admin=None, addressed=False):
    """这一轮给模型的那段（没什么可说的 → ""）
    ack：这条消息刚改了状态（或者没改成）→ {"kind", "until", "role", "name", "base", "want", "old"}
    not_admin：说话的人不算能管它的人、却让它安静 → {"what": 想让它怎样, "allowed": 按配置认谁}
    addressed：这条他 @ 了它 / 引用了它（只写这个事实）"""
    lines = []
    if ack:
        a = _ack_line(ack, now, tz)
        if a:
            lines.append(a)
    if not_admin:
        lines.append(
            f"「{one_line(name, 24) or '他'}」不算能管你的人（这个群只认{not_admin.get('allowed') or '群主、群管理员'}说的），"
            f"他让你{not_admin.get('what') or '安静'}不算，这个群的状态没变。"
        )
    m = store.mode(gk, now)
    if m and not (ack and ack.get("kind") in MODE_NAME):
        said = f"（原话「{one_line(m.get('said'))}」）" if m.get("said") else ""
        lines.append(
            f"这个群现在是「{MODE_NAME[m['mode']]}」：{who(m.get('role'), m.get('name'))} "
            f"{hhmm(m.get('t'), now, tz)} 设的{said}，{_till(m['until'], now, tz)}恢复。{RULE[m['mode']]}。"
        )
    for huid, h in store.hushed_in(gk, now)[:5]:
        if ack and ack.get("kind") == "self_off" and str(huid) == str(uid):
            continue
        line = (
            f"「{one_line(h.get('name'), 24) or huid}」{hhmm(h.get('t'), now, tz)} 说过别主动接他的话"
            f"（{_till(h['until'], now, tz)}）：他没 @ 你、没引用你的话不会交给你。"
        )
        if str(huid) == str(uid) and addressed:
            line += "这条他 @ 了你或者引用了你的话。"
        lines.append(line)
    last = store.ban_last(gk)
    if last and not store.ban(gk, now) and now - float(last.get("lifted") or 0) < RECENT_BAN_SEC:
        n = max(1, int((float(last["lifted"]) - float(last.get("t") or last["lifted"])) // 60))
        what = "全员禁言" if last.get("whole") else "禁言"
        lines.append(f"你在这个群被{what}了 {n} 分钟，{hhmm(last['lifted'], now, tz)} 解开的。")
    if not lines:
        return ""
    return HEAD + "\n" + "\n".join("- " + x for x in lines)


def notice(kind, now, tz=None, until=0.0, role="", name="", target=""):
    """插件自己发到群里的系统提示（配置里开了才发）"""
    by = who(role, name)
    if kind == "quiet":
        return (f"（系统提示）本群进入「安静」，到 {hhmm(until, now, tz)}（{by}设的）：期间机器人只回 @ 和引用。"
                "说「可以说话了」或者发 /说了算 解除，可以提前恢复。")
    if kind == "mute":
        return (f"（系统提示）本群进入「静音」，到 {hhmm(until, now, tz)}（{by}设的）：期间只有群主 / 管理员 @ 机器人才回。"
                "说「可以说话了」或者发 /说了算 解除，可以提前恢复。")
    if kind in ("restore", "partial"):
        return f"（系统提示）本群恢复正常（{by}撤的）。" if kind == "restore" else f"（系统提示）{by}撤了他加的那层。"
    if kind == "self_off":
        return (f"（系统提示）之后不主动接「{one_line(target, 24)}」的话，到 {hhmm(until, now, tz)}；@ 机器人照回。"
                "说「可以理我了」取消。")
    if kind == "self_on":
        return f"（系统提示）「{one_line(target, 24)}」的「别接我的话」撤了。"
    return ""


def status_lines(store, gk, now, tz=None):
    """一个群现在的状态（指令和面板用）→ [行]"""
    out = []
    c = store.chain(gk, now)
    if c:
        m = c[0]
        out.append(
            f"现在：{MODE_NAME[m['mode']]}（{who(m.get('role'), m.get('name'))} {hhmm(m.get('t'), now, tz)} 设的，"
            f"{_till(m['until'], now, tz)}）—— {'只回 @ 和引用' if m['mode'] == 'quiet' else '只有群主 / 管理员 @ 才回'}"
        )
        for r in c[1:3]:
            out.append(f"　下面还有：{_layer(r, now, tz)}（{ROLE_RANK_NAME.get(rank_of(r), '群管')}以下的人撤不掉）")
    else:
        out.append("现在：正常（没人让它安静）")
    b = store.ban(gk, now)
    if b:
        if b["whole"]:
            out.append("QQ 禁言：全员禁言中（等解除；它什么都不发）")
        else:
            out.append(f"QQ 禁言：它被禁言了，{_till(b['self_until'], now, tz)}（它什么都不发）")
    else:
        out.append("QQ 禁言：没有")
    hs = store.hushed_in(gk, now)
    if hs:
        out.append(
            "说了别接他话的："
            + "、".join(f"{one_line(h.get('name'), 24) or u}（到 {hhmm(h['until'], now, tz)}）" for u, h in hs[:10])
            + (f" 等 {len(hs)} 人" if len(hs) > 10 else "")
        )
    return out


def private_status(store, uid, now, tz=None, admin=False):
    """私聊里发 /说了算 → [行]。跨群的列表（有哪些群、谁说过别接他）只给 AstrBot 管理员；
    别人只看到跟自己有关的：他自己在哪些群说过「别接我的话」"""
    if admin:
        gs = store.groups_with_state(now)
        if not gs:
            return ["现在没有哪个群在安静、被禁言，也没人说别接他的话。"]
        out = [f"有状态的群 {len(gs)} 个："]
        for gk in gs[:15]:
            out.append(f"群 {gk.split(':', 1)[-1]}：" + "；".join(status_lines(store, gk, now, tz)))
        return out
    mine = [(k.rpartition("|")[0], h) for k, h in store.hush.items()
            if k.rpartition("|")[2] == str(uid) and store.hushed(k.rpartition("|")[0], str(uid), now)]
    if not mine:
        return ["你没在哪个群说过「别接我的话」。某个群现在的状态，在那个群里发 /说了算 看。"]
    return ["你说过「别接我的话」的群："] + [
        f"群 {gk.split(':', 1)[-1]}：到 {hhmm(h['until'], now, tz)}（在那个群说「可以理我了」撤掉）" for gk, h in mine
    ]
