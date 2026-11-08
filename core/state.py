# -*- coding: utf-8 -*-
"""各个群现在的状态：安静 / 静音（群管说的）、QQ 禁言（QQ 那边的事实）、群友的「别接我的话」。不依赖 AstrBot。

都带到点时间：读的时候过了点就当没有（不靠后台准时清，重启、卡了一会儿都不会多关它）；后台定时清掉过期的、记一笔。
整个存成一个 dict，main.py 负责写进 plugin_data。
"""

import time
from collections import deque

WHOLE_BAN_MAX = 24 * 3600  # 全员禁言没有时长：最多当它禁 24 小时（解禁通知漏了也不会一直不说话）
UNKNOWN_BAN_SEC = 600  # 禁言通知没带时长、也没查到到几点：先当它禁 10 分钟，到时候再查（不按 30 天算）
SYNC_DRIFT = 90  # 查到的禁言到点时间跟记的差这么多秒以上才改
LOG_MAX = 60
MODE_NAME = {"quiet": "安静", "mute": "静音"}
STRICT = {"quiet": 1, "mute": 2}  # 静音比安静严
RANK = {"群管理员": 1, "群主": 2, "AstrBot 管理员": 3}  # 身份高低：群主设的，群管理员撤不掉


def rank_of(rec):
    """这一层是什么身份的人设的（老存档没记 rank：按身份名算）"""
    try:
        r = int(rec.get("rank") or 0)
    except (TypeError, ValueError):
        r = 0
    return r or RANK.get(str(rec.get("role") or ""), 1)


def _num(v, d=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return d


class GroupStore:
    def __init__(self, data=None, clock=time.time):
        d = data if isinstance(data, dict) else {}
        self.clock = clock
        self.groups = d.get("groups") if isinstance(d.get("groups"), dict) else {}
        self.hush = d.get("hush") if isinstance(d.get("hush"), dict) else {}
        self.log = deque(
            [x for x in (d.get("log") or []) if isinstance(x, dict)], maxlen=LOG_MAX
        )
        self.dirty = False

    def dump(self):
        return {"groups": self.groups, "hush": self.hush, "log": list(self.log)}

    def _g(self, gk):
        g = self.groups.get(gk)
        if not isinstance(g, dict):
            g = self.groups[gk] = {}
        return g

    def note(self, text, gk="", now=None):
        self.log.append({"t": int(now if now is not None else self.clock()), "g": str(gk), "text": str(text)[:200]})
        self.dirty = True

    # ───────── 安静 / 静音（群管说的；分高低）─────────
    # 一个群的安静 / 静音是一摞：最上面是后加的。身份低的人（群管理员）只能在身份高的人（群主 / AstrBot 管理员）
    # 设的那层上面往严了加；他撤 / 到点了 → 回到下面那层。身份够的人设的会把他能管的那几层整个换掉。
    def chain(self, gk, now=None):
        """这个群还在生效的那几层（从上往下）"""
        now = self.clock() if now is None else now
        out, m, n = [], (self.groups.get(gk) or {}).get("mode"), 0
        while isinstance(m, dict) and n < 10:
            if m.get("mode") in MODE_NAME and _num(m.get("until")) > now:
                out.append(m)
            m, n = m.get("prev"), n + 1
        return out

    def mode(self, gk, now=None):
        """这个群现在的安静 / 静音（最上面还在生效的那层；都过了点 = None）"""
        c = self.chain(gk, now)
        return c[0] if c else None

    def put_mode(self, gk, mode, until, rank, by="", name="", role="", said="", now=None):
        """群管让它安静 / 静音 → {"how": set（换掉）/ stack（加在上面）/ kept（下面那层本来就更严或一样、管得更久）/
        denied（想改松，改不了）, "rec": 新的那层, "base": 比他身份高的那层}"""
        now = self.clock() if now is None else now
        base = next((r for r in self.chain(gk, now) if rank_of(r) > rank), None)
        rec = {
            "mode": mode, "until": float(until), "rank": int(rank), "by": str(by), "name": str(name)[:24],
            "role": str(role), "said": str(said)[:60], "t": float(now),
        }
        if base is None:
            self._g(gk)["mode"] = rec
            self.dirty = True
            return {"how": "set", "rec": rec, "base": None}
        if STRICT[mode] < STRICT[base["mode"]]:
            return {"how": "denied", "rec": None, "base": base}
        if STRICT[mode] == STRICT[base["mode"]] and float(until) <= _num(base.get("until")):
            return {"how": "kept", "rec": None, "base": base}
        rec["prev"] = base
        self._g(gk)["mode"] = rec
        self.dirty = True
        return {"how": "stack", "rec": rec, "base": base}

    def clear_mode(self, gk, rank=99, now=None):
        """撤掉他能撤的那几层 → {"how": none（本来就没有）/ denied（最上面那层身份比他高）/ cleared（全撤了）/
        partial（撤了上面的，回到身份比他高的那层）, "old": 原来最上面那层, "now": 撤完以后生效的那层}"""
        now = self.clock() if now is None else now
        c = self.chain(gk, now)
        if not c:
            g = self.groups.get(gk)
            if isinstance(g, dict) and g.pop("mode", None) is not None:
                self.dirty = True
            return {"how": "none", "old": None, "now": None}
        if rank_of(c[0]) > rank:
            return {"how": "denied", "old": c[0], "now": c[0]}
        rest = next((r for r in c if rank_of(r) > rank), None)
        g = self._g(gk)
        if rest is None:
            g.pop("mode", None)
        else:
            g["mode"] = rest
        self.dirty = True
        return {"how": "partial" if rest else "cleared", "old": c[0], "now": rest}

    # ───────── QQ 禁言（QQ 那边的事实）─────────
    def ban(self, gk, now=None):
        """这个群它现在被禁言着吗 → {"self_until", "all", ...}（没有 = None）
        它自己被禁言（有时长）和全员禁言（没有时长、等解除）分开记：解了全员禁言，它自己的禁言还在"""
        now = self.clock() if now is None else now
        b = (self.groups.get(gk) or {}).get("ban")
        if not isinstance(b, dict):
            return None
        mine = _num(b.get("self_until")) > now
        whole = bool(b.get("all")) and now - _num(b.get("all_t")) < WHOLE_BAN_MAX
        if not (mine or whole):
            return None
        return dict(b, mine=mine, whole=whole)

    def ban_until(self, gk, now=None):
        """禁言到几点（全员禁言 = -1，不知道几点）；没禁言 = 0"""
        b = self.ban(gk, now)
        if not b:
            return 0.0
        if b["whole"]:
            return -1.0
        return _num(b.get("self_until"))

    def set_ban(self, gk, whole, until=0.0, by="", now=None):
        now = self.clock() if now is None else now
        g = self._g(gk)
        b = g.get("ban") if isinstance(g.get("ban"), dict) else {}
        if whole:
            b["all"], b["all_t"] = True, float(now)
        else:
            b["self_until"] = float(until)
        b["by"], b["t"] = str(by or ""), float(now)
        g["ban"] = b
        self.dirty = True
        return b

    def lift_ban(self, gk, whole=None, now=None):
        """解禁：whole=True 只解全员 / False 只解它自己 / None 都解 → 解之前是不是禁着的"""
        now = self.clock() if now is None else now
        was = self.ban(gk, now)
        g = self.groups.get(gk)
        if not isinstance(g, dict) or not isinstance(g.get("ban"), dict):
            return False
        b = g["ban"]
        if whole in (True, None):
            b["all"] = False
        if whole in (False, None):
            b["self_until"] = 0.0
        if not self.ban(gk, now):
            g.pop("ban", None)
            if was:
                g["ban_last"] = {"t": _num(was.get("t")), "lifted": float(now), "whole": bool(was.get("whole"))}
        self.dirty = True
        return bool(was)

    def sync_ban(self, gk, shut_until, whole_shut=None, my_role="", now=None):
        """拿 QQ 查到的禁言状态对一下（漏收了禁言 / 解禁通知也能对上）→ 改了什么 [说明]
        shut_until：它自己被禁言到几点（get_group_member_info 的 shut_up_timestamp；0 = 没被禁言；None = 没查到）
        whole_shut：这个群是不是在全员禁言（None = 没查到，不动）；它是群主 / 管理时全员禁言管不到它"""
        now = self.clock() if now is None else now
        out = []
        cur = self.ban(gk, now) or {}
        if shut_until is not None:
            shut = _num(shut_until)
            if shut > now:
                if not cur.get("mine") or abs(_num(cur.get("self_until")) - shut) > SYNC_DRIFT:
                    self.set_ban(gk, False, shut, "", now)
                    out.append(("self_on", shut))
            elif cur.get("mine"):
                self.lift_ban(gk, whole=False, now=now)
                out.append(("self_off", 0))
        if whole_shut is not None:
            whole = bool(whole_shut) and str(my_role or "") not in ("owner", "admin")
            if whole and not cur.get("whole"):
                self.set_ban(gk, True, 0, "", now)
                out.append(("all_on", 0))
            elif not whole and cur.get("whole"):
                self.lift_ban(gk, whole=True, now=now)
                out.append(("all_off", 0))
        return out

    def banned_groups(self, now=None):
        """记着在禁言的群（定时查这几个对不对）"""
        now = self.clock() if now is None else now
        return [gk for gk in list(self.groups) if self.ban(gk, now)]

    def ban_last(self, gk):
        b = (self.groups.get(gk) or {}).get("ban_last")
        return b if isinstance(b, dict) else None

    # ───────── 群友的「别接我的话」─────────
    @staticmethod
    def _hk(gk, uid):
        return f"{gk}|{uid}"

    def hushed(self, gk, uid, now=None):
        now = self.clock() if now is None else now
        h = self.hush.get(self._hk(gk, uid))
        if isinstance(h, dict) and _num(h.get("until")) > now:
            return h
        return None

    def set_hush(self, gk, uid, until, name="", said="", now=None):
        now = self.clock() if now is None else now
        rec = {"until": float(until), "name": str(name)[:24], "said": str(said)[:60], "t": float(now)}
        self.hush[self._hk(gk, uid)] = rec
        self.dirty = True
        return rec

    def clear_hush(self, gk, uid):
        old = self.hush.pop(self._hk(gk, uid), None)
        if old is not None:
            self.dirty = True
        return old

    def hushed_in(self, gk, now=None):
        """这个群里现在说过「别接我的话」的人 → [(QQ, 记录)]（先说的在前）"""
        now = self.clock() if now is None else now
        out = []
        for k, h in self.hush.items():
            g, _, uid = k.rpartition("|")
            if g == gk and isinstance(h, dict) and _num(h.get("until")) > now:
                out.append((uid, h))
        out.sort(key=lambda x: _num(x[1].get("t")))
        return out

    # ───────── 到点清掉 ─────────
    def expire(self, now=None):
        """清掉过了点的 → [(哪种, 群, 详情)]，给日志和面板用"""
        now = self.clock() if now is None else now
        out = []
        for gk, g in list(self.groups.items()):
            if not isinstance(g, dict):
                self.groups.pop(gk, None)
                continue
            m = g.get("mode")
            if isinstance(m, dict) and _num(m.get("until")) <= now:
                rest = self.chain(gk, now)
                if rest:  # 上面那层到点了：回到下面身份高的人设的那层
                    g["mode"] = rest[0]
                    out.append(("mode_back", gk, dict(m, back=rest[0])))
                else:
                    g.pop("mode", None)
                    out.append(("mode", gk, m))
            b = g.get("ban")
            if isinstance(b, dict) and not self.ban(gk, now):
                g.pop("ban", None)
                g["ban_last"] = {"t": _num(b.get("t")), "lifted": float(now), "whole": bool(b.get("all"))}
                out.append(("ban", gk, b))
            bl = g.get("ban_last")
            if isinstance(bl, dict) and now - _num(bl.get("lifted")) > 86400:
                g.pop("ban_last", None)  # 一天前解的禁言不用再提
            if not any(k in g for k in ("mode", "ban", "ban_last")):
                self.groups.pop(gk, None)
        for k, h in list(self.hush.items()):
            if not isinstance(h, dict) or _num(h.get("until")) <= now:
                self.hush.pop(k, None)
                gk, _, uid = k.rpartition("|")
                out.append(("hush", gk, dict(h if isinstance(h, dict) else {}, uid=uid)))
        if out:
            self.dirty = True
        return out

    def groups_with_state(self, now=None):
        """有状态的群（安静 / 静音 / 禁言 / 有人说了别接他）→ [群]"""
        now = self.clock() if now is None else now
        gs = {gk for gk in self.groups if self.mode(gk, now) or self.ban(gk, now)}
        gs |= {k.rpartition("|")[0] for k, h in self.hush.items() if isinstance(h, dict) and _num(h.get("until")) > now}
        return sorted(gs)
