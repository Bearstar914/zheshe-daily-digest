# zheshe-daily-digest · 哲社预印本每日推送

每日从「[哲社预印本](https://zsyyb.cn)」（PSSXiv，中国人民大学，基于 ChinaXiv）抓取最新论文，
按你的研究兴趣筛选出 **推荐 10 篇**，并附 **当日新论文清单**，早上 **北京时间 06:20** 推送到邮箱。

- **无 AI / 无 LLM**：推送平台自己的中文摘要，不需要任何大模型密钥。
- **推荐 10 篇**：标题 + 作者 + 分类 + 摘要 + **📄 下载 PDF 按钮** + DOI。
- **新论文清单**：当天全部新论文的标题 + 摘要 + DOI（点击 DOI 或标题即可回到详情页下载）。
- **去重**：已推送过的论文记录在 `data/sent_ids.json`，不会重复推送。

## 目录结构

```
zheshe-daily-digest/
├── .github/workflows/daily.yml   # GitHub Actions 定时任务（06:20 北京时间）
├── config/
│   ├── keywords.json             # 关键词（strong/weak/en 三档，可按需增删）
│   └── subjects.json             # 学科（focus=重点学科；all=全部学科）
├── data/
│   └── sent_ids.json             # 已推送论文 ID（去重状态，自动生成）
├── src/digest.py                 # 主脚本
└── requirements.txt
```

## 部署到 GitHub

1. 把本项目推送到你自己的 GitHub 仓库。
2. 在仓库 **Settings → Secrets and variables → Actions** 添加三个 Secrets：

| Secret | 说明 |
|---|---|
| `SENDER` | 发件邮箱（QQ 邮箱，如 `123456@qq.com`） |
| `SENDER_PASSWORD` | QQ 邮箱 **SMTP 授权码**（不是登录密码） |
| `RECEIVER` | 收件邮箱（你的 QQ 邮箱） |

3. 授权码获取：QQ 邮箱 → 设置 → 账户 → 开启 SMTP 服务 → 生成授权码。

4. 推送后，GitHub Actions 会每天北京时间 06:20 自动运行；也可在 Actions 页手动 `Run workflow`。

## 本地测试

```bash
pip install -r requirements.txt
SENDER="你的邮箱" SENDER_PASSWORD="授权码" RECEIVER="收件邮箱" python src/digest.py
```

## 调参

- `TOP_N`：推荐篇数（默认 10）。
- `LIST_MAX`：清单上限（默认 100）。
- `FETCH_PAGES`：抓取页数（默认 8 页 = 160 篇，覆盖最近 1~2 天）。
- 关键词在 `config/keywords.json` 中增删；重点学科在 `config/subjects.json` 的 `focus` 里改。

## 数据说明

- 论文 ID 形如 `202609.05121`，DOI 由平台统一规则派生：`10.12451/{ID}`。
- PDF 下载地址为平台页面内 `downloadFile('uuid')` 提供的 `https://zsyyb.cn/user/download.htm?uuid={uuid}`。
