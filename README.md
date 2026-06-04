# AI_stock_market_weekly

台股每週訊號追蹤專案。程式會在每週五台灣時間收盤後抓取資料，追蹤台灣加權指數與中大型權值股，產出「每週台股趨勢報告」PDF。

正式工作目錄：

`C:\Users\zergv\Documents\GitHub\AI_stock_market_weekly`

GitHub repo：

https://github.com/ryanhsu1983/AI_stock_market_weekly

## 追蹤標的

- 台灣加權指數
- 台積電
- 聯發科
- 台達電
- 鴻海
- 廣達
- 緯創
- 緯穎

## 主要檔案

- `stock_market_tracking_system.py`：主程式，負責抓資料、計算週報模型與週報指標、產生 PDF 報告並上傳 Google Drive。
- `config.json`：追蹤標的、指標門檻、重大事件、Google Drive 上傳與 PDF 設定。
- `.github/workflows/weekly_run.yml`：GitHub Actions 每週五自動執行設定。
- `email_preview.html`：本機執行後產生的預覽檔，不應提交到 Git。

## 週報模型

週報沿用每日版的趨勢、MACD、三大法人、KD、OBV、匯率、利率、量能、BIAS60 模型，並增加每週趨勢資訊：

- 本週收盤價變化與 5 日漲跌幅
- 本週高低點
- 週成交量、週日均量與 20 日均量比
- 本週三大法人買賣超合計
- 收盤價相對 10/20/60 日均線位置
- 本週趨勢總結
- 下週觀察重點
- 強勢續抱、過熱不追、轉弱觀察、修正等待、盤整區間等週報判讀

## PDF 輸出

週報目前不寄 Email，workflow 不依賴 SMTP secrets。

免費觀眾版固定輸出：

- 檔名：`每週台股報告.pdf`
- Google Drive folder ID：`1HnfRzfdu5XeBF51zBKBLvhTNZ8Z-rSCS`
- 若 Google Drive 已存在同名檔案或已設定固定 file_id，程式會更新同一個檔案，避免 LINE 官方回覆連結變動。

自用備份版：

- 檔名格式：`每週台股報告_YYYYMMDD.pdf`
- 上傳位置維持既有週報備份資料夾。

Google Drive 根資料夾：

https://drive.google.com/drive/u/0/folders/1Do1tG2n_HPY1MmMVj2oYRxO6CLPrOv1T

上傳路徑：

直接上傳到上述 Google Drive 根資料夾，不再建立子資料夾。

程式會先確認 8 個追蹤標的皆成功完成，且資料日一致；任何標的缺漏或資料日不符都會中止，不會發布不完整週報。

同一週防重複產出會以最後上傳的自用備份 PDF 檔名作為完整發布判斷。免費固定 PDF 與自用備份 PDF 都成功上傳後，後續每小時排程偵測到同週備份檔便會直接跳過。手動執行可用 `force_run=true` 強制重新產生並更新 PDF。

## GitHub Actions

排程：

- 台灣時間每週五 15:00 至 23:00，每整點執行
- 若週報尚未完整產出或 Google Drive 上傳失敗，下個整點自動重試
- 若免費固定 PDF 與日期備份 PDF 已完整上傳，後續排程自動跳過
- 保留 `workflow_dispatch` 手動執行，支援 `force_run`

GitHub Actions 使用 UTC，因此 workflow 內為：

- `0 7-15 * * 5`

## 本機測試

在正式工作目錄執行：

```powershell
python -m py_compile stock_market_tracking_system.py
python stock_market_tracking_system.py
```

程式會產生 HTML 預覽、免費版 PDF 與自用備份 PDF。若沒有 Google OAuth 憑證，PDF 會保留在本機但不會上傳。
