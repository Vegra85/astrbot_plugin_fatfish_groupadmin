#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
端到端测试：真的 AstrBot（测过 4.29.4）+ 这个插件 + 假 QQ 端（假 NapCat 反向 WebSocket）+ 假模型（OpenAI 兼容）
几个群都开了 AstrBot 自带的「主动回复」（概率 1 = 没 @ 的每句都想接），看插件拦不拦得住：
  A 群主 @ 它「安静点，半小时」→ 模型回这一句（请求里带着：谁、让你安静、到几点）→ 之后没 @ 的不接、@ 的照回（请求里带着「这个群在安静」）；
    普通群友 @ 它「闭嘴」→ 不算（请求里写着他说的不算、状态没变）；/说了算 看得到；群主「可以说话了」→ 没 @ 的又接了
  B 群管理员不 @、叫它名字「小鱼 闭嘴十分钟」→ 认出来、叫醒模型回一句；静音中普通群友 @ 它不回、群管理员 @ 照回；
    普通群友 /说了算 解除 → 没权限；AstrBot 管理员（群里是普通群友）@ 它「恢复」→ 恢复
  B2 分高低（v0.1.0 复查）：群主让它安静 → 群管理员撤不掉（模型知道撤不掉）→ 群管理员往严了加「闭嘴」→ 他撤自己加的那层 → 回到群主的安静；
     静音中普通群友 @ 它「别理我」→ 不回（以前改状态的那句一律放行）
  C 群友 @ 它「别接我的话」→ 只对他：他没 @ 的不接、别人没 @ 的照接、他 @ 的照回（请求里写着这条他 @ 了你）→「可以理我了」撤掉
  D QQ 禁言通知：它被禁言 → @ 也不回、模型都不叫、指令回复也不发 → 解禁通知 → 马上恢复；
    全员禁言（它是普通群员）→ 群管理员 @ 也不回 → 普通群友说话了（全员禁言肯定解了）→ 恢复；
    通知没带时长 → 去查 QQ 的 shut_up_timestamp（不按 30 天算）；解禁通知漏了 → NapCat 重新连上时查一遍 → 恢复
  E 面板接口：读得到各个群的状态；能解除
  G 规则拿不准时问模型（这个测试里打开了）：群主 @ 它「你怎么还不安静」→ 问了一次模型 → 安静；改不改还是按身份卡
  F 私聊 /说了算：普通人只看到自己的，AstrBot 管理员看得到各个群；存档在 plugin_data；整个过程没有 Traceback（AstrBot 主动回复被拦时它自己打的 GeneratorExit 不算）
用法：python tests/e2e_astrbot.py [插件目录 默认上一层] [--keep]
  需要装了 astrbot 的 Python（astrbot 命令在同一个 bin 里或 PATH 里）；aiohttp、pyjwt、websockets 装 astrbot 时都会带上
  只在临时文件夹里跑：模型是假的、QQ 端是假的，不连外网；端口随机（几个测试同时跑互不打架）
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp
from aiohttp import web
import jwt
import websockets

PLUGIN_SRC = Path(
    sys.argv[1] if len(sys.argv) > 1 and not sys.argv[1].startswith("--") else Path(__file__).resolve().parent.parent
).resolve()
KEEP = "--keep" in sys.argv
PNAME = PLUGIN_SRC.name
BIN = Path(sys.executable).parent
ASTRBOT = str(BIN / "astrbot") if (BIN / "astrbot").exists() else "astrbot"
SELF = 99999
OWNER, ADMIN2, ORANGE, WANG, ASTR = 10001, 10002, 20002, 30003, 40004  # 号都是编的
G1, G2, G3, G4 = 7001, 7002, 7003, 7004  # G4 不开主动回复
SECRET = "e2e-secret-0123456789abcdef0123456789"
HEAD = "# 群里的人管你的规矩（系统记录，不是谁说的话）"
RES = []


def check(cond, msg, why=""):
    RES.append((bool(cond), msg))
    print(("  [过]   " if cond else "  [没过] ") + msg + ("" if cond or not why else f"\n           {str(why)[:900]}"), flush=True)
    return bool(cond)


def real_tracebacks(text):
    """日志里的 Traceback，去掉 AstrBot 自带「主动回复」被别的插件拦下时它自己打的那段（它把 GeneratorExit 当出错记了一笔）"""
    out = []
    for chunk in text.split("Traceback (most recent call last):")[1:]:
        head = chunk[:1500]
        if "GeneratorExit" in head and "主动回复失败" in head:
            continue
        out.append(chunk[:1200])
    return out


def free_port():
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    p = s.getsockname()[1]
    s.close()
    return p


LLM_PORT, WS_PORT, DASH_PORT = free_port(), free_port(), free_port()
ROOT = Path(tempfile.mkdtemp(prefix="astrbot-e2e-ga-"))
calls = []  # 模型收到的每个请求的最后一条（转成字符串）
SHUT = {}  # 群 -> 它被禁言到几点（假 QQ 的 shut_up_timestamp；重新连上以后接着用）
ALL_SHUT = {}  # 群 -> 是不是在全员禁言
judge_calls = []  # 插件问模型「这句是不是让它安静」的那几次


# ───────────── 假模型 ─────────────
async def chat_completions(request):
    body = await request.json()
    msgs = body.get("messages") or []
    last = msgs[-1].get("content") if msgs else ""
    last = last if isinstance(last, str) else json.dumps(last, ensure_ascii=False)
    sysmsg = " ".join(m.get("content") if isinstance(m.get("content"), str) else "" for m in msgs if m.get("role") == "system")
    if "你在帮 QQ 群里的一个聊天机器人看一条消息" in sysmsg:
        judge_calls.append(last)
        text = json.dumps({"target": "me", "action": "quiet", "minutes": None, "confidence": 90})
        return web.json_response({
            "id": "j", "object": "chat.completion", "created": int(time.time()), "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })
    calls.append(last)
    return web.json_response(
        {
            "id": "x",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": body.get("model", "fake"),
            "choices": [{"index": 0, "message": {"role": "assistant", "content": f"收到第{len(calls)}句"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        }
    )


async def models(request):
    return web.json_response({"object": "list", "data": [{"id": "fake-model", "object": "model"}]})


# ───────────── 假 QQ 端（NapCat 反向 WebSocket） ─────────────
class FakeQQ:
    def __init__(self):
        self.ws = None
        self.mid = 1000
        self.sent = []  # (群, 文字, 时间)
        self.store = {}
        self.names = {SELF: "测试鱼", OWNER: "阿木", ADMIN2: "阿珍", ORANGE: "小橙", WANG: "老王", ASTR: "大刘"}
        self.roles = {OWNER: "owner", ADMIN2: "admin"}  # 别的都是 member（它自己也是）
        self.new_sent = asyncio.Event()

    async def connect(self):
        for _ in range(120):
            try:
                self.ws = await websockets.connect(
                    f"ws://127.0.0.1:{WS_PORT}/ws/",
                    additional_headers={"X-Self-ID": str(SELF), "X-Client-Role": "Universal"},
                )
                break
            except Exception:
                await asyncio.sleep(0.5)
        if not self.ws:
            raise RuntimeError("连不上 AstrBot 的反向 WebSocket")
        asyncio.create_task(self.loop())
        await self.ws.send(json.dumps({"post_type": "meta_event", "meta_event_type": "lifecycle", "sub_type": "connect", "self_id": SELF, "time": int(time.time())}))

    async def loop(self):
        try:
            await self._loop()
        except websockets.exceptions.ConnectionClosed:
            pass  # AstrBot 关了

    async def _loop(self):
        async for raw in self.ws:
            try:
                d = json.loads(raw)
            except Exception:
                continue
            if "action" not in d:
                continue
            act, p, echo = d["action"], d.get("params") or {}, d.get("echo")
            data = {}
            if act in ("send_group_msg", "send_msg", "send_private_msg"):
                self.mid += 1
                msg = p.get("message")
                text = (
                    "".join((s.get("data") or {}).get("text", "") for s in msg if isinstance(s, dict) and s.get("type") == "text")
                    if isinstance(msg, list) else str(msg)
                )
                where = int(p.get("group_id") or 0) or ("p", int(p.get("user_id") or 0))
                self.sent.append((where, text, time.time()))
                self.store[self.mid] = {
                    "message_id": self.mid, "message_type": "group", "group_id": p.get("group_id"),
                    "sender": {"user_id": SELF, "nickname": self.names[SELF]}, "user_id": SELF, "self_id": SELF,
                    "time": int(time.time()), "message": msg if isinstance(msg, list) else [{"type": "text", "data": {"text": text}}],
                }
                data = {"message_id": self.mid}
                self.new_sent.set()
            elif act == "get_group_member_info":
                uid, gid = int(p.get("user_id") or 0), int(p.get("group_id") or 0)
                data = {"user_id": uid, "nickname": self.names.get(uid, str(uid)), "card": self.names.get(uid, ""), "role": self.roles.get(uid, "member"),
                        "shut_up_timestamp": int(SHUT.get(gid, 0)) if uid == SELF else 0}
            elif act == "get_group_info":
                gid = int(p.get("group_id") or 0)
                data = {"group_id": gid, "group_name": f"群{gid}", "group_all_shut": -1 if ALL_SHUT.get(gid) else 0}
            elif act == "get_group_list":
                data = [{"group_id": g, "group_name": f"群{g}", "group_all_shut": -1 if ALL_SHUT.get(g) else 0} for g in (G1, G2, G3, G4)]
            elif act == "get_stranger_info":
                uid = int(p.get("user_id") or 0)
                data = {"user_id": uid, "nickname": self.names.get(uid, str(uid))}
            elif act == "get_msg":
                data = self.store.get(int(p.get("message_id") or 0)) or {}
            elif act == "get_login_info":
                data = {"user_id": SELF, "nickname": self.names[SELF]}
            await self.ws.send(json.dumps({"status": "ok", "retcode": 0, "data": data, "echo": echo}))

    async def say(self, gid, uid, text, at_bot=False, quote=None):
        self.mid += 1
        segs = []
        if quote:
            segs.append({"type": "reply", "data": {"id": str(quote)}})
        if at_bot:
            segs.append({"type": "at", "data": {"qq": str(SELF)}})
            segs.append({"type": "text", "data": {"text": " " + text}})
        else:
            segs.append({"type": "text", "data": {"text": text}})
        ev = {
            "post_type": "message", "message_type": "group", "sub_type": "normal", "message_id": self.mid,
            "group_id": gid, "user_id": uid, "self_id": SELF, "time": int(time.time()), "message": segs,
            "raw_message": text, "font": 0,
            "sender": {"user_id": uid, "nickname": self.names.get(uid, str(uid)), "card": self.names.get(uid, ""), "role": self.roles.get(uid, "member")},
        }
        self.store[self.mid] = dict(ev)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))
        return self.mid

    async def say_private(self, uid, text):
        self.mid += 1
        ev = {
            "post_type": "message", "message_type": "private", "sub_type": "friend", "message_id": self.mid,
            "user_id": uid, "self_id": SELF, "time": int(time.time()), "message": [{"type": "text", "data": {"text": text}}],
            "raw_message": text, "font": 0, "sender": {"user_id": uid, "nickname": self.names.get(uid, str(uid))},
        }
        await self.ws.send(json.dumps(ev, ensure_ascii=False))

    async def notice(self, **kw):
        ev = {"post_type": "notice", "self_id": SELF, "time": int(time.time())}
        ev.update(kw)
        await self.ws.send(json.dumps(ev, ensure_ascii=False))

    async def wait_reply(self, gid, since, timeout=8.0):
        end = time.time() + timeout
        while time.time() < end:
            got = [s for s in self.sent if s[0] == gid and s[2] >= since]
            if got:
                return got
            self.new_sent.clear()
            try:
                await asyncio.wait_for(self.new_sent.wait(), max(0.05, end - time.time()))
            except asyncio.TimeoutError:
                pass
        return []


def setup_root():
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT))
    subprocess.run([ASTRBOT, "init", "-y"], cwd=ROOT, env=env, check=True, capture_output=True)
    shutil.copytree(PLUGIN_SRC, ROOT / "data" / "plugins" / PNAME, ignore=shutil.ignore_patterns("__pycache__", "*.pyc", ".git", "tests", ".ruff_cache"))
    code = f"from astrbot.core.config.astrbot_config import AstrBotConfig;AstrBotConfig(config_path=r'{ROOT / 'data' / 'cmd_config.json'}')"
    subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, check=True, capture_output=True)
    p = ROOT / "data" / "cmd_config.json"
    cfg = json.loads(p.read_text(encoding="utf-8-sig"))
    cfg["platform"] = [{"id": "napcat", "type": "aiocqhttp", "enable": True, "ws_reverse_host": "127.0.0.1", "ws_reverse_port": WS_PORT, "ws_reverse_token": ""}]
    cfg["provider"] = [{
        "id": "fake", "type": "openai_chat_completion", "provider_type": "chat_completion", "enable": True,
        "key": ["sk-fake"], "api_base": f"http://127.0.0.1:{LLM_PORT}/v1", "model": "fake-model", "timeout": 30, "modalities": ["text"],
    }]
    cfg.setdefault("provider_settings", {})["default_provider_id"] = "fake"
    cfg["admins_id"] = [str(ASTR)]
    # 主动回复（概率 1：没 @ 的每句都想接）：看插件拦不拦得住；G4 不开
    cfg["provider_ltm_settings"]["active_reply"] = {
        "enable": True, "method": "possibility_reply", "possibility_reply": 1.0, "whitelist": [str(G1), str(G2), str(G3)],
    }
    cfg["platform_settings"]["rate_limit"]["count"] = 1000
    cfg.setdefault("dashboard", {})["port"] = DASH_PORT
    cfg["dashboard"]["host"] = "127.0.0.1"
    cfg["dashboard"]["jwt_secret"] = SECRET
    p.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    cdir = ROOT / "data" / "config"
    cdir.mkdir(parents=True, exist_ok=True)
    (cdir / f"{PNAME}_config.json").write_text(json.dumps({"names": ["小鱼"], "use_llm": True}, ensure_ascii=False), encoding="utf-8")


def log_text():
    return (ROOT / "astrbot_stdout.log").read_text(encoding="utf-8", errors="replace")


async def main():
    setup_root()
    app = web.Application()
    app.router.add_post("/v1/chat/completions", chat_completions)
    app.router.add_get("/v1/models", models)
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "127.0.0.1", LLM_PORT).start()
    env = dict(os.environ, ASTRBOT_ROOT=str(ROOT), DASHBOARD_PORT=str(DASH_PORT), PYTHONUNBUFFERED="1")
    log = open(ROOT / "astrbot_stdout.log", "w", encoding="utf-8")
    proc = subprocess.Popen([ASTRBOT, "run"], cwd=ROOT, env=env, stdout=log, stderr=subprocess.STDOUT)
    qq = FakeQQ()

    async def ask(gid, uid, text, at_bot=False, timeout=8.0, quote=None):
        """说一句 → (这之后它在这个群发的, 这之后模型收到的请求)"""
        n0, t0 = len(calls), time.time()
        await qq.say(gid, uid, text, at_bot=at_bot, quote=quote)
        got = await qq.wait_reply(gid, t0, timeout)
        await asyncio.sleep(0.3)
        return got, calls[n0:]

    async def silent(gid, uid, text, at_bot=False, wait=4.0):
        """说一句、等一会儿 → (它发了什么, 模型收到的请求)：应该什么都没有"""
        n0, t0 = len(calls), time.time()
        await qq.say(gid, uid, text, at_bot=at_bot)
        await asyncio.sleep(wait)
        return [s for s in qq.sent if s[0] == gid and s[2] >= t0], calls[n0:]

    try:
        await qq.connect()
        await asyncio.sleep(4)
        text = log_text()
        check(PNAME in text and "Traceback" not in text and "[群管说了算] v" in text, "AstrBot 起来了、插件加载了、没有报错", text[-1500:])

        # ═════════ A 群主让它安静 ═════════
        print("\n[A 群主 @ 它「安静点，半小时」]")
        got, _ = await ask(G1, WANG, "大家好", at_bot=True)
        check(got, "（准备）有人 @ 它：回了（这个群有了对话，主动回复才接得上）")
        await asyncio.sleep(1)
        got, _ = await ask(G1, WANG, "今天天气不错")
        check(got, "（对照）没 @ 的普通话：AstrBot 主动回复照常接")
        await asyncio.sleep(1)
        t_set = time.time()
        got, req = await ask(G1, OWNER, "安静点，半小时", at_bot=True)
        check(got, "群主 @ 它「安静点，半小时」：它回了这一句（怎么回由模型定）", got)
        r = req[-1] if req else ""
        check(HEAD in r and "群主「阿木」刚才这句是让你在这个群安静：到 " in r and "还有 30 分钟" in r,
              "给模型的状态：谁、让你安静、到几点（只写事实）", r[-500:])
        check("你可以说" not in r and "语气" not in r, "没给模型写台词", r[-300:])
        await asyncio.sleep(1)
        got, req = await silent(G1, WANG, "今天中午吃什么")
        check(not got and not req, "安静中：没 @ 的话主动回复不接（连模型都没叫）", (got, req[-1:]))
        got, req = await ask(G1, WANG, "你还在吗", at_bot=True)
        check(got, "安静中：@ 它的照回")
        check(req and "这个群现在是「安静」：群主「阿木」" in req[-1] and "只有 @ 你、引用你的话会交给你" in req[-1], "请求里带着「这个群在安静、到几点、只有 @ 和引用它的会交给它」", (req or [""])[-1][-500:])
        quoted = qq.mid - 1  # 它刚回的那条在假 QQ 里的编号（回复先发出去，再收到这句）
        bot_mid = max(k for k, v in qq.store.items() if v.get("user_id") == SELF)
        got, _ = await ask(G1, WANG, "那我引用你一下", quote=bot_mid)
        check(got, "安静中：引用它的话照回")
        got, req = await ask(G1, ORANGE, "闭嘴", at_bot=True)
        check(got and req and "「小橙」不算能管你的人" in req[-1] and "这个群的状态没变" in req[-1],
              "普通群友 @ 它「闭嘴」：照回，请求里写着他说的不算、状态没变", (req or [""])[-1][-400:])
        got, _ = await ask(G1, WANG, "/说了算")
        check(got and "现在：安静" in got[-1][1] and "群主「阿木」" in got[-1][1] and "还有" in got[-1][1],
              "/说了算：看得到现在安静、谁设的、还有多久（普通群友说的「闭嘴」没把它改成静音）", got)
        got, req = await ask(G1, OWNER, "可以说话了", at_bot=True)
        check(got and req and "让你恢复正常说话" in req[-1], "群主「可以说话了」→ 恢复（请求里写着撤了）", (req or [""])[-1][-300:])
        await asyncio.sleep(1)
        got, _ = await ask(G1, WANG, "晚上吃火锅吧")
        check(got, "恢复以后：没 @ 的话主动回复又接了")
        check("群 7001 群主「阿木」让它安静 → 到" in log_text() and "按「半小时」" in log_text(), "日志写清楚了：谁让它安静、到几点、按他说的多久")

        # ═════════ B 叫名字的静音 ═════════
        print("\n[B 群管理员不 @、叫它名字「小鱼 闭嘴十分钟」]")
        got, req = await ask(G4, ADMIN2, "小鱼 闭嘴十分钟")
        check(got and req and "群管理员「阿珍」刚才这句是让你在这个群静音：到 " in req[-1] and "还有 10 分钟" in req[-1],
              "没 @、叫名字也认：叫醒模型回了这一句（G4 没开主动回复，回它的只能是插件叫醒的）", (got, (req or [""])[-1][-300:]))
        got, req = await silent(G4, WANG, "在吗在吗", at_bot=True)
        check(not got and not req, "静音中：普通群友 @ 它也不回（模型都不叫）", (got, req[-1:]))
        got, req = await ask(G4, ADMIN2, "你还在吗", at_bot=True)
        check(got and req and "这个群现在是「静音」" in req[-1], "静音中：群管理员 @ 它照回（请求里带着「静音」）", (req or [""])[-1][-300:])
        got, req = await silent(G4, WANG, "别理我", at_bot=True)
        check(not got and not req, "静音中普通群友 @ 它「别理我」：不回（改状态的那句只给群管放行，不然谁都能让它开口、还能来回刷）", (got, req[-1:]))
        got, _ = await ask(G4, WANG, "/说了算 解除")
        check(got and "只有" in got[-1][1] and "能解除" in got[-1][1], "普通群友 /说了算 解除：没权限", got)
        got, req = await ask(G4, ASTR, "恢复", at_bot=True)
        check(got and req and "AstrBot 管理员「大刘」刚才这句是让你恢复正常说话" in req[-1],
              "AstrBot 管理员（在群里是普通群友）@ 它「恢复」→ 恢复", (req or [""])[-1][-300:])
        got, _ = await ask(G4, WANG, "现在能说话了吗", at_bot=True)
        check(got, "恢复以后普通群友 @ 它照回")
        got, _ = await ask(G4, OWNER, "安静", at_bot=True)
        got, _ = await ask(G4, OWNER, "/说了算 解除")
        check(got and "恢复正常了" in got[-1][1], "群主 /说了算 解除：马上恢复", got)

        # ═════════ B2 分高低 ═════════
        print("\n[B2 分高低：群主设的，群管理员撤不掉；往严了加得了]")
        await ask(G4, OWNER, "安静一小时", at_bot=True)
        got, req = await ask(G4, ADMIN2, "可以说话了", at_bot=True)
        check(got and req and "群主「阿木」设的「安静」" in req[-1] and "群管理员撤不掉，状态没变" in req[-1],
              "群管理员「可以说话了」→ 撤不掉群主设的（它照回，请求里写着撤不掉、状态没变）", (req or [""])[-1][-400:])
        got, _ = await ask(G4, ADMIN2, "/说了算 解除")
        check(got and "撤不掉" in got[-1][1], "群管理员 /说了算 解除 → 也撤不掉", got)
        got, req = await ask(G4, ADMIN2, "闭嘴", at_bot=True)
        check(got and req and "让你在这个群静音" in req[-1] and "到点以后回到群主「阿木」设的「安静」" in req[-1],
              "群管理员「闭嘴」→ 往严了加得了：静音（请求里写着到点回到群主的安静）", (req or [""])[-1][-400:])
        got, req = await silent(G4, WANG, "在吗", at_bot=True)
        check(not got and not req, "静音中普通群友 @ 它不回")
        got, req = await ask(G4, ADMIN2, "可以说话了", at_bot=True)
        check(got and req and "撤了上面那层「静音」" in req[-1] and "回到群主「阿木」设的「安静」" in req[-1],
              "群管理员撤自己加的那层 → 回到群主的安静", (req or [""])[-1][-400:])
        got, _ = await ask(G4, WANG, "现在呢", at_bot=True)
        check(got, "回到安静：普通群友 @ 它照回")
        got, req = await ask(G4, OWNER, "可以说话了", at_bot=True)
        check(got and req and "让你恢复正常说话：这个群的「安静」撤了" in req[-1], "群主自己撤 → 恢复正常", (req or [""])[-1][-300:])

        # ═════════ C 别接我的话 ═════════
        print("\n[C 群友「别接我的话」]")
        await ask(G3, WANG, "嗨", at_bot=True)
        await asyncio.sleep(1)
        got, req = await ask(G3, ORANGE, "别接我的话", at_bot=True)
        check(got and req and "「小橙」刚才这句是让你别主动接他的话" in req[-1] and "他 @ 你、引用你的话不受影响" in req[-1],
              "小橙 @ 它「别接我的话」：它回了这一句，请求里写着到几点、@ 照回", (req or [""])[-1][-400:])
        await asyncio.sleep(1)
        got, req = await silent(G3, ORANGE, "我自己说说而已")
        check(not got and not req, "小橙没 @ 的话：不接（主动回复被拦）", (got, req[-1:]))
        got, _ = await ask(G3, WANG, "我也随便说说")
        check(got, "老王没 @ 的话：照接（只对小橙）")
        got, req = await ask(G3, ORANGE, "问你个事", at_bot=True)
        check(got and req and "这条他 @ 了你或者引用了你的话" in req[-1] and "照回" not in req[-1], "小橙 @ 它：照回（请求里只写事实：这条他 @ 了你）", (req or [""])[-1][-400:])
        got, req = await ask(G3, ORANGE, "可以理我了", at_bot=True)
        check(got and req and "让你又可以接他的话" in req[-1], "小橙「可以理我了」→ 撤掉", (req or [""])[-1][-300:])
        await asyncio.sleep(1)
        got, _ = await ask(G3, ORANGE, "那我继续说")
        check(got, "撤掉以后小橙没 @ 的话又接了")

        # ═════════ D QQ 禁言 ═════════
        print("\n[D QQ 禁言通知]")
        await ask(G2, WANG, "你好", at_bot=True)
        await qq.notice(notice_type="group_ban", sub_type="ban", group_id=G2, user_id=SELF, operator_id=OWNER, duration=600)
        await asyncio.sleep(1.5)
        got, req = await silent(G2, WANG, "在吗", at_bot=True)
        check(not got and not req, "它被禁言了：@ 它也不回、模型都不叫", (got, req[-1:]))
        got, req = await silent(G2, WANG, "/说了算", wait=3)
        check(not got, "被禁言时指令回复也不发", got)
        check("群 7002 它被禁言了 10 分钟" in log_text(), "日志写了：被禁言 10 分钟、到几点之前什么都不发")
        await qq.notice(notice_type="group_ban", sub_type="lift_ban", group_id=G2, user_id=SELF, operator_id=OWNER, duration=0)
        await asyncio.sleep(1.5)
        got, req = await ask(G2, WANG, "现在呢", at_bot=True)
        check(got and req and "你在这个群被禁言了" in req[-1], "解禁通知一来马上恢复（请求里写着刚被禁言过）", (got, (req or [""])[-1][-300:]))
        await qq.notice(notice_type="group_ban", sub_type="ban", group_id=G2, user_id=0, operator_id=OWNER, duration=0)
        await asyncio.sleep(1.5)
        got, req = await silent(G2, ADMIN2, "全员禁言了你还在吗", at_bot=True)
        check(not got and not req, "全员禁言（它是普通群员）：群管理员 @ 它也不回", (got, req[-1:]))
        await qq.notice(notice_type="group_ban", sub_type="lift_ban", group_id=G2, user_id=0, operator_id=OWNER, duration=0)
        await asyncio.sleep(1.5)
        got, _ = await ask(G2, ADMIN2, "解了吗", at_bot=True)
        check(got, "全员禁言解除通知 → 恢复")
        await qq.notice(notice_type="group_ban", sub_type="ban", group_id=G2, user_id=0, operator_id=OWNER, duration=0)
        await asyncio.sleep(1.5)
        got, _ = await ask(G2, WANG, "我能说话了诶", at_bot=True)
        check(got, "全员禁言、解除通知漏了：普通群友能说话了（全员禁言肯定解了）→ 自己恢复")
        SHUT[G2] = int(time.time()) + 300
        await qq.notice(notice_type="group_ban", sub_type="ban", group_id=G2, user_id=SELF, operator_id=OWNER, duration=0)
        await asyncio.sleep(2)
        got, req = await silent(G2, WANG, "禁言了吗", at_bot=True, wait=3)
        check(not got and not req and "查到它被禁言到" in log_text(), "禁言通知没带时长 → 去查 QQ（查到 5 分钟）→ 不回（不再按 30 天算）", log_text()[-600:])
        SHUT[G2] = 0  # QQ 那边解了，但解禁通知漏了
        await qq.ws.close()
        await asyncio.sleep(2)
        mid0 = qq.mid
        qq = FakeQQ()
        qq.mid = mid0 + 100
        await qq.connect()
        await asyncio.sleep(10)
        got, _ = await ask(G2, WANG, "现在能说了吧", at_bot=True)
        check(got and "查到它的禁言已经解了" in log_text(), "解禁通知漏了：NapCat 重新连上时查了一遍 → 恢复", log_text()[-600:])

        # ═════════ E 面板接口 ═════════
        print("\n[E 面板接口]")
        await ask(G1, OWNER, "安静一小时", at_bot=True)
        token = jwt.encode({"username": "astrbot", "exp": int(time.time()) + 3600}, SECRET, algorithm="HS256")
        hdr = {"Authorization": f"Bearer {token}"}
        base = f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/extensions/{PNAME}"
        async with aiohttp.ClientSession() as s:
            async with s.get(f"{base}/state", headers=hdr) as r:
                d = await r.json(content_type=None)
            st = (d or {}).get("data") or {}
            g1 = next((g for g in st.get("groups", []) if g.get("gid") == str(G1)), {})
            check(r.status == 200 and g1.get("mode") == "quiet" and "阿木" in g1.get("by", ""), "GET state：读到 7001 在安静、谁设的", str(d)[:400])
            async with s.post(f"{base}/action", headers=hdr, json={"op": "clear", "gk": g1.get("gk", "")}) as r:
                d = await r.json(content_type=None)
            g1b = next((g for g in ((d or {}).get("data") or {}).get("groups", []) if g.get("gid") == str(G1)), {})
            check(r.status == 200 and not g1b.get("mode"), "POST action：面板里解除", str(d)[:300])
            async with s.get(f"{base}/state") as r:
                check(r.status in (401, 403), f"没登录读不到（{r.status}）")
            async with s.get(f"http://127.0.0.1:{DASH_PORT}/api/v1/plugins/{PNAME}/views/panel", headers=hdr) as r:
                raw = json.dumps(await r.json(content_type=None), ensure_ascii=False)
            check(r.status == 200 and "/_t/" in raw, "面板页 panel 认出来了（插件页面里能打开）", raw[:300])
        await asyncio.sleep(1)
        got, _ = await ask(G1, WANG, "面板解除了吧")
        check(got, "面板解除以后没 @ 的话又接了")

        # ═════════ G 规则拿不准时问模型 ═════════
        print("\n[G 规则拿不准时问模型（这个测试里打开了；默认关）]")
        n0 = len(judge_calls)
        got, req = await ask(G4, OWNER, "你怎么还不安静", at_bot=True)
        check(len(judge_calls) == n0 + 1 and "说话的人：阿木（群主）" in judge_calls[-1] and "@ 了这个机器人" in judge_calls[-1],
              "群主「你怎么还不安静」规则拿不准 → 问了一次模型（给它看：谁、什么身份、@ 了它、原话）", judge_calls[n0:])
        check(got and req and "群主「阿木」刚才这句是让你在这个群安静" in req[-1], "模型说是让它安静 → 安静；它回了这一句", (req or [""])[-1][-300:])
        check("规则拿不准" in log_text() and "问了模型" in log_text(), "日志写着规则拿不准、问了模型")
        n0 = len(judge_calls)
        got, req = await ask(G4, WANG, "你怎么还不安静", at_bot=True)
        check(len(judge_calls) == n0 and got, "普通群友说同一句：不问模型（他说了也改不了，不花这个钱），照常回", judge_calls[n0:])
        got, _ = await ask(G4, WANG, "/说了算")
        check(got and "现在：安静" in got[-1][1] and "阿木" in got[-1][1], "状态还是群主设的安静（普通群友那句没改它）", got)
        await ask(G4, OWNER, "/说了算 解除")

        # ═════════ F 存档、日志 ═════════
        print("\n[F 存档、日志]")
        await ask(G3, ORANGE, "别理我", at_bot=True)
        t0 = time.time()
        await qq.say_private(WANG, "/说了算")
        got = await qq.wait_reply(("p", WANG), t0)
        check(got and "7003" not in got[-1][1] and "小橙" not in got[-1][1] and "有状态的群" not in got[-1][1]
              and "7004" in got[-1][1],
              "普通人私聊 /说了算：看不到别的群号、别人说过别接他的话；只看到他自己（静音时在 7004 说的「别理我」记下了、没回）", got)
        t0 = time.time()
        await qq.say_private(ORANGE, "/说了算")
        got = await qq.wait_reply(("p", ORANGE), t0)
        check(got and "7003" in got[-1][1], "小橙私聊 /说了算：看得到他自己在 7003 说过别接他的话", got)
        t0 = time.time()
        await qq.say_private(ASTR, "/说了算")
        got = await qq.wait_reply(("p", ASTR), t0)
        check(got and "7003" in got[-1][1] and "有状态的群" in got[-1][1], "AstrBot 管理员私聊 /说了算：看得到各个群", got)
        sf = ROOT / "data" / "plugin_data" / PNAME / "state.json"
        st = json.loads(sf.read_text(encoding="utf-8")) if sf.exists() else {}
        check(any(k.endswith(f"|{ORANGE}") for k in (st.get("hush") or {})), "状态存在 data/plugin_data 里（重启还在）", list(st))
        bad = real_tracebacks(log_text())
        check(not bad, "整个过程 AstrBot 日志里没有 Traceback（主动回复被拦时它自己打的 GeneratorExit 不算）", bad[:1] or "")
        check("[群管说了算] 处理这条消息出错" not in log_text() and "出错（这条照常）" not in log_text(), "插件自己没报过错", "")
    finally:
        proc.terminate()
        try:
            proc.wait(15)
        except Exception:
            proc.kill()
        await runner.cleanup()
        log.close()

    ok = sum(1 for c, _ in RES if c)
    bad = [m for c, m in RES if not c]
    print("\n" + "=" * 60)
    print(f"  结果：{ok} 项过了" + (f"，{len(bad)} 项没过" if bad else "，全过"))
    for m in bad:
        print("    -", m)
    print("=" * 60)
    if KEEP or bad:
        print("AstrBot 目录留着：", ROOT)
    else:
        shutil.rmtree(ROOT, ignore_errors=True)
    sys.exit(1 if bad else 0)


asyncio.run(main())
