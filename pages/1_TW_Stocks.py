from __future__ import annotations

import concurrent.futures
from io import BytesIO

import pandas as pd
import plotly.graph_objects as go
import streamlit as st
import twstock
import yfinance as yf


st.set_page_config(page_title="台股跳空掃描器", page_icon="📈", layout="wide")

PAGE_SCAN = "🔎 市場掃描"
PAGE_QUERY = "🔍 個股查詢 / K線"


@st.cache_data(show_spinner=False)
def get_stock_catalog() -> pd.DataFrame:
    rows = []
    for code, info in twstock.codes.items():
        code = str(code)
        if len(code) != 4 or str(getattr(info, "type", "")) != "股票":
            continue
        market = str(getattr(info, "market", ""))
        if market == "上市":
            suffix = ".TW"
        elif market == "上櫃":
            suffix = ".TWO"
        else:
            continue
        rows.append({
            "代號": code,
            "名稱": str(getattr(info, "name", code)),
            "市場": market,
            "Ticker": f"{code}{suffix}",
        })
    return pd.DataFrame(rows).drop_duplicates("Ticker").sort_values(["市場", "代號"]).reset_index(drop=True)


def normalize_history(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.copy()
    if isinstance(df.columns, pd.MultiIndex):
        for level in range(df.columns.nlevels):
            vals = set(map(str, df.columns.get_level_values(level)))
            if {"Open", "High", "Low", "Close", "Volume"}.issubset(vals):
                df.columns = df.columns.get_level_values(level)
                break
    cols = ["Open", "High", "Low", "Close", "Volume"]
    if not set(cols).issubset(df.columns):
        return pd.DataFrame()
    df = df[cols].copy()
    for c in cols:
        df[c] = pd.to_numeric(df[c], errors="coerce")
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


@st.cache_data(ttl=300, show_spinner=False)
def get_history_cached(ticker: str, period: str = "6mo") -> pd.DataFrame:
    return download_history(ticker, period)


def evaluate_history(df: pd.DataFrame, lookback: int, gap_limit: float, min_vol_lots: int):
    if df.empty or len(df) < lookback + 1:
        return None

    avg_vol = df["Volume"].tail(5).mean()
    if pd.isna(avg_vol) or avg_vol < min_vol_lots * 1000:
        return None

    recent = df.tail(lookback + 1)
    current_price = float(df.iloc[-1]["Close"])

    # 由最新交易日往回掃描；包含最後一筆，因此最新交易日可被判定。
    for i in range(len(recent) - 1, 0, -1):
        curr = recent.iloc[i]
        prev = recent.iloc[i - 1]
        gap_support = float(prev["High"])
        prev_close = float(prev["Close"])
        if prev_close <= 0:
            continue

        gap_size = (float(curr["Open"]) - prev_close) / prev_close * 100
        is_gap_up = float(curr["Low"]) > gap_support
        if not is_gap_up or gap_size < gap_limit:
            continue

        days_after = recent.iloc[i + 1:]
        is_broken = False if days_after.empty else float(days_after["Low"].min()) < gap_support
        if not is_broken and current_price >= gap_support:
            return {
                "跳空日期": recent.index[i].strftime("%Y-%m-%d"),
                "缺口支撐": round(gap_support, 2),
                "目前股價": round(current_price, 2),
                "距離支撐(%)": round((current_price - gap_support) / gap_support * 100, 2),
                "跳空幅度(%)": round(gap_size, 2),
                "近5日均量(張)": round(float(avg_vol) / 1000),
                "資料日期": df.index[-1].strftime("%Y-%m-%d"),
            }
    return None


def analyze_stock(meta: dict, lookback: int, gap_limit: float, min_vol_lots: int):
    df = download_history(meta["Ticker"], "6mo")
    result = evaluate_history(df, lookback, gap_limit, min_vol_lots)
    if not result:
        return None
    return {
        "代號": meta["代號"],
        "名稱": meta["名稱"],
        "市場": meta["市場"],
        **result,
    }


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    output = BytesIO()
    with pd.ExcelWriter(output, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="資料")
    return output.getvalue()


def stock_search(catalog: pd.DataFrame, query: str) -> pd.DataFrame:
    q = query.strip().upper().replace(" ", "")
    if not q:
        return catalog.iloc[0:0]
    q_code = q.replace(".TW", "").replace(".TWO", "")
    code = catalog["代號"].astype(str).str.upper()
    name = catalog["名稱"].astype(str).str.upper()
    ticker = catalog["Ticker"].astype(str).str.upper()
    exact = (code == q_code) | (name == q) | (ticker == q)
    partial = code.str.contains(q_code, regex=False) | name.str.contains(q, regex=False) | ticker.str.contains(q, regex=False)
    return pd.concat([catalog[exact], catalog[partial & ~exact]]).drop_duplicates("Ticker").head(30)


def show_candlestick(df: pd.DataFrame, title: str, support: float | None = None, gap_date: str | None = None):
    plot_df = df.tail(120)
    fig = go.Figure(data=[go.Candlestick(
        x=plot_df.index,
        open=plot_df["Open"],
        high=plot_df["High"],
        low=plot_df["Low"],
        close=plot_df["Close"],
        name="K線",
    )])
    if support is not None:
        fig.add_hline(y=support, line_dash="dash", annotation_text=f"缺口支撐 {support:.2f}")
    if gap_date:
        fig.add_vline(x=pd.Timestamp(gap_date).timestamp() * 1000, line_dash="dot")
    fig.update_layout(title=title, xaxis_rangeslider_visible=False, height=620)
    st.plotly_chart(fig, use_container_width=True)


catalog = get_stock_catalog()
st.sidebar.title("台股跳空掃描器")
page = st.sidebar.radio("功能", [PAGE_SCAN, PAGE_QUERY])

if page == PAGE_SCAN:
    st.title("📈 跳空上漲 + 回測不破掃描器")
    st.caption("包含最新交易日，並優先顯示每檔股票最近一次仍有效的向上跳空。")

    c1, c2, c3 = st.columns(3)
    with c1:
        lookback = st.slider("回測觀察天數", 5, 60, 15)
    with c2:
        gap_limit = st.slider("最低跳空幅度 (%)", 0.5, 5.0, 1.5, 0.1)
    with c3:
        min_vol = st.number_input("最低近 5 日均量 (張)", min_value=0, value=500, step=100)

    mode = st.radio("掃描模式", ["快速測試（約 50 檔）", "全市場掃描"], horizontal=True)

    if st.button("🚀 開始掃描", type="primary"):
        preferred = ["8033", "3013", "2330", "3231", "2317"]
        if mode.startswith("快速"):
            targets = pd.concat([catalog[catalog["代號"].isin(preferred)], catalog.head(50)]).drop_duplicates("Ticker")
        else:
            targets = catalog
        target_rows = targets[["代號", "名稱", "市場", "Ticker"]].to_dict("records")

        results = []
        bar = st.progress(0)
        status = st.empty()
        total = len(target_rows)
        with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
            futures = [executor.submit(analyze_stock, row, lookback, gap_limit, int(min_vol)) for row in target_rows]
            for idx, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                try:
                    res = future.result()
                    if res:
                        results.append(res)
                except Exception:
                    pass
                if idx % 5 == 0 or idx == total:
                    bar.progress(idx / total)
                    status.write(f"已分析 {idx}/{total}；目前找到 {len(results)} 檔")

        st.session_state["scan_results"] = results

    results = st.session_state.get("scan_results", [])
    if results:
        df_res = pd.DataFrame(results).sort_values(["跳空日期", "距離支撐(%)"], ascending=[False, True])
        st.success(f"找到 {len(df_res)} 檔")
        st.dataframe(df_res, use_container_width=True, hide_index=True)
        d1, d2 = st.columns(2)
        with d1:
            st.download_button("⬇️ 下載 CSV", df_res.to_csv(index=False).encode("utf-8-sig"), "gap_scan.csv", "text/csv")
        with d2:
            st.download_button("⬇️ 下載 Excel", to_excel_bytes(df_res), "gap_scan.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    elif "scan_results" in st.session_state:
        st.warning("掃描完成，但沒有符合目前條件的股票。")

else:
    st.title("🔍 個股查詢 / K 線詳情")
    query = st.text_input("輸入股票代號或名稱", placeholder="例如：2330、2330.TW、台積電")
    matches = stock_search(catalog, query)

    if query and matches.empty:
        st.warning("找不到符合的股票。")
    elif not matches.empty:
        options = matches.to_dict("records")
        labels = [f"{x['代號']} {x['名稱']}（{x['市場']}）" for x in options]
        selected_label = st.selectbox("選擇股票", labels)
        selected = options[labels.index(selected_label)]

        lookback_q = st.slider("判定跳空回看天數", 5, 60, 15, key="q_lookback")
        gap_q = st.slider("最低跳空幅度 (%)", 0.5, 5.0, 1.5, 0.1, key="q_gap")
        min_vol_q = st.number_input("最低近 5 日均量 (張)", min_value=0, value=0, step=100, key="q_vol")

        with st.spinner("讀取股價資料中..."):
            df = get_history_cached(selected["Ticker"], "6mo")

        if df.empty:
            st.error("目前無法取得這檔股票的股價資料。")
        else:
            gap = evaluate_history(df, lookback_q, gap_q, int(min_vol_q))
            latest = df.iloc[-1]
            m1, m2, m3 = st.columns(3)
            m1.metric("最新收盤", f"{latest['Close']:.2f}")
            m2.metric("資料日期", df.index[-1].strftime("%Y-%m-%d"))
            m3.metric("成交量", f"{latest['Volume']/1000:,.0f} 張")

            if gap:
                st.success(f"最近有效跳空：{gap['跳空日期']}｜缺口支撐 {gap['缺口支撐']}｜距離支撐 {gap['距離支撐(%)']}%")
                show_candlestick(df, selected_label, float(gap["缺口支撐"]), str(gap["跳空日期"]))
            else:
                st.info("目前依照設定條件，沒有找到仍有效的向上跳空。")
                show_candlestick(df, selected_label)

            export_df = df.reset_index().rename(columns={df.index.name or "index": "日期"})
            e1, e2 = st.columns(2)
            with e1:
                st.download_button("⬇️ K 線 CSV", export_df.to_csv(index=False).encode("utf-8-sig"), f"{selected['代號']}_history.csv", "text/csv")
            with e2:
                st.download_button("⬇️ K 線 Excel", to_excel_bytes(export_df), f"{selected['代號']}_history.xlsx", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
