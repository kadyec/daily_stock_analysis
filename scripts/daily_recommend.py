#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
每日智能选股推荐：全市场选股 -> LLM 精选 -> 深度分析 -> 推送

用法:
    python scripts/daily_recommend.py [--strategy momentum_quality] [--top 3]
                                      [--screen-only] [--no-notify] [--no-analyze]
"""
import argparse
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("daily_recommend")


def main() -> int:
    parser = argparse.ArgumentParser(description="每日智能选股推荐")
    parser.add_argument("--strategy", default="momentum_quality",
                        help="选股策略（对应 src/services/screening/strategies/*.yaml）")
    parser.add_argument("--market", default="cn", choices=["cn", "us"], help="市场范围")
    parser.add_argument("--top", type=int, default=3, help="推荐股票数量")
    parser.add_argument("--screen-only", action="store_true", help="只选股，不做深度分析")
    parser.add_argument("--no-analyze", action="store_true", help="跳过深度分析")
    parser.add_argument("--no-notify", action="store_true", help="不发送推送通知")
    args = parser.parse_args()

    from src.config import Config
    from src.config import get_config
    config = get_config()

    # ---------- 1. 全市场选股 ----------
    from src.services.screening_service import ScreeningService

    def on_progress(pct: int, stage: str) -> None:
        logger.info("[选股 %3d%%] %s", pct, stage)

    logger.info("开始全市场选股: strategy=%s market=%s top=%d", args.strategy, args.market, args.top)
    service = ScreeningService(config)
    result = service.screen(
        strategy=args.strategy,
        market=args.market,
        max_results=args.top,
        progress_callback=on_progress,
    )

    candidates = result.get("candidates") or []
    if not candidates:
        logger.error("选股无结果: %s", {k: result.get(k) for k in ("error", "message", "degradation", "source_errors")})
        return 1

    def _g(item, *keys, default=None):
        for k in keys:
            v = item.get(k)
            if v is not None and v != "":
                return v
        raw = item.get("raw") if isinstance(item.get("raw"), dict) else {}
        for k in keys:
            v = raw.get(k)
            if v is not None and v != "":
                return v
        return default

    logger.info("选股完成: 推荐 %d 只", len(candidates))
    lines = [f"🏆 今日智能选股推荐（策略: {args.strategy}）", ""]
    for i, c in enumerate(candidates[:args.top], 1):
        code = str(_g(c, "code", "symbol", default=""))
        name = str(_g(c, "name", "stock_name", default=""))
        score = _g(c, "final_score", "screen_score", "score", default=0) or 0
        price = _g(c, "price", "latest_price", default=0) or 0
        chg = _g(c, "change_pct", default=0) or 0
        industry = _g(c, "industry", default="-")
        reason = _g(c, "reason", "ranking_reason", default="")
        logger.info("  #%d %s(%s) 得分=%s 现价=%s 涨跌=%s%% 行业=%s", i, name, code, score, price, chg, industry)
        lines.append(f"{i}. {name} ({code}) 得分 {float(score):.1f} 现价 {float(price):.2f} "
                     f"涨跌 {float(chg):+.2f}% 行业 {industry}" + (f"｜{reason}" if reason else ""))
    market_view = result.get("llm_market_view")
    risk_view = result.get("llm_portfolio_risk")
    if market_view:
        lines += ["", "📈 LLM 市场观点: " + str(market_view)]
    if risk_view:
        lines += ["⚠️ 组合风险提示: " + str(risk_view)]
    recommend_summary = "\n".join(lines)
    print("\n" + recommend_summary + "\n")

    # ---------- 2. 初始化通知渠道（分析阶段保持静默，最后统一推送精简卡片） ----------
    notifier = None
    push = None
    if not args.no_notify:
        from src.notification import NotificationService
        notifier = NotificationService()
        if notifier.is_available():
            push = notifier
        else:
            logger.warning("通知渠道不可用，跳过推送")

    if args.screen_only or args.no_analyze:
        if push:
            push.send(recommend_summary, route_type="report")
            logger.info("推荐摘要已推送")
        return 0

    # ---------- 3. 对推荐股做深度分析（静默，不推送单股长报告） ----------
    codes = [str(_g(c, "code", "symbol", default="")) for c in candidates[:args.top]]
    logger.info("开始深度分析 %d 只推荐股: %s", len(codes), ",".join(codes))
    from src.services.analyzer_service import analyze_stocks
    results = analyze_stocks(codes, config=config, notifier=None)
    logger.info("深度分析完成: 成功 %d/%d", len(results), len(codes))
    by_code = {r.code: r for r in results}
    for r in results:
        logger.info("  %s(%s) 评分=%s 建议=%s", r.name, r.code,
                    getattr(r, "sentiment_score", "-"), getattr(r, "operation_advice", "-"))

    # ---------- 4. 推送精简推荐卡片 ----------
    def _clip(text, limit):
        text = " ".join(str(text or "").split())
        return text if len(text) <= limit else text[: limit - 1] + "…"

    emoji = ["1️⃣", "2️⃣", "3️⃣", "4️⃣", "5️⃣"]
    card = [f"🏆 今日推荐（策略 {args.strategy}）", ""]
    for i, c in enumerate(candidates[:args.top], 1):
        code = str(_g(c, "code", "symbol", default=""))
        name = str(_g(c, "name", "stock_name", default=""))
        r = by_code.get(code)
        icon = emoji[i - 1] if i <= len(emoji) else f"{i}."
        if r is not None and getattr(r, "success", True):
            card.append(f"{icon} {name} {code} ｜评分 {r.sentiment_score} ｜{r.operation_advice}")
            price = getattr(r, "current_price", None)
            chg = getattr(r, "change_pct", None)
            if price:
                card.append(f"💰 现价 {price}（{chg:+.2f}%）" if chg is not None else f"💰 现价 {price}")
            reason = _clip(getattr(r, "buy_reason", "") or getattr(r, "analysis_summary", ""), 70)
            if reason:
                card.append(f"📌 {reason}")
            risk = _clip(getattr(r, "risk_warning", ""), 45)
            if risk:
                card.append(f"⚠️ {risk}")
        else:
            score = _g(c, "final_score", "screen_score", "score", default=0) or 0
            price = _g(c, "price", "latest_price", default=0) or 0
            chg = _g(c, "change_pct", default=0) or 0
            card.append(f"{icon} {name} {code} ｜选股分 {float(score):.1f}")
            card.append(f"💰 现价 {float(price):.2f}（{float(chg):+.2f}%）")
            reason = _clip(_g(c, "reason", "ranking_reason", default=""), 70)
            if reason:
                card.append(f"📌 {reason}")
        card.append("")
    if market_view:
        card.append("📈 市场观点: " + _clip(market_view, 90))
    if risk_view:
        card.append("⚠️ 组合风险: " + _clip(risk_view, 90))
    card.append("AI生成，仅供参考，不构成投资建议")
    card_text = "\n".join(card)
    print("\n" + card_text + "\n")
    if push:
        push.send(card_text, route_type="report")
        logger.info("精简推荐卡片已推送")
    return 0


if __name__ == "__main__":
    sys.exit(main())
