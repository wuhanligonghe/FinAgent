"""Conservative host routing for standalone social and assistant-help messages."""
from __future__ import annotations

import re
import unicodedata


_CONVERSATION = re.compile(
    r"(?:请问|请你|请)?(?:"
    r"你好(?:呀|啊)?|您好|嗨|哈喽|早上好|下午好|晚上好|早安|晚安|"
    r"你是谁|你叫什么(?:名字)?|你是(?:什么|哪个)(?:助手|模型)|"
    r"介绍(?:一下)?你(?:自己)?|自我介绍(?:一下)?|"
    r"你能做什么|你会什么|你(?:有|支持)哪些(?:功能|能力)|"
    r"你可以(?:帮我)?做(?:些)?什么|你能帮我(?:做)?什么|"
    r"谢谢(?:你)?|多谢|感谢|再见|拜拜|"
    r"hello|hi|hey|goodmorning|goodevening|"
    r"whoareyou|whatisyourname|whatcanyoudo|introduceyourself|"
    r"thanks|thankyou|bye|goodbye)"
)


def requires_data(goal: str) -> bool:
    """Exempt only fully recognized conversation; mixed/unknown goals need data.

    A greeting substring never exempts a financial request. The model cannot
    change this decision through its final answer or tool observations.
    """
    normalized = unicodedata.normalize("NFKC", goal).casefold()
    clauses = [re.sub(r"\s+", "", part)
               for part in re.split(r"[,，。.！!？?；;、\r\n]+", normalized) if part.strip()]
    return not clauses or not all(_CONVERSATION.fullmatch(part) for part in clauses)
