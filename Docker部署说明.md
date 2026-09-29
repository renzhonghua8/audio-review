# CentOS 上用 Docker 部署声检

公开仓库：[renzhonghua8/audio-review](https://github.com/renzhonghua8/audio-review)。对外和容器端口均为 **8001**。只需服务器已安装 Docker；不需要在宿主安装 Python、FFmpeg 或模型组件。镜像使用 Python 3.12 与锁定依赖，模型随源码提供，评测不调用付费 API。

## 完整部署命令

以下命令以 **root** 执行。先把 `10.0.0.10` 改为服务器实际网卡上的内网 IPv4 地址，不要填写 NAT 映射的公网 IP。源码在 `/opt/audio-review/src`，数据在 `/opt/audio-review/data`，兼容旧部署包的数据目录。

```bash
set -e
SERVER_IP=10.0.0.10

# 已安装 Docker：启动服务并设置开机启动
systemctl enable --now docker

# Git/curl 未安装时才安装
if ! command -v git >/dev/null 2>&1 || ! command -v curl >/dev/null 2>&1; then
  if command -v dnf >/dev/null 2>&1; then
    dnf install -y git curl
  else
    yum install -y git curl
  fi
fi

mkdir -p /opt/audio-review
if [ -d /opt/audio-review/src/.git ]; then
  git -C /opt/audio-review/src pull --ff-only
else
  git clone https://github.com/renzhonghua8/audio-review.git /opt/audio-review/src
fi

cd /opt/audio-review/src
AUDIO_REVIEW_WORKERS=2 AUDIO_REVIEW_MODEL_THREADS=2 \
  bash scripts/deploy-docker.sh "$SERVER_IP"

curl -fsS "http://${SERVER_IP}:8001/api/health"
docker ps --filter name=audio-review
docker logs --tail 50 audio-review
```

部署脚本先验证 IP 和配置，再构建镜像 `audio-review:2.0`。如果存在旧容器，会检查它的数据挂载目录，确认没有未完成评测，然后停止旧容器、备份数据并重新创建容器。已有音频、自动结果和人工评分保留；数据目录不一致或评测未完成时，脚本停止并说明原因。升级期间请暂停提交新评测。

脚本通过健康检查才输出“部署完成”。健康检查应有 `ready: true`、`evaluator_version: "2.0"`、`workers: 2`。浏览器访问 `http://服务器内网IP:8001/`，网页应显示“服务器运行 / 文件保存在服务器”。

首次构建需联网拉取基础镜像、系统组件和 Python 依赖。构建失败时，旧服务继续运行。CentOS 如果无法使用 yum/dnf，需要先修复该系统的软件源；此项目不修改系统软件源。

## 镜像构建与容器启动的具体命令

部署脚本使用以下配置。手动部署时，先确保同名旧容器已停并备份数据，再移除旧容器；不要同时运行两个指向同一数据目录的实例。

```bash
set -e
SERVER_IP=10.0.0.10
cd /opt/audio-review/src

mkdir -p /opt/audio-review/data
chown 10001:10001 /opt/audio-review/data

docker build -t audio-review:2.0 .

docker run -d \
  --name audio-review \
  --restart unless-stopped \
  --init \
  -p "${SERVER_IP}:8001:8001" \
  -e "AUDIO_REVIEW_ALLOWED_ORIGINS=http://${SERVER_IP}:8001" \
  -e AUDIO_REVIEW_WORKERS=2 \
  -e AUDIO_REVIEW_MODEL_THREADS=2 \
  -v /opt/audio-review/data:/data:Z \
  --log-opt max-size=10m \
  --log-opt max-file=3 \
  audio-review:2.0
```

容器以 UID/GID `10001:10001` 运行。`:Z` 为专用数据目录配置 SELinux 标签，无需关闭 SELinux。[Docker 挂载说明](https://docs.docker.com/engine/storage/bind-mounts/)

指定服务器实际 IP 绑定宿主网卡，不能直接使用未配置在网卡上的 NAT 公网 IP。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)

## 再次升级

升级期间先暂停提交新评测，等待现有队列完成后，以 root 执行：

```bash
set -e
SERVER_IP=10.0.0.10
cd /opt/audio-review/src
git pull --ff-only
AUDIO_REVIEW_WORKERS=2 AUDIO_REVIEW_MODEL_THREADS=2 \
  bash scripts/deploy-docker.sh "$SERVER_IP"
```

如果原来通过压缩包部署在 `/opt/audio-review` 根目录，首次运行上面的完整命令会把 GitHub 源码放进 `src`，继续挂载原来的 `/opt/audio-review/data`。无需删除原目录或重新导入音频。

如果备份因磁盘不足等原因失败，旧容器和数据仍保留，可执行 `docker start audio-review` 恢复旧服务，再处理失败原因。

每次替换旧容器前，完整数据备份存放在 `/opt/audio-review/backups/data-时间戳.tar.gz`。旧镜像仍可按其镜像 ID 找到；需要回退时，应停止新容器后再使用对应备份和旧镜像。

## 域名和访问地址

默认允许浏览器使用 `http://服务器内网IP:8001`。使用额外域名或反向代理 HTTPS 时，在部署命令增加实际访问地址，例如：

```bash
AUDIO_REVIEW_EXTRA_ORIGINS='https://audio-review.example.com,http://audio-review.example.com:8001' \
AUDIO_REVIEW_WORKERS=2 AUDIO_REVIEW_MODEL_THREADS=2 \
  bash scripts/deploy-docker.sh "$SERVER_IP"
```

反向代理需要另外配置。来源值必须是完整协议、主机和端口，多个地址用逗号分隔。`http://127.0.0.1:8001`、`http://localhost:8001` 自动允许。未配置的主机或写入来源会被拒绝。

当前应用没有登录和用户隔离，面向公司内网或 VPN 使用。来源检查不能代替身份认证。

## 并发与速度

默认同时评测 2 个不同文件，模型算子内部使用 2 线程。`AUDIO_REVIEW_WORKERS` 和 `AUDIO_REVIEW_MODEL_THREADS` 都支持 1–8。服务器资源较少时可用 `1` 和 `2`；有 8 核及足够内存与临时磁盘时可尝试 `4` 和 `2`，比较实际批次耗时再调整。

始终保持 **一个 Web 服务进程**，不要增加 Uvicorn worker 数。多进程会产生独立队列并重置其他进程的任务；评测并发由应用内部线程控制。

“全量快速”约 9 秒一个模型窗口，连续覆盖整段音频，尾部也评分；“快速抽样”覆盖开头与中段共 2 分钟；“全量精细”按 1 秒步长密集评分。基础声学检测始终覆盖全量。模式不同，均分、P10 和最低分可能不同。

勾选或全选后批量检测，每次导入最多 20 条；一次提交最多 200 条，网页会自动分批提交更大的已有列表。相同内容、模式和评测版本共享自动结果，人工评分独立。结果页“重新检测”会强制计算。

压缩音频会临时展开为 WAV。例如 1 小时 48 kHz 双声道 float WAV 约 1.38 GB，另有 16 kHz 单声道模型 WAV；需按同时处理的文件数预留空间。完成或失败后会清理解码文件。

## 访问排查与维护

```bash
docker logs --tail 100 audio-review
docker inspect --format '{{json .State.Health}}' audio-review
docker restart audio-review
```

重启保留已保存结果与人工评分；未完成任务会回到“等待重新检测”，需要重新提交。升级脚本会拒绝中断正在运行的评测。

本机健康检查正常但其他电脑无法访问时，检查内网路由、实际访问 IP，以及云安全组和公司网络对 TCP 8001 的规则。firewalld 可用以下命令查看实际网卡区域，再按公司的访问策略配置：

```bash
firewall-cmd --get-active-zones
firewall-cmd --zone=public --list-all
```

`public` 应换为实际网卡区域。Docker 会维护自己的转发规则；firewalld 的普通端口规则不能当作容器端口的完整访问白名单。[Docker 防火墙说明](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## 验证

Mac 本机已验证真实模型、批量并行、重复内容共享、人工评分独立、失败重试及数据保留，详见 `验证说明.md`。

仓库的 GitHub Actions 会在标准 Ubuntu runner 中实际构建 Docker 镜像，并用合成测试音验证 FFmpeg 解码、ONNX 推理、API、重复结果复用和人工评分独立保存。合成音只验证执行链路，不代表音质预测准确率。[查看构建结果](https://github.com/renzhonghua8/audio-review/actions)

实际 CentOS 主机的网卡、Docker 服务、软件源、磁盘和网络访问仍以服务器部署后的健康检查为准。
