"""
Polymarket 跟單情報系統 - v2.0 完整版
整合：
- v1.5.2 階段3全面修復（持倉300、日均0.1-50、樣本≥1、改用/activity）
- v1.6 追蹤標籤系統（連續天數、90天紀錄、6種智能標籤）
"""

import os
import re
import sys
import json
import smtplib
import requests
import statistics
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
import time

# ====================================
# 設定區
# ====================================
DATA_API_BASE = "https://data-api.polymarket.com"
POLYMARKET_PROFILE_URL = "https://polymarket.com/profile/{wallet}"

# === 硬性篩選 ===
MIN_PNL_30D = 500
MIN_PNL_7D = -500
MAX_POSITIONS = 300
MAX_DAILY_TRADES = 50
MIN_DAILY_TRADES = 0.1
MAX_VIEWS = 2000
MAX_MEDIAN_TRADE_SIZE = 10000
MIN_SAMPLE_COUNT = 1

MIN_DISCRETENESS = 40
TOP_RANK_EXCLUDE = 30

TOTAL_CANDIDATES = 200
RECOMMENDATIONS_COUNT = 10
DIAGNOSTIC_MODE = True

# === 追蹤系統 ===
TRACKING_FILE = "tracking.json"
TRACKING_HISTORY_DAYS = 90

# === 智能標籤門檻 ===
TAG_VETERAN_DAYS = 7
TAG_REGULAR_30D_COUNT = 15
TAG_RISING_DAYS = 3
TAG_RETURN_GAP_DAYS = 7
TAG_FLASH_MAX_DAYS = 2

BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")

# ====================================
# 追蹤系統
# ====================================
def load_tracking():
    p = Path(TRACKING_FILE)
    if not p.exists():
        print("📋 未找到 tracking.json，建立全新追蹤紀錄")
        return {}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
        print(f"📋 已載入追蹤紀錄：共 {len(data)} 個錢包歷史")
        return data
    except Exception as e:
        print(f"⚠️ 讀取追蹤紀錄失敗，重建：{e}")
        return {}


def update_tracking(tracking, today_wallets, run_date):
    for w in today_wallets:
        wallet = w["proxyWallet"]
        username = w.get("userName", "") or ""
        if wallet not in tracking:
            tracking[wallet] = {
                "first_seen": run_date,
                "last_seen": run_date,
                "appearance_dates": [],
                "username_history": [],
            }
        if run_date not in tracking[wallet]["appearance_dates"]:
            tracking[wallet]["appearance_dates"].append(run_date)
        tracking[wallet]["last_seen"] = run_date
        if username and username not in tracking[wallet]["username_history"]:
            tracking[wallet]["username_history"].append(username)
    
    cutoff = (datetime.strptime(run_date, "%Y-%m-%d") -
              timedelta(days=TRACKING_HISTORY_DAYS)).strftime("%Y-%m-%d")
    cleaned = {}
    for wallet, info in tracking.items():
        recent = [d for d in info.get("appearance_dates", []) if d >= cutoff]
        if not recent:
            continue
        info["appearance_dates"] = recent
        cleaned[wallet] = info
    print(f"📋 追蹤紀錄更新後：{len(cleaned)} 個錢包（保留近 {TRACKING_HISTORY_DAYS} 天）")
    return cleaned


def save_tracking(tracking):
    Path(TRACKING_FILE).write_text(
        json.dumps(tracking, ensure_ascii=False, indent=2),
        encoding="utf-8"
    )
    print(f"💾 追蹤紀錄已儲存到 {TRACKING_FILE}")


def compute_tracking_stats(wallet_addr, tracking, run_date):
    """計算追蹤統計"""
    if wallet_addr not in tracking:
        return {
            "consecutive_days": 1, "total_30d_count": 1, "total_appearances": 1,
            "is_new": True, "is_returning": False, "days_since_last": 0,
        }
    dates = sorted(tracking[wallet_addr].get("appearance_dates", []))
    if not dates:
        return {
            "consecutive_days": 1, "total_30d_count": 1, "total_appearances": 1,
            "is_new": True, "is_returning": False, "days_since_last": 0,
        }
    today = datetime.strptime(run_date, "%Y-%m-%d")
    consecutive = 0
    check = today
    date_set = set(dates)
    while check.strftime("%Y-%m-%d") in date_set:
        consecutive += 1
        check -= timedelta(days=1)
    cutoff_30 = (today - timedelta(days=30)).strftime("%Y-%m-%d")
    count_30d = sum(1 for d in dates if d >= cutoff_30)
    is_returning = False
    days_since = 0
    if len(dates) >= 2 and dates[-1] == run_date:
        prev = [d for d in dates if d < run_date]
        if prev:
            last = datetime.strptime(prev[-1], "%Y-%m-%d")
            days_since = (today - last).days
            if days_since >= TAG_RETURN_GAP_DAYS:
                is_returning = True
    return {
        "consecutive_days": consecutive,
        "total_30d_count": count_30d,
        "total_appearances": len(dates),
        "is_new": len(dates) == 1 and dates[0] == run_date,
        "is_returning": is_returning,
        "days_since_last": days_since,
    }


def assign_tags(stats):
    """6 種標籤分配"""
    tags = []
    if stats["is_new"]:
        tags.append({"emoji": "🆕", "label": "新發現", "color": "#3b82f6"})
        return tags
    if stats["consecutive_days"] >= TAG_VETERAN_DAYS:
        tags.append({"emoji": "🏆", "label": "王牌穩定", "color": "#fbbf24"})
    if stats["total_30d_count"] >= TAG_REGULAR_30D_COUNT:
        tags.append({"emoji": "🌟", "label": "常客", "color": "#10b981"})
    if stats["is_returning"]:
        tags.append({"emoji": "🔥", "label": "重返", "color": "#f97316"})
    if TAG_RISING_DAYS <= stats["consecutive_days"] < TAG_VETERAN_DAYS:
        tags.append({"emoji": "📈", "label": "崛起中", "color": "#06b6d4"})
    if (stats["total_appearances"] <= TAG_FLASH_MAX_DAYS
            and stats["consecutive_days"] == 1 and not stats["is_new"]):
        tags.append({"emoji": "⚠️", "label": "曇花一現", "color": "#ef4444"})
    return tags

# ====================================
# 資料抓取
# ====================================
def fetch_leaderboard_paginated(time_period="MONTH"):
    print(f"📥 抓取 {time_period} 排行榜...")
    all_results = []
    offset = 0
    batch = 50
    while offset < TOTAL_CANDIDATES:
        url = f"{DATA_API_BASE}/v1/leaderboard"
        params = {"category": "OVERALL", "timePeriod": time_period,
                  "orderBy": "PNL", "limit": batch, "offset": offset}
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            all_results.extend(data)
            print(f"   ✓ 取得第 {offset+1}-{offset+len(data)} 名")
            if len(data) < batch:
                break
            offset += batch
            time.sleep(0.5)
        except Exception as e:
            print(f"   ✗ 第 {offset+1} 筆起錯誤: {e}")
            break
    print(f"   總計取得: {len(all_results)} 筆")
    return all_results


def fetch_positions(wallet):
    url = f"{DATA_API_BASE}/positions"
    params = {"user": wallet, "limit": 200, "sizeThreshold": 0.01}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return None


def fetch_recent_activity(wallet, days=7):
    """改用 /activity 端點，資料更完整"""
    url = f"{DATA_API_BASE}/activity"
    params = {"user": wallet, "limit": 500}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        data = r.json()
        if not isinstance(data, list):
            return []
        trades = [a for a in data if a.get("type") == "TRADE"]
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        return [t for t in trades if t.get("timestamp", 0) >= cutoff]
    except Exception:
        return []


def fetch_view_count(wallet):
    url = POLYMARKET_PROFILE_URL.format(wallet=wallet)
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        if r.status_code != 200:
            return -1
        html = r.text
        match = re.search(r'(\d+(?:\.\d+)?)\s*([KMB]?)\s*views?\b', html, re.IGNORECASE)
        if not match:
            match = re.search(r'(\d+(?:\.\d+)?)\s*([KMB]?)\s*次觀看', html)
        if not match:
            return -1
        num = float(match.group(1))
        s = match.group(2).upper()
        if s == "K":
            num *= 1_000
        elif s == "M":
            num *= 1_000_000
        elif s == "B":
            num *= 1_000_000_000
        return int(num)
    except Exception:
        return -1


def fetch_closed_positions(wallet):
    url = f"{DATA_API_BASE}/closed-positions"
    params = {"user": wallet, "limit": 100}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            data = r.json()
            return data if isinstance(data, list) else []
    except Exception:
        pass
    return []

# ====================================
# 計算指標
# ====================================
def compute_trade_stats(trades):
    sizes = []
    for t in trades:
        s = t.get("usdcSize", 0)
        try:
            s = float(s)
            if s > 0:
                sizes.append(s)
        except (TypeError, ValueError):
            continue
    if len(sizes) == 0:
        return (0, 0, 0)
    if len(sizes) == 1:
        return (sizes[0], 0, 1)
    median = statistics.median(sizes)
    mean = statistics.mean(sizes)
    stdev = statistics.stdev(sizes) if len(sizes) > 1 else 0
    cv = stdev / mean if mean > 0 else 999
    return (median, cv, len(sizes))


def compute_weighted_winrate(closed_positions):
    total = 0
    win = 0
    for p in closed_positions:
        try:
            inv = float(p.get("initialValue", 0) or 0)
            pnl = float(p.get("cashPnl", 0) or 0)
        except (TypeError, ValueError):
            continue
        if inv <= 0:
            continue
        total += inv
        if pnl > 0:
            win += inv
    if total <= 0:
        return 0
    return round(win / total * 100, 1)

# ====================================
# 篩選
# ====================================
def merge_leaderboards(lb_30d, lb_7d):
    merged = {}
    for i, e in enumerate(lb_30d, 1):
        wallet = e.get("proxyWallet")
        if not wallet:
            continue
        try:
            r = int(e.get("rank", i))
        except (ValueError, TypeError):
            r = i
        merged[wallet] = {
            "proxyWallet": wallet,
            "userName": e.get("userName", ""),
            "xUsername": e.get("xUsername", ""),
            "verified": e.get("verifiedBadge", False),
            "pnl_30d": float(e.get("pnl", 0) or 0),
            "rank_30d": r,
            "volume_30d": float(e.get("vol", 0) or 0),
            "pnl_7d": 0,
            "rank_7d": 999,
        }
    for i, e in enumerate(lb_7d, 1):
        wallet = e.get("proxyWallet")
        if not wallet:
            continue
        try:
            r7 = int(e.get("rank", i))
        except (ValueError, TypeError):
            r7 = i
        pnl7 = float(e.get("pnl", 0) or 0)
        if wallet not in merged:
            merged[wallet] = {
                "proxyWallet": wallet,
                "userName": e.get("userName", ""),
                "xUsername": e.get("xUsername", ""),
                "verified": e.get("verifiedBadge", False),
                "pnl_30d": 0,
                "rank_30d": 999,
                "volume_30d": float(e.get("vol", 0) or 0),
                "pnl_7d": pnl7,
                "rank_7d": r7,
            }
        else:
            merged[wallet]["pnl_7d"] = pnl7
            merged[wallet]["rank_7d"] = r7
    return list(merged.values())


def hard_filter(wallets):
    print(f"\n🔍 開始硬性篩選（共 {len(wallets)} 個候選）...")
    
    pre = []
    for w in wallets:
        if not w.get("proxyWallet"):
            continue
        if w.get("rank_30d", 999) <= TOP_RANK_EXCLUDE:
            continue
        if w.get("pnl_30d", 0) < MIN_PNL_30D:
            continue
        if w.get("pnl_7d", 0) <= MIN_PNL_7D:
            continue
        pre.append(w)
    print(f"   階段1 (PnL+排名): 剩 {len(pre)} 個")
    
    print(f"   階段2: 嘗試抓取觀看次數...")
    views_got = 0
    for i, w in enumerate(pre, 1):
        if i % 20 == 0 or i == len(pre):
            print(f"      進度 {i}/{len(pre)}...")
        v = fetch_view_count(w["proxyWallet"])
        if v >= 0:
            w["views"] = v
            views_got += 1
            if v > MAX_VIEWS:
                w["_filter_out_views"] = True
        else:
            w["views"] = -1
        time.sleep(0.3)
    pre2 = [w for w in pre if not w.get("_filter_out_views", False)]
    print(f"   階段2 (views ≤ {MAX_VIEWS}): 抓到 {views_got}/{len(pre)}，剩 {len(pre2)} 個")
    
    print(f"   階段3: 抓取持倉/交易/勝率...")
    filtered = []
    rej = {
        "positions_fail": 0,
        "no_recent_activity": 0,
        "daily_trades_out_of_range": 0,
        "sample_too_small": 0,
        "median_too_high": 0,
    }
    for i, w in enumerate(pre2, 1):
        addr = w["proxyWallet"]
        if i % 5 == 0 or i == len(pre2):
            print(f"      進度 {i}/{len(pre2)}...")
        
        positions = fetch_positions(addr)
        if positions is None:
            rej["positions_fail"] += 1
            continue
        pcount = len(positions)
        if pcount > MAX_POSITIONS:
            rej["positions_fail"] += 1
            continue
        
        recent = fetch_recent_activity(addr, days=7)
        if len(recent) < 1:
            rej["no_recent_activity"] += 1
            continue
        
        avg_daily = len(recent) / 7
        if avg_daily < MIN_DAILY_TRADES or avg_daily > MAX_DAILY_TRADES:
            rej["daily_trades_out_of_range"] += 1
            continue
        
        med, cv, sc = compute_trade_stats(recent)
        if sc < MIN_SAMPLE_COUNT:
            rej["sample_too_small"] += 1
            continue
        if med > MAX_MEDIAN_TRADE_SIZE:
            rej["median_too_high"] += 1
            continue
        
        closed = fetch_closed_positions(addr)
        wwr = compute_weighted_winrate(closed) if closed else 0
        
        w["current_positions"] = pcount
        w["trades_last_7d"] = len(recent)
        w["avg_daily_trades"] = round(avg_daily, 1)
        w["median_trade_size"] = round(med, 0)
        w["trade_cv"] = round(cv, 2)
        w["weighted_winrate"] = wwr
        w["closed_positions_count"] = len(closed)
        
        pnl_7d = w.get("pnl_7d", 0)
        pnl_30d = w.get("pnl_30d", 0)
        exp_7d = pnl_30d / 4
        w["pnl_acceleration"] = round(pnl_7d / max(exp_7d, 1), 2) if exp_7d > 0 else 0
        vol_30d = w.get("volume_30d", 1)
        w["roi_7d_estimate"] = round(pnl_7d / max(vol_30d / 4, 1) * 100, 2) if vol_30d > 0 else 0
        
        filtered.append(w)
        time.sleep(0.3)
    
    print(f"\n   ✓ 通過所有硬性篩選: {len(filtered)} 個")
    print(f"   📊 階段3 淘汰統計:")
    for k, v in rej.items():
        if v > 0:
            print(f"      - {k}: {v} 個")
    return filtered


def print_diagnostic_stats(wallets):
    if not wallets:
        return
    print("\n" + "=" * 60)
    print("📊 診斷報告：所有指標的真實分布")
    print("=" * 60)
    metrics = {
        "30D PnL ($)": [w.get("pnl_30d", 0) for w in wallets],
        "7D PnL ($)": [w.get("pnl_7d", 0) for w in wallets],
        "30D Volume ($)": [w.get("volume_30d", 0) for w in wallets],
        "排名": [w.get("rank_30d", 0) for w in wallets],
        "當前持倉數": [w.get("current_positions", 0) for w in wallets],
        "日均交易次數": [w.get("avg_daily_trades", 0) for w in wallets],
        "下注中位數 ($)": [w.get("median_trade_size", 0) for w in wallets],
        "下注 CV": [w.get("trade_cv", 0) for w in wallets],
        "金額加權勝率 (%)": [w.get("weighted_winrate", 0) for w in wallets],
        "已平倉部位數": [w.get("closed_positions_count", 0) for w in wallets],
        "獲利加速度 (x)": [w.get("pnl_acceleration", 0) for w in wallets],
    }
    views_data = [w.get("views", -1) for w in wallets]
    valid_views = [v for v in views_data if v >= 0]
    for name, vals in metrics.items():
        if not vals:
            continue
        sv = sorted(vals)
        n = len(sv)
        print(f"\n  {name}:")
        print(f"    最低: {min(sv):.1f}  | 25%: {sv[n//4]:.1f}  | 中位: {sv[n//2]:.1f}")
        print(f"    75%: {sv[3*n//4]:.1f}  | 90%: {sv[min(int(0.9*n), n-1)]:.1f}  | 最高: {max(sv):.1f}")
    print(f"\n  觀看次數:")
    if valid_views:
        n = len(valid_views)
        sv = sorted(valid_views)
        print(f"    成功抓到: {n}/{len(views_data)} 個")
        print(f"    最低: {min(sv)}  | 中位: {sv[n//2]}  | 最高: {max(sv)}")
    else:
        print(f"    全部抓不到")
    print("=" * 60)


def compute_discreteness_score(wallet):
    score = 0
    v = wallet.get("views", -1)
    if v >= 0:
        if v < 100:
            score += 40
        elif v < 300:
            score += 30
        elif v < 700:
            score += 20
        elif v < 2000:
            score += 10
    else:
        score += 20
    rank = wallet.get("rank_30d", 999)
    if rank > 100:
        score += 20
    elif rank > 50:
        score += 10
    if not wallet.get("xUsername"):
        score += 20
    elif not wallet.get("verified"):
        score += 10
    vol = wallet.get("volume_30d", 0)
    if vol < 100_000:
        score += 20
    elif vol < 500_000:
        score += 10
    return score


def compute_profit_score(wallet, all_wallets):
    p30 = [w.get("pnl_30d", 0) for w in all_wallets]
    p7 = [w.get("pnl_7d", 0) for w in all_wallets]
    rois = [w.get("roi_7d_estimate", 0) for w in all_wallets]
    m30 = max(p30) if p30 else 1
    m7 = max(p7) if p7 else 1
    mr = max(rois) if rois else 1
    n30 = (wallet.get("pnl_30d", 0) / m30) * 100 if m30 > 0 else 0
    n7 = (wallet.get("pnl_7d", 0) / m7) * 100 if m7 > 0 else 0
    nr = (wallet.get("roi_7d_estimate", 0) / mr) * 100 if mr > 0 else 0
    wr = wallet.get("weighted_winrate", 0)
    a = wallet.get("pnl_acceleration", 0)
    a_score = 100 if a >= 1.5 else (a / 1.5) * 100 if a > 0 else 0
    score = n30 * 0.25 + n7 * 0.25 + nr * 0.2 + wr * 0.15 + a_score * 0.15
    return round(max(0, min(100, score)), 1)


def generate_reasoning(wallet, tracking_stats):
    reasons, warnings = [], []
    
    # 追蹤相關理由
    if tracking_stats["consecutive_days"] >= 7:
        reasons.append(f"連續上榜 {tracking_stats['consecutive_days']} 天（王牌級）")
    elif tracking_stats["consecutive_days"] >= 3:
        reasons.append(f"連續上榜 {tracking_stats['consecutive_days']} 天")
    
    if tracking_stats["total_30d_count"] >= 15:
        reasons.append(f"30 天內上榜 {tracking_stats['total_30d_count']} 次（穩定常客）")
    
    if tracking_stats["is_returning"]:
        reasons.append(f"消失 {tracking_stats['days_since_last']} 天後重返")
    
    accel = wallet.get("pnl_acceleration", 0)
    if accel >= 1.5:
        reasons.append(f"獲利加速 {accel}x")
    
    v = wallet.get("views", -1)
    if v == -1:
        pass
    elif v < 100:
        reasons.append(f"極低調（{v} 次觀看）")
    elif v < 700:
        reasons.append(f"低調（{v} 次觀看）")
    
    if not wallet.get("xUsername"):
        reasons.append("完全匿名")
    
    wwr = wallet.get("weighted_winrate", 0)
    if wwr >= 70:
        reasons.append(f"加權勝率 {wwr}%")
    
    med = wallet.get("median_trade_size", 0)
    if med < 100:
        reasons.append(f"小額穩定（中位數 ${med:.0f}）")
    
    # 警示
    if tracking_stats["is_new"]:
        warnings.append("首次上榜，需觀察持續性")
    if tracking_stats["total_appearances"] <= 2 and not tracking_stats["is_new"]:
        warnings.append("過去出現次數少，可能曇花一現")
    if wallet.get("current_positions", 0) > 80:
        warnings.append(f"持倉 {wallet.get('current_positions')} 個過於分散")
    if wallet.get("avg_daily_trades", 0) > 20:
        warnings.append(f"日均交易 {wallet.get('avg_daily_trades')} 次偏頻繁")
    if wallet.get("avg_daily_trades", 0) < 1:
        warnings.append(f"交易頻率低（日均 {wallet.get('avg_daily_trades')}）")
    if med > 500:
        warnings.append(f"單筆中位數 ${med:.0f} 對 $300 資金偏大")
    if wallet.get("trade_cv", 0) > 2:
        warnings.append(f"下注金額波動大（CV={wallet.get('trade_cv')}）")
    if 0 < wwr < 50:
        warnings.append(f"加權勝率僅 {wwr}% 偏低")
    if wallet.get("closed_positions_count", 0) < 10:
        warnings.append("已平倉樣本少")
    if v >= 700 and v < 2000:
        warnings.append(f"觀看次數 {v} 偏高")
    if wallet.get("pnl_7d", 0) < 0:
        warnings.append(f"近 7 天虧損 ${abs(wallet.get('pnl_7d', 0)):.0f}")
    
    if not reasons:
        reasons.append("綜合指標通過篩選")
    if not warnings:
        warnings.append("各項指標健康，但任何跟單仍有風險")
    return "、".join(reasons), "、".join(warnings)


def get_tier(score):
    if score >= 80:
        return "tier-green"
    elif score >= 60:
        return "tier-yellow"
    return "tier-gray"

def format_money(n):
    if n >= 0:
        return f"+${n:,.0f}"
    return f"-${abs(n):,.0f}"

def format_views(n):
    if n < 0:
        return "未知"
    if n >= 1000:
        return f"{n/1000:.1f}K"
    return str(n)


def generate_html(recommendations, run_date, summary_extras=None):
    summary_extras = summary_extras or {}
    green = sum(1 for w in recommendations if w["combined_score"] >= 80)
    yellow = sum(1 for w in recommendations if 60 <= w["combined_score"] < 80)
    top_score = max((w["combined_score"] for w in recommendations), default=0)
    
    veteran_count = sum(1 for w in recommendations if w.get("_tracking", {}).get("consecutive_days", 0) >= 7)
    new_count = sum(1 for w in recommendations if w.get("_tracking", {}).get("is_new", False))
    
    cards_html = ""
    for i, w in enumerate(recommendations, 1):
        tier = get_tier(w["combined_score"])
        username = w.get("userName") or "未命名錢包"
        addr = w["proxyWallet"]
        url = f"https://polymarket.com/profile/{addr}"
        p30c = "positive" if w.get("pnl_30d", 0) > 0 else ""
        p7c = "positive" if w.get("pnl_7d", 0) > 0 else ""
        ac = "positive" if w.get("pnl_acceleration", 0) >= 1.2 else ""
        
        # 標籤 HTML
        tags = w.get("_tags", [])
        tags_html = ""
        for t in tags:
            tags_html += f'<span class="tag" style="background:{t["color"]}22; color:{t["color"]}; border:1px solid {t["color"]}">{t["emoji"]} {t["label"]}</span>'
        
        # 追蹤天數顯示
        ts = w.get("_tracking", {})
        consecutive = ts.get("consecutive_days", 1)
        total_30d = ts.get("total_30d_count", 1)
        total_all = ts.get("total_appearances", 1)
        
        if ts.get("is_new"):
            tracking_line = '<span style="color:#3b82f6">🆕 首次上榜</span>'
        else:
            tracking_line = (
                f'<span>連續 <strong style="color:#fbbf24">{consecutive}</strong> 天 · '
                f'過去 30 天 <strong>{total_30d}</strong> 次 · '
                f'累積 <strong>{total_all}</strong> 次</span>'
            )
        
        cards_html += f"""
<div class="wallet-card {tier}">
  <div class="card-header">
    <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
      <div class="rank-badge">{i}</div>
      <div>
        <div style="display:flex; align-items:center; gap:8px; flex-wrap:wrap;">
          <div class="username">{username}</div>
          <div class="tags">{tags_html}</div>
        </div>
        <div class="wallet-addr">{addr}</div>
      </div>
    </div>
    <a class="pm-link" href="{url}" target="_blank">前往 Polymarket →</a>
  </div>
  <div class="tracking-line">{tracking_line}</div>
  <div class="scores">
    <div class="score-box"><div class="score-label">綜合評分</div><div class="score-value combined">{w['combined_score']:.0f}</div></div>
    <div class="score-box"><div class="score-label">獲利分數</div><div class="score-value profit">{w['profit_score']:.0f}</div></div>
    <div class="score-box"><div class="score-label">低調分數</div><div class="score-value discreteness">{w['discreteness_score']:.0f}</div></div>
  </div>
  <div class="metrics-grid">
    <div class="metric"><div class="metric-label">30D PnL</div><div class="metric-value {p30c}">{format_money(w.get('pnl_30d', 0))}</div></div>
    <div class="metric"><div class="metric-label">7D PnL</div><div class="metric-value {p7c}">{format_money(w.get('pnl_7d', 0))}</div></div>
    <div class="metric"><div class="metric-label">觀看次數</div><div class="metric-value">{format_views(w.get('views', -1))}</div></div>
    <div class="metric"><div class="metric-label">當前持倉</div><div class="metric-value">{w.get('current_positions', 0)}</div></div>
    <div class="metric"><div class="metric-label">日均交易</div><div class="metric-value">{w.get('avg_daily_trades', 0)}</div></div>
    <div class="metric"><div class="metric-label">下注中位數</div><div class="metric-value">${w.get('median_trade_size', 0):.0f}</div></div>
    <div class="metric"><div class="metric-label">下注 CV</div><div class="metric-value">{w.get('trade_cv', 0)}</div></div>
    <div class="metric"><div class="metric-label">加權勝率</div><div class="metric-value">{w.get('weighted_winrate', 0)}%</div></div>
    <div class="metric"><div class="metric-label">獲利加速度</div><div class="metric-value {ac}">{w.get('pnl_acceleration', 0)}x</div></div>
  </div>
  <div class="reason">💡 {w['recommendation_reason']}</div>
  <div class="warning">⚠️ {w['risk_warning']}</div>
</div>
"""
    
    if not recommendations:
        cards_html = """
<div style="background: rgba(245, 158, 11, 0.1); border: 1px solid #f59e0b; padding: 24px; border-radius: 12px; text-align: center; color: #fbbf24;">
  <h3>今日無符合條件的錢包</h3>
  <p style="margin-top: 8px;">請查看 Actions log。</p>
</div>
"""
    
    html = f"""<!DOCTYPE html>
<html lang="zh-TW">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Polymarket 跟單情報 — {run_date}</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans TC", sans-serif; background: linear-gradient(135deg, #0f172a 0%, #1e293b 100%); color: #e2e8f0; min-height: 100vh; padding: 24px 16px; }}
  .container {{ max-width: 1100px; margin: 0 auto; }}
  header {{ text-align: center; margin-bottom: 32px; padding-bottom: 24px; border-bottom: 1px solid #334155; }}
  h1 {{ font-size: 28px; font-weight: 700; background: linear-gradient(90deg, #06b6d4, #8b5cf6); -webkit-background-clip: text; -webkit-text-fill-color: transparent; margin-bottom: 8px; }}
  .subtitle {{ color: #94a3b8; font-size: 14px; }}
  .summary-bar {{ display: grid; grid-template-columns: repeat(6, 1fr); gap: 12px; margin-bottom: 32px; }}
  .stat {{ background: rgba(30, 41, 59, 0.6); border: 1px solid #334155; border-radius: 12px; padding: 16px; text-align: center; }}
  .stat-label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
  .stat-value {{ font-size: 22px; font-weight: 700; color: #06b6d4; }}
  .wallet-card {{ background: rgba(30, 41, 59, 0.7); border: 2px solid #334155; border-radius: 16px; padding: 20px; margin-bottom: 20px; }}
  .wallet-card.tier-green {{ border-color: #10b981; box-shadow: 0 0 20px rgba(16, 185, 129, 0.15); }}
  .wallet-card.tier-yellow {{ border-color: #f59e0b; box-shadow: 0 0 20px rgba(245, 158, 11, 0.15); }}
  .wallet-card.tier-gray {{ border-color: #64748b; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 12px; flex-wrap: wrap; gap: 12px; }}
  .rank-badge {{ background: linear-gradient(135deg, #06b6d4, #8b5cf6); color: white; width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px; flex-shrink: 0; }}
  .username {{ font-size: 18px; font-weight: 600; color: #f1f5f9; }}
  .wallet-addr {{ font-size: 12px; color: #94a3b8; font-family: monospace; word-break: break-all; margin-top: 2px; }}
  .pm-link {{ background: #06b6d4; color: #0f172a; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 13px; white-space: nowrap; }}
  .tags {{ display: flex; flex-wrap: wrap; gap: 4px; }}
  .tag {{ font-size: 11px; padding: 3px 8px; border-radius: 12px; font-weight: 600; }}
  .tracking-line {{ font-size: 12px; color: #cbd5e1; padding: 8px 12px; background: rgba(15, 23, 42, 0.5); border-radius: 6px; margin-bottom: 12px; }}
  .scores {{ display: grid; grid-template-columns: repeat(3, 1fr); gap: 12px; margin-bottom: 16px; }}
  .score-box {{ background: rgba(15, 23, 42, 0.6); border-radius: 8px; padding: 12px; text-align: center; }}
  .score-label {{ font-size: 11px; color: #94a3b8; margin-bottom: 4px; }}
  .score-value {{ font-size: 20px; font-weight: 700; }}
  .score-value.combined {{ color: #8b5cf6; }}
  .score-value.profit {{ color: #10b981; }}
  .score-value.discreteness {{ color: #f59e0b; }}
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(120px, 1fr)); gap: 8px; margin-bottom: 16px; }}
  .metric {{ background: rgba(15, 23, 42, 0.4); padding: 8px 12px; border-radius: 6px; font-size: 13px; }}
  .metric-label {{ color: #94a3b8; font-size: 11px; }}
  .metric-value {{ color: #e2e8f0; font-weight: 600; }}
  .metric-value.positive {{ color: #10b981; }}
  .reason {{ background: rgba(6, 182, 212, 0.1); border-left: 3px solid #06b6d4; padding: 10px 14px; border-radius: 4px; margin-bottom: 10px; font-weight: 600; color: #f1f5f9; }}
  .warning {{ background: rgba(239, 68, 68, 0.1); border-left: 3px solid #ef4444; padding: 10px 14px; border-radius: 4px; color: #fca5a5; font-size: 13px; }}
  footer {{ margin-top: 48px; padding: 24px; background: rgba(239, 68, 68, 0.05); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 12px; text-align: center; font-size: 13px; color: #fca5a5; line-height: 1.6; }}
  .legend {{ background: rgba(30, 41, 59, 0.4); border: 1px solid #334155; border-radius: 8px; padding: 12px 16px; margin-bottom: 24px; font-size: 12px; color: #94a3b8; }}
  .legend strong {{ color: #e2e8f0; }}
  @media (max-width: 700px) {{ .summary-bar {{ grid-template-columns: repeat(3, 1fr); }} }}
  @media (max-width: 500px) {{ .summary-bar {{ grid-template-columns: repeat(2, 1fr); }} .scores {{ grid-template-columns: 1fr; }} h1 {{ font-size: 22px; }} }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🎯 Polymarket 跟單情報 v2.0</h1>
    <div class="subtitle">{run_date} · 含追蹤標籤系統</div>
  </header>
  <div class="summary-bar">
    <div class="stat"><div class="stat-label">推薦數</div><div class="stat-value">{len(recommendations)}</div></div>
    <div class="stat"><div class="stat-label">最高分</div><div class="stat-value">{top_score:.0f}</div></div>
    <div class="stat"><div class="stat-label">綠燈級</div><div class="stat-value">{green}</div></div>
    <div class="stat"><div class="stat-label">黃燈級</div><div class="stat-value">{yellow}</div></div>
    <div class="stat"><div class="stat-label">🏆 王牌</div><div class="stat-value">{veteran_count}</div></div>
    <div class="stat"><div class="stat-label">🆕 新發現</div><div class="stat-value">{new_count}</div></div>
  </div>
  <div class="legend">
    <strong>標籤說明：</strong> 🆕 新發現（首次上榜）｜📈 崛起中（連 3-6 天）｜🏆 王牌穩定（連 7+ 天）｜🌟 常客（30 天上榜 ≥15 次）｜🔥 重返（消失 7+ 天又回來）｜⚠️ 曇花一現（總共只上 1-2 次）
  </div>
  {cards_html}
  <footer>
    <strong>⚠️ 免責聲明</strong><br>
    本系統僅為情報參考工具。跟單交易具有高度風險，過去績效不代表未來表現。<br>
    建議單筆跟單金額不超過總資金 10–20%，並設定停損點。
  </footer>
</div>
</body>
</html>
"""
    return html


def send_email(html_content, run_date, count):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("⚠️ Email 設定不完整")
        return False
    print(f"📧 寄送 Email 給 {RECIPIENT_EMAIL}...")
    msg = EmailMessage()
    msg["Subject"] = f"Polymarket Daily Report - {run_date} ({count} picks)"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(f"Polymarket Daily Report\nDate: {run_date}\nRecommendations: {count}\n")
    msg.add_alternative(html_content, subtype="html")
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.send_message(msg)
        print("   ✓ Email 寄送成功")
        return True
    except Exception as e:
        print(f"   ✗ Email 寄送失敗: {e}")
        return False


def main():
    print("=" * 60)
    print("Polymarket 跟單情報系統 v2.0")
    print(f"執行時間: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # 載入歷史追蹤
    tracking = load_tracking()
    
    # 抓資料
    lb_30d = fetch_leaderboard_paginated("MONTH")
    time.sleep(1)
    lb_7d = fetch_leaderboard_paginated("WEEK")
    
    if not lb_30d and not lb_7d:
        print("❌ 無法取得排行榜資料")
        html = generate_html([], run_date)
        save_and_send(html, run_date, 0)
        sys.exit(1)
    
    candidates = merge_leaderboards(lb_30d, lb_7d)
    print(f"\n合併後共 {len(candidates)} 個候選")
    
    passed = hard_filter(candidates)
    
    if DIAGNOSTIC_MODE:
        print_diagnostic_stats(passed)
    
    if not passed:
        print("\n⚠️ 沒有錢包通過硬性篩選")
        html = generate_html([], run_date)
        save_and_send(html, run_date, 0)
        return
    
    print(f"\n📊 計算評分...")
    for w in passed:
        w["discreteness_score"] = compute_discreteness_score(w)
        w["profit_score"] = compute_profit_score(w, passed)
        w["combined_score"] = round(w["profit_score"] * 0.7 + w["discreteness_score"] * 0.3, 1)
    
    passed.sort(key=lambda x: x["combined_score"], reverse=True)
    final = [w for w in passed if w["discreteness_score"] >= MIN_DISCRETENESS][:RECOMMENDATIONS_COUNT]
    if len(final) < 3 and len(passed) >= 3:
        final = passed[:min(10, len(passed))]
    
    print(f"   ✓ 最終推薦: {len(final)} 個錢包")
    
    # 更新追蹤紀錄（用 final 名單）
    tracking = update_tracking(tracking, final, run_date)
    
    # 為每個 final 錢包加上追蹤統計、標籤、推薦理由
    print(f"\n🏷️  套用標籤與追蹤統計...")
    for w in final:
        ts = compute_tracking_stats(w["proxyWallet"], tracking, run_date)
        w["_tracking"] = ts
        w["_tags"] = assign_tags(ts)
        w["recommendation_reason"], w["risk_warning"] = generate_reasoning(w, ts)
    
    # 儲存追蹤紀錄
    save_tracking(tracking)
    
    html = generate_html(final, run_date)
    save_and_send(html, run_date, len(final))


def save_and_send(html, run_date, count):
    rd = Path("reports")
    rd.mkdir(exist_ok=True)
    rp = rd / f"report_{run_date}.html"
    rp.write_text(html, encoding="utf-8")
    print(f"\n💾 報告已存檔: {rp}")
    (rd / "latest.html").write_text(html, encoding="utf-8")
    send_email(html, run_date, count)
    print("\n" + "=" * 60)
    print("✅ 執行完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
