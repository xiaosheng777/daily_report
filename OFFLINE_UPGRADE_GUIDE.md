# 旧版本升级至本版本：完整离线部署流程

此流程适用于旧版本正在 Linux 服务器运行、新版本在本地或联网构建机打包、目标服务器没有互联网的情形。新旧版本可在同一台服务器上切换；新版本默认安装到 `/opt/daily-report`，旧版本目录不要覆盖。

本流程迁移的是整个 `storage/`，其中包含 SQLite 数据库、日报原件、附件、任务结果和导出文件。新程序首次启动会自动执行 SQLite 的增量结构迁移。

## 1. 部署前确认

目标服务器必须是 Linux x86_64，并且已经安装：Docker Engine、Docker Compose（`docker compose` 或 `docker-compose`）。它不需要 Docker Hub、PyPI 或其他互联网访问。

构建机需要 Docker。Windows 本地构建时，启动 Docker Desktop 并确认正在使用 Linux containers；Linux/WSL 构建时使用 Bash 脚本。构建机和服务器必须同为 x86_64；本项目 wheelhouse 是 Linux x86_64 版本。

在旧服务器记下旧项目目录。以下用 `<旧目录>` 表示，例如 `/opt/daily_report`；不要猜测路径，先确认数据库文件存在：

```bash
ls -lh <旧目录>/storage/daily_report.sqlite3
```

若文件不在该位置，先根据旧版 Compose 的 `volumes:` 找到实际挂载的 storage 目录，再将下文的 `<旧目录>/storage` 替换为实际目录。

## 2. 在本地/联网构建机制作离线包

进入新项目根目录。Windows PowerShell 推荐使用：

```powershell
docker pull python:3.12-slim
docker pull nginx:alpine
powershell -ExecutionPolicy Bypass -File deploy/scripts/build_complete_offline_bundle.ps1
```

Linux 或 WSL 使用：

```bash
docker pull python:3.12-slim
docker pull nginx:alpine
bash deploy/scripts/build_complete_offline_bundle.sh
```

构建成功后得到：

```text
dist/daily-report-offline-<时间>.tar.gz
```

这个压缩包内的 `images/daily-report-images.tar` 同时含后端和 nginx 两个运行时镜像。JPlag、JDK、`vendor/` 与 API key 均不需要，也不会被打入包中。

将这个 `.tar.gz` 通过内网 SCP、U 盘或受控文件传输带到目标服务器。例如：

```bash
scp dist/daily-report-offline-<时间>.tar.gz <用户>@<服务器IP>:/tmp/
```

## 3. 冻结旧版本并备份（停机窗口开始）

先禁止用户继续访问旧系统。然后停止并移除旧容器；**不要加 `-v`**，因为不能删除数据卷：

```bash
cd <旧目录>
sudo docker compose down --remove-orphans
```

若旧服务器使用的是 Compose v1，则把上面的 `docker compose` 换成 `docker-compose`。`down` 仅删除容器和网络；宿主机上的 `storage/` 不会删除。这样也能避免新旧项目的同名容器和 80 端口冲突。

确认旧容器已经停止，再打包整个存储目录：

```bash
sudo tar -C <旧目录> -czf /tmp/daily-report-storage-before-upgrade.tar.gz storage
sudo sha256sum /tmp/daily-report-storage-before-upgrade.tar.gz
```

将这个备份文件保留在安全位置，并复制到新部署目录所在服务器；同机升级时它可留在 `/tmp/`。停机后备份能保证 SQLite 数据库、`-wal` / `-shm` 及关联文件一致。

## 4. 解压、安装新版本并恢复旧数据

在目标服务器执行。下面以 `/tmp` 为传输目录：

```bash
cd /tmp
tar -xzf daily-report-offline-<时间>.tar.gz
cd daily-report-offline-<时间>
sudo bash deploy/scripts/install.sh /opt/daily-report
```

安装过程只执行本地 `docker load`，不会访问网络。它会创建空的 `/opt/daily-report/storage` 和配置文件，但此时**不要启动服务**。

解压旧数据备份，并恢复到新项目：

```bash
sudo mkdir -p /tmp/daily-report-restore
sudo tar -xzf /tmp/daily-report-storage-before-upgrade.tar.gz -C /tmp/daily-report-restore
cd /opt/daily-report
sudo bash deploy/scripts/restore_storage.sh /tmp/daily-report-restore/storage
```

恢复脚本会将新安装时生成的空 `storage/` 改名为 `storage.before-restore-<时间>`，不会直接删除它。

## 5. 配置并首次启动

离线包不含 API key。先检查并按实际环境编辑配置：

```bash
cd /opt/daily-report
sudoedit config/config.yaml
sudoedit config/llm_api_key
sudo chmod 600 config/llm_api_key
```

重点确认内网模型地址、模型名与 key；若不使用模型，保持 `llm_judge.enabled: false` 即可。不要直接用旧版本 `config.yaml` 覆盖新文件，先逐项合并你需要的模型地址和业务参数，避免旧配置缺少新字段。

启动并查看服务状态：

```bash
sudo bash deploy/scripts/start.sh
sudo bash deploy/scripts/status.sh
sudo docker compose logs --tail 200 daily-report-backend daily-report-worker daily-report-monitor
```

首次启动时后端会升级 SQLite 表结构。待容器状态正常后，访问 `http://<服务器IP>/`，验证：旧账号可登录、历史日报可打开、附件可下载、管理员可查看历史任务。确认完成前不要删除旧项目目录和 `/tmp/daily-report-storage-before-upgrade.tar.gz`。

## 6. 回退方案

如果新版本验证失败，停止并移除新容器：

```bash
cd /opt/daily-report
sudo docker compose down --remove-orphans
```

然后回到旧项目目录启动旧版本：

```bash
cd <旧目录>
sudo docker compose up -d
```

旧目录及其原始 `storage/` 在本流程中没有被修改，因此可直接回退。新版本已恢复的是旧数据的副本，即使其首次启动执行了数据库迁移，也不会影响旧版本使用的原数据库。

## 7. 后续更新代码或再次升级

服务器上可直接修改 `/opt/daily-report/backend/src`。后端修改后执行：

```bash
cd /opt/daily-report
sudo docker compose restart daily-report-backend daily-report-worker daily-report-monitor
```

前端改动在 `/opt/daily-report/frontend`，刷新浏览器即可。涉及 Python 依赖、Dockerfile 或基础系统依赖的改动，必须在联网构建机重新制作离线包，再按本流程安装；重新安装默认保留服务器上的 `config/`、`storage/` 和源码，若要覆盖程序文件使用 `--refresh-app`。
