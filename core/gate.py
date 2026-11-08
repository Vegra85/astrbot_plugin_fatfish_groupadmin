# -*- coding: utf-8 -*-
"""要叫模型之前，这一条放不放（「要叫模型之前」那个钩子按这个拦）。不依赖 AstrBot。

改状态的那一句本身要让模型回（它知道谁让它怎样、到几点）—— 但只限群管改安静 / 静音的那几句：
群友的「别接我的话 / 可以理我了」照样过静音、安静的拦（不然静音中谁都能拿这几句让它开口，还能来回刷）。
"""

# 群管改安静 / 静音的那句（改成了、没改成都算）：放行让模型回
ADMIN_ACKS = ("quiet", "mute", "restore", "partial", "denied", "kept")


def decide(banned=False, mode=None, admin=False, direct=False, hushed=False, ack_kind=None):
    """→ (放不放, 为什么不放)
    banned：它被 QQ 禁言着；mode：这个群现在 quiet / mute / None；admin：说话的人算不算群管；
    direct：这条 @ 了它 / 引用了它 / 用了唤醒词；hushed：说话的人说过别接他的话；ack_kind：这条刚改了什么状态"""
    if banned:
        return False, "被禁言"
    if admin and ack_kind in ADMIN_ACKS:
        return True, ""
    if mode == "mute" and not (admin and direct):
        return False, "静音"
    if mode == "quiet" and not direct:
        return False, "安静"
    if hushed and not direct:
        return False, "别接他"
    return True, ""
