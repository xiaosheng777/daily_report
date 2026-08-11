# daily_report

企业内部日报核验系统。基于上传的 clean 版重构，保持原前端视觉风格，补齐真实部署需要的 storage、task、权限、部门级测试用例快照、OpenAI-compatible 内网模型、SQLite 短期运行和未来数据库替换接口。

## 关键说明

- 项目根目录已从 `daily-report-v2/` 改为 `daily_report/`。
- Python 业务代码不需要因为目录改名而改；我只同步了 README、DEPLOY、Dockerfile、compose、部署脚本里的项目名/路径/镜像名。
- Docker 镜像名改为 `daily-report-backend:latest`，不再带 `v2`。
- 部署目录改为 `/opt/daily_report`。
- JPlag 位置仍是 `vendor/jplag/jplag.jar`；不要改这个文件名。

## 核心变化

- 前后端目录拆分：`backend/`、`frontend/`、`deploy/`。
- 文件保存改为文件系统：`storage/` 存真实文件，SQLite 只存 metadata/path/hash。
- 每次查重都是独立 task：`storage/tasks/<task_id>/` 保存 JSON、Excel、Word、metadata、error。
- 日报主查重改为 top-N 多候选、多 finding、加权 score、跨人协作判断 prompt。
- top-N、阈值、权重都可在管理员页面调整。
- 测试用例 Excel 由组长或主任上传本部门每日总表。
- 测试用例 baseline 改为 report_date 之前最近一份部门快照。
- 组织架构改为“员工 → 组长 → 主任 → 部长”，并增加部门下的小组管理和自动直属领导推导。
- 文档查重保持本人历史范围，docx 文本读取支持 paragraphs + tables。
- 文档附件会提取正文（支持 docx、pdf、txt、md、xlsx），并以 JPlag 的 `text` 语言模式与本人近 30 天历史文档进行正式查重。
- 代码附件（单个源文件或 zip 源码包）会以 JPlag 的 `multi` 语言模式与本人近 30 天历史代码进行正式查重。
- 查重任务和记录按创建人隔离；新增独立查重记录页、任务显示清空和管理员业务数据双确认清理。
- 日报提交时间由系统按上海时区自动记录；09:00 前提交归入前一日，09:00 起归入当天。12:00 自动生成前一日全公司查重任务，结果按管理范围展示。
- LLM API Key 默认从项目根目录只读文件 `config/llm_api_key` 读取，环境变量仅作为兼容回退。

## 目录

```text
daily_report/
  backend/
    src/
    config/
    requirements.txt
  frontend/
    index.html
    app.js
    styles.css
  deploy/
    Dockerfile.backend
    nginx.conf
    docker-compose.yml
    scripts/
  vendor/
    jplag/
      README.md
      # jplag.jar 放这里
  storage/
  tests/
  README.md
  DEPLOY.md
```

## 初始管理员账号

| 角色 | 账号 | 密码 |
|---|---|---|
| 管理员 | `admin` | `admin123` |
| 测试员工 | `test_employee` | `test123` |

首次登录后，请在“系统管理”中创建部门、小组和业务账号，并修改管理员密码。

所有日报的提交时间均由服务器按上海时间自动记录，员工不能手动选择；09:00 前提交归前一日，09:00 起归当天。

## 本地运行

首次运行前创建密钥文件（即使暂时关闭大模型，也建议创建空文件以便 Docker 挂载）：

```bash
cp config/config.example.yaml config/config.yaml
cp backend/config/llm_api_key.example config/llm_api_key
chmod 600 config/llm_api_key
```

```bash
cd daily_report/backend
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python -m src.web.app --host 0.0.0.0 --port 8000
```

默认会自动加载项目根目录的 `config/config.yaml`（不再优先使用 `backend/config/`）。

访问：

```text
http://127.0.0.1:8000
```

本地模式下，backend 会直接服务 `frontend/` 静态文件；生产推荐 nginx。

## JPlag

正式代码查重请把真实 jar 放入：

```text
vendor/jplag/jplag.jar
```

推荐用 JPlag `v6.0.0`：

```bash
bash deploy/scripts/download_jplag.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File deploy/scripts/download_jplag.ps1
```

手动下载命令：

```bash
mkdir -p vendor/jplag
curl -L -o vendor/jplag/jplag.jar \
  https://github.com/jplag/JPlag/releases/download/v6.0.0/jplag-6.0.0-jar-with-dependencies.jar
java -jar vendor/jplag/jplag.jar --help
```

原因：JPlag 最新版要求 JDK 25；这版部署改成 JDK 21 + JPlag v6.0.0，兼容性更稳。jar 不存在时，系统会保留代码附件和记录，但正式代码查重会降级。

## Docker 构建

不再要求使用整体 `daily-report-backend-image.tar`。先逐个准备基础镜像：

```bash
bash deploy/scripts/pull_base_images_one_by_one.sh
```

然后构建 backend 镜像：

```bash
bash deploy/scripts/build_backend_image.sh
```

详见 `DEPLOY.md`。
