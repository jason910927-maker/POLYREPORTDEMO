# 🎯 Polymarket 跟單情報系統

每天自動分析 Polymarket 公開資料，篩選出 5–10 個低調但表現優異的交易者錢包，並透過 Email 寄送報告。

## 📊 篩選條件

### 硬性門檻
- 過去 30 天累積 PnL ≥ $1,000
- 同時持有部位數 ≤ 150
- 平均每日交易次數：1–50 次

### 低調度評分（≥ 60 分才入選）
- 排行榜排名位置（不在 Top 50）
- 社群曝光度（X 帳號狀態）
- 交易規模（避免鯨魚）

### 獲利能力評分
- 30D / 7D PnL
- 7D ROI
- 勝率估算
- 獲利加速度

## 🏗️ 架構

```
GitHub Actions (每日 UTC 00:00)
    ↓
Python 腳本執行
    ↓
Polymarket Data API (公開、免費)
    ↓
三層篩選 + 評分計算
    ↓
HTML 報告生成
    ↓
SMTP 寄送 Email
```

## ⚙️ 環境變數（GitHub Secrets）

- `GMAIL_USER` - 寄件 Gmail 地址
- `GMAIL_APP_PASSWORD` - Gmail 應用程式密碼（16位元）
- `RECIPIENT_EMAIL` - 收件 Email

## 📁 檔案結構

```
polymarket-bot/
├── .github/workflows/daily.yml    # 自動排程
├── main.py                        # 主程式
├── requirements.txt               # Python 依賴
├── reports/                       # 歷史報告（自動生成）
└── README.md
```

## ⚠️ 免責聲明

本系統僅為情報參考工具，所有資料來自公開 API。跟單交易具有高度風險，過去績效不代表未來表現。任何投資決策請自行評估。
