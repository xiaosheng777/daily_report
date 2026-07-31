# daily_report 部署说明

适用场景：

- Linux 服务器无外网，或 Docker Hub 拉取不稳定。
- 你不想使用一个整体的 `daily-report-backend-image.tar`。
- 你希望手动逐个准备基础镜像，再在机器上构建/启动。
- Linux 可以访问内网大模型 API。
- Linux 已安装 Docker。

## 0. 名称约定

```text
项目目录：daily_report/
服务器目录：/opt/daily_report
backend 镜像：daily-report-backend:latest
backend 容器：daily-report-backend
nginx 容器：daily-report-web
Docker 网络：daily-report-net
JPlag jar：vendor/jplag/jplag.jar
```

目录名改成 `daily_report` 不影响 Python 代码 import；这里同步改的是文档、部署路径、镜像名和脚本默认值。

## 1. 准备 JPlag jar

推荐 JPlag `v6.0.0`，因为它使用 JDK 21；本项目 Dockerfile 已改为从 `eclipse-temurin:21-jdk` 复制 JDK 21，这样 Java 代码查重也有 `javac`。

有网机器进入项目根目录：

```bash
cd daily_report
bash deploy/scripts/download_jplag.sh
```

Windows PowerShell：

```powershell
powershell -ExecutionPolicy Bypass -File deploy/scripts/download_jplag.ps1
```

等价手动下载：

```bash
mkdir -p vendor/jplag
curl -L -o vendor/jplag/jplag.jar \
  https://github.com/jplag/JPlag/releases/download/v6.0.0/jplag-6.0.0-jar-with-dependencies.jar
java -jar vendor/jplag/jplag.jar --help
```

必须保持最终路径：

```text
vendor/jplag/jplag.jar
```

## 2. 逐个准备基础镜像

如果机器能直接访问 Docker Hub：

```bash
docker pull python:3.12-slim
docker pull eclipse-temurin:21-jdk
docker pull nginx:alpine
```

也可以运行：

```bash
bash deploy/scripts/pull_base_images_one_by_one.sh
```

如果无外网服务器不能 pull，就在有网机器逐个保存，再拷过去逐个 load：

```bash
docker pull python:3.12-slim
docker save -o python_3.12_slim.tar python:3.12-slim

docker pull eclipse-temurin:21-jdk
docker save -o eclipse_temurin_21_jdk.tar eclipse-temurin:21-jdk

docker pull nginx:alpine
docker save -o nginx_alpine.tar nginx:alpine
```

到无外网服务器：

```bash
docker load -i python_3.12_slim.tar
docker load -i eclipse_temurin_21_jdk.tar
docker load -i nginx_alpine.tar
```

## 3. 构建 backend 镜像

进入项目根目录：

```bash
cd daily_report
bash deploy/scripts/build_backend_image.sh
```

生成本地镜像：

```text
daily-report-backend:latest
```

注意：这个构建步骤会执行 `pip install -r backend/requirements.txt`。如果构建机器完全不能访问 PyPI，需要提前准备 wheelhouse；否则请在能访问 PyPI 的机器上构建。

原来的 `build_offline_image.sh` 还保留，但只作为可选导出 tar 的兼容脚本，不再是推荐部署方式。

## 4. 无外网 Linux 准备目录

```bash
sudo mkdir -p /opt/daily_report/{config,storage,frontend,deploy}
sudo chown -R $USER:$USER /opt/daily_report
```

复制文件：

```bash
cp -r frontend/* /opt/daily_report/frontend/
cp deploy/nginx.conf /opt/daily_report/deploy/nginx.conf
cp backend/config/config.example.yaml /opt/daily_report/config/config.yaml
```

如果你是直接拷整个项目，也可以：

```bash
cp -r daily_report /opt/daily_report_source
```

运行目录仍建议用 `/opt/daily_report`。

## 5. 修改配置

编辑：

```bash
nano /opt/daily_report/config/config.yaml
```

Docker 部署时，建议这些路径保持：

```yaml
storage:
  root_dir: /app/storage

database:
  backend: sqlite
  sqlite_path: /app/storage/daily_report.sqlite3

app:
  frontend_dir: /app/frontend
```

模型配置：

```yaml
llm_judge:
  enabled: true
  provider: openai_compatible
  base_url: http://你的内网模型IP:端口
  chat_path: /v1/chat/completions
  model: 你的模型名
  api_key_file: config/llm_api_key
  api_key_env: DAILY_REPORT_LLM_API_KEY
```

Top-N 和阈值配置：

```yaml
daily_duplicate:
  llm_candidate_top_n: 3
  llm_candidate_score_threshold: 0.72
  low_info_candidate_score_threshold: 0.82
  max_candidates: 20
  score_weights:
    text: 0.45
    semantic: 0.45
    recency: 0.10
```

测试用例配置：

```yaml
testcase_verification:
  enabled: true
  snapshot_scope: department
  baseline_strategy: latest_before_report_date
  allow_overwrite_same_day_snapshot: true
```

## 6. 测试内网模型

```bash
sudo install -m 600 /dev/null /opt/daily_report/config/llm_api_key
printf '%s' '你的key' | sudo tee /opt/daily_report/config/llm_api_key >/dev/null

curl -s http://你的内网模型IP:端口/v1/chat/completions \
  -H "Authorization: Bearer $(tr -d '\r\n' < /opt/daily_report/config/llm_api_key)" \
  -H "Content-Type: application/json" \
  -d '{
    "model":"你的模型名",
    "messages":[{"role":"user","content":"只返回 JSON：{\"ok\":true}"}],
    "temperature":0
  }'
```

这一步不通，系统里的模型连通性测试也会失败。

## 7. 启动 backend + nginx

```bash
docker network create daily-report-net 2>/dev/null || true
docker rm -f daily-report-web daily-report-backend 2>/dev/null || true

docker run -d \
  --name daily-report-backend \
  --restart always \
  --network daily-report-net \
  -v /opt/daily_report/config/config.yaml:/app/backend/config/config.yaml:ro \
  -v /opt/daily_report/config/llm_api_key:/app/backend/config/llm_api_key:ro \
  -v /opt/daily_report/storage:/app/storage \
  daily-report-backend:latest

docker run -d \
  --name daily-report-web \
  --restart always \
  --network daily-report-net \
  -p 80:80 \
  -v /opt/daily_report/frontend:/usr/share/nginx/html:ro \
  -v /opt/daily_report/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine
```

访问：

```text
http://服务器IP
```

也可以用脚本启动：

```bash
bash deploy/scripts/load_and_run_offline.sh /opt/daily_report daily-report-backend:latest
```

这个脚本现在不再 `docker load` 整体 backend tar，只假设本地已经有 `daily-report-backend:latest` 和 `nginx:alpine`。

## 8. 部署后在哪里改

### 改模型地址/模型名

```text
/opt/daily_report/config/config.yaml
```

改完：

```bash
docker restart daily-report-backend
```

### 改 API key

```bash
printf '%s' '新key' | sudo tee /opt/daily_report/config/llm_api_key >/dev/null
sudo chmod 600 /opt/daily_report/config/llm_api_key
docker restart daily-report-backend
```

### 改 Top-N/阈值/权重

推荐登录 `admin / admin123`，进入“系统管理 → 参数配置”修改。

也可以改：

```text
/opt/daily_report/config/config.yaml
```

### 文件在哪里

```text
/opt/daily_report/storage/submitted_reports/      员工日报 Word
/opt/daily_report/storage/artifacts/code/         代码附件
/opt/daily_report/storage/artifacts/document/     文档附件
/opt/daily_report/storage/testcase_snapshots/     部门级测试用例 Excel
/opt/daily_report/storage/tasks/<task_id>/        查重任务结果 JSON/Excel/Word
/opt/daily_report/storage/exports/weekly_reports/ 周报导出
/opt/daily_report/storage/daily_report.sqlite3    SQLite 数据库
```

## 9. 常用命令

看日志：

```bash
docker logs -f daily-report-backend
docker logs -f daily-report-web
```

重启：

```bash
docker restart daily-report-backend daily-report-web
```

停止：

```bash
docker stop daily-report-backend daily-report-web
```

备份：

```bash
tar -czf daily-report-storage-$(date +%F).tar.gz /opt/daily_report/storage
```

## 10. 最终使用流程

1. 管理员登录 `admin / admin123`，在“系统管理”中创建部门、小组和业务账号。
2. 员工登录后提交日报，可上传代码附件和文档附件。
3. 组长或主任登录，进入“测试总表”，上传本部门当天 Excel。
4. 组长、主任或部长进入“查重任务”，选择日期范围后查看查重结果与缺报名单。
5. 管理员在系统管理中维护参数、测试模型连接和备份。
