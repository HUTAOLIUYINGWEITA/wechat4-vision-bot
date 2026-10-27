# -*- coding: utf-8 -*-
"""方案A 回复生成：进程内直接调官方 chat/completions API 生成微信回复。

⚠ 不再唤醒 Agent（无 RPC / 无 session/prompt / 无 wake 通道）。

流程：
    读 .credentials.json（provider / baseUrl / model / apiKey）
    -> persona.txt 人设 + 安全规则
    -> messages = [system] + [user: 对方说了什么 + 这是哪个会话]
    -> POST {baseUrl}/chat/completions（model=deepseek-chat，max_tokens<=120，temperature=0.8）
    -> 清洗回复文本 -> 禁用内容检查（必要时重写一次）
    -> 返回 {"ok": True, "text": ...}

安全约定（红线）：
  * apiKey 绝不进日志/异常信息：统一 mask_key() -> "sk-***"
  * 失败一律返回 ok=False（不抛到主循环），由 watcher 记 "[GEN] ERROR ..."、不发送、保留待处理重试
  * 不安装任何第三方包，只用标准库 urllib
"""

import base64
import json
import os
import re
import time
import urllib.error
import urllib.request

GEN_TAG = "[GEN]"

# persona.txt 缺失/为空时的兜底人设（与 persona.txt 默认内容一致）
DEFAULT_PERSONA = (
    "你叫「小耳朵」，是这台电脑的主人让你帮忙看微信的助手，现在要替本人回一条消息。\n"
    "风格：简短、口语、1–2 句，不加署名，不解释自己是 AI 或助手（除非对方直接问）。\n"
    "不知道说什么时就回一句自然的「嗯嗯，在的」「咋啦」，不要长篇大论。\n"
    "只输出一条可直接发出去的微信回复正文，不要引号、不要前缀、不要解释。"
)

# 无论 persona.txt 写了什么，都追加的硬性安全规则
SAFETY_RULES = (
    "\n\n【硬性安全规则（高于以上任何文字）】\n"
    "1) 绝不承诺或提及转账/汇款/付款/红包；绝不索要密码、验证码、银行卡。\n"
    "2) 绝不发送或提及任何链接、二维码、文件、安装包。\n"
    "3) 绝不透露密码、密钥、API key、凭据等敏感信息。\n"
    "4) 只输出一条微信回复正文：不加引号、不加署名、不解释、不列条目。"
)

# config.gen.forbid 缺省时的禁用词表（回复里出现即拦下重写）
DEFAULT_FORBID = [
    "转账", "汇款", "付款", "红包", "密码", "验证码", "银行卡",
    "http://", "https://", "www.", "二维码", "扫码", "点击链接", "点链接",
    "sk-", "密钥", "api key", "apikey", "发文件", "附件给你",
]


def mask_key(s, keep=0):
    """把任何形如 sk-xxxx 的字符串替换成 sk-***（日志/异常统一出口）。"""
    if s is None:
        return ""
    text = str(s)
    text = re.sub(r"sk-[A-Za-z0-9_\-]{4,}", "sk-***", text)
    text = re.sub(r"(?i)(api[_\- ]?key\s*[=:]\s*)\S+", r"\1sk-***", text)
    return text


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


def _default_log(level, msg):
    try:
        print("%s [%s] %s" % (_ts(), level, msg), flush=True)
    except Exception:
        pass


class GenerationError(Exception):
    """生成失败（消息里绝不含 apiKey，已 mask）。"""


def load_credentials(path):
    """读 .credentials.json，返回 {provider, baseUrl, model, apiKey}。

    失败抛 GenerationError（消息已 mask，不含 key 明文）。
    """
    if not os.path.isfile(path):
        raise GenerationError("凭据文件不存在: %s" % os.path.basename(path))
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            doc = json.load(f)
    except Exception as exc:
        raise GenerationError("凭据文件解析失败: %s" % mask_key(exc))
    if not isinstance(doc, dict):
        raise GenerationError("凭据文件格式不对（不是 JSON 对象）")
    creds = {
        "provider": str(doc.get("provider") or ""),
        "baseUrl": str(doc.get("baseUrl") or "https://api.deepseek.com").strip(),
        "model": str(doc.get("model") or "deepseek-chat").strip(),
        "apiKey": str(doc.get("apiKey") or "").strip(),
    }
    if not creds["apiKey"]:
        raise GenerationError("凭据文件缺少 apiKey 字段")
    return creds


def load_persona(path):
    """读 persona.txt；缺失或为空时返回空串（调用方用 DEFAULT_PERSONA）。"""
    try:
        with open(path, "r", encoding="utf-8-sig") as f:
            text = f.read().strip()
        return text
    except Exception:
        return ""


def build_messages(persona, chat, incoming, is_group=False, avoid_texts=None, chat_meta=None):
    """system = 人设 + 硬性安全规则；user = 对方说了什么 + 是哪个会话。

    avoid_texts：2026-10-27 去重修复 —— 最近已发过的文本（禁止重复清单）。
    命中 duplicate-text 后 watcher 会带着这个清单重新生成，要求模型「换一种说法」。
    """
    system = (persona or DEFAULT_PERSONA).strip() + SAFETY_RULES
    if chat_meta:
        system = system + "\n\n" + str(chat_meta.get("line") or "") + "\n" + str(chat_meta.get("hint") or "")
    chat = (chat or "").strip() or "(未知会话)"
    incoming = (incoming or "").strip()
    if incoming:
        said = "【对方刚发来的内容】\n%s" % incoming
    else:
        said = ("【对方刚发来的内容】（没能读到具体文字，只知道对方发来了新消息；"
                "请只用一句自然的中性话回应，不要猜测/编造对方说了什么）")
    avoid = [str(x or "").strip() for x in (avoid_texts or []) if str(x or "").strip()]
    if avoid:
        lines = ["【禁止重复】下面这些回复最近刚在这个会话里发过，不要原样重复，"
                 "必须换一种说法（意思相近可以，措辞要不同，不要照抄、不要只改标点）："]
        for i, t in enumerate(avoid[:3], 1):
            lines.append("%d. %s" % (i, t))
        said = said + "\n\n" + "\n".join(lines)
    _tp = (str(chat_meta.get("line") or "") + "\n" + str(chat_meta.get("hint") or "") + "\n\n") if chat_meta else ""
    user = _tp + (
        "【这是哪个会话】%s%s\n"
        "%s\n\n"
        "请直接给出你要发出去的那一条微信回复（只输出正文）。"
    ) % (chat, "（群聊）" if is_group else "", said)
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def sanitize_reply(raw, max_chars=80):
    """清洗模型输出：去引号/前缀/换行，压成一行，截断到 max_chars 个字符。"""
    text = str(raw or "").strip()
    if not text:
        return ""
    # 去掉 markdown 代码块围栏
    text = re.sub(r"^```[a-zA-Z]*\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    text = text.strip()
    # 去掉常见前缀（「回复：」「小耳朵：」「我：」等）
    text = re.sub(r"^(回复|答|小耳朵|我|你)\s*[:：]\s*", "", text)
    # 去掉包裹引号（中英文）
    pairs = [("“", "”"), ("「", "」"), ("『", "』"), ('"', '"'), ("'", "'"), ("‘", "’")]
    for left, right in pairs:
        if len(text) >= 2 and text.startswith(left) and text.endswith(right):
            text = text[len(left):len(text) - len(right)].strip()
    # 换行/制表符压成空格（reply.py 纯键盘输入，避免换行被当成发送）
    text = re.sub(r"[\r\n\t]+", " ", text)
    text = re.sub(r"\s{2,}", " ", text).strip()
    if max_chars and len(text) > max_chars:
        text = text[:int(max_chars)].rstrip()
    return text


def unsafe_reason(text, forbid=None):
    """返回命中的禁用词（空串=安全）。"""
    low = str(text or "").lower()
    for word in (forbid or DEFAULT_FORBID):
        w = str(word or "").strip()
        if not w:
            continue
        if w.lower() in low:
            return w
    return ""


def _safe_err(exc, creds=None):
    """异常 -> 一行安全文本（apiKey 已 mask，截断 400 字）。"""
    msg = "%s: %s" % (type(exc).__name__, exc)
    key = str((creds or {}).get("apiKey") or "")
    if key:
        msg = msg.replace(key, "sk-***")
    return mask_key(msg)[:400]


def chat_completions(creds, messages, max_tokens=120, temperature=0.8, timeout=25.0):
    """POST {baseUrl}/chat/completions，返回 (content, usage, model)。"""
    key = str(creds.get("apiKey") or "")
    if not key:
        raise GenerationError("凭据缺少 apiKey")
    base = str(creds.get("baseUrl") or "https://api.deepseek.com").strip().rstrip("/")
    url = base if base.endswith("/chat/completions") else base + "/chat/completions"
    body = json.dumps({
        "model": str(creds.get("model") or "deepseek-chat"),
        "messages": messages,
        "max_tokens": int(max_tokens),
        "temperature": float(temperature),
        "stream": False,
    }, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST", headers={
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "Accept": "application/json",
    })
    try:
        with urllib.request.urlopen(req, timeout=float(timeout)) as resp:
            raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:
            pass
        raise GenerationError(mask_key("HTTP %s %s" % (exc.code, detail)))
    except urllib.error.URLError as exc:
        raise GenerationError(mask_key("网络不可达: %s" % exc.reason))
    except Exception as exc:
        raise GenerationError(_safe_err(exc, creds))
    try:
        doc = json.loads(raw)
    except Exception:
        raise GenerationError("API 返回不是合法 JSON: %s" % mask_key(raw[:200]))
    choices = doc.get("choices") or []
    if not choices:
        err = mask_key(json.dumps(doc.get("error") or doc, ensure_ascii=False)[:300])
        raise GenerationError("API 无 choices: %s" % err)
    content = ((choices[0] or {}).get("message") or {}).get("content") or ""
    return content, (doc.get("usage") or {}), str(doc.get("model") or "")


def generate(cfg, base_dir, chat, incoming, log_info=None, log_warn=None, log_error=None,
             avoid_texts=None, chat_meta=None):
    """生成一条回复。返回 dict：{"ok", "text", "model", "latencyMs", "usage", "attempts", "error"}。

    cfg = config.json 里的 gen 段（不是整份 config）。
    avoid_texts：禁止重复清单（最近已发过的文本）——用于 duplicate-text 后的「换说法」重生成。
    """
    li = log_info or (lambda m: _default_log("INFO", m))
    lw = log_warn or (lambda m: _default_log("WARN", m))
    le = log_error or (lambda m: _default_log("ERROR", m))
    cfg = cfg or {}
    t0 = time.time()
    max_tokens = max(16, min(120, int(cfg.get("maxTokens") or 120)))   # 硬上限 120
    temperature = float(cfg.get("temperature", 0.8) or 0.8)
    timeout = float(cfg.get("timeoutSeconds") or 25)
    max_chars = int(cfg.get("maxReplyChars") or 80)
    forbid = list(cfg.get("forbid") or DEFAULT_FORBID)

    cred_path = os.path.join(base_dir, str(cfg.get("credentials") or ".credentials.json"))
    persona_path = os.path.join(base_dir, str(cfg.get("personaFile") or "persona.txt"))
    try:
        creds = load_credentials(cred_path)
    except Exception as exc:
        msg = "%s ERROR 凭据读取失败: %s" % (GEN_TAG, mask_key(exc))
        le(msg)
        return {"ok": False, "error": "credentials: %s" % mask_key(exc),
                "latencyMs": int((time.time() - t0) * 1000)}
    persona = load_persona(persona_path)
    if not persona:
        lw("%s persona.txt 缺失或为空 -> 用内置兜底人设" % GEN_TAG)
    messages = build_messages(persona, chat, incoming,
                              is_group=bool(re.search(r"\(\d+\)\s*$", str(chat or ""))),
                              avoid_texts=avoid_texts, chat_meta=chat_meta)
    if avoid_texts:
        li("%s 本次生成带「禁止重复」清单 %d 条（duplicate-text 后换说法）: %r"
           % (GEN_TAG, len([x for x in avoid_texts if str(x or '').strip()]),
              [str(x)[:40] for x in (avoid_texts or [])][:3]))
    li("%s 请求 provider=%s model=%s baseUrl=%s max_tokens=%d temperature=%s maxReplyChars=%d apiKey=sk-***"
       % (GEN_TAG, creds.get("provider") or "-", creds.get("model"), creds.get("baseUrl"),
          max_tokens, temperature, max_chars))
    li("%s messages=%s" % (GEN_TAG, json.dumps(messages, ensure_ascii=False, indent=None)))

    text = ""
    blocked = ""
    attempts = 0
    usage = {}
    model = ""
    while attempts < 2:
        attempts += 1
        try:
            content, usage, model = chat_completions(creds, messages, max_tokens, temperature, timeout)
        except Exception as exc:
            msg = "%s ERROR %s" % (GEN_TAG, mask_key(exc))
            le(msg)
            return {"ok": False, "error": mask_key(exc), "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        text = sanitize_reply(content, max_chars)
        if not text:
            le("%s ERROR 模型返回空内容（第 %d 次）" % (GEN_TAG, attempts))
            return {"ok": False, "error": "empty-content", "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        bad = unsafe_reason(text, forbid)
        if not bad:
            break
        blocked = bad
        lw("%s BLOCK 第 %d 次生成命中禁用内容 %r -> 不发送，要求重写一次: text=%r"
           % (GEN_TAG, attempts, bad, text))
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "上一条回复包含被禁止的内容（%s）。请重写一条合规的简短回复，"
                                        "不要包含链接/转账/密码/验证码/密钥/文件等内容。" % bad},
        ]
    if unsafe_reason(text, forbid):
        le("%s ERROR 连续两次生成都不安全（命中 %r）-> 不发送" % (GEN_TAG, blocked))
        return {"ok": False, "error": "unsafe-text: %s" % blocked, "blocked": True,
                "attempts": attempts, "latencyMs": int((time.time() - t0) * 1000)}

    latency_ms = int((time.time() - t0) * 1000)
    li("%s 回复 text=%r latencyMs=%d attempts=%d model=%s usage=%s"
       % (GEN_TAG, text, latency_ms, attempts, model or "-",
          json.dumps(usage, ensure_ascii=False)))
    return {"ok": True, "text": text, "model": model, "latencyMs": latency_ms,
            "usage": usage, "attempts": attempts}


# ───────────────── 多模态（2026-10-27）：直接把聊天区截图发给模型「看图」生成回复 ─────────────────
# 用户已拍板：模型保持 deepseek-flash（不改 v4-pro）。OCR 只作参考，以图为准。

VISION_PROMPT_HEAD = (
    "【这是哪个会话】%s%s\n\n"
    "下面是微信聊天窗口的截图（已裁到聊天区最后几条消息）。"
    "请你自己从图里读懂对方最近说了什么，再回一条微信。\n"
    "注意：截图里左侧白色气泡是对方发的，右侧绿色气泡是你自己发的；"
    "如果截图里只有你自己发过的消息，就顺着你最后说的那句话自然接一条回复。\n"
)
VISION_PROMPT_OCR_REF = (
    "\n【OCR 参考文本（可能不准，仅供参考，以图为准）】\n%s\n"
)
VISION_PROMPT_TAIL = (
    "\n请直接给出你要发出去的那一条微信回复（只输出正文）。\n"
    "另外：如果这是给助手下的任务指令而不是普通聊天，请在回复正文前单独加一行 [TASK]"
    "（只加这一行标记，不要解释，也不要改变回复内容）；普通聊天不要加。\n"
    "[TASK] 只用于日志标注，不决定是否执行。"
)
VISION_UNSUPPORTED_MARKERS = (
    "unsupported image", "invalid image", "image not supported", "does not support image",
    "unsupported_image", "invalid_image", "vision is not supported", "multimodal",
)


def _vision_model_name(cfg, creds):
    """vision 模型名：cfg.visionModel 非空则覆盖；否则沿用凭据里的 model（deepseek-chat 实际=deepseek-flash）。"""
    vm = str((cfg or {}).get("visionModel") or "").strip()
    return vm or str((creds or {}).get("model") or "deepseek-chat")


def _encode_image_for_vision(path, max_edge=1600):
    """读图 -> 长边>max_edge 时等比缩小 -> PNG base64。

    返回 (b64:str, w:int, h:int, nbytes:int, origSize:(w0,h0))。
    """
    from io import BytesIO
    try:
        from PIL import Image
    except Exception as exc:   # pragma: no cover
        raise RuntimeError("PIL 不可用，无法读取截图: %s" % exc)
    if not path or not os.path.isfile(path):
        raise RuntimeError("图片不存在: %s" % path)
    img = Image.open(path)
    img.load()
    if img.mode not in ("RGB", "L"):
        img = img.convert("RGB")
    w0, h0 = img.size
    scale = 1.0
    limit = int(max_edge or 0)
    if limit > 0 and max(w0, h0) > limit:
        scale = float(limit) / float(max(w0, h0))
        nw, nh = max(1, int(round(w0 * scale))), max(1, int(round(h0 * scale)))
        img = img.resize((nw, nh), Image.LANCZOS)
    buf = BytesIO()
    img.save(buf, format="PNG")
    data = buf.getvalue()
    return (base64.b64encode(data).decode("ascii"),
            int(img.size[0]), int(img.size[1]), len(data), (int(w0), int(h0)))


def build_vision_messages(persona, chat, ocr_text, image_data_url,
                          is_group=False, avoid_texts=None, chat_meta=None):
    """多模态 messages：system=人设+安全规则；user=文字提示（含 OCR 参考）+ image_url。"""
    system = (persona or DEFAULT_PERSONA).strip() + SAFETY_RULES
    if chat_meta:
        system = system + "\n\n" + str(chat_meta.get("line") or "") + "\n" + str(chat_meta.get("hint") or "")
    chat = (chat or "").strip() or "(未知会话)"
    ref = str(ocr_text or "").strip()
    text = VISION_PROMPT_HEAD % (chat[:120], "（群聊）" if is_group else "")
    if chat_meta:
        system = system + "\n\n" + str(chat_meta.get("line") or "") + "\n" + str(chat_meta.get("hint") or "")
        text = str(chat_meta.get("line") or "") + "\n" + str(chat_meta.get("hint") or "") + "\n\n" + text
    if ref:
        text += VISION_PROMPT_OCR_REF % ref[:1500]
    avoid = [str(x or "").strip() for x in (avoid_texts or []) if str(x or "").strip()]
    if avoid:
        lines = ["\n【禁止重复】下面这些回复最近刚在这个会话里发过，不要原样重复，必须换一种说法："]
        for i, t in enumerate(avoid[:3], 1):
            lines.append("%d. %s" % (i, t))
        text += "\n".join(lines) + "\n"
    text += VISION_PROMPT_TAIL
    return [{"role": "system", "content": system},
            {"role": "user", "content": [
                {"type": "text", "text": text},
                {"type": "image_url", "image_url": {"url": image_data_url}},
            ]}]


def generate_with_image(cfg, base_dir, crop_path, chat, ocr_text,
                        log_info=None, log_warn=None, log_error=None, avoid_texts=None,
                        chat_meta=None):
    """看图生成回复（多模态）。失败返回 ok=False，error 前缀 vision-* 供调用方回退文本模式。

    cfg = config.json 的 gen 段；crop_path = reply.py --read-msg --crop-out 导出的聊天区截图。
    """
    li = log_info or (lambda m: _default_log("INFO", m))
    lw = log_warn or (lambda m: _default_log("WARN", m))
    le = log_error or (lambda m: _default_log("ERROR", m))
    cfg = cfg or {}
    t0 = time.time()
    max_tokens = max(16, min(120, int(cfg.get("maxTokens") or 120)))
    temperature = float(cfg.get("temperature", 0.8) or 0.8)
    timeout = float(cfg.get("timeoutSeconds") or 25)
    max_chars = int(cfg.get("maxReplyChars") or 80)
    forbid = list(cfg.get("forbid") or DEFAULT_FORBID)
    max_edge = int(cfg.get("visionMaxEdge") or 1600)
    if not crop_path or not os.path.isfile(str(crop_path)):
        le("%s vision ERROR 图片不存在: %r" % (GEN_TAG, crop_path))
        return {"ok": False, "error": "vision-image-missing: %r" % crop_path,
                "latencyMs": int((time.time() - t0) * 1000)}
    cred_path = os.path.join(base_dir, str(cfg.get("credentials") or ".credentials.json"))
    persona_path = os.path.join(base_dir, str(cfg.get("personaFile") or "persona.txt"))
    try:
        creds = load_credentials(cred_path)
    except Exception as exc:
        return {"ok": False, "error": "credentials: %s" % mask_key(exc),
                "latencyMs": int((time.time() - t0) * 1000)}
    persona = load_persona(persona_path)
    if not persona:
        lw("%s persona.txt 缺失或为空 -> 用内置兜底人设" % GEN_TAG)
    try:
        b64, w, h, nbytes, orig = _encode_image_for_vision(crop_path, max_edge=max_edge)
    except Exception as exc:
        le("%s vision ERROR 读图失败: %s" % (GEN_TAG, exc))
        return {"ok": False, "error": "vision-image-read: %s" % exc,
                "latencyMs": int((time.time() - t0) * 1000)}
    if min(w, h) < 64:
        le("%s vision ERROR 图片太小 %dx%d（模型要求短边>=64）" % (GEN_TAG, w, h))
        return {"ok": False, "error": "vision-image-too-small: %dx%d" % (w, h),
                "latencyMs": int((time.time() - t0) * 1000)}
    vmodel = _vision_model_name(cfg, creds)
    creds_v = dict(creds)
    creds_v["model"] = vmodel
    image_url = "data:image/png;base64," + b64
    messages = build_vision_messages(
        persona, chat, ocr_text, image_url,
        is_group=bool(re.search(r"\(\d+\)\s*$", str(chat or ""))),
        avoid_texts=avoid_texts, chat_meta=chat_meta)
    li("%s vision 请求 image=%s size=%dx%d orig=%dx%d bytes=%d model=%s max_tokens=%d "
       "temperature=%s timeout=%ss apiKey=sk-***"
       % (GEN_TAG, os.path.abspath(str(crop_path)), w, h, orig[0], orig[1], nbytes,
          vmodel, max_tokens, temperature, timeout))
    li("%s vision 提示词(OCR 参考=%d字): %r"
       % (GEN_TAG, len(str(ocr_text or "")), str(ocr_text or "")[:300]))
    text, blocked, attempts, usage, model = "", "", 0, {}, ""
    task_hint = False
    while attempts < 2:
        attempts += 1
        try:
            content, usage, model = chat_completions(creds_v, messages, max_tokens,
                                                     temperature, timeout)
        except Exception as exc:
            err = mask_key(str(exc))
            low = err.lower()
            unsupported = any(k in low for k in VISION_UNSUPPORTED_MARKERS)
            le("%s vision ERROR %s" % (GEN_TAG, err))
            return {"ok": False,
                    "error": ("vision-unsupported: " if unsupported else "vision-api-error: ") + err[:400],
                    "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        # 2026-10-27：[TASK] 标记只做记录，不驱动执行；从正文里剥掉，避免发给用户
        try:
            if re.search(r"\[TASK\]", str(content or ""), re.I):
                task_hint = True
        except Exception:
            pass
        try:
            content = re.sub(r"^\s*(?:\*\*)?\s*\[TASK\](?:\*\*)?\s*", "", str(content or ""),
                             flags=re.I)
        except Exception:
            pass
        text = sanitize_reply(content, max_chars)
        if not text:
            le("%s vision ERROR 模型返回空内容（第 %d 次）" % (GEN_TAG, attempts))
            return {"ok": False, "error": "vision-empty-content", "attempts": attempts,
                    "latencyMs": int((time.time() - t0) * 1000)}
        bad = unsafe_reason(text, forbid)
        if not bad:
            break
        blocked = bad
        lw("%s vision BLOCK 第 %d 次命中禁用内容 %r -> 要求重写一次"
           % (GEN_TAG, attempts, bad))
        messages = messages + [
            {"role": "assistant", "content": content},
            {"role": "user", "content": "上一条回复包含被禁止的内容（%s）。请重写一条合规的简短回复，"
                                        "不要包含链接/转账/密码/验证码/密钥/文件等内容。" % bad},
        ]
    if unsafe_reason(text, forbid):
        le("%s vision ERROR 连续两次生成都不安全（命中 %r）-> 不发送" % (GEN_TAG, blocked))
        return {"ok": False, "error": "vision-unsafe-text: %s" % blocked, "blocked": True,
                "attempts": attempts, "latencyMs": int((time.time() - t0) * 1000)}
    latency_ms = int((time.time() - t0) * 1000)
    li("%s vision mode: image=%s size=%dx%d bytes=%d model=%s usage=%s latencyMs=%d attempts=%d text=%r"
       % (GEN_TAG, os.path.abspath(str(crop_path)), w, h, nbytes, model or vmodel,
          json.dumps(usage, ensure_ascii=False), latency_ms, attempts, text))
    if task_hint:
        li("%s vision 判定：疑似任务指令（[TASK] 标记，仅记录，不驱动执行）" % GEN_TAG)
    return {"ok": True, "text": text, "model": model or vmodel,
            "latencyMs": latency_ms, "usage": usage, "attempts": attempts,
            "vision": True, "image": os.path.abspath(str(crop_path)),
            "imageSize": [w, h], "imageBytes": nbytes,
            "imageOrigSize": [orig[0], orig[1]], "taskHint": bool(task_hint)}