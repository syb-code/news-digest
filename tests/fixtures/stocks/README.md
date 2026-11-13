# Fixtures for the stock section

Trimmed copies of real public responses, captured on November 12, 2026, for the
offline tests of `scripts/stocks/`. No fixture names a ticker the analysis
tracks: the ticker list is private and lives in the Worker. Tests use the
public examples here (AAPL, SPOT, Berkshire Hathaway's holdings) instead.

Personal details are replaced: the reporting owner, address and signer of each
Form 4, and the signer of each 13F cover page, are placeholders. Members of
Congress appear under their own names, as in the public House index.

| File | Source | Trimmed to |
|---|---|---|
| `sec_company_tickers.json` | https://www.sec.gov/files/company_tickers.json | 24 entries, same shape |
| `sec_companyfacts_aapl.json` | https://data.sec.gov/api/xbrl/companyfacts/CIK0000320193.json | us-gaap EarningsPerShareDiluted and EarningsPerShareBasic from mid 2023, one dei fact |
| `sec_companyfacts_spot_ifrs.json` | companyfacts of CIK 1639920 (an IFRS filer) | ifrs-full DilutedEarningsLossPerShare and BasicEarningsLossPerShare, EUR/shares, annual only |
| `sec_companyfacts_no_eps.json` | the same filer | only its ffd and dei facts: a filer with no earnings facts |
| `sec_submissions_aapl.json` | https://data.sec.gov/submissions/CIK0000320193.json | top-level fields and 70 recent filings (Forms 3, 4, 8-K, 10-Q, 10-K) |
| `sec_submissions_berkshire.json` | https://data.sec.gov/submissions/CIK0001067983.json | its 13F-HR filings and a few others |
| `sec_form4_exercise_and_sale.xml` | an AAPL Form 4 (codes M, F, S and a derivative table) | owner anonymized |
| `sec_form4_planned_sale.xml` | an AAPL Form 4 (code S under a 10b5-1 plan) | owner anonymized |
| `sec_13f_filing_index.json` | `index.json` of Berkshire's 13F-HR for 2026-06-30 | as served |
| `sec_13f_infotable_2026q2.xml` | Berkshire's information table for 2026-06-30 | 11 issuers; two option rows and one principal-amount row were added from another filer's table |
| `sec_13f_infotable_2026q1.xml` | Berkshire's information table for 2026-03-31 | the same issuers |
| `sec_13f_cover_restatement.xml` | a 13F-HR/A cover page with amendmentType RESTATEMENT | signer anonymized |
| `sec_13f_cover_notice.xml` | a 13F-NT cover page that points to another manager | signer anonymized |
| `finra_cnms_shvol_20261110.txt` | https://cdn.finra.org/equity/regsho/daily/CNMSshvol20261110.txt | header, 13 symbols (BRK/B and BF/B show FINRA's slash spelling), the record-count line; CRLF kept |
| `finra_regsho_daily_aapl.json` | POST https://api.finra.org/data/group/otcMarket/name/regShoDaily | AAPL rows from 2026-09-28, one row per reporting facility a day |
| `finra_short_interest_aapl.json` | POST https://api.finra.org/data/group/otcMarket/name/consolidatedShortInterest | AAPL, one rolling year |
| `fred_*.csv` | https://fred.stlouisfed.org/graph/fredgraph.csv?id=SERIES | recent rows; empty values on holidays kept |
| `fomc_calendars.html` | https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm | the 2027 (tentative, no footer), 2026, 2025 and 2023 (Jan/Feb and Oct/Nov month cells) panels |
| `bls_schedule.ics` | https://www.bls.gov/schedule/news_release/bls.ics | CPI, Employment Situation, PPI, JOLTS and Real Earnings, September to December 2026, plus two State JOLTS look-alikes; LF line endings as served |
| `bea_schedule.ics` | https://www.bea.gov/news/schedule/ics/online-calendar-subscription.ics | GDP, personal income and trade events, October 2026 to March 2027; folded lines and CRLF kept |
| `bea_current_releases.html` | https://www.bea.gov/news/current-releases | the release table |
| `fed_press_monetary.xml` | https://www.federalreserve.gov/feeds/press_monetary.xml | 6 items; the byte order mark kept |
| `bls_cpi_feed.atom` | https://www.bls.gov/feed/cpi.rss (an Atom feed) | 3 entries |
| `bls_cpi_archive.html` | https://www.bls.gov/bls/news-release/cpi.htm | 40 release links |
| `house_2026FD.xml` | `2026FD.xml` inside https://disclosures-clerk.house.gov/public_disc/financial-pdfs/2026FD.zip | 19 entries of members, one with an empty FilingDate |
| `house_ptr_20035591.pdf` | https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/2026/20035591.pdf | as served: an electronic report with 6 trades |
| `house_ptr_9116342.pdf` | the same path, DocID 9116342 | as served: a scanned paper report with no text layer |
| `house_ptr_20035491.txt`, `house_ptr_20035434.txt` | text of those reports, extracted with pypdf 6.9.2, pages joined with a newline | 99 trades with a page-break inside one amount; 17 trades with no tickers |
| `tiingo_prices_synthetic.json`, `tiingo_meta_synthetic.json` | synthetic, in the shape of the Tiingo daily endpoints (api.tiingo.com cannot be reached from the build sandbox) | 50 rows with one cash dividend and one 2-for-1 split |
| `fmp_senate_trades_unverified.json` | synthetic, in the shape Financial Modeling Prep documents for its Senate trades endpoint, not checked against the live API | 2 rows of an example senator |
| `analysis_reference.json` | synthetic, built for the analysis tests: an 80 bar series with indicator values from pandas, Wilder's RSI example series with its published values, a regression checked with statsmodels and a regularized logistic regression checked with scikit-learn | not trimmed; no ticker |
