# daily_report 部署说明

旧版本升级、旧 SQLite 数据与附件迁移、回退操作请优先按 [OFFLINE_UPGRADE_GUIDE.md](OFFLINE_UPGRADE_GUIDE.md) 执行。

## 完整离线部署包（推荐）

本仓库现在提供一键构造完整离线包的脚本。它会在**有 Docker 且已准备基础镜像的联网 Linux/x86_64 构建机**上构建后端镜像，并把运行时所需的全部镜像一起导出：

```bash
bash deploy/scripts/build_complete_offline_bundle.sh
```

构建产物在 `dist/daily-report-offline-<时间>.tar.gz`，其中包含：

- `images/daily-report-images.tar`：`daily-report-backend:latest` 和 `nginx:alpine`；后端镜像已包含 Python 依赖，不需要在目标机重新构建；
- Compose 文件、后端/前端源码、wheelhouse、nginx 配置；
- `install.sh` 和 `start.sh`、`stop.sh`、`status.sh`、数据备份/恢复脚本。

构建脚本只检查 `python:3.12-slim` 与 `nginx:alpine` 两个本地基础镜像。JPlag jar 和 JDK 都不再是构建或运行依赖。API key 不会被打入 Docker 镜像或离线包。

在无网络 Linux 服务器上解压并安装（服务器仍需预先安装 Docker Engine 与 Docker Compose，Docker 本身不在本项目离线包中）：

```bash
tar -xzf daily-report-offline-*.tar.gz
cd daily-report-offline-*
sudo ./deploy/scripts/install.sh /opt/daily-report
cd /opt/daily-report
sudo bash deploy/scripts/start.sh
```

安装脚本只执行本地 `docker load`，不会 pull。再次运行安装时默认保留既有 `config/`、`storage/` 和源码；确需用新离线包覆盖程序文件时使用 `--refresh-app`，且应先停止服务：

```bash
sudo bash deploy/scripts/stop.sh
sudo /解压目录/daily-report-offline-*/deploy/scripts/install.sh /opt/daily-report --refresh-app
sudo bash deploy/scripts/start.sh
```

### 迁移旧数据库和附件

可以迁移，但 SQLite 数据库中保存了附件、日报和任务产物的相对路径，因此推荐迁移**整个旧 `storage/` 目录**，不要只复制 `daily_report.sqlite3`。迁移前先停止两端服务，旧端执行：

```bash
cd /opt/daily-report
sudo bash deploy/scripts/stop.sh
sudo bash deploy/scripts/backup_storage.sh /tmp
```

将得到的 `daily-report-storage-*.tar.gz` 拷到新服务器并解压，然后在新端（同样必须已停止）执行：

```bash
sudo bash deploy/scripts/restore_storage.sh /解压后的/storage
sudo bash deploy/scripts/start.sh
```

恢复脚本不会直接删除原 `storage/`，而是改名保留为 `storage.before-restore-<时间>`，可回滚。若只迁移 SQLite 文件也能启动，但历史上传文件、导出文件及任务附件会因缺失而无法下载；同时必须同时复制 SQLite 的 `-wal`/`-shm` 文件，或在旧端停服务后再复制。

### 在离线服务器改代码

Compose 会将 `/opt/daily-report/backend/src` 和 `frontend/` 挂载到容器。修改后端源码后无需构建镜像，只需重启依赖它的三个服务：

```bash
cd /opt/daily-report
sudo docker compose restart daily-report-backend daily-report-worker daily-report-monitor
```

修改前端文件后刷新浏览器即可；若浏览器缓存未刷新，可重启 `daily-report-web`。若改动涉及 `requirements.txt`、系统包或 Dockerfile，则必须在有网/有完整构建依赖的构建机重新构造离线包，不能只在离线服务器重启解决。

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
```

目录名改成 `daily_report` 不影响 Python 代码 import；这里同步改的是文档、部署路径、镜像名和脚本默认值。

## 1. 逐个准备基础镜像

如果机器能直接访问 Docker Hub：

```bash
docker pull python:3.12-slim
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

docker pull nginx:alpine
docker save -o nginx_alpine.tar nginx:alpine
```

到无外网服务器：

```bash
docker load -i python_3.12_slim.tar
docker load -i eclipse_temurin_21_jdk.tar
docker load -i nginx_alpine.tar
```

## 2. 构建 backend 镜像

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

## 3. 无外网 Linux 准备目录

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

## 4. 修改配置

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

日报提交与自动查重规则：

```yaml
daily_report_submission:
  enabled: true
  rollover_time: "09:00" # 09:00 前提交归前一日，09:00 起归当天

automatic_daily_duplicate:
  enabled: true
  run_at: "12:00" # 对前一自然日的全公司日报执行查重
  catch_up_on_start: true # worker 恢复后补建当天从未创建的任务；已失败任务不自动重试

task_runner:
  stale_heartbeat_seconds: 90
  shutdown_grace_seconds: 30

resource_monitor:
  enabled: true
  collector_url: http://daily-report-monitor:8010
  sample_interval_seconds: 10
  retention_days: 7
  metrics_path: /app/storage/resource_metrics.sqlite3
  container_allowlist:
    - daily-report-web
    - daily-report-backend
    - daily-report-worker
    - daily-report-monitor
```

自动任务由独立 `daily-report-worker` 容器入队并执行，无需额外配置系统 cron。数据库租约保证同一存储上只有一个 worker 执行重型任务；不要绕过资源限制直接在 backend 容器内运行 worker。

查重任务只有一个时间限制：从任务子进程启动起满 60 分钟仍未结束，worker 会终止该进程并将任务标记为失败。LLM 单次请求不设独立超时，也没有 45 分钟停止新增调用的截止线。

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
  max_retries: 1 # 单次调用遇到可重试错误时最多补试一次；不会熔断后续候选
```

所有达到 LLM 复核阈值的候选都会调用模型；不设置单任务 LLM 调用次数上限，也不因连续失败触发熔断。调用失败的候选会保留本地相似度判断，并计入“降级本地判断”。

并行数、Top-N 和阈值配置：

```yaml
daily_duplicate:
  report_worker_count: 3 # 不同日报的查重并行数，范围 1-8；结果仍按日报原顺序输出
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

## 5. 测试内网模型

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

## 6. 启动 backend + worker + monitor + nginx

```bash
docker network create daily-report-net 2>/dev/null || true
docker rm -f daily-report-web daily-report-monitor daily-report-worker daily-report-backend 2>/dev/null || true

docker run -d \
  --name daily-report-backend \
  --restart always \
  --network daily-report-net \
  -v /opt/daily_report/config/config.yaml:/app/backend/config/config.yaml:ro \
  -v /opt/daily_report/config/llm_api_key:/app/backend/config/llm_api_key:ro \
  -v /opt/daily_report/storage:/app/storage \
  daily-report-backend:latest

docker run -d \
  --name daily-report-worker \
  --restart always \
  --network daily-report-net \
  --cpus 2 \
  --memory 8g \
  -v /opt/daily_report/config/config.yaml:/app/backend/config/config.yaml:ro \
  -v /opt/daily_report/config/llm_api_key:/app/backend/config/llm_api_key:ro \
  -v /opt/daily_report/storage:/app/storage \
  daily-report-backend:latest python -m src.worker --config config/config.yaml

docker run -d \
  --name daily-report-web \
  --restart always \
  --network daily-report-net \
  -p 80:80 \
  -v /opt/daily_report/frontend:/usr/share/nginx/html:ro \
  -v /opt/daily_report/deploy/nginx.conf:/etc/nginx/conf.d/default.conf:ro \
  nginx:alpine
```

如果不用 Compose，还需在启动 nginx 前启动监控侧车：

```bash
docker run -d \
  --name daily-report-monitor \
  --restart always \
  --network daily-report-net \
  --cpus 0.5 \
  --memory 256m \
  -v /opt/daily_report/config/config.yaml:/app/backend/config/config.yaml:ro \
  -v /opt/daily_report/storage:/app/storage \
  -v /var/run/docker.sock:/var/run/docker.sock:ro \
  -v /proc:/host/proc:ro \
  -v /sys/fs/cgroup:/host/cgroup:ro \
  daily-report-backend:latest python -m src.monitoring.collector --config config/config.yaml
```

监控侧车不发布宿主机端口，代码只调用固定的 Docker GET 接口，也不会重启或停止容器。需要注意：Unix socket 的 `:ro` 只能防止替换 socket 文件，不能在 Docker API 层阻止写操作；获得该 socket 的容器仍具有很高的宿主机权限。不要为监控侧车增加公网端口或安装额外服务，容器白名单也应只填写确实需要观察的服务。

以上配置按 16 GB 宿主机规划：查重 worker 保持 2 CPU，并设置 8 GB 内存硬上限；backend 和 nginx 不额外设置内存上限。大型任务运行时可通过 `docker stats daily-report-worker` 观察峰值，并用 `docker inspect daily-report-worker --format '{{.State.OOMKilled}}'` 检查是否发生 OOM。若峰值长期接近 8 GB，应先评估任务规模和并行内存占用，再考虑提高到 10–12 GB；16 GB 宿主机不建议取消上限。

访问：

```text
http://服务器IP
```

也可以用脚本启动：

```bash
bash deploy/scripts/load_and_run_offline.sh /opt/daily_report daily-report-backend:latest
```

这个脚本现在不再 `docker load` 整体 backend tar，只假设本地已经有 `daily-report-backend:latest` 和 `nginx:alpine`。

## 7. 部署后在哪里改

### 改模型地址/模型名

```text
/opt/daily_report/config/config.yaml
```

改完：

```bash
docker restart daily-report-backend daily-report-worker daily-report-monitor
```

### 改 API key

```bash
printf '%s' '新key' | sudo tee /opt/daily_report/config/llm_api_key >/dev/null
sudo chmod 600 /opt/daily_report/config/llm_api_key
docker restart daily-report-backend daily-report-worker daily-report-monitor
```

### 改并行数/Top-N/阈值/权重

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

## 8. 常用命令

看日志：

```bash
docker logs -f daily-report-backend
docker logs -f daily-report-worker
docker logs -f daily-report-monitor
docker logs -f daily-report-web
```

重启：

```bash
docker restart daily-report-backend daily-report-worker daily-report-monitor daily-report-web
```

停止：

```bash
docker stop daily-report-backend daily-report-worker daily-report-monitor daily-report-web
```

备份：

```bash
tar -czf daily-report-storage-$(date +%F).tar.gz /opt/daily_report/storage
```

## 9. 最终使用流程

1. 管理员登录 `admin / admin123`，在“系统管理”中创建部门、小组和业务账号。
2. 员工登录后由系统自动记录提交时间；09:00 前提交归入前一日，09:00 起归入当天。前一日归属日报在 09:00 后不可再编辑。
3. 组长或主任登录，进入“测试总表”，上传本部门当天 Excel。
4. 系统每天 12:00 自动查重前一日全员日报；所有管理者可在“查重任务”查看自己权限范围内的结果。组长、主任或部长仍可手动选择日期范围查重。
5. 管理员在系统管理中维护参数、测试模型连接和备份。
