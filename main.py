"""
Polymarket 跟單情報系統 - v1.4 診斷版
變更：
- 移除 CV 篩選（暫時不要求下注金額穩定性）
- 單筆中位數放寬到 $10,000
- 觀看次數仍嘗試抓取，但抓不到時不篩除（先記錄）
- 新增「診斷模式」：把所有通過硬篩的錢包都印出，附上所有指標分布
"""

import os
import re
import sys
import smtplib
import requests
import statistics
from datetime import datetime, timezone, timedelta
from email.message import EmailMessage
from pathlib import Path
import time

# ====================================
# 設定區（可修改）
# ====================================
DATA_API_BASE = "https://data-api.polymarket.com"
POLYMARKET_PROFILE_URL = "https://polymarket.com/profile/{wallet}"

# === 硬性篩選 ===
MIN_PNL_30D = 1000              # 30天最低累積獲利 ($)
MIN_PNL_7D = 0                  # 7天 PnL 必須 > 0
MAX_POSITIONS = 150              # 最大同時持倉數
MAX_DAILY_TRADES = 50            # 每日最大交易次數
MIN_DAILY_TRADES = 1             # 每日最低交易次數
MAX_VIEWS = 700                  # 觀看次數上限（抓不到時不篩除）
MAX_MEDIAN_TRADE_SIZE = 10000    # 單筆下注中位數上限 ($) ← 從 1000 放寬到 10000

# === 評分用 ===
MIN_DISCRETENESS = 60            # 低調分數門檻
TOP_RANK_EXCLUDE = 50            # 排除 Top N 名

# === 抓取設定 ===
TOTAL_CANDIDATES = 200           # 想抓的候選總數
RECOMMENDATIONS_COUNT = 10       # 每天推薦幾個

# === 診斷模式 ===
DIAGNOSTIC_MODE = True           # True 時：通過硬篩的全列出，且印出統計分布

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
# 資料抓取
# ====================================
def fetch_leaderboard_paginated(time_period="MONTH"):
    print(f"📥 抓取 {time_period} 排行榜...")
    all_results = []
    offset = 0
    batch_size = 50
    while offset < TOTAL_CANDIDATES:
        url = f"{DATA_API_BASE}/v1/leaderboard"
        params = {
            "category": "OVERALL", "timePeriod": time_period,
            "orderBy": "PNL", "limit": batch_size, "offset": offset
        }
        try:
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
            if not data:
                break
            all_results.extend(data)
            print(f"   ✓ 取得第 {offset+1}-{offset+len(data)} 名")
            if len(data) < batch_size:
                break
            offset += batch_size
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


def fetch_recent_trades(wallet, days=7):
    url = f"{DATA_API_BASE}/trades"
    params = {"user": wallet, "limit": 500, "takerOnly": False}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        trades = r.json()
        if not isinstance(trades, list):
            return []
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        return [t for t in trades if t.get("timestamp", 0) >= cutoff]
    except Exception:
        return []


def fetch_view_count(wallet):
    """嘗試抓觀看次數。Polymarket 用 React 動態渲染，靜態 HTML 通常抓不到。
    抓到回傳整數，抓不到回傳 -1（會被當作「未知」處理，不篩除）
    """
    url = POLYMARKET_PROFILE_URL.format(wallet=wallet)
    try:
        r = requests.get(url, headers=BROWSER_HEADERS, timeout=20)
        if r.status_code != 200:
            return -1
        html = r.text
        # 英文版
        match = re.search(r'(\d+(?:\.\d+)?)\s*([KMB]?)\s*views?\b', html, re.IGNORECASE)
        # 中文版
        if not match:
            match = re.search(r'(\d+(?:\.\d+)?)\s*([KMB]?)\s*次觀看', html)
        if not match:
            return -1
        num = float(match.group(1))
        suffix = match.group(2).upper()
        if suffix == "K":
            num *= 1_000
        elif suffix == "M":
            num *= 1_000_000
        elif suffix == "B":
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
    """計算下注金額的中位數、變異係數
    回傳：(median, cv, count)
    """
    sizes = []
    for t in trades:
        s = t.get("usdcSize", 0)
        try:
            s = float(s)
            if s > 0:
                sizes.append(s)
        except (TypeError, ValueError):
            continue
    if len(sizes) < 2:
        return (0, 0, len(sizes))
    median = statistics.median(sizes)
    mean = statistics.mean(sizes)
    stdev = statistics.stdev(sizes) if len(sizes) > 1 else 0
    cv = stdev / mean if mean > 0 else 999
    return (median, cv, len(sizes))


def compute_weighted_winrate(closed_positions):
    total_invested = 0
    winning_invested = 0
    for p in closed_positions:
        try:
            invested = float(p.get("initialValue", 0) or 0)
            pnl = float(p.get("cashPnl", 0) or 0)
        except (TypeError, ValueError):
            continue
        if invested <= 0:
            continue
        total_invested += invested
        if pnl > 0:
            winning_invested += invested
    if total_invested <= 0:
        return 0
    return round(winning_invested / total_invested * 100, 1)

# ====================================
# 篩選
# ====================================
def merge_leaderboards(lb_30d, lb_7d):
    merged = {}
    for i, entry in enumerate(lb_30d, 1):
        wallet = entry.get("proxyWallet")
        if not wallet:
            continue
        try:
            rank_val = int(entry.get("rank", i))
        except (ValueError, TypeError):
            rank_val = i
        merged[wallet] = {
            "proxyWallet": wallet,
            "userName": entry.get("userName", ""),
            "xUsername": entry.get("xUsername", ""),
            "verified": entry.get("verifiedBadge", False),
            "pnl_30d": float(entry.get("pnl", 0) or 0),
            "rank_30d": rank_val,
            "volume_30d": float(entry.get("vol", 0) or 0),
            "pnl_7d": 0,
            "rank_7d": 999,
        }
    for i, entry in enumerate(lb_7d, 1):
        wallet = entry.get("proxyWallet")
        if not wallet:
            continue
        try:
            rank_7d_val = int(entry.get("rank", i))
        except (ValueError, TypeError):
            rank_7d_val = i
        pnl_7d = float(entry.get("pnl", 0) or 0)
        if wallet not in merged:
            merged[wallet] = {
                "proxyWallet": wallet,
                "userName": entry.get("userName", ""),
                "xUsername": entry.get("xUsername", ""),
                "verified": entry.get("verifiedBadge", False),
                "pnl_30d": 0,
                "rank_30d": 999,
                "volume_30d": float(entry.get("vol", 0) or 0),
                "pnl_7d": pnl_7d,
                "rank_7d": rank_7d_val,
            }
        else:
            merged[wallet]["pnl_7d"] = pnl_7d
            merged[wallet]["rank_7d"] = rank_7d_val
    return list(merged.values())


def hard_filter(wallets):
    print(f"\n🔍 開始硬性篩選（共 {len(wallets)} 個候選）...")
    
    # 階段 1：用排行榜資料先篩
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
    print(f"   階段1 (PnL+排名+7D PnL>0): 剩 {len(pre)} 個")
    
    # 階段 2：嘗試抓觀看次數（抓不到不篩除）
    print(f"   階段2: 嘗試抓取觀看次數（抓不到不影響篩選）...")
    views_got_count = 0
    for i, w in enumerate(pre, 1):
        if i % 20 == 0 or i == len(pre):
            print(f"      進度 {i}/{len(pre)}...")
        views = fetch_view_count(w["proxyWallet"])
        if views >= 0:
            w["views"] = views
            views_got_count += 1
            # 只有抓到時才篩
            if views > MAX_VIEWS:
                w["_filter_out_views"] = True
        else:
            w["views"] = -1  # 抓不到
        time.sleep(0.3)
    
    pre2 = [w for w in pre if not w.get("_filter_out_views", False)]
    print(f"   階段2 (views ≤ {MAX_VIEWS}): 抓到 {views_got_count}/{len(pre)} 個觀看數，剩 {len(pre2)} 個")
    
    # 階段 3：抓持倉、交易、勝率（不再篩 CV）
    print(f"   階段3: 抓取持倉/交易/勝率...")
    filtered = []
    rejection_stats = {
        "positions_fail": 0,
        "no_recent_trades": 0,
        "daily_trades_out_of_range": 0,
        "sample_too_small": 0,
        "median_too_high": 0,
    }
    
    for i, w in enumerate(pre2, 1):
        wallet_addr = w["proxyWallet"]
        if i % 5 == 0 or i == len(pre2):
            print(f"      進度 {i}/{len(pre2)}...")
        
        positions = fetch_positions(wallet_addr)
        if positions is None:
            rejection_stats["positions_fail"] += 1
            continue
        positions_count = len(positions)
        if positions_count > MAX_POSITIONS:
            rejection_stats["positions_fail"] += 1
            continue
        
        recent = fetch_recent_trades(wallet_addr, days=7)
        if len(recent) < 1:
            rejection_stats["no_recent_trades"] += 1
            continue
        avg_daily = len(recent) / 7
        if avg_daily < MIN_DAILY_TRADES or avg_daily > MAX_DAILY_TRADES:
            rejection_stats["daily_trades_out_of_range"] += 1
            continue
        
        median_size, cv, sample_count = compute_trade_stats(recent)
        if sample_count < 5:
            rejection_stats["sample_too_small"] += 1
            continue
        if median_size > MAX_MEDIAN_TRADE_SIZE:
            rejection_stats["median_too_high"] += 1
            continue
        # 注意：CV 不再做為篩選條件
        
        closed = fetch_closed_positions(wallet_addr)
        weighted_winrate = compute_weighted_winrate(closed) if closed else 0
        
        w["current_positions"] = positions_count
        w["trades_last_7d"] = len(recent)
        w["avg_daily_trades"] = round(avg_daily, 1)
        w["median_trade_size"] = round(median_size, 0)
        w["trade_cv"] = round(cv, 2)
        w["weighted_winrate"] = weighted_winrate
        w["closed_positions_count"] = len(closed)
        
        pnl_7d = w.get("pnl_7d", 0)
        pnl_30d = w.get("pnl_30d", 0)
        expected_7d = pnl_30d / 4
        w["pnl_acceleration"] = round(pnl_7d / max(expected_7d, 1), 2) if expected_7d > 0 else 0
        
        vol_30d = w.get("volume_30d", 1)
        w["roi_7d_estimate"] = round(pnl_7d / max(vol_30d / 4, 1) * 100, 2) if vol_30d > 0 else 0
        
        filtered.append(w)
        time.sleep(0.3)
    
    print(f"\n   ✓ 通過所有硬性篩選: {len(filtered)} 個")
    print(f"   📊 階段3 淘汰統計:")
    for reason, count in rejection_stats.items():
        if count > 0:
            print(f"      - {reason}: {count} 個")
    
    return filtered


def print_diagnostic_stats(wallets):
    """印出所有指標的分布"""
    if not wallets:
        return
    print("\n" + "=" * 60)
    print("📊 診斷報告：所有指標的真實分布")
    print("=" * 60)
    
    metrics = {
        "30D PnL ($)": [w.get("pnl_30d", 0) for w in wallets],
        "7D PnL ($)": [w.get("pnl_7d", 0) for w in wallets],
        "30D Volume ($)": [w.get("volume_30d", 0) for w in wallets],
        "排名 (rank_30d)": [w.get("rank_30d", 0) for w in wallets],
        "當前持倉數": [w.get("current_positions", 0) for w in wallets],
        "日均交易次數": [w.get("avg_daily_trades", 0) for w in wallets],
        "下注中位數 ($)": [w.get("median_trade_size", 0) for w in wallets],
        "下注 CV": [w.get("trade_cv", 0) for w in wallets],
        "金額加權勝率 (%)": [w.get("weighted_winrate", 0) for w in wallets],
        "已平倉部位數": [w.get("closed_positions_count", 0) for w in wallets],
        "獲利加速度 (x)": [w.get("pnl_acceleration", 0) for w in wallets],
    }
    
    # 觀看次數另外處理（有 -1 表示抓不到）
    views_data = [w.get("views", -1) for w in wallets]
    valid_views = [v for v in views_data if v >= 0]
    
    for name, vals in metrics.items():
        if not vals:
            continue
        vals_sorted = sorted(vals)
        n = len(vals_sorted)
        p25 = vals_sorted[n // 4]
        p50 = vals_sorted[n // 2]
        p75 = vals_sorted[3 * n // 4]
        p90 = vals_sorted[min(int(0.9 * n), n - 1)]
        print(f"\n  {name}:")
        print(f"    最低: {min(vals_sorted):.1f}  | 25%: {p25:.1f}  | 中位: {p50:.1f}")
        print(f"    75%: {p75:.1f}  | 90%: {p90:.1f}  | 最高: {max(vals_sorted):.1f}")
    
    print(f"\n  觀看次數 (views):")
    if valid_views:
        n = len(valid_views)
        sv = sorted(valid_views)
        print(f"    成功抓到: {n}/{len(views_data)} 個")
        print(f"    最低: {min(sv)}  | 中位: {sv[n//2]}  | 最高: {max(sv)}")
    else:
        print(f"    全部抓不到（Polymarket 用 JS 渲染，靜態 HTML 沒有此欄位）")
    
    print("=" * 60)


def compute_discreteness_score(wallet):
    score = 0
    views = wallet.get("views", -1)
    # 觀看次數（如果抓到了）
    if views >= 0:
        if views < 100:
            score += 40
        elif views < 300:
            score += 30
        elif views < 500:
            score += 20
        elif views < 700:
            score += 10
    else:
        # 抓不到時，給中性分數（不獎不罰）
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
    pnls_30d = [w.get("pnl_30d", 0) for w in all_wallets]
    pnls_7d = [w.get("pnl_7d", 0) for w in all_wallets]
    rois = [w.get("roi_7d_estimate", 0) for w in all_wallets]
    max_30d = max(pnls_30d) if pnls_30d else 1
    max_7d = max(pnls_7d) if pnls_7d else 1
    max_roi = max(rois) if rois else 1
    norm_30d = (wallet.get("pnl_30d", 0) / max_30d) * 100 if max_30d > 0 else 0
    norm_7d = (wallet.get("pnl_7d", 0) / max_7d) * 100 if max_7d > 0 else 0
    norm_roi = (wallet.get("roi_7d_estimate", 0) / max_roi) * 100 if max_roi > 0 else 0
    win_rate = wallet.get("weighted_winrate", 0)
    accel = wallet.get("pnl_acceleration", 0)
    accel_score = 100 if accel >= 1.5 else (accel / 1.5) * 100 if accel > 0 else 0
    score = (norm_30d * 0.25 + norm_7d * 0.25 + norm_roi * 0.20 +
             win_rate * 0.15 + accel_score * 0.15)
    return round(max(0, min(100, score)), 1)


def generate_reasoning(wallet):
    reasons, warnings = [], []
    accel = wallet.get("pnl_acceleration", 0)
    if accel >= 1.5:
        reasons.append(f"近 7 天獲利加速 {accel}x")
    
    views = wallet.get("views", -1)
    if views == -1:
        reasons.append("觀看次數需手動於 Polymarket 確認")
    elif views < 100:
        reasons.append(f"極低調（{views} 次觀看）")
    elif views < 300:
        reasons.append(f"低調（{views} 次觀看）")
    
    if not wallet.get("xUsername"):
        reasons.append("完全匿名無社群曝光")
    
    weighted_wr = wallet.get("weighted_winrate", 0)
    if weighted_wr >= 70:
        reasons.append(f"金額加權勝率 {weighted_wr}%（穩健）")
    elif weighted_wr >= 60:
        reasons.append(f"加權勝率 {weighted_wr}%")
    
    median_size = wallet.get("median_trade_size", 0)
    if median_size < 100:
        reasons.append(f"小額穩定下注（中位數 ${median_size:.0f}）")
    
    if wallet.get("current_positions", 0) > 80:
        warnings.append(f"持倉達 {wallet.get('current_positions')} 個倉位過於分散")
    if wallet.get("avg_daily_trades", 0) > 20:
        warnings.append(f"日均交易 {wallet.get('avg_daily_trades')} 次偏頻繁")
    if wallet.get("pnl_30d", 0) < 1500:
        warnings.append("PnL 剛過門檻需觀察持續性")
    if median_size > 500:
        warnings.append(f"單筆中位數 ${median_size:.0f} 對 $300 資金偏大")
    if wallet.get("trade_cv", 0) > 2:
        warnings.append(f"下注金額波動大（CV={wallet.get('trade_cv')}）")
    if weighted_wr < 50 and weighted_wr > 0:
        warnings.append(f"加權勝率僅 {weighted_wr}% 偏低")
    if wallet.get("closed_positions_count", 0) < 10:
        warnings.append("已平倉樣本少需觀察")
    if views >= 500:
        warnings.append(f"觀看次數已達 {views} 偏高")
    
    if not reasons:
        reasons.append("綜合指標通過篩選")
    if not warnings:
        warnings.append("各項指標健康，但任何跟單仍有風險")
    return "、".join(reasons), "、".join(warnings)

# ====================================
# HTML 報告
# ====================================
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

def generate_html(recommendations, run_date):
    green_count = sum(1 for w in recommendations if w["combined_score"] >= 80)
    yellow_count = sum(1 for w in recommendations if 60 <= w["combined_score"] < 80)
    top_score = max((w["combined_score"] for w in recommendations), default=0)

    cards_html = ""
    for i, w in enumerate(recommendations, 1):
        tier = get_tier(w["combined_score"])
        username = w.get("userName") or "未命名錢包"
        wallet_addr = w["proxyWallet"]
        pm_url = f"https://polymarket.com/profile/{wallet_addr}"
        pnl_30d_class = "positive" if w.get("pnl_30d", 0) > 0 else ""
        pnl_7d_class = "positive" if w.get("pnl_7d", 0) > 0 else ""
        accel_class = "positive" if w.get("pnl_acceleration", 0) >= 1.2 else ""

        cards_html += f"""
<div class="wallet-card {tier}">
  <div class="card-header">
    <div style="display:flex; align-items:center; gap:14px; flex-wrap:wrap;">
      <div class="rank-badge">{i}</div>
      <div>
        <div class="username">{username}</div>
        <div class="wallet-addr">{wallet_addr}</div>
      </div>
    </div>
    <a class="pm-link" href="{pm_url}" target="_blank">前往 Polymarket →</a>
  </div>
  <div class="scores">
    <div class="score-box"><div class="score-label">綜合評分</div><div class="score-value combined">{w['combined_score']:.0f}</div></div>
    <div class="score-box"><div class="score-label">獲利分數</div><div class="score-value profit">{w['profit_score']:.0f}</div></div>
    <div class="score-box"><div class="score-label">低調分數</div><div class="score-value discreteness">{w['discreteness_score']:.0f}</div></div>
  </div>
  <div class="metrics-grid">
    <div class="metric"><div class="metric-label">30D PnL</div><div class="metric-value {pnl_30d_class}">{format_money(w.get('pnl_30d', 0))}</div></div>
    <div class="metric"><div class="metric-label">7D PnL</div><div class="metric-value {pnl_7d_class}">{format_money(w.get('pnl_7d', 0))}</div></div>
    <div class="metric"><div class="metric-label">觀看次數</div><div class="metric-value">{format_views(w.get('views', -1))}</div></div>
    <div class="metric"><div class="metric-label">當前持倉</div><div class="metric-value">{w.get('current_positions', 0)}</div></div>
    <div class="metric"><div class="metric-label">日均交易</div><div class="metric-value">{w.get('avg_daily_trades', 0)}</div></div>
    <div class="metric"><div class="metric-label">下注中位數</div><div class="metric-value">${w.get('median_trade_size', 0):.0f}</div></div>
    <div class="metric"><div class="metric-label">下注 CV</div><div class="metric-value">{w.get('trade_cv', 0)}</div></div>
    <div class="metric"><div class="metric-label">加權勝率</div><div class="metric-value">{w.get('weighted_winrate', 0)}%</div></div>
    <div class="metric"><div class="metric-label">獲利加速度</div><div class="metric-value {accel_class}">{w.get('pnl_acceleration', 0)}x</div></div>
  </div>
  <div class="reason">💡 {w['recommendation_reason']}</div>
  <div class="warning">⚠️ {w['risk_warning']}</div>
</div>
"""

    if not recommendations:
        cards_html = """
<div style="background: rgba(245, 158, 11, 0.1); border: 1px solid #f59e0b; padding: 24px; border-radius: 12px; text-align: center; color: #fbbf24;">
  <h3>今日無符合條件的錢包</h3>
  <p style="margin-top: 8px;">所有候選錢包都未通過篩選。請查看 Actions log 看診斷資料分布。</p>
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
  .summary-bar {{ display: grid; grid-template-columns: repeat(4, 1fr); gap: 12px; margin-bottom: 32px; }}
  .stat {{ background: rgba(30, 41, 59, 0.6); border: 1px solid #334155; border-radius: 12px; padding: 16px; text-align: center; }}
  .stat-label {{ font-size: 11px; color: #94a3b8; text-transform: uppercase; letter-spacing: 0.5px; margin-bottom: 4px; }}
  .stat-value {{ font-size: 22px; font-weight: 700; color: #06b6d4; }}
  .wallet-card {{ background: rgba(30, 41, 59, 0.7); border: 2px solid #334155; border-radius: 16px; padding: 20px; margin-bottom: 20px; }}
  .wallet-card.tier-green {{ border-color: #10b981; box-shadow: 0 0 20px rgba(16, 185, 129, 0.15); }}
  .wallet-card.tier-yellow {{ border-color: #f59e0b; box-shadow: 0 0 20px rgba(245, 158, 11, 0.15); }}
  .wallet-card.tier-gray {{ border-color: #64748b; }}
  .card-header {{ display: flex; justify-content: space-between; align-items: flex-start; margin-bottom: 16px; flex-wrap: wrap; gap: 12px; }}
  .rank-badge {{ background: linear-gradient(135deg, #06b6d4, #8b5cf6); color: white; width: 40px; height: 40px; border-radius: 50%; display: flex; align-items: center; justify-content: center; font-weight: 700; font-size: 18px; flex-shrink: 0; }}
  .username {{ font-size: 18px; font-weight: 600; color: #f1f5f9; }}
  .wallet-addr {{ font-size: 12px; color: #94a3b8; font-family: monospace; word-break: break-all; }}
  .pm-link {{ background: #06b6d4; color: #0f172a; padding: 8px 16px; border-radius: 8px; text-decoration: none; font-weight: 600; font-size: 13px; white-space: nowrap; }}
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
  .info-banner {{ background: rgba(6, 182, 212, 0.1); border: 1px solid #06b6d4; padding: 12px 16px; border-radius: 8px; margin-bottom: 24px; color: #67e8f9; font-size: 13px; }}
  @media (max-width: 600px) {{ .summary-bar {{ grid-template-columns: repeat(2, 1fr); }} .scores {{ grid-template-columns: 1fr; }} h1 {{ font-size: 22px; }} }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🎯 Polymarket 跟單情報 v1.4</h1>
    <div class="subtitle">{run_date} · 診斷模式 + 放寬篩選</div>
  </header>
  <div class="info-banner">
    💡 此版本為「診斷模式」，所有條件已放寬。請查看 GitHub Actions log 中的「診斷報告」段落了解資料分布，再決定如何收緊條件。
  </div>
  <div class="summary-bar">
    <div class="stat"><div class="stat-label">推薦數量</div><div class="stat-value">{len(recommendations)}</div></div>
    <div class="stat"><div class="stat-label">最高分</div><div class="stat-value">{top_score:.0f}</div></div>
    <div class="stat"><div class="stat-label">綠燈級</div><div class="stat-value">{green_count}</div></div>
    <div class="stat"><div class="stat-label">黃燈級</div><div class="stat-value">{yellow_count}</div></div>
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

# ====================================
# Email 寄送
# ====================================
def send_email(html_content, run_date, recommendations_count):
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("⚠️ Email 設定不完整，跳過寄送")
        return False
    print(f"📧 寄送 Email 給 {RECIPIENT_EMAIL}...")
    msg = EmailMessage()
    msg["Subject"] = f"Polymarket Daily Report - {run_date} ({recommendations_count} picks)"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    msg.set_content(
        f"Polymarket Daily Report\nDate: {run_date}\nRecommendations: {recommendations_count}\n\n"
        f"Please view this email in HTML format."
    )
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

# ====================================
# 主程式
# ====================================
def main():
    print("=" * 60)
    print("Polymarket 跟單情報系統 v1.4 (診斷模式)")
    print(f"執行時間: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    lb_30d = fetch_leaderboard_paginated("MONTH")
    time.sleep(1)
    lb_7d = fetch_leaderboard_paginated("WEEK")
    
    if not lb_30d and not lb_7d:
        print("❌ 無法取得排行榜資料")
        html = generate_html([], run_date)
        save_and_send(html, run_date, 0)
        sys.exit(1)
    
    candidates = merge_leaderboards(lb_30d, lb_7d)
    print(f"\n合併後共 {len(candidates)} 個候選錢包")
    
    passed = hard_filter(candidates)
    
    # 診斷模式：印出所有指標分布
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
        w["recommendation_reason"], w["risk_warning"] = generate_reasoning(w)
    
    passed.sort(key=lambda x: x["combined_score"], reverse=True)
    final = [w for w in passed if w["discreteness_score"] >= MIN_DISCRETENESS][:RECOMMENDATIONS_COUNT]
    if len(final) < 3 and len(passed) >= 3:
        final = passed[:min(10, len(passed))]
    
    print(f"   ✓ 最終推薦: {len(final)} 個錢包")
    
    html = generate_html(final, run_date)
    save_and_send(html, run_date, len(final))


def save_and_send(html, run_date, count):
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"report_{run_date}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"\n💾 報告已存檔: {report_path}")
    latest_path = reports_dir / "latest.html"
    latest_path.write_text(html, encoding="utf-8")
    send_email(html, run_date, count)
    print("\n" + "=" * 60)
    print("✅ 執行完成")
    print("=" * 60)


if __name__ == "__main__":
    main()
