#!/usr/bin/env python3
"""
小夏视频流水线 1号线 — OpenRouter 调用示例
文件：openrouter-example.py
日期：2026-11-09（设计版，未部署、未联网实测）

只走免费模型，做标题 / 文案 / 分镜文案。
不做图片生成，不调付费模型。

用法：
    export OPENROUTER_API_KEY="YOUR_API_KEY"   # 占位，换成你自己的 key
    python3 openrouter-example.py --episode Ep7 --platform xiaohongshu

    # 只看还剩多少免费次数（不耗次数）：
    python3 openrouter-example.py --check-only

前提（待 Chris 确认，见 DESIGN.md §0）：
  - 是否已有 OpenRouter 账号、是否已有 API key
  - key 存在哪里：只放本地 .env 或环境变量，绝不写进代码、绝不提交到 Git

免费档（2026-11-09 核查，见 DESIGN.md §1）：
  - :free 模型按 token $0，按请求次数限流：
    20 次/分钟；每天 50 次（从未买过 credits），
    一次性买过 >= $10 credits 后每天 1,000 次（UTC 午夜重置）。
  - $10 充值是付费行为，零付费最高优先级下默认不做。
    本示例按 50 次/天设计：一集文案控制在 3 次调用以内。
  - 免费模型名单会轮换，不要写死某一个 :free 模型。
    本示例默认用 openrouter/free 路由，让 OpenRouter 自己选
    当前可用的免费模型，并在返回里记录实际用的模型。

输出：
  - 打印标题、正文、标签三段，并打印实际模型、token 数、
    当天剩余次数（/key 接口）。
  - 不自动写入 Supabase。本设计要求：人看过、改过、确认后，
    再手动把采用版写入 copy 表（is_current=true）。
"""

import argparse
import json
import os
import sys
import urllib.request
import urllib.error

BASE_URL = "https://openrouter.ai/api/v1"
# 默认走免费路由，不写死某个 :free 模型（名单会轮换）。
# 如需指定，可改成如 "nvidia/nemotron-3-ultra-550b-a55b:free"，
# 但部署前必须先在 openrouter.ai/models 上确认该模型仍为 :free。
DEFAULT_MODEL = "openrouter/free"

# 小夏人物锁死（A版），写进系统提示词，防止模型改人设。
# 禁栗宝；猫为灰色身体、四只白爪、无心形白毛。
SYSTEM_PROMPT = (
    "你是小夏系列的文案助手。风格：吉卜力手绘治愈、温柔、克制，"
    "不用夸张网红词，不编造剧情。人物锁死：小夏为栗色短发、"
    "黄色雏菊发夹、浅黄色上衣加橄榄绿背带裙；奶奶银灰短卷发、"
    "碎花衫；爷爷银灰短发、圆眼镜；无名灰色小猫，灰色身体、"
    "四只白爪、毛茸茸小巧，无心形白毛、无其他装饰斑纹。"
    "本系列不再出现栗宝。只输出中文。"
)

# 一集文案 = 1 次调用（标题+正文+标签一次出完），省次数。
# 不要按标题、正文、标签拆成 3 次调用。
USER_TEMPLATE = """为小夏系列一集写小红书文案。

集号：{episode}
集名：{title}
一句话梗概：{synopsis}
分镜要点：{shots}

要求：
1. 标题 1 条，20 字以内，温柔、不标题党。
2. 正文 80-120 字，分 2-3 段，结尾加 1 个提问。
3. 标签 5 个，以 # 开头。
4. 只输出 JSON，格式：{{"title": "...", "body": "...", "tags": ["#...", "..."]}}
不要输出 JSON 以外的解释。
"""


def _headers(api_key: str) -> dict:
    return {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        # OpenRouter 建议的来源头，可空；有自己的站再填真实域名。
        "HTTP-Referer": "https://example.invalid/xiaoxia-pipeline",
        "X-Title": "xiaoxia-pipeline-design",
    }


def check_quota(api_key: str) -> dict:
    """查当天免费剩余次数（GET /key，不耗调用次数）。"""
    req = urllib.request.Request(
        f"{BASE_URL}/key", headers=_headers(api_key), method="GET"
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate_copy(api_key: str, model: str, episode: str, title: str,
                  synopsis: str, shots: str) -> dict:
    """1 次调用出标题+正文+标签。"""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": SYSTEM_PROMPT},
            {
                "role": "user",
                "content": USER_TEMPLATE.format(
                    episode=episode, title=title,
                    synopsis=synopsis, shots=shots,
                ),
            },
        ],
        "temperature": 0.7,
        "max_tokens": 800,
    }
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        f"{BASE_URL}/chat/completions",
        data=data, headers=_headers(api_key), method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        print(f"HTTP {e.code}：{body}", file=sys.stderr)
        if e.code == 429:
            print("提示：429 一般是免费次数用完（50 次/天）或 20 次/分钟限流，"
                  "等 UTC 午夜重置，或降低调用频率。", file=sys.stderr)
        if e.code == 402:
            print("提示：402 表示余额为负，免费模型也会被停用。先处理余额，"
                  "不要直接改调付费模型。", file=sys.stderr)
        raise


def main() -> int:
    parser = argparse.ArgumentParser(description="小夏文案：OpenRouter 免费模型示例")
    parser.add_argument("--episode", default="Ep7")
    parser.add_argument("--platform", default="xiaohongshu")
    parser.add_argument("--title", default="奶奶的番茄汤")
    parser.add_argument("--synopsis", default="小夏和奶奶在菜园摘番茄，一起做一锅番茄汤。")
    parser.add_argument("--shots", default="1 摘番茄；2 洗番茄；3 切番茄；4 炖汤；5 端汤；6 小猫闻香。")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--check-only", action="store_true",
                        help="只查剩余次数，不调用生成")
    args = parser.parse_args()

    api_key = os.environ.get("OPENROUTER_API_KEY", "")
    if not api_key or api_key == "YOUR_API_KEY":
        print("未设置 OPENROUTER_API_KEY。", file=sys.stderr)
        print('请先：export OPENROUTER_API_KEY="YOUR_API_KEY"  # 换成你自己的 key',
              file=sys.stderr)
        print("key 只放环境变量，不要写进代码、不要发给别人。", file=sys.stderr)
        return 2

    # 先查剩余额度，再决定要不要花这 1 次。
    try:
        quota = check_quota(api_key)
    except Exception as e:  # noqa: BLE001 — 示例：查不到就提示，不直接退出
        print(f"查剩余额度失败：{e}（可能是网络问题，继续前请先确认）",
              file=sys.stderr)
        quota = {}
    if quota:
        free = quota.get("data", {}).get("free_model_daily_requests") or quota.get("free_model_daily_requests") or {}
        print(f"免费次数：已用 {free.get('used')} / 上限 {free.get('limit')}，"
              f"剩余 {free.get('remaining')}（UTC 日）")

    if args.check_only:
        return 0

    result = generate_copy(api_key, args.model, args.episode, args.title,
                           args.synopsis, args.shots)
    choice = result["choices"][0]["message"]["content"]
    usage = result.get("usage", {})
    model_used = result.get("model", args.model)
    print(f"\n实际模型：{model_used}")
    print(f"token：prompt {usage.get('prompt_tokens')} / "
          f"completion {usage.get('completion_tokens')}")
    print(f"集：{args.episode} 平台：{args.platform}\n")
    try:
        parsed = json.loads(choice)
        print("标题：", parsed.get("title"))
        print("正文：\n", parsed.get("body"))
        print("标签：", " ".join(parsed.get("tags", [])))
    except json.JSONDecodeError:
        print("模型未按 JSON 返回，原文如下（请人工整理后写入 copy 表）：")
        print(choice)
    print("\n注意：以上为模型草稿，未自动写入 Supabase。"
          "请人工审改后，再把采用版写入 copy 表（is_current=true，"
          "并记录 model_used 与 token 数）。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
