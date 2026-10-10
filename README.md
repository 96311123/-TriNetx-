# 慈濟醫療體系 TriNetX 發表文章追蹤

每週自動從 PubMed 擷取「慈濟體系作者 × TriNetX 資料庫」之文章，整理成網頁與可下載的 Excel / CSV / JSON，
並統計各院區、慈濟大學之文章數與第一作者 / 通訊作者分布。

- 網站：GitHub Pages（`docs/` 目錄）
- 排程：GitHub Actions，每週一 08:15（台北時間）執行，有變動即自動 commit
- 搜尋策略：見 `scripts/fetch_pubmed.py` 的 `QUERY`
- 通訊作者補強：PMC 全文（Europe PMC / NCBI PMC）、OpenAlex、Crossref（見下方「通訊作者判定」）

## 架設步驟（約 5 分鐘）

1. **建立 repo**：在 GitHub 新增一個 repository（例如 `tzuchi-trinetx`，Public 或 Private 皆可；Private 的 Pages 需 GitHub Pro/Team）。
2. **上傳本專案**：把整個資料夾內容 push 上去（或在網頁上直接拖曳上傳）。
   ```bash
   git init && git add . && git commit -m "init"
   git branch -M main
   git remote add origin https://github.com/<帳號>/tzuchi-trinetx.git
   git push -u origin main
   ```
3. **允許 Actions 寫入**：repo → Settings → Actions → General → Workflow permissions → 勾選 **Read and write permissions** → Save。
4. **開啟 GitHub Pages**：Settings → Pages → Source 選 **Deploy from a branch**，Branch 選 `main`、資料夾選 **/docs** → Save。
   網址會是 `https://<帳號>.github.io/tzuchi-trinetx/`。
5. **第一次手動執行**：Actions → 左側「Update PubMed data」→ **Run workflow**。約 1 分鐘後 `docs/data/` 會出現資料並自動 commit，網站隨即更新。
6. *(選用)* **NCBI API key**：Settings → Secrets and variables → Actions → New repository secret
   - `NCBI_API_KEY`：到 <https://www.ncbi.nlm.nih.gov/account/settings/> 取得（提高速率上限）
   - `NCBI_EMAIL`：你的聯絡信箱（NCBI 建議提供；同時用於 OpenAlex / Crossref 的 polite pool，速度較快、較不易被限流）
   - `OPENALEX_API_KEY`：到 <https://openalex.org> 免費申請；不設也能跑，但每日免費額度較少

## 產出檔案（`docs/data/`）

| 檔案 | 內容 |
|---|---|
| `tzuchi_trinetx.xlsx` | 總覽（含通訊作者來源分布）、文章清單（通訊作者來源以顏色標示、出版社、被引次數）、作者與隸屬（所有 affiliation 原文、ORCID）、院區×年份、期刊統計 |
| `articles.csv` | 每篇一列：PMID、年份、標題、期刊、DOI、第一/通訊作者及其院區、涉及院區 |
| `author_affiliations.csv` | 每位作者的每一條 affiliation 一列，含院區判定、ORCID、PMC/OpenAlex 通訊作者標記 |
| `articles.json` / `summary.json` | 網頁資料來源 |

## 計數規則

- **文章數（任一作者）**：只要有任一作者的隸屬屬於該院區即計 1；一篇文章可同時計入多個院區。
- **第一作者文章數**：第一作者隸屬含該院區；若第一作者有多重隸屬則各院區各計一次。
- **通訊作者文章數**：通訊作者所屬院區各計一次（判定方式見下）。
- **作者人次**：該院區作者出現的總次數（同一人在不同文章重複計算）。

## 通訊作者判定

PubMed 本身沒有通訊作者欄位，因此依下列優先順序判定，每篇文章都會標示採用的來源：

| 順位 | 來源 | 判定依據 | 可信度 |
|---|---|---|---|
| ① | **PMC 全文** | Europe PMC `fullTextXML`（失敗時改用 NCBI `efetch db=pmc`）的 JATS 標記：`<contrib corresp="yes">`、`<xref ref-type="corresp">`、指向含 e-mail 之 `<author-notes>` 的 xref，或 `<corresp>` 文字中點名的作者 | 最高（出版社原始標記） |
| ② | **OpenAlex** | `authorships[].is_corresponding` | 高 |
| ③ | **PubMed e-mail** | PubMed 隸屬字串中含 e-mail 的作者 | 中 |
| ④ | **末位作者(推定)** | 以上皆無時取末位作者 | 低 |

- 同時有 PMC 與 OpenAlex 結果時會比對，不一致的文章在網頁與 Excel 中標紅，方便人工複核。
- **Crossref 沒有通訊作者欄位**，用於補充 ORCID、出版社、被引次數。
- 外部查詢結果存於 `cache/enrich_cache.json` 並一起 commit；每週只查新文章或過期項目
  （OpenAlex 找到 90 天／未找到 14 天、PMC 找到永久／未找到 30 天、Crossref 7 天更新被引次數）。
- 只想跑 PubMed、不查外部來源：`python scripts/fetch_pubmed.py --no-external`（仍會套用既有快取）。

## 院區判定規則

依 affiliation 文字：`Hualien / Dalin / Taipei / Taichung / Yuli / Guanshan / Douliu Tzu Chi Hospital`、
`Tzu Chi University`、`Tzu Chi University of Science and Technology`。
只寫 `Buddhist Tzu Chi Medical Foundation` 而無城市者歸入「慈濟醫療財團法人(未指明院區)」。
要調整規則請修改 `scripts/fetch_pubmed.py` 的 `CITY_TOKENS` 與 `classify_affiliation()`。

## 本機執行

```bash
pip install -r requirements.txt
python scripts/fetch_pubmed.py                         # 抓取並產生所有檔案
python scripts/fetch_pubmed.py --from-json docs/data/articles.json   # 只重建 Excel/CSV（改規則後）
python -m http.server -d docs 8000                      # 本機預覽 http://localhost:8000
```
