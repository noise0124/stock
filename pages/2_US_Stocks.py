from __future__ import annotations

import concurrent.futures
from io import BytesIO, StringIO

import pandas as pd
import requests
import streamlit as st
import yfinance as yf


st.set_page_config(page_title="美股跳空掃描器", page_icon="🇺🇸", layout="wide")
st.title("🇺🇸 美股「全市場 + 產業分類」跳空掃描器")
st.markdown("支援 **GICS 11 大產業板塊**、**Russell 2000 小型股**、**全美股市場**、熱門科技、道瓊與自訂清單。")
st.caption("已納入最新交易日，並優先回傳每檔股票最近一次仍有效的向上跳空。")

SECTOR_MAP = {
    "Information Technology": "💻 資訊科技 (Technology)",
    "Health Care": "💊 醫療保健 (Health Care)",
    "Financials": "💰 金融 (Financials)",
    "Consumer Discretionary": "🛍️ 非必需消費 (Discretionary)",
    "Communication Services": "📡 通訊服務 (Communication)",
    "Industrials": "🏭 工業 (Industrials)",
    "Consumer Staples": "🛒 必需消費 (Staples)",
    "Energy": "🛢️ 能源 (Energy)",
    "Utilities": "⚡ 公用事業 (Utilities)",
    "Real Estate": "🏠 房地產 (Real Estate)",
    "Materials": "⛏️ 原物料 (Materials)",
}

USER_AGENT = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/127 Safari/537.36"
}


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        for level in range(df.columns.nlevels):
            values = set(map(str, df.columns.get_level_values(level)))
            if {"Open", "High", "Low", "Close", "Volume"}.issubset(values):
                df.columns = df.columns.get_level_values(level)
                break
    required = ["Open", "High", "Low", "Close", "Volume"]
    if not set(required).issubset(df.columns):
        return pd.DataFrame()
    df = df[required].copy()
    for col in required:
        df[col] = pd.to_numeric(df[col], errors="coerce")
    df.index = pd.to_datetime(df.index)
    if getattr(df.index, "tz", None) is not None:
        df.index = df.index.tz_localize(None)
    return df.dropna(subset=["Open", "High", "Low", "Close"]).sort_index()


def download_history(ticker: str, period: str = "6mo") -> pd.DataFrame:
    try:
        raw = yf.Ticker(ticker).history(period=period, auto_adjust=False, actions=False)
        return normalize_history(raw)
    except Exception:
        return pd.DataFrame()


@st.cache_data(ttl=3600, show_spinner=False)
def get_stock_list(mode: str, sector_filter: str, custom_txt: str = "") -> list[str]:
    try:
        if "S&P 500" in mode:
            url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
            response = requests.get(url, headers=USER_AGENT, timeout=25)
            response.raise_for_status()
            df = pd.read_html(StringIO(response.text))[0]
            df["Symbol"] = df["Symbol"].astype(str).str.replace(".", "-", regex=False)
            if sector_filter != "全掃描 (All Sectors)":
                target_sector = next((key for key, value in SECTOR_MAP.items() if value == sector_filter), None)
                if target_sector:
                    df = df[df["GICS Sector"] == target_sector]
            return df["Symbol"].dropna().astype(str).drop_duplicates().tolist()

        if "Russell 2000" in mode:
            url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/russell/russell2000_tickers.txt"
            response = requests.get(url, headers=USER_AGENT, timeout=25)
            response.raise_for_status()
            return list(dict.fromkeys(t.strip().upper() for t in response.text.splitlines() if t.strip()))[:2000]

        if "All US Stocks" in mode:
            url = "https://raw.githubusercontent.com/rreichel3/US-Stock-Symbols/main/all/all_tickers.txt"
            response = requests.get(url, headers=USER_AGENT, timeout=25)
            response.raise_for_status()
            tickers = [t.strip().upper() for t in response.text.splitlines() if t.strip()]
            tickers = [t for t in tickers if "^" not in t and "/" not in t]
            return list(dict.fromkeys(tickers))

        if "Mag 7" in mode:
            return ["NVDA", "TSLA", "AAPL", "MSFT", "AMZN", "GOOGL", "META", "AMD", "PLTR", "AVGO"]

        if "Dow Jones" in mode:
            url = "https://en.wikipedia.org/wiki/Dow_Jones_Industrial_Average"
            response = requests.get(url, headers=USER_AGENT, timeout=25)
            response.raise_for_status()
            tables = pd.read_html(StringIO(response.text))
            for table in tables:
                if "Symbol" in table.columns:
                    symbols = table["Symbol"].dropna().astype(str).str.replace(".", "-", regex=False)
                    if 20 <= len(symbols) <= 50:
                        return symbols.drop_duplicates().tolist()
            raise ValueError("找不到道瓊成分股表格")

        if "自訂" in mode:
            text = custom_txt.replace("\n", ",").replace(" ", ",").upper()
            return list(dict.fromkeys(x.strip() for x in text.split(",") if x.strip()))

    except Exception as exc:
        st.error(f"股票清單下載失敗：{exc}")
        return []

    return []


def analyze_stock(ticker: str, lookback: int, gap_limit: float, min_vol_ten_thousands: int):
    ticker = ticker.strip().upper()
    df = download_history(ticker, "6mo")
    if df.empty or len(df) < lookback + 1:
        return None

    avg_vol = df["Volume"].tail(5).mean()
    if pd.isna(avg_vol) or avg_vol < min_vol_ten_thousands * 10000:
        return None

    recent = df.tail(lookback + 1)
    current_price = float(df.iloc[-1]["Close"])

    # 從最新交易日往回掃描，確保最新交易日可被判定，且優先回傳最近一次有效跳空。
    for i in range(len(recent) - 1, 0, -1):
        curr = recent.iloc[i]
        prev = recent.iloc[i - 1]
        gap_support = float(prev["High"])
        prev_close = float(prev["Close"])
        if prev_close <= 0 or gap_support <= 0:
            continue

        is_gap_up = float(curr["Low"]) > gap_support
        gap_size = (float(curr["Open"]) - prev_close) / prev_close * 100
        if not is_gap_up or gap_size < gap_limit:
            continue

        days_after = recent.iloc[i + 1:]
        is_broken = False if days_after.empty else float(days_after["Low"].min()) < gap_support
        if not is_broken and current_price >= gap_support:
            return {
                "代號": ticker,
                "股價": round(current_price, 2),
                "跳空幅度(%)": round(gap_size, 2),
                "距離支撐(%)": round((current_price - gap_support) / gap_support * 100, 2),
                "缺口支撐": round(gap_support, 2),
                "跳空日": recent.index[i].strftime("%Y-%m-%d"),
                "成交量(萬)": round(float(avg_vol) / 10000, 0),
                "資料日期": df.index[-1].strftime("%Y-%m-%d"),
            }
    return None


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="美股掃描")
    return output.getvalue()


st.sidebar.header("🔍 美股篩選設定")
lookback_days = st.sidebar.slider("回測觀察天數", 5, 60, 15, key="us_lookback")
gap_threshold = st.sidebar.slider("跳空幅度 (%)", 1.0, 15.0, 3.0, 0.5, key="us_gap")
min_volume = st.sidebar.number_input("最低成交量 (萬股)", min_value=0, value=30, step=10, key="us_volume")

scan_mode = st.sidebar.selectbox(
    "選擇掃描範圍",
    [
        "🏛️ S&P 500 (標普 500 - 依產業分類)",
        "🐜 Russell 2000 (羅素 2000 - 小型股)",
        "🌎 All US Stocks (全美股市場 - 6000+檔)",
        "🔥 Mag 7 + AI (熱門科技)",
        "💎 Dow Jones (道瓊)",
        "📝 自訂輸入",
    ],
    key="us_scan_mode",
)

selected_sector = "全掃描 (All Sectors)"
if "S&P 500" in scan_mode:
    sector_options = ["全掃描 (All Sectors)"] + list(SECTOR_MAP.values())
    selected_sector = st.sidebar.selectbox("選擇產業板塊", sector_options, key="us_sector")

custom_input = ""
if "自訂" in scan_mode:
    custom_input = st.sidebar.text_area("輸入代號", "PLTR, UBER, NVDA", key="us_custom")

if "All US Stocks" in scan_mode:
    st.warning("全美股模式會對 Yahoo Finance 發出大量資料請求，可能遇到暫時限流；若發生可先改用 S&P 500、Russell 2000 或自訂清單。")

if st.button("🚀 啟動美股掃描", type="primary"):
    target_stocks = get_stock_list(scan_mode, selected_sector, custom_input)
    if not target_stocks:
        st.error("沒有取得可掃描的股票清單。")
    else:
        st.info(f"📊 正在分析 {len(target_stocks):,} 檔股票...")
        results = []
        progress_bar = st.progress(0)
        status_text = st.empty()

        workers = 10 if len(target_stocks) > 500 else 8
        total = len(target_stocks)
        with concurrent.futures.ThreadPoolExecutor(max_workers=workers) as executor:
            futures = [
                executor.submit(analyze_stock, symbol, lookback_days, gap_threshold, int(min_volume))
                for symbol in target_stocks
            ]
            for completed, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                try:
                    result = future.result()
                    if result:
                        results.append(result)
                except Exception:
                    pass

                if completed % 20 == 0 or completed == total:
                    progress_bar.progress(completed / total)
                    status_text.text(f"已掃描：{completed:,}/{total:,}｜找到：{len(results):,} 檔")

        st.session_state["us_scan_results"] = results
        st.session_state["us_scan_mode_label"] = scan_mode

results = st.session_state.get("us_scan_results", [])
if results:
    df_res = pd.DataFrame(results)
    columns = ["代號", "股價", "跳空幅度(%)", "距離支撐(%)", "缺口支撐", "跳空日", "成交量(萬)", "資料日期"]
    df_res = df_res[columns].sort_values(["跳空日", "距離支撐(%)"], ascending=[False, True])
    st.success(f"🎉 找到 {len(df_res)} 檔符合條件的股票！")
    st.dataframe(df_res, use_container_width=True, hide_index=True)

    c1, c2 = st.columns(2)
    with c1:
        st.download_button(
            "⬇️ 下載 CSV",
            df_res.to_csv(index=False).encode("utf-8-sig"),
            "us_gap_scan.csv",
            "text/csv",
        )
    with c2:
        st.download_button(
            "⬇️ 下載 Excel",
            to_excel_bytes(df_res),
            "us_gap_scan.xlsx",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
elif "us_scan_results" in st.session_state:
    st.warning("掃描完成，但沒有找到符合目前條件的股票。")
