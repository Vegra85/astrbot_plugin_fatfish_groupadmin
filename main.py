# -*- coding: utf-8 -*-
"""
鱼力max · 群管说了算（AstrBot 插件）

市场上的群管插件都是机器人去管群；这个反过来：群里的人管机器人。
  - 群主 / 管理员（QQ 身份，或者 AstrBot 管理员）@ 它或者叫它名字说「安静点 / 少说点」→ 这个群进「安静」（只回 @ 和引用它的）；
    「别说话 / 闭嘴」→「静音」（只有群主 / 管理员 @ 它才回）。时长按他说的（「一小时」「到晚上」），没说用默认，到点自己恢复；
    「可以说话了 / 恢复」提前恢复。它怎么回这一句由模型自己说（给它状态：谁、让你怎样、到几点）。
  - 群友说「别理我 / 别接我的话 / 别 @ 我」→ 只对他：他没 @ 它的话不主动接；「可以理我了」取消。
  - 它自己被 QQ 禁言（或者全员禁言、它不是管理）→ 这个群什么都不发，解禁通知一来马上恢复；
    通知漏了也对得上：开机 / NapCat 重新连上时查一遍所有群、每 10 分钟查一遍记着在禁言的群（自己的 shut_up_timestamp、全员禁言）。
  - 分高低：群主设的安静 / 静音，群管理员撤不掉、改不松（群主自己、AstrBot 管理员可以）；往严了改谁都可以，到点 / 撤了回到群主那层。

钩子（顺序写死在这里）：
  群消息（max-2）          认话（规则先认；拿不准的配置开了才问模型）→ 改状态；QQ 禁言 / 解禁通知 → 记下来
  要叫模型之前（2000000）   按状态拦（拦之前替天使之心收口）—— 排在「认 AI」(max) 之后、「活人感」(1000000) 之前，活人感不用白攒
  回话请求（35）            这一轮给模型的临时段：这个群现在怎样、谁说了别接他（只写事实，不进对话记录）
  发送前（max-3）           被禁言的群：什么都不发（指令回复、别的插件的话也拦）
  发完（10）                记下它刚回了谁（「别接我的话」不 @ 也算冲它说的）
判断逻辑在 core/（不依赖 AstrBot，可以单独测）。
"""

import asyncio
import importlib.util
import json
import os
import sys
import time
from pathlib import Path

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, MessageChain, MessageEventResult, filter
from astrbot.api.provider import ProviderRequest
from astrbot.api.star import Context, Star, StarTools
import astrbot.api.message_components as Comp

try:  # 这一轮临时加给模型的段落（不进对话记录）；老版本没有 → 不加
    from astrbot.core.agent.message import TextPart
except Exception:  # pragma: no cover
    TextPart = None

from .core.angel import release_angel
from .core.conf import num, str_list, truthy
from .core.gate import decide
from .core.judge import JUDGE_SYSTEM, judge_body, judge_kind, judge_span, parse_judge
from .core.notes import model_note, notice, private_status, status_lines
from .core.rules import Verdict, names_called, recognize
from .core.state import MODE_NAME, RANK, UNKNOWN_BAN_SEC, GroupStore
from .core.when import clamp_span, hhmm, parse_span, resolve_tz, span_text

PLUGIN_NAME = "astrbot_plugin_fatfish_groupadmin"
VERSION = "0.1.1"
TAG = "[群管说了算]"
AIGUARD = "astrbot_plugin_fatfish_aiguard"
WAIT_PRIORITY = 2000000
RECENT_SEC = 180  # 它回了某人之后这么久里，那人说「别接我的话」不 @ 也算冲它说的
HUSH_MAX_SEC = 7 * 86400
JUDGE_PER_HOUR = 10  # 每个群每小时最多问几次模型（防有人刷）
EXTRA_ACK = "fatfish_groupadmin_ack"
EXTRA_NOT_ADMIN = "fatfish_groupadmin_not_admin"
ROLE_NAME = {"owner": "群主", "admin": "群管理员"}
SYNC_SEC = 600  # 每 10 分钟查一遍记着在禁言的群（漏了解禁通知也能对上）
FULL_SYNC_SEC = 6 * 3600  # 每 6 小时（和开机、NapCat 重新连上时）查一遍所有群
SYNC_GROUPS_MAX = 300


class FatfishGroupAdmin(Star):
    """群里的人管机器人：/说了算 看这个群现在的状态；群主 / 管理员 /说了算 解除"""

    def __init__(self, context: Context, config: AstrBotConfig = None):
        super().__init__(context)
        self.config = config if config is not None else {}
        try:
            self.data_dir = Path(StarTools.get_data_dir(PLUGIN_NAME))
        except Exception:
            self.data_dir = Path(os.getcwd()) / "data" / "plugin_data" / PLUGIN_NAME
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = self.data_dir / "state.json"
        self.store = GroupStore(self._load())
        self._recent = {}  # (群, QQ) -> 它上次回他的时间
        self._cards = {}  # 群 -> (查的时间, 它在这个群的名片, 它在这个群的身份)
        self._nick = ["", 0.0]  # 它的 QQ 昵称、查的时间
        self._judged = {}  # (群, QQ, 原话) -> 时间：同一句 10 分钟内不再问
        self._judge_rate = {}  # 群 -> [问模型的时间]
        self._said = {}  # (群, 为什么) -> 上次记日志的时间（拦下的日志别刷屏）
        self._tasks = set()
        self._loop_task = None
        self._sync_task = None
        self._full_sync_at = 0.0  # 上次查所有群的时间
        self._full_sync_busy = False
        self._subs = {}  # id(CQHttp) -> (CQHttp, 处理函数)：收 NapCat「重新连上了」
        self._closed = False
        self._tz_cache = (None, None)
        self._register_web()

    async def initialize(self):
        self._ensure_loop()
        try:
            self._subscribe()  # 热重载时平台已经在了；开机时平台还没建好，等 on_platform_loaded
        except Exception as e:
            logger.warning(f"{TAG} 挂 NapCat 元事件出错：{e!r}"[:200])
        logger.info(
            f"{TAG} v{VERSION} 已启用{'' if self._on() else '（总开关关着：什么都不做）'}："
            f"默认安静 {int(self._c('quiet_default_min', 30, 1, 1440))} 分钟 · "
            f"群友「别接我的话」{self._c('hush_hours', 24, 0.1, 168):g} 小时 · 认谁：{self._allowed_text()}"
        )

    async def terminate(self):
        self._closed = True
        for bot, h in list(self._subs.values()):
            try:
                bot.unsubscribe("meta_event", h)
            except Exception:
                pass
        self._subs.clear()
        for t in list(self._tasks):
            t.cancel()
        for t in (self._loop_task, self._sync_task):
            if t:
                t.cancel()
        self._save(force=True)

    # ───────── 配置 / 存档 ─────────
    def _on(self):
        return truthy(self.config.get("enabled"), True)

    def _c(self, key, default, lo=None, hi=None):
        return num(self.config.get(key, default), default, lo, hi)

    def _allowed(self):
        out = []
        if truthy(self.config.get("owner_counts"), True):
            out.append("owner")
        if truthy(self.config.get("admin_counts"), True):
            out.append("admin")
        if truthy(self.config.get("astrbot_admin_counts"), True):
            out.append("astrbot")
        return out

    def _allowed_text(self):
        names = {"owner": "群主", "admin": "群管理员", "astrbot": "AstrBot 管理员"}
        return "、".join(names[x] for x in self._allowed()) or "没人（配置里都关了）"

    def _tz(self):
        """AstrBot 配置里的时区（「到晚上 8 点」按它算）；拿不到 = 北京时间（不退回服务器的时区）"""
        try:
            name = str(self.context.get_config().get("timezone") or "").strip()
        except Exception:
            name = ""
        if self._tz_cache[0] != name or self._tz_cache[1] is None:
            self._tz_cache = (name, resolve_tz(name))
        return self._tz_cache[1]

    def _load(self):
        try:
            d = json.loads(self.state_file.read_text(encoding="utf-8"))
            return d if isinstance(d, dict) else {}
        except Exception:
            return {}

    def _save(self, force=False):
        if not (self.store.dirty or force):
            return
        try:
            tmp = self.state_file.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.store.dump(), ensure_ascii=False), encoding="utf-8")
            tmp.replace(self.state_file)
            self.store.dirty = False
        except Exception as e:
            logger.warning(f"{TAG} 存档失败：{e!r}"[:200])

    def _ensure_loop(self):
        if self._closed:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        if self._loop_task is None or self._loop_task.done():
            self._loop_task = loop.create_task(self._tick_loop())
        if self._sync_task is None or self._sync_task.done():
            self._sync_task = loop.create_task(self._sync_loop())

    async def _tick_loop(self):
        """到点的安静 / 禁言 / 别接我的话：清掉、记一笔（读的时候本来就按到点时间算，这里只是让日志和面板跟上）"""
        while not self._closed:
            try:
                await asyncio.sleep(20)
                now = time.time()
                for kind, gk, d in self.store.expire(now):
                    gid = gk.split(":", 1)[-1]
                    if kind == "mode":
                        text = f"群 {gid} 的「{MODE_NAME.get(d.get('mode'), '安静')}」到点了，恢复正常"
                    elif kind == "mode_back":
                        bk = d.get("back") or {}
                        text = (f"群 {gid} {d.get('role')}加的「{MODE_NAME.get(d.get('mode'), '安静')}」到点了，"
                                f"回到{bk.get('role')}「{bk.get('name')}」设的「{MODE_NAME.get(bk.get('mode'), '安静')}」")
                    elif kind == "ban":
                        text = f"群 {gid} 的禁言到点了（没等到解禁通知），恢复说话"
                    else:
                        text = f"群 {gid}「{d.get('name') or d.get('uid')}」的「别接我的话」到点了"
                    self.store.note(text, gk, now)
                    logger.info(f"{TAG} {text}")
                self._save()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{TAG} 后台出错：{e!r}"[:200])

    def _spawn(self, coro):
        try:
            t = asyncio.get_running_loop().create_task(coro)
        except RuntimeError:
            coro.close()
            return
        self._tasks.add(t)
        t.add_done_callback(self._tasks.discard)

    # ───────── 读消息 ─────────
    @staticmethod
    def _gk(event):
        return f"{event.get_platform_id()}:{event.get_group_id()}"

    @staticmethod
    def _raw(event):
        raw = getattr(getattr(event, "message_obj", None), "raw_message", None)
        return raw if hasattr(raw, "get") else None

    def _role(self, event):
        """QQ 给的身份：owner / admin / member（NapCat 每条群消息都带 sender.role）"""
        raw = self._raw(event)
        try:
            return str(((raw or {}).get("sender") or {}).get("role") or "member")
        except Exception:
            return "member"

    def _peer(self, name):
        """装了、开着的别的鱼力max 插件（用它的公开方法）；没装 / 没开 → None"""
        try:
            md = self.context.get_registered_star(name)
        except Exception:
            return None
        if md is None or not getattr(md, "activated", True):
            return None
        return getattr(md, "star_cls", None)

    def _is_ai(self, uid):
        """认 AI 认出来的别人家的 AI：就算是群管也不听它的"""
        fn = getattr(self._peer(AIGUARD), "is_bot", None)
        if callable(fn):
            try:
                return bool(fn(str(uid)))
            except Exception:
                return False
        return False

    def _rank(self, event):
        """→ (算不算能管它的人, 身份名, 高低)。只看 QQ 给的身份和 AstrBot 管理员名单；嘴上说「我是管理」不算。
        又是群主又是 AstrBot 管理员的，按高的算（AstrBot 管理员 3 > 群主 2 > 群管理员 1）"""
        role = self._role(event)
        label = ROLE_NAME.get(role, "群友")
        try:
            astr_admin = bool(event.is_admin())
        except Exception:
            astr_admin = False
        if self._is_ai(event.get_sender_id()):
            return False, "别人家的 AI", 0
        allowed = self._allowed()
        best = None
        if role in allowed:
            best = label
        if astr_admin and "astrbot" in allowed:
            best = "AstrBot 管理员"
        if best:
            return True, best, RANK[best]
        return False, label, 0

    def _flags(self, event):
        """这条消息：字（去掉 @ 它那一段）、@ 了它 / 引用了它 / @ 了别人 / 用了唤醒词"""
        self_id = str(event.get_self_id() or "")
        parts, at_me, at_other, quoted_me = [], False, [], False
        for c in event.get_messages() or []:
            try:
                if isinstance(c, Comp.Plain):
                    parts.append(c.text or "")
                elif isinstance(c, Comp.At):
                    q = str(getattr(c, "qq", "") or "")
                    if q and q == self_id:
                        at_me = True
                    elif q and q != "all":
                        nm = str(getattr(c, "name", "") or "") or q
                        at_other.append(nm)
                        parts.append(f"@{nm} ")
                elif isinstance(c, Comp.Reply):
                    if str(getattr(c, "sender_id", "") or "") == self_id and self_id:
                        quoted_me = True
                elif isinstance(c, Comp.Image):
                    parts.append("[图片]")
            except Exception:
                continue
        wake = False
        try:
            orig = str(getattr(event.message_obj, "message_str", "") or "").strip()
            prefixes = self.context.get_config(umo=event.unified_msg_origin).get("wake_prefix") or []
            wake = any(p and orig.startswith(p) for p in prefixes)
        except Exception:
            wake = False
        return {
            "text": "".join(parts).strip(),
            "at_me": at_me,
            "quoted_me": quoted_me,
            "at_other": at_other,
            "wake": wake,
        }

    def _names(self, event, gk):
        """它的名字：配置里填的 + QQ 昵称 + 在这个群的名片（后两样后台查，查到之前先用配置里的）"""
        names = str_list(self.config.get("names"))
        now = time.time()
        if self._nick[0]:
            names.append(self._nick[0])
        c = self._cards.get(gk)
        if c and c[1]:
            names.append(c[1])
        if now - self._nick[1] > 3600 or not c or now - c[0] > 3600:
            self._spawn(self._fetch_names(event, gk))
        return names

    async def _fetch_names(self, event, gk):
        bot = getattr(event, "bot", None)
        if bot is None:
            return
        now = time.time()
        try:
            if now - self._nick[1] > 3600:
                self._nick[1] = now
                d = await asyncio.wait_for(bot.call_action("get_login_info"), 5)
                self._nick[0] = str((d or {}).get("nickname") or "")
        except Exception:
            pass
        try:
            c = self._cards.get(gk)
            if not c or now - c[0] > 3600:
                self._cards[gk] = (now, c[1] if c else "", c[2] if c else "")
                d = await asyncio.wait_for(
                    bot.call_action(
                        "get_group_member_info",
                        group_id=int(event.get_group_id()),
                        user_id=int(event.get_self_id()),
                        no_cache=False,
                    ),
                    5,
                )
                d = d or {}
                self._cards[gk] = (now, str(d.get("card") or ""), str(d.get("role") or ""))
        except Exception:
            pass

    async def _my_role(self, event, gid):
        """它自己在这个群的身份（全员禁言管不到群主 / 管理）；查不到 = member"""
        gk = f"{event.get_platform_id()}:{gid}"
        c = self._cards.get(gk)
        if c and c[2] and time.time() - c[0] < 3600:
            return c[2]
        bot = getattr(event, "bot", None)
        if bot is None:
            return "member"
        try:
            d = await asyncio.wait_for(
                bot.call_action(
                    "get_group_member_info",
                    group_id=int(gid),
                    user_id=int(event.get_self_id()),
                    no_cache=True,
                ),
                5,
            )
            d = d or {}
            self._cards[gk] = (time.time(), str(d.get("card") or ""), str(d.get("role") or ""))
            return str(d.get("role") or "member")
        except Exception:
            return "member"

    # ───────── QQ 禁言：拿 QQ 查到的对一下（漏了禁言 / 解禁通知也能对上）─────────
    def _platforms(self):
        out = []
        try:
            insts = list(getattr(getattr(self.context, "platform_manager", None), "platform_insts", None) or [])
        except Exception:
            insts = []
        for p in insts:
            try:
                if p.meta().name == "aiocqhttp":
                    out.append(p)
            except Exception:
                continue
        return out

    @staticmethod
    def _bot(plat):
        bot = getattr(plat, "bot", None)
        if bot is None:
            try:
                bot = plat.get_client()
            except Exception:
                bot = None
        return bot

    def _subscribe(self):
        """挂到 aiocqhttp 上收「NapCat 重新连上了」（AstrBot 的适配器自己不转这个）：连上了就查一遍所有群的禁言"""
        plats = self._platforms()
        live = {id(self._bot(p)) for p in plats}
        for k in [k for k in self._subs if k not in live]:
            bot, h = self._subs.pop(k)
            try:
                bot.unsubscribe("meta_event", h)
            except Exception:
                pass
        for plat in plats:
            bot = self._bot(plat)
            if bot is None or id(bot) in self._subs or not hasattr(bot, "subscribe"):
                continue

            async def handler(ev):
                try:
                    if ev.get("meta_event_type") == "lifecycle" and ev.get("sub_type") == "connect":
                        self._spawn(self._sync_all(delay=5, why="NapCat 连上了"))
                except Exception as e:
                    logger.warning(f"{TAG} 处理 NapCat 元事件出错：{e!r}"[:200])

            try:
                bot.subscribe("meta_event", handler)
                self._subs[id(bot)] = (bot, handler)
            except Exception as e:
                logger.warning(f"{TAG} 挂不上 NapCat 的连上事件（只靠定时查禁言）：{e!r}"[:200])

    @filter.on_platform_loaded()
    async def on_platform_loaded(self):
        try:
            self._subscribe()
        except Exception as e:
            logger.warning(f"{TAG} 挂 NapCat 元事件出错：{e!r}"[:200])

    async def _call(self, bot, action, timeout=8, **params):
        return await asyncio.wait_for(bot.call_action(action, **params), timeout)

    async def _query_shut(self, bot, gid, self_id):
        """它自己在这个群被禁言到几点（QQ 的 shut_up_timestamp；0 = 没被禁言）；查不到 = None"""
        if bot is None or not str(gid).isdigit() or not str(self_id).isdigit():
            return None
        try:
            d = await self._call(bot, "get_group_member_info", group_id=int(gid), user_id=int(self_id), no_cache=True)
        except Exception:
            return None
        if not isinstance(d, dict) or "shut_up_timestamp" not in d:
            return None
        try:
            return float(d.get("shut_up_timestamp") or 0)
        except (TypeError, ValueError):
            return None

    async def _sync_group(self, bot, pid, gid, self_id, ginfo=None):
        """对一个群：它自己的禁言（shut_up_timestamp）+ 全员禁言（group_all_shut，它是群主 / 管理时不算）"""
        gk, now = f"{pid}:{gid}", time.time()
        try:
            md = await self._call(bot, "get_group_member_info", group_id=int(gid), user_id=int(self_id), no_cache=True)
        except Exception:
            md = None
        md = md if isinstance(md, dict) else {}
        if ginfo is None:
            try:
                ginfo = await self._call(bot, "get_group_info", group_id=int(gid), no_cache=True)
            except Exception:
                ginfo = None
        ginfo = ginfo if isinstance(ginfo, dict) else {}
        shut = None
        if "shut_up_timestamp" in md:
            try:
                shut = float(md.get("shut_up_timestamp") or 0)
            except (TypeError, ValueError):
                shut = None
        whole = None
        if "group_all_shut" in ginfo:
            try:
                whole = int(float(ginfo.get("group_all_shut") or 0)) != 0
            except (TypeError, ValueError):
                whole = None
        if md.get("role"):
            self._cards[gk] = (now, str(md.get("card") or ""), str(md.get("role") or ""))
        for kind, until in self.store.sync_ban(gk, shut, whole, str(md.get("role") or ""), now):
            line = {
                "self_on": f"群 {gid} 查到它被 QQ 禁言到 {hhmm(until, now, self._tz())} → 那之前什么都不发",
                "self_off": f"群 {gid} 查到它的禁言已经解了（解禁通知漏了）→ 恢复说话",
                "all_on": f"群 {gid} 查到在全员禁言 → 解除前什么都不发",
                "all_off": f"群 {gid} 查到全员禁言已经解了 → 恢复说话",
            }[kind]
            self.store.note(line, gk, now)
            logger.info(f"{TAG} {line}")
        self._save()

    async def _self_id_of(self, bot):
        try:
            d = await self._call(bot, "get_login_info", timeout=5)
            return str((d or {}).get("user_id") or "")
        except Exception:
            return ""

    async def _sync_all(self, delay=0, why=""):
        """查一遍所有群（开机、NapCat 重新连上、每 6 小时）"""
        if delay:
            await asyncio.sleep(delay)
        if self._full_sync_busy or not self._on():
            return False
        if time.time() - self._full_sync_at < 60:
            return True  # 刚查过
        self._full_sync_busy = True
        try:
            done_any = False
            for plat in self._platforms():
                bot, pid = self._bot(plat), plat.meta().id
                self_id = await self._self_id_of(bot) if bot is not None else ""
                if not self_id:
                    continue
                try:
                    gl = await self._call(bot, "get_group_list", timeout=15)
                except Exception:
                    gl = None
                gl = [g for g in (gl if isinstance(gl, list) else []) if isinstance(g, dict) and g.get("group_id")]
                seen = set()
                for g in gl[:SYNC_GROUPS_MAX]:
                    gid = str(g.get("group_id"))
                    seen.add(f"{pid}:{gid}")
                    await self._sync_group(bot, pid, gid, self_id, g)
                    await asyncio.sleep(0.2)
                for gk in self.store.banned_groups():  # 不在群列表里的（被踢了 / 退了）也对一下
                    p, _, gid = gk.partition(":")
                    if p == pid and gk not in seen and gid.isdigit():
                        await self._sync_group(bot, pid, gid, self_id)
                done_any = True
            if done_any:
                self._full_sync_at = time.time()
                logger.info(f"{TAG} {why or '定时'}：查了一遍各个群的禁言")
            return done_any
        except Exception as e:
            logger.warning(f"{TAG} 查禁言出错：{e!r}"[:200])
            return False
        finally:
            self._full_sync_busy = False

    async def _sync_loop(self):
        """开机：等 QQ 连上查一遍所有群；之后每 10 分钟查记着在禁言的群、每 6 小时查一遍所有群"""
        booted = False
        while not self._closed:
            try:
                await asyncio.sleep(SYNC_SEC if booted else 15)
                if not self._on():
                    continue
                self._subscribe()
                if not booted or time.time() - self._full_sync_at > FULL_SYNC_SEC:
                    booted = await self._sync_all(why="开机" if not booted else "每 6 小时") or booted
                    continue
                for gk in self.store.banned_groups():
                    pid, _, gid = gk.partition(":")
                    plat = next((p for p in self._platforms() if p.meta().id == pid), None)
                    bot = self._bot(plat) if plat is not None else None
                    self_id = await self._self_id_of(bot) if bot is not None else ""
                    if self_id and gid.isdigit():
                        await self._sync_group(bot, pid, gid, self_id)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.warning(f"{TAG} 定时查禁言出错：{e!r}"[:200])

    # ───────── 每条群消息：认话、QQ 禁言通知 ─────────
    @filter.event_message_type(filter.EventMessageType.ALL, priority=sys.maxsize - 2)
    async def on_message(self, event: AstrMessageEvent):
        try:
            await self._on_message(event)
        except Exception as e:
            logger.warning(f"{TAG} 处理这条消息出错（这条照常）：{e!r}"[:200])

    async def _on_message(self, event):
        if not self._on():
            return
        self._ensure_loop()
        raw = self._raw(event)
        if raw is not None and raw.get("post_type") == "notice":
            if raw.get("notice_type") == "group_ban":
                await self._on_ban(event, raw)
            return
        gid = str(event.get_group_id() or "")
        if not gid or event.is_private_chat():
            return
        uid, self_id = str(event.get_sender_id() or ""), str(event.get_self_id() or "")
        if not uid or uid == self_id:
            return
        gk, now = self._gk(event), time.time()
        b = self.store.ban(gk, now)
        if b and b["whole"] and self._role(event) == "member":
            # 全员禁言时普通群友说不了话：普通群友说话了 = 全员禁言已经解了（解禁通知漏了也能自己好）
            self.store.lift_ban(gk, whole=True, now=now)
            self.store.note(f"群 {gid} 有普通群友说话了 → 全员禁言应该已经解了，恢复", gk, now)
            logger.info(f"{TAG} 群 {gid} 有普通群友说话了 → 全员禁言应该已经解了，恢复")
            self._save()
        if event.get_extra("handlers_parsed_params"):
            return  # 是指令（/说了算 解除 之类）：不当成在跟它说话
        f = self._flags(event)
        if not f["text"]:
            return
        names = self._names(event, gk)
        called = names_called(f["text"], names)
        direct = f["at_me"] or f["quoted_me"]
        admin, role, rank = self._rank(event)
        hushed = self.store.hushed(gk, uid, now)
        restricted = bool(self.store.mode(gk, now))
        recent = now - self._recent.get((gk, uid), 0) < RECENT_SEC
        v = recognize(
            f["text"],
            admin=admin,
            addressed=direct or called,
            direct=direct,
            at_other=bool(f["at_other"]),
            restricted=restricted,
            recent=recent,
            hushed=bool(hushed),
            names=names,  # v0.1.1：认话前先把它的名字拿掉（「小鱼恢复」）
        )
        if v.kind is None and v.sure:
            return
        name = event.get_sender_name() or uid
        span = None
        if not v.sure:
            if not truthy(self.config.get("use_llm"), False):
                logger.info(
                    f"{TAG} 群 {gid} {name}({uid})「{f['text'][:30]}」{v.why} → 不改（配置「规则拿不准时问一次模型」关着）"
                )
                return
            f["called"] = called
            res = await self._judge(event, gk, uid, name, role, f)
            kind = judge_kind(res, admin=admin, restricted=restricted, hushed=bool(hushed))
            logger.info(
                f"{TAG} 群 {gid} {name}({uid})「{f['text'][:30]}」规则拿不准（{v.why}）→ 问了模型："
                f"{res or '没问成'} → {kind or '不改'}"
            )
            if not kind:
                return
            what = {"quiet": "安静", "mute": "静音", "restore": "恢复"}.get((res or {}).get("action"), "安静")
            v, span = Verdict(kind, True, f"模型看了：让它{what}"), judge_span(res)
        await self._apply(event, gk, uid, name, role, v, f["text"], now, span, rank)

    async def _apply(self, event, gk, uid, name, role, v, text, now, span=None, rank=0):
        """照认出来的改状态（权限已经在认的时候按身份卡过）；这一条带上「刚改了什么」给模型"""
        gid, tz = gk.split(":", 1)[-1], self._tz()
        if span is None:
            span, how = parse_span(text, now, tz)
        else:
            how = "模型认的时长"
        kind, ack = v.kind, None
        if kind == "not_admin":
            what = "恢复" if "恢复" in v.why else ("静音" if "静音" in v.why else "安静")
            event.set_extra(EXTRA_NOT_ADMIN, {"what": what, "allowed": self._allowed_text()})
            logger.info(f"{TAG} 群 {gid} {name}({uid}) 是{role}，让它{what}不算（只认{self._allowed_text()}）")
            return
        if kind in MODE_NAME:
            sec = clamp_span(
                span,
                self._c("quiet_default_min", 30, 1, 1440) * 60,
                self._c("max_hours", 24, 0.1, 720) * 3600,
            )
            until = now + sec
            old = self.store.mode(gk, now)
            r = self.store.put_mode(gk, kind, until, rank, uid, name, role, text, now)
            base = r.get("base")
            if r["how"] in ("denied", "kept"):
                # 分高低：身份比他高的人设的那层更严（或者一样、管得更久）→ 改不了，模型知道为什么
                ack = {"kind": r["how"], "want": kind, "base": base, "role": role, "name": name}
                line = (f"群 {gid} {role}「{name}」让它{MODE_NAME[kind]} → 没改：现在是{base.get('role')}「{base.get('name')}」"
                        f"设的「{MODE_NAME[base['mode']]}」" + ("，他改不松" if r["how"] == "denied" else "，本来就管得更久"))
            else:
                ack = {"kind": kind, "until": until, "role": role, "name": name, "base": base}
                line = (
                    f"群 {gid} {role}「{name}」让它{MODE_NAME[kind]} → 到 {hhmm(until, now, tz)}（{span_text(sec)}"
                    + (f"，按「{how}」" if how else "，没说多久用默认")
                    + "）"
                    + (f"；原来是「{MODE_NAME[old['mode']]}」" if old else "")
                    + (f"；到点回到{base.get('role')}「{base.get('name')}」设的「{MODE_NAME[base['mode']]}」" if base else "")
                )
        elif kind == "restore":
            r = self.store.clear_mode(gk, rank, now)
            if r["how"] == "none":
                return
            old, base = r.get("old") or {}, r.get("now")
            if r["how"] == "denied":
                ack = {"kind": "denied", "want": "restore", "base": old, "role": role, "name": name}
                line = (f"群 {gid} {role}「{name}」让它恢复 → 没改：「{MODE_NAME[old['mode']]}」是{old.get('role')}"
                        f"「{old.get('name')}」设的，{role}撤不掉")
            elif r["how"] == "partial":
                ack = {"kind": "partial", "old": old.get("mode"), "base": base, "role": role, "name": name}
                line = (f"群 {gid} {role}「{name}」让它恢复 → 撤了上面那层「{MODE_NAME[old['mode']]}」，"
                        f"回到{base.get('role')}「{base.get('name')}」设的「{MODE_NAME[base['mode']]}」")
            else:
                ack = {"kind": "restore", "old": old.get("mode"), "role": role, "name": name}
                line = f"群 {gid} {role}「{name}」让它恢复 → 撤了「{MODE_NAME[old['mode']]}」"
        elif kind == "self_off":
            sec = clamp_span(span, self._c("hush_hours", 24, 0.1, 168) * 3600, HUSH_MAX_SEC)
            until = now + sec
            self.store.set_hush(gk, uid, until, name, text, now)
            ack = {"kind": "self_off", "until": until, "name": name}
            line = f"群 {gid}「{name}」({uid}) 说别接他的话 → 他没 @ 它的话不主动接，到 {hhmm(until, now, tz)}"
        elif kind == "self_on":
            if not self.store.clear_hush(gk, uid):
                return
            ack = {"kind": "self_on", "name": name}
            line = f"群 {gid}「{name}」({uid}) 说可以理他了 → 撤了「别接我的话」"
        else:
            return
        event.set_extra(EXTRA_ACK, ack)
        self.store.note(line, gk, now)
        logger.info(f"{TAG} {line}")
        self._save()
        if kind in ("quiet", "mute", "restore", "self_on") and not event.is_at_or_wake_command:
            # 只叫了它名字（没 @）：AstrBot 不会自己叫醒它 → 叫醒，让模型自己回这一句（「别接我的话」不叫醒：人家不想被接话）
            event.is_at_or_wake_command = True
        if truthy(self.config.get("notice"), False) and ack["kind"] not in ("denied", "kept"):
            if not self._decide(event, gk, ack["kind"], time.time())[0]:
                # 这一条本身不让它开口（静音 / 安静 / 禁言着）：插件自己的系统提示也不发，不然谁都能拿这几句让它在群里说话
                return
            text_n = notice(ack["kind"], now, tz, until=ack.get("until", 0), role=role, name=name, target=name)
            if text_n:
                try:
                    await self.context.send_message(event.unified_msg_origin, MessageChain().message(text_n))
                except Exception as e:
                    logger.warning(f"{TAG} 系统提示没发出去：{e!r}"[:200])

    async def _judge(self, event, gk, uid, name, role, f):
        """规则拿不准：问一次模型（同一句 10 分钟内不重复问；每个群每小时最多 JUDGE_PER_HOUR 次）"""
        now = time.time()
        key = (gk, uid, f["text"][:80])
        if now - self._judged.get(key, 0) < 600:
            return None
        rate = [t for t in self._judge_rate.get(gk, []) if now - t < 3600]
        if len(rate) >= JUDGE_PER_HOUR:
            self._judge_rate[gk] = rate
            return None
        self._judge_rate[gk] = rate + [now]
        self._judged[key] = now
        if len(self._judged) > 500:
            for k in sorted(self._judged, key=self._judged.get)[:200]:
                self._judged.pop(k, None)
        try:
            pid = str(self.config.get("judge_provider_id") or "").strip()
            if not pid:
                pid = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
            resp = await asyncio.wait_for(
                self.context.llm_generate(
                    chat_provider_id=pid,
                    prompt=judge_body(name, role, f["text"], f),
                    system_prompt=JUDGE_SYSTEM,
                ),
                20,
            )
            res = parse_judge(getattr(resp, "completion_text", "") or "")
        except Exception as e:
            logger.warning(f"{TAG} 问模型没问成：{e!r}"[:200])
            res = None
        if res is None and key in self._judged:
            # v0.1.1：没问成（超时 / 报错 / 回的看不懂）不算问过：同一句过 1 分钟可以再问（以前 10 分钟内不再问）。
            # 每小时的名额照占，模型挂着的时候不会被刷
            self._judged[key] = now - 540
        return res

    async def _on_ban(self, event, raw):
        """QQ 的禁言 / 解禁通知：只管它自己被禁言、全员禁言（它是群主 / 管理时全员禁言管不到它）"""
        gid = str(raw.get("group_id") or "")
        if not gid:
            return
        self_id = str(raw.get("self_id") or event.get_self_id() or "")
        target = str(raw.get("user_id") if raw.get("user_id") is not None else "")
        whole = target in ("0", "")
        if not whole and target != self_id:
            return
        gk, now = f"{event.get_platform_id()}:{gid}", time.time()
        op = str(raw.get("operator_id") or "")
        sub = str(raw.get("sub_type") or "")
        try:
            dur = int(float(raw.get("duration") or 0))
        except (TypeError, ValueError):
            dur = 0
        if sub == "ban":
            if whole:
                role = await self._my_role(event, gid)
                if role in ("owner", "admin"):
                    logger.info(f"{TAG} 群 {gid} 全员禁言了，它是{ROLE_NAME.get(role)}，管不到它 → 照常")
                    return
                self.store.set_ban(gk, True, 0, op, now)
                line = f"群 {gid} 全员禁言了 → 它什么都不发，等解除（最多当它禁 24 小时）"
            elif dur > 0:
                self.store.set_ban(gk, False, now + max(60, dur), op, now)
                line = f"群 {gid} 它被禁言了 {span_text(max(60, dur))} → 到 {hhmm(now + max(60, dur), now, self._tz())} 之前什么都不发"
            else:
                # 通知没带时长：不瞎猜（以前按 30 天算），去查它自己的 shut_up_timestamp
                shut = await self._query_shut(getattr(event, "bot", None), gid, event.get_self_id())
                if shut is None:
                    self.store.set_ban(gk, False, now + UNKNOWN_BAN_SEC, op, now)
                    line = f"群 {gid} 它被禁言了（通知没带时长、也没查到）→ 先停 {UNKNOWN_BAN_SEC // 60} 分钟，到时候再查"
                elif shut > now:
                    self.store.set_ban(gk, False, shut, op, now)
                    line = f"群 {gid} 它被禁言了：查到它被禁言到 {hhmm(shut, now, self._tz())} → 那之前什么都不发"
                else:
                    line = f"群 {gid} 收到禁言通知，但查到它没被禁言 → 照常"
        elif sub == "lift_ban":
            if not self.store.lift_ban(gk, whole=whole, now=now):
                return
            still = self.store.ban(gk, now)
            line = f"群 {gid} {'全员禁言' if whole else '它的禁言'}解除了" + (
                "；还有" + ("全员禁言" if still["whole"] else "它自己的禁言") + "没解 → 照旧不发" if still else " → 恢复说话"
            )
        else:
            return
        self.store.note(line, gk, now)
        logger.info(f"{TAG} {line}")
        self._save()

    # ───────── 要叫模型之前：按状态拦 ─────────
    @filter.on_waiting_llm_request(priority=WAIT_PRIORITY)
    async def gate(self, event: AstrMessageEvent):
        try:
            self._gate(event)
        except Exception as e:
            logger.warning(f"{TAG} 拦的时候出错（这条照常）：{e!r}"[:200])

    def _gate(self, event):
        if not self._on():
            return
        gid = str(event.get_group_id() or "")
        if not gid or event.is_private_chat():
            return
        gk, now = self._gk(event), time.time()
        uid = str(event.get_sender_id() or "")
        ok, why, b, m, h = self._decide(event, gk, (event.get_extra(EXTRA_ACK) or {}).get("kind"), now)
        if ok:
            return
        if why == "被禁言":  # 禁言着：谁说的都不回（回了也发不出去，白花钱）
            line = f"群 {gid} 它被{'全员' if b['whole'] else ''}禁言着 → 不回"
        elif why == "静音":
            line = f"群 {gid} 在静音（到 {hhmm(m['until'], now, self._tz())}）→ 只有群主 / 管理员 @ 才回，这条不回"
        elif why == "安静":
            line = f"群 {gid} 在安静（到 {hhmm(m['until'], now, self._tz())}）→ 没 @ 它、没引用它的不接"
        else:
            line = f"群 {gid}「{h.get('name') or uid}」说过别接他的话 → 他没 @ 它的这条不接"
        self._block(event, gk, why, line)

    def _decide(self, event, gk, ack_kind, now):
        """这一条放不放（规则在 core/gate.py）→ (放不放, 为什么, 禁言记录, 模式, 别接他)"""
        b = self.store.ban(gk, now)
        m = self.store.mode(gk, now)
        h = self.store.hushed(gk, str(event.get_sender_id() or ""), now)
        f = self._flags(event)
        admin, _, _ = self._rank(event)
        ok, why = decide(
            banned=bool(b), mode=m["mode"] if m else None, admin=admin,
            direct=f["at_me"] or f["quoted_me"] or f["wake"], hushed=bool(h), ack_kind=ack_kind,
        )
        return ok, why, b, m, h

    def _block(self, event, gk, why, line):
        release_angel(self.context, event, why)
        event.stop_event()
        now = time.time()
        k = (gk, why)
        if now - self._said.get(k, 0) > 60:  # 同一个群同一个原因一分钟只记一行
            self._said[k] = now
            logger.info(f"{TAG} {line}")

    # ───────── 回话请求：这一轮给模型的状态（只写事实）─────────
    @filter.on_llm_request(priority=35)
    async def on_llm_req(self, event: AstrMessageEvent, req: ProviderRequest):
        try:
            self._note(event, req)
        except Exception as e:
            logger.warning(f"{TAG} 状态没加上（这一轮照常）：{e!r}"[:200])

    def _note(self, event, req):
        if not self._on() or TextPart is None:
            return
        gid = str(event.get_group_id() or "")
        if not gid or event.is_private_chat():
            return
        gk, now = self._gk(event), time.time()
        uid = str(event.get_sender_id() or "")
        f = self._flags(event)
        text = model_note(
            self.store,
            gk,
            uid,
            event.get_sender_name() or uid,
            now,
            self._tz(),
            ack=event.get_extra(EXTRA_ACK),
            not_admin=event.get_extra(EXTRA_NOT_ADMIN),
            addressed=f["at_me"] or f["quoted_me"] or f["wake"],
        )
        if text:
            req.extra_user_content_parts.append(TextPart(text=text).mark_as_temp())

    # ───────── 发送前：被禁言的群什么都不发 ─────────
    @filter.on_decorating_result(priority=sys.maxsize - 3)
    async def on_decorate(self, event: AstrMessageEvent):
        try:
            if not self._on():
                return
            gid = str(event.get_group_id() or "")
            if not gid or event.is_private_chat():
                return
            gk = self._gk(event)
            if not self.store.ban(gk):
                return
            res = event.get_result()
            if res is None or not res.chain:
                return
            event.set_result(MessageEventResult())
            self._block(event, gk, "被禁言（发送前）", f"群 {gid} 它被禁言着 → 要发的这条不发")
        except Exception as e:
            logger.warning(f"{TAG} 发送前那一步出错（这条照常）：{e!r}"[:200])

    # ───────── 发完：记下它刚回了谁 ─────────
    @filter.after_message_sent(priority=10)
    async def after_sent(self, event: AstrMessageEvent):
        try:
            gid = str(event.get_group_id() or "")
            if not gid or event.is_private_chat():
                return
            self._recent[(self._gk(event), str(event.get_sender_id() or ""))] = time.time()
            if len(self._recent) > 2000:
                for k in sorted(self._recent, key=self._recent.get)[:500]:
                    self._recent.pop(k, None)
        except Exception as e:
            logger.warning(f"{TAG} 记「刚回了谁」出错：{e!r}"[:200])

    # ───────── 给别的插件用（duck typing）─────────
    def _keys_of(self, group_id, platform_id=""):
        gid = str(group_id)
        if platform_id:
            return [f"{platform_id}:{gid}"]
        return [k for k in set(self.store.groups) | {h.rpartition("|")[0] for h in self.store.hush} if k.split(":", 1)[-1] == gid]

    def group_state(self, group_id, platform_id=""):
        """这个群现在的状态 → {"mode": "quiet" / "mute" / None, "until": 到几点, "banned": QQ 禁言着没有}"""
        now = time.time()
        out = {"mode": None, "until": 0.0, "banned": False}
        if not self._on():
            return out
        for gk in self._keys_of(group_id, platform_id):
            m = self.store.mode(gk, now)
            if m:
                out["mode"], out["until"] = m["mode"], float(m["until"])
            if self.store.ban(gk, now):
                out["banned"] = True
        return out

    def may_speak(self, group_id, user_id="", addressed=False, platform_id="", admin=False):
        """它现在能不能在这个群里说话 → (能不能, 为什么)。addressed = 这句是回 @ 它 / 引用它的；admin = 回的是群管"""
        if not self._on():
            return True, ""
        st = self.group_state(group_id, platform_id)
        if st["banned"]:
            return False, "被 QQ 禁言着"
        if st["mode"] == "mute" and not (addressed and admin):
            return False, "群管让它静音着"
        if st["mode"] == "quiet" and not addressed:
            return False, "群管让它安静着"
        if user_id and not addressed:
            now = time.time()
            for gk in self._keys_of(group_id, platform_id):
                if self.store.hushed(gk, str(user_id), now):
                    return False, "他说过别接他的话"
        return True, ""

    # ───────── 指令 ─────────
    @filter.command("说了算")
    async def cmd(self, event: AstrMessageEvent, op: str = ""):
        """/说了算：这个群现在的状态；群主 / 管理员 /说了算 解除：马上恢复"""
        try:
            text = self._cmd(event, str(op or "").strip())
        except Exception as e:
            logger.warning(f"{TAG} 指令出错：{e!r}"[:200])
            text = "出错了，看日志。"
        yield event.plain_result(text)

    def _cmd(self, event, op):
        now, tz = time.time(), self._tz()
        gid = str(event.get_group_id() or "")
        if op in ("解除", "恢复", "解开"):
            if not gid or event.is_private_chat():
                return "在要解除的那个群里发；或者在 AstrBot 面板 → 插件 → 群管说了算 里点「解除」。"
            admin, role, rank = self._rank(event)
            if not admin:
                return f"只有{self._allowed_text()}能解除。"
            gk = self._gk(event)
            r = self.store.clear_mode(gk, rank, now)
            if r["how"] == "denied":
                old = r["old"]
                return f"「{MODE_NAME[old['mode']]}」是{old.get('role')}「{old.get('name')}」设的，{role}撤不掉（他自己或 AstrBot 管理员可以）。"
            was_ban = self.store.lift_ban(gk, None, now)
            name = event.get_sender_name() or str(event.get_sender_id())
            old, base = r.get("old"), r.get("now")
            if not old and not was_ban:
                return "这个群本来就是正常的。"
            line = f"群 {gid} {role}「{name}」用指令解除了" + (f"「{MODE_NAME[old['mode']]}」" if old else "") + (
                f"，回到{base.get('role')}「{base.get('name')}」设的「{MODE_NAME[base['mode']]}」" if base else ""
            ) + ("（连 QQ 禁言的记录也清了）" if was_ban else "")
            self.store.note(line, gk, now)
            logger.info(f"{TAG} {line}")
            self._save()
            if base:
                return f"撤了上面那层；还剩{base.get('role')}「{base.get('name')}」设的「{MODE_NAME[base['mode']]}」，{role}撤不掉。"
            return "好，恢复正常了。" + ("（QQ 禁言的记录也清了 —— QQ 那边要是还禁着，发出去也会失败。）" if was_ban else "")
        if op and op not in ("状态", "看看"):
            return "用法：/说了算（看这个群现在的状态）；群主 / 管理员：/说了算 解除"
        head = f"鱼力max · 群管说了算 v{VERSION}" + ("" if self._on() else "（总开关关着：什么都不管）")
        if gid and not event.is_private_chat():
            lines = [head + " · 本群"] + status_lines(self.store, self._gk(event), now, tz)
            lines.append(f"能让它安静的：{self._allowed_text()}（@ 它或叫它名字说「安静点 / 闭嘴 / 可以说话了」）")
            return "\n".join(lines)
        # 私聊：跨群的列表（有哪些群、谁说过别接他）只给 AstrBot 管理员；别人只看跟自己有关的
        try:
            astr_admin = bool(event.is_admin())
        except Exception:
            astr_admin = False
        return "\n".join([head] + private_status(self.store, str(event.get_sender_id() or ""), now, tz, admin=astr_admin))

    # ───────── 面板（AstrBot WebUI → 插件 → 打开页面）─────────
    def _register_web(self):
        try:
            if importlib.util.find_spec("astrbot.api.web") is None:  # 4.29 以后才有：老版本就不开面板
                return
        except Exception:
            return
        self.context.register_web_api(f"/{PLUGIN_NAME}/state", self.web_state, ["GET"], "群管说了算：状态")
        self.context.register_web_api(f"/{PLUGIN_NAME}/action", self.web_action, ["POST"], "群管说了算：解除")

    def snapshot(self):
        now, tz = time.time(), self._tz()
        groups = []
        for gk in self.store.groups_with_state(now):
            m = self.store.mode(gk, now)
            b = self.store.ban(gk, now)
            groups.append(
                {
                    "gk": gk,
                    "gid": gk.split(":", 1)[-1],
                    "mode": m["mode"] if m else None,
                    "mode_name": MODE_NAME[m["mode"]] if m else "正常",
                    "until": float(m["until"]) if m else 0,
                    "until_text": hhmm(m["until"], now, tz) if m else "",
                    "left": int(m["until"] - now) if m else 0,
                    "by": f"{m.get('role')}「{m.get('name')}」" if m else "",
                    "said": m.get("said", "") if m else "",
                    "ban": (
                        {"whole": b["whole"], "until_text": "等解除" if b["whole"] else hhmm(b["self_until"], now, tz)}
                        if b
                        else None
                    ),
                    "hush": [
                        {"uid": u, "name": h.get("name") or u, "until_text": hhmm(h["until"], now, tz)}
                        for u, h in self.store.hushed_in(gk, now)
                    ],
                }
            )
        return {
            "version": VERSION,
            "enabled": self._on(),
            "allowed": self._allowed_text(),
            "groups": groups,
            "log": list(self.store.log)[-40:][::-1],
            "config": {
                "quiet_default_min": self._c("quiet_default_min", 30, 1, 1440),
                "max_hours": self._c("max_hours", 24, 0.1, 720),
                "hush_hours": self._c("hush_hours", 24, 0.1, 168),
                "use_llm": truthy(self.config.get("use_llm"), False),
                "notice": truthy(self.config.get("notice"), False),
            },
        }

    async def web_state(self):
        return {"status": "ok", "data": self.snapshot()}

    async def web_action(self):
        from astrbot.api.web import request

        body = await request.json({})
        body = body if isinstance(body, dict) else {}
        op, gk = str(body.get("op") or ""), str(body.get("gk") or "")
        now = time.time()
        gid = gk.split(":", 1)[-1]
        if op == "clear":
            old = self.store.clear_mode(gk, 99, now).get("old")  # 面板是 AstrBot 的管理员在用：哪层都能撤
            if old:
                self.store.note(f"群 {gid} 面板里解除了「{MODE_NAME[old['mode']]}」", gk, now)
        elif op == "unban":
            if self.store.lift_ban(gk, None, now):
                self.store.note(f"群 {gid} 面板里清掉了 QQ 禁言的记录", gk, now)
        elif op == "unhush":
            uid = str(body.get("uid") or "")
            old = self.store.clear_hush(gk, uid)
            if old:
                self.store.note(f"群 {gid} 面板里撤了「{old.get('name') or uid}」的「别接我的话」", gk, now)
        else:
            return {"status": "error", "message": "不认识的操作"}
        self._save()
        return {"status": "ok", "data": self.snapshot()}
