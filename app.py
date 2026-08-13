import streamlit as st

pages = [
    st.Page("pages/1_TW_Stocks.py", title="TW Stocks", icon="🇹🇼", default=True),
    st.Page("pages/2_US_Stocks.py", title="US Stocks", icon="🇺🇸"),
]

page = st.navigation(pages)
page.run()
