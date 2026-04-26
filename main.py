"""
Polymarket 跟單情報系統 - 自動化版本
每天執行一次，產生 HTML 報告並寄送 Email

作者：你的 AI 助手
版本：1.0
"""

import os
import sys
import json
import time
import smtplib
import requests
from datetime import datetime, timezone, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.mime.base import MIMEBase
from email import encoders
from pathlib import Path

# ====================================
# 設定區
# ====================================
DATA_API_BASE = "https://data-api.polymarket.com"

# 篩選條件（可修改）
MIN_PNL_30D = 1000          # 30天最低獲利門檻 ($)
MAX_POSITIONS = 150          # 最大同時持倉數
MAX_DAILY_TRADES = 50        # 每日最大交易次數
MIN_DAILY_TRADES = 1         # 每日最低交易次數
MIN_DISCRETENESS = 60        # 最低低調分數
TOP_RANK_EXCLUDE = 50        # 排除 Top N 名（避免太熱門）
LEADERBOARD_LIMIT = 200      # 抓多少名候選
RECOMMENDATIONS_COUNT = 10   # 每天推薦幾個

# 從環境變數讀取 Email 設定（GitHub Secrets）
GMAIL_USER = os.environ.get("GMAIL_USER", "")
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "")
RECIPIENT_EMAIL = os.environ.get("RECIPIENT_EMAIL", "")

# ====================================
# 資料抓取
# ====================================
def fetch_leaderboard(window="30d"):
    """抓取排行榜資料"""
    url = f"{DATA_API_BASE}/leaderboard"
    params = {"window": window, "orderBy": "pnl", "limit": LEADERBOARD_LIMIT}
    
    print(f"📥 抓取 {window} 排行榜...")
    try:
        r = requests.get(url, params=params, timeout=30)
        r.raise_for_status()
        data = r.json()
        print(f"   ✓ 取得 {len(data)} 筆")
        return data
    except Exception as e:
        print(f"   ✗ 錯誤: {e}")
        return []

def fetch_positions(wallet):
    """抓取錢包當前持倉數"""
    url = f"{DATA_API_BASE}/positions"
    params = {"user": wallet, "limit": 200, "sizeThreshold": 0.01}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code == 200:
            return len(r.json())
    except:
        pass
    return -1

def fetch_recent_trades(wallet, days=7):
    """抓取最近 N 天的交易紀錄"""
    url = f"{DATA_API_BASE}/trades"
    params = {"user": wallet, "limit": 500, "takerOnly": False}
    try:
        r = requests.get(url, params=params, timeout=15)
        if r.status_code != 200:
            return []
        trades = r.json()
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).timestamp()
        return [t for t in trades if t.get("timestamp", 0) >= cutoff]
    except:
        return []

# ====================================
# 篩選與評分邏輯
# ====================================
def merge_leaderboards(lb_30d, lb_7d):
    """合併兩個排行榜，去重複"""
    merged = {}
    for entry in lb_30d:
        wallet = entry.get("proxyWallet")
        if wallet:
            merged[wallet] = {
                **entry,
                "pnl_30d": float(entry.get("pnl", 0)),
                "rank_30d": int(entry.get("rank", 999)),
                "volume_30d": float(entry.get("vol", 0)),
            }
    for entry in lb_7d:
        wallet = entry.get("proxyWallet")
        if not wallet:
            continue
        if wallet not in merged:
            merged[wallet] = {
                **entry,
                "pnl_30d": 0,
                "rank_30d": 999,
                "volume_30d": float(entry.get("vol", 0)),
            }
        merged[wallet]["pnl_7d"] = float(entry.get("pnl", 0))
        merged[wallet]["rank_7d"] = int(entry.get("rank", 999))
    return list(merged.values())

def hard_filter(wallets):
    """第一層硬性篩選"""
    print(f"\n🔍 開始硬性篩選（共 {len(wallets)} 個候選）...")
    filtered = []
    for i, w in enumerate(wallets, 1):
        wallet_addr = w.get("proxyWallet", "")
        if not wallet_addr:
            continue
        
        # 排除 Top 50 鯨魚
        if w.get("rank_30d", 999) <= TOP_RANK_EXCLUDE:
            continue
        
        # PnL 門檻
        if w.get("pnl_30d", 0) < MIN_PNL_30D:
            continue
        
        if i % 20 == 0:
            print(f"   處理中 {i}/{len(wallets)}...")
        
        # 抓持倉
        positions_count = fetch_positions(wallet_addr)
        if positions_count < 0 or positions_count > MAX_POSITIONS:
            continue
        
        # 抓近期交易
        recent = fetch_recent_trades(wallet_addr, days=7)
        if len(recent) < 1:
            continue
        
        avg_daily = len(recent) / 7
        if avg_daily < MIN_DAILY_TRADES or avg_daily > MAX_DAILY_TRADES:
            continue
        
        w["current_positions"] = positions_count
        w["trades_last_7d"] = len(recent)
        w["avg_daily_trades"] = round(avg_daily, 1)
        
        # 計算勝率（簡化版：用近期交易的盈虧分布估算）
        wins = sum(1 for t in recent if float(t.get("pnl", 0)) > 0)
        w["win_rate_estimate"] = round(wins / max(len(recent), 1) * 100, 1)
        
        # 計算獲利加速度
        pnl_7d = w.get("pnl_7d", 0)
        pnl_30d = w.get("pnl_30d", 0)
        expected_7d = pnl_30d / 4
        w["pnl_acceleration"] = round(pnl_7d / max(expected_7d, 1), 2) if expected_7d > 0 else 0
        
        # 計算 7D ROI
        vol_30d = w.get("volume_30d", 1)
        w["roi_7d_estimate"] = round(pnl_7d / max(vol_30d / 4, 1) * 100, 2)
        
        filtered.append(w)
        time.sleep(0.3)  # 避免限流
    
    print(f"   ✓ 通過硬性篩選: {len(filtered)} 個")
    return filtered

def compute_discreteness_score(wallet):
    """計算低調分數 0-100"""
    score = 0
    
    # (a) 排名分數 40 分
    rank = wallet.get("rank_30d", 999)
    if rank > 100:
        score += 40
    elif rank > 50:
        score += 20
    
    # (b) 社群曝光 30 分
    if not wallet.get("xUsername"):
        score += 30
    elif not wallet.get("verified"):
        score += 15
    
    # (c) 規模 30 分
    vol = wallet.get("volume_30d", 0)
    if vol < 100_000:
        score += 30
    elif vol < 500_000:
        score += 15
    
    return score

def compute_profit_score(wallet, all_wallets):
    """計算獲利分數 0-100（標準化）"""
    pnls_30d = [w.get("pnl_30d", 0) for w in all_wallets]
    pnls_7d = [w.get("pnl_7d", 0) for w in all_wallets]
    rois = [w.get("roi_7d_estimate", 0) for w in all_wallets]
    
    max_30d = max(pnls_30d) if pnls_30d else 1
    max_7d = max(pnls_7d) if pnls_7d else 1
    max_roi = max(rois) if rois else 1
    
    norm_30d = (wallet.get("pnl_30d", 0) / max_30d) * 100 if max_30d > 0 else 0
    norm_7d = (wallet.get("pnl_7d", 0) / max_7d) * 100 if max_7d > 0 else 0
    norm_roi = (wallet.get("roi_7d_estimate", 0) / max_roi) * 100 if max_roi > 0 else 0
    
    win_rate = wallet.get("win_rate_estimate", 0)
    
    accel = wallet.get("pnl_acceleration", 0)
    accel_score = 100 if accel >= 1.5 else (accel / 1.5) * 100 if accel > 0 else 0
    
    score = (
        norm_30d * 0.25 +
        norm_7d * 0.25 +
        norm_roi * 0.20 +
        win_rate * 0.15 +
        accel_score * 0.15
    )
    return round(max(0, min(100, score)), 1)

def generate_reasoning(wallet):
    """為每個錢包生成推薦理由與風險警示"""
    reasons = []
    warnings = []
    
    # 推薦理由
    accel = wallet.get("pnl_acceleration", 0)
    if accel >= 1.5:
        reasons.append(f"近 7 天獲利加速 {accel}x")
    
    if wallet.get("rank_30d", 0) > 100:
        reasons.append(f"排名第 {wallet.get('rank_30d')} 屬雷達下級別")
    
    if not wallet.get("xUsername"):
        reasons.append("完全匿名無社群曝光")
    
    win_rate = wallet.get("win_rate_estimate", 0)
    if win_rate >= 65:
        reasons.append(f"勝率 {win_rate}% 表現穩健")
    
    if wallet.get("avg_daily_trades", 0) < 5:
        reasons.append("低頻交易適合手動跟單")
    
    # 風險警示
    if wallet.get("current_positions", 0) > 80:
        warnings.append(f"持倉達 {wallet.get('current_positions')} 個倉位過於分散")
    
    if wallet.get("avg_daily_trades", 0) > 20:
        warnings.append(f"日均交易 {wallet.get('avg_daily_trades')} 次過於頻繁")
    
    if wallet.get("pnl_30d", 0) < 1500:
        warnings.append("PnL 剛過門檻需觀察持續性")
    
    if accel < 1:
        warnings.append("獲利動能放緩")
    
    if not reasons:
        reasons.append("綜合指標通過篩選")
    if not warnings:
        warnings.append("各項指標健康，但任何跟單仍有風險")
    
    return "、".join(reasons), "、".join(warnings)

# ====================================
# HTML 報告產生
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

def generate_html(recommendations, run_date):
    """生成完整 HTML 報告"""
    green_count = sum(1 for w in recommendations if w["combined_score"] >= 80)
    yellow_count = sum(1 for w in recommendations if 60 <= w["combined_score"] < 80)
    top_score = max((w["combined_score"] for w in recommendations), default=0)
    
    cards_html = ""
    for i, w in enumerate(recommendations, 1):
        tier = get_tier(w["combined_score"])
        username = w.get("name") or w.get("username") or "未命名錢包"
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
    <div class="metric"><div class="metric-label">當前持倉</div><div class="metric-value">{w.get('current_positions', 0)}</div></div>
    <div class="metric"><div class="metric-label">日均交易</div><div class="metric-value">{w.get('avg_daily_trades', 0)}</div></div>
    <div class="metric"><div class="metric-label">勝率估算</div><div class="metric-value">{w.get('win_rate_estimate', 0)}%</div></div>
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
  <p style="margin-top: 8px;">所有候選錢包都未通過硬性篩選或低調度門檻。可能因為：</p>
  <ul style="text-align: left; max-width: 400px; margin: 12px auto;">
    <li>市場活躍度低，本日交易者較少</li>
    <li>頂尖獲利者皆已被廣泛關注</li>
    <li>API 暫時延遲，建議明日再看</li>
  </ul>
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
  .wallet-card {{ background: rgba(30, 41, 59, 0.7); border: 2px solid #334155; border-radius: 16px; padding: 20px; margin-bottom: 20px; transition: transform 0.2s; }}
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
  .metrics-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(140px, 1fr)); gap: 8px; margin-bottom: 16px; }}
  .metric {{ background: rgba(15, 23, 42, 0.4); padding: 8px 12px; border-radius: 6px; font-size: 13px; }}
  .metric-label {{ color: #94a3b8; font-size: 11px; }}
  .metric-value {{ color: #e2e8f0; font-weight: 600; }}
  .metric-value.positive {{ color: #10b981; }}
  .reason {{ background: rgba(6, 182, 212, 0.1); border-left: 3px solid #06b6d4; padding: 10px 14px; border-radius: 4px; margin-bottom: 10px; font-weight: 600; color: #f1f5f9; }}
  .warning {{ background: rgba(239, 68, 68, 0.1); border-left: 3px solid #ef4444; padding: 10px 14px; border-radius: 4px; color: #fca5a5; font-size: 13px; }}
  footer {{ margin-top: 48px; padding: 24px; background: rgba(239, 68, 68, 0.05); border: 1px solid rgba(239, 68, 68, 0.3); border-radius: 12px; text-align: center; font-size: 13px; color: #fca5a5; line-height: 1.6; }}
  @media (max-width: 600px) {{ .summary-bar {{ grid-template-columns: repeat(2, 1fr); }} .scores {{ grid-template-columns: 1fr; }} h1 {{ font-size: 22px; }} }}
</style>
</head>
<body>
<div class="container">
  <header>
    <h1>🎯 Polymarket 跟單情報</h1>
    <div class="subtitle">{run_date} · 每日精選低調高獲利錢包</div>
  </header>
  <div class="summary-bar">
    <div class="stat"><div class="stat-label">推薦數量</div><div class="stat-value">{len(recommendations)}</div></div>
    <div class="stat"><div class="stat-label">最高分</div><div class="stat-value">{top_score:.0f}</div></div>
    <div class="stat"><div class="stat-label">綠燈級</div><div class="stat-value">{green_count}</div></div>
    <div class="stat"><div class="stat-label">黃燈級</div><div class="stat-value">{yellow_count}</div></div>
  </div>
  {cards_html}
  <footer>
    <strong>⚠️ 免責聲明</strong><br>
    本系統僅為情報參考工具，所有錢包資料來自 Polymarket 公開 API。<br>
    跟單交易具有高度風險，過去績效不代表未來表現。任何投資決策請自行評估，後果自負。<br>
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
    """寄送 HTML 報告到 Email"""
    if not (GMAIL_USER and GMAIL_APP_PASSWORD and RECIPIENT_EMAIL):
        print("⚠️ Email 設定不完整，跳過寄送")
        return False
    
    print(f"📧 寄送 Email 給 {RECIPIENT_EMAIL}...")
    
    msg = MIMEMultipart("alternative")
    msg["Subject"] = f"🎯 Polymarket 跟單情報 - {run_date} ({recommendations_count} 個推薦)"
    msg["From"] = GMAIL_USER
    msg["To"] = RECIPIENT_EMAIL
    
    msg.attach(MIMEText(html_content, "html", "utf-8"))
    
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(GMAIL_USER, GMAIL_APP_PASSWORD)
            server.sendmail(GMAIL_USER, RECIPIENT_EMAIL, msg.as_string())
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
    print("Polymarket 跟單情報系統")
    print(f"執行時間: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}")
    print("=" * 60)
    
    run_date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    
    # 1. 抓取資料
    lb_30d = fetch_leaderboard("30d")
    time.sleep(1)
    lb_7d = fetch_leaderboard("7d")
    
    if not lb_30d and not lb_7d:
        print("❌ 無法取得排行榜資料，可能是 API 暫時故障")
        sys.exit(1)
    
    # 2. 合併
    candidates = merge_leaderboards(lb_30d, lb_7d)
    print(f"\n合併後共 {len(candidates)} 個候選錢包")
    
    # 3. 硬性篩選
    passed = hard_filter(candidates)
    
    if not passed:
        print("\n⚠️ 沒有錢包通過硬性篩選")
        html = generate_html([], run_date)
        save_and_send(html, run_date, 0)
        return
    
    # 4. 計算分數
    print(f"\n📊 計算評分...")
    for w in passed:
        w["discreteness_score"] = compute_discreteness_score(w)
        w["profit_score"] = compute_profit_score(w, passed)
        w["combined_score"] = round(w["profit_score"] * 0.7 + w["discreteness_score"] * 0.3, 1)
        w["recommendation_reason"], w["risk_warning"] = generate_reasoning(w)
    
    # 5. 排序、過濾低調度、取前 N 名
    passed.sort(key=lambda x: x["combined_score"], reverse=True)
    final = [w for w in passed if w["discreteness_score"] >= MIN_DISCRETENESS][:RECOMMENDATIONS_COUNT]
    
    # 確保至少 5 個（如果有）
    if len(final) < 5 and len(passed) >= 5:
        final = passed[:5]
    
    print(f"   ✓ 最終推薦: {len(final)} 個錢包")
    
    # 6. 產生 HTML
    html = generate_html(final, run_date)
    save_and_send(html, run_date, len(final))

def save_and_send(html, run_date, count):
    # 存檔
    reports_dir = Path("reports")
    reports_dir.mkdir(exist_ok=True)
    report_path = reports_dir / f"report_{run_date}.html"
    report_path.write_text(html, encoding="utf-8")
    print(f"\n💾 報告已存檔: {report_path}")
    
    # 也存一份 latest.html
    latest_path = reports_dir / "latest.html"
    latest_path.write_text(html, encoding="utf-8")
    
    # 寄送 Email
    send_email(html, run_date, count)
    
    print("\n" + "=" * 60)
    print("✅ 執行完成")
    print("=" * 60)

if __name__ == "__main__":
    main()
