# 把投资情报提醒发到自己的邮箱

项目会轮询官方来源，在发现符合条件的重要变化后发邮件。你可以把它部署在 GitHub Actions、家里的 Mac 或一台 Linux 服务器上。收件人只需一个邮箱。采集和事实提醒都可以在没有 AI 账号的情况下运行。

每个人运行自己的实例。本机安装时，把邮箱和密钥写进自己的 `.env`，采集记录保存在本机。选用 Actions 时，把配置填到自己控制的仓库。社区仓库只提供空白配置示例。

## 直接在本机终端运行

进入已克隆的仓库目录，使用 Python 3.11 或更新版本：

```sh
python3 -m venv .venv
source .venv/bin/activate
python -m pip install .
umask 077
cp -n .env.example .env
```

在本机编辑 `.env`。使用 Gmail 自发自收时，填写 `GMAIL_ADDRESS` 和 `GMAIL_APP_PASSWORD`；使用其他发件服务时，填写下文的 SMTP 参数。运行 `python -m investment_intelligence test-email`，收到测试邮件后运行 `python -m investment_intelligence serve`。此命令在前台持续运行，按 Ctrl+C 停止。默认数据库位于当前目录的 `data/`，报告位于 `output/`。

## 在 GitHub 上运行，电脑关机也能收信

1. 把仓库复制到自己的 GitHub 账号，保留默认分支上的 `.github/workflows/pipeline.yml`，并在 **Actions** 页启用工作流。个人使用可以选私有仓库。
2. 打开 **Settings → Secrets and variables → Actions → Secrets**。用 Gmail 给自己发信时，创建两个 repository secrets，`GMAIL_ADDRESS` 填自己的 Gmail，`GMAIL_APP_PASSWORD` 填为此项目生成的应用专用密码。生成前需要在 Google 账号上开启两步验证。若你的组织限制应用专用密码，可使用下节的 SMTP 服务。[Google 设置说明](https://support.google.com/mail/answer/185833?hl=zh-Hans)
3. 要采集美国上市公司的 SEC 申报，再添加 `SEC_USER_AGENT`，格式为真实的姓名或组织名、空格、可联系的邮箱。需要自定义关注列表时，把自己的 YAML 内容保存在 `WATCHLIST` secret 中。
4. 默认用 `intelligence-state` 分支保存 SQLite 快照。确保 Actions 有写入仓库内容的权限，且该分支允许工作流更新。**公开仓库**应再创建 `STATE_ENCRYPTION_KEY` secret，通过下面命令在自己电脑生成并妥善备份。私有仓库也可以加密。已有 PostgreSQL 的用户可直接配置 `DATABASE_URL` secret，使用 TLS 连接自己的数据库。
5. 在 **Actions → Intelligence pipeline → Run workflow** 选择默认分支及 `test-email`，运行后检查收件箱和垃圾邮件。在同一菜单选择 `dispatch`。第一次使用 SQLite 分支时勾选 `initialize_state`。手动运行成功后，在自己部署仓库的 **Settings → Secrets and variables → Actions → Variables** 中添加 `ENABLE_SCHEDULE=true`。GitHub 随后每 15 分钟请求一次调度，恢复数据并检查新提醒。

```sh
python3 -c 'import base64,secrets; print(base64.urlsafe_b64encode(secrets.token_bytes(32)).decode())'
```

密钥、私有关注列表和邮箱配置都放在自己仓库的 Secrets 中。需要一并发送定期简报时，在 **Variables** 添加 `EMAIL_SCOPE=all`，默认值 `alerts` 仅邮件发送重要事件。可设置 `UPLOAD_REPORTS=false` 关闭 Markdown 制品上传；数据库加密后，单独上传的报告仍按仓库的制品权限开放。

## 在自己的 Mac 或 Linux 服务器上运行

安装 Docker 和 Docker Compose 2.24 或以上版本，把项目克隆到设备上，在仓库根目录创建配置。电脑保持开机、联网，Docker 服务持续运行。

```sh
umask 077
cp -n .env.example .env
```

用本机编辑器填写 `.env`。Gmail 自发自收时只需填写 `GMAIL_ADDRESS` 和 `GMAIL_APP_PASSWORD`。若收件人与发件人不同，再填 `SMTP_TO`。使用单位邮箱、事务邮件服务或自己的 SMTP 中继时，填写以下独立配置。将示例里的主机、账号、密码和地址替换为服务方提供的值，`SMTP_FROM` 应为该服务允许使用的发件人。

```dotenv
SMTP_HOST=smtp.example.org
SMTP_PORT=587
SMTP_SECURITY=starttls
SMTP_USER=your-smtp-username
SMTP_PASSWORD=your-smtp-credential
SMTP_FROM=alerts@example.org
SMTP_TO=reader@example.net
EMAIL_SCOPE=alerts
```

按服务方要求也可选 `SMTP_SECURITY=ssl` 搭配端口 465。下面第一条构建镜像，第二条会发送一封真实测试邮件。确认收到后再启动监控。

```sh
docker compose build
docker compose run --rm intelligence python -m investment_intelligence test-email
docker compose up -d
docker compose exec intelligence python -m investment_intelligence doctor
docker compose exec intelligence python -m investment_intelligence reports
```

用 `docker compose logs --tail 100 intelligence` 查运行记录，用 `docker compose down` 停止。默认的数据库和报告保存在 Docker 命名卷中。本机调度器每分钟检查各项任务是否到期，科技和加密新闻每 15 分钟轮询一次，SEC 在监控窗口内每 15 分钟检查。GitHub Actions 每 15 分钟请求一次运行，也可能因排队而延后。邮件到达时间还取决于来源更新及邮件投递速度。[任务时间表](../README.md#job-schedule)

## 用 Codex、Claude Code、Grok 或 DeepSeek 查看简报

带终端的助手可以直接读项目的 `skills/investment-intelligence/SKILL.md` 并在部署环境运行 CLI。例如，你可以让它“在这套服务里查最近一次 CPI 更新，给我统计月份、首次观察时间和原文链接”。在 Docker 部署中，助手应在命令前加上 `docker compose exec -T intelligence`，以便读取同一数据库。

如果希望在项目内自动发现技能，把整个 `skills/investment-intelligence` 文件夹复制到 Codex 的 `.agents/skills/` 或 Claude Code 的 `.claude/skills/`。使用 Grok、DeepSeek 等纯聊天界面时，在服务器执行 `python -m investment_intelligence reports` 找到报告 ID，再运行 `python -m investment_intelligence reports REPORT_ID`，把需要分析的 Markdown 交给助手即可。[项目技能](../skills/investment-intelligence/SKILL.md)

若希望每次采集后由模型自动分析，再配置模型服务商签发的 `LLM_API_KEY`、`LLM_BASE_URL` 和 `LLM_MODEL`，并按所选模型填写输入、输出每百万 token 的最高适用美元价格。服务支持 Claude 原生 Messages、DeepSeek、Grok 和支持 JSON 的 Chat Completions 接口。API 收费与聊天客户端的登录分开配置。预算通过 `LLM_DAILY_USD`、`LLM_MONTHLY_USD` 和每个任务的 `LLM_MAX_CALLS` 限制。留空 API 密钥即可持续收到事实提醒。[服务商地址与字段](../README.md#use-your-own-ai-tool)
