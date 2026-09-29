# CentOS 上用 Docker 部署声检

公开仓库：[renzhonghua8/audio-review](https://github.com/renzhonghua8/audio-review)。端口为 **8001**。预构建镜像在 GitHub 发布页提供，服务器下载镜像后直接运行，不在宿主安装 Python、FFmpeg、模型组件，也不构建镜像或调用付费 API。

## 已有其他服务的服务器

部署默认新增一个名为 `audio-review` 的独立容器，使用 `/opt/audio-review/data`。不启动或重启 Docker，不修改 Docker 配置、系统软件源、防火墙、Nginx 或其他容器。不执行清理镜像、卷、容器等全局操作。8001 被任何其他服务占用时，停止部署；容器名冲突时也不会自动替换。

默认限制为 **0.5 核 CPU、1 GiB 内存、256 个进程**，禁用声检容器的交换空间，降低竞争时的 CPU 权重。每次只处理 1 条音频，仍支持批量提交。下载镜像限速 2 MB/s，校验 SHA256 后才导入。[Docker 资源限制说明](https://docs.docker.com/engine/containers/resource_constraints/)

脚本要求当前可用内存至少 1.5 GiB、声检目录及 Docker 镜像目录所在磁盘分别至少 5 GiB 空间，并验证宿主支持 CPU、内存和交换空间限制；不满足时退出。声检数据目录与其他容器挂载重叠时也会退出。当前预构建镜像支持 Linux x86_64。不会为了部署修改内核配置或升级 Docker。

同机运行仍共用磁盘、网络和内核，容器限制不能保证旧服务完全不受影响。镜像下载、导入和音频写入仍有磁盘与网络负载；要求完全隔离时，应使用另一台服务器。该空间检查也不是存储配额，需持续关注音频与备份占用。

## 完整部署命令

以 **root** 执行。服务器应已安装并运行 Docker，已有 Git、curl、ip、ss 和常见系统工具。下面的命令使用独立 Bash，失败不会因 `set -e` 退出交互终端。兼容 CentOS 7 自带的 Git 1.8.3，避免使用 `git -C`。

```bash
set +e
bash <<'DEPLOY'
set -e
for task_tool in git curl docker ip ss sha256sum; do
  command -v "$task_tool" >/dev/null || { echo "缺少工具：$task_tool"; exit 1; }
done

docker info >/dev/null
mkdir -p /opt/audio-review
if [ -d /opt/audio-review/src/.git ]; then
  (cd /opt/audio-review/src && git pull --ff-only)
else
  git clone https://github.com/renzhonghua8/audio-review.git /opt/audio-review/src
fi

cd /opt/audio-review/src
bash scripts/deploy-docker.sh

docker ps --filter name=audio-review
docker logs --tail 30 audio-review
DEPLOY
```

无需填写示例 IP。脚本从默认路由识别实际网卡 IPv4；无默认路由且有多个候选地址时退出，提示执行 `ip -4 addr` 查看，再将实际地址作为参数传入。路由查询不会向查询地址发送网络请求。不能将 NAT 映射的公网 IP 当作本机网卡 IP。

部署脚本默认使用发布版 `v2.0.2` 的 `audio-review:2.0.2` 镜像；评测算法版本仍为 `2.0`。镜像下载或校验失败时没有停止原服务。脚本只有通过健康检查后才输出“部署完成”以及实际访问 URL。浏览器访问 `http://实际内网IP:8001/`，健康检查应显示 `ready: true`、`evaluator_version: "2.0"`、`workers: 1`。

容器以 UID/GID `10001:10001` 运行。`:Z` 为声检专用数据目录配置 SELinux 标签，无需关闭 SELinux。不要将其他服务的数据目录作为声检目录。[Docker 挂载说明](https://docs.docker.com/engine/storage/bind-mounts/)

Docker 发布端口会维护其自身的网络转发规则。本脚本不修改已有防火墙配置，但新增容器会增加该端口对应的 Docker 规则；来源检查不是身份认证。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)

## 从截图中的失败继续

如果源码已经下载，终端提示“服务器网卡上没有 10.0.0.10”，说明停在 IP 校验阶段，没有构建镜像或停止容器。重新连接终端后执行：

```bash
set +e
bash <<'DEPLOY'
set -e
cd /opt/audio-review/src
git pull --ff-only
bash scripts/deploy-docker.sh
docker logs --tail 30 audio-review
DEPLOY
```

若提示端口、内存、磁盘不足或容器名冲突，先处理该提示，不要通过停止其他服务、删除数据或取消资源限制强行部署。

## 升级已部署的声检

默认不会停止任何已有容器。只有显式传入 `--upgrade` 时才替换声检实例，且容器必须带本项目的管理标记，挂载 `/opt/audio-review/data`。标记或挂载不符时拒绝操作。旧版没有管理标记的容器和无法确认归属的已有数据目录不会自动接管。

升级前暂停提交新评测，等待队列完成。以下操作只会短暂停止已有声检容器，不会停止其他服务。

```bash
set +e
bash <<'DEPLOY'
set -e
cd /opt/audio-review/src
git pull --ff-only
bash scripts/deploy-docker.sh --upgrade
docker logs --tail 30 audio-review
DEPLOY
```

镜像校验成功、确认无未完成评测后，停止声检，完整备份到 `/opt/audio-review/backups/data-时间戳.tar.gz`，再替换自己的容器。已有音频、自动结果和人工评分保留。备份失败会尝试恢复原声检；新实例启动或健康检查失败也会尝试恢复旧容器。恢复仍以 `docker ps` 和日志为准。

## 域名与云服务器公网地址

自动识别的 IP 是绑定宿主网卡的地址。如果通过 VPN 或公司内网使用该地址，不需要额外配置。使用公网映射或域名访问时，应另外配置**实际浏览器访问来源**；绑定 IP 仍为网卡地址，不要改为 NAT 公网 IP。例如：

```bash
AUDIO_REVIEW_EXTRA_ORIGINS='https://audio-review.example.com,http://audio-review.example.com:8001' \
  bash scripts/deploy-docker.sh
```

域名仅为示例，需要替换为实际地址。反向代理需另行配置，本脚本不会修改已有代理。来源值包括协议、主机和非默认端口，多个来源用逗号分隔。`http://127.0.0.1:8001`、`http://localhost:8001` 自动允许。

当前应用没有登录和用户隔离，面向公司内网或 VPN 使用。通过公网访问时应有已有的访问控制；容器端口发布和来源检查均不能代替身份认证。

## 批量与评测速度

保护已有服务的部署默认使用 1 个任务线程、1 个模型线程。批量提交后会逐条执行；不会通过增加 Uvicorn 进程数加速。资源宽裕并已确认原服务负载时，可调整 `AUDIO_REVIEW_WORKERS` 与 `AUDIO_REVIEW_MODEL_THREADS`（1–8），容器 CPU/内存上限仍保持不变。提高线程数不会突破资源上限，也不保证更快。

“全量快速”约 9 秒一个模型窗口，连续覆盖整段音频，尾部也评分；“快速抽样”覆盖开头与中段共 2 分钟；“全量精细”按 1 秒步长密集评分。基础声学检测始终覆盖全量。模式不同，均分、P10 和最低分可能不同。

勾选或全选后批量检测，每次导入最多 20 条；一次提交最多 200 条，网页会自动分批提交更大的已有列表。相同内容、模式和评测版本共享自动结果，人工评分独立。结果页“重新检测”会强制计算。

压缩音频会临时展开为 WAV。例如 1 小时 48 kHz 双声道 float WAV 约 1.38 GB，另有模型 WAV；完成或失败后清理解码文件。音频和评分数据库长期保留，备份不会自动删除。

## 只读检查与访问排查

```bash
docker ps
docker logs --tail 100 audio-review
docker inspect --format '{{json .State.Health}}' audio-review
docker stats --no-stream audio-review
ss -ltnp | awk 'NR==1 || /:8001([[:space:]]|$)/'
```

健康检查正常但其他电脑无法访问时，核对实际访问地址、公司内网/VPN 路由、云安全组以及 TCP 8001 的访问策略。不要为排查停用防火墙或重启 Docker。[Docker 防火墙说明](https://docs.docker.com/engine/network/packet-filtering-firewalls/)

## 验证

Mac 本机已验证真实模型、批量并行、重复内容共享、人工评分独立、失败重试及数据保留，见 `验证说明.md`。部署保护分支另用隔离命令桩检查，实际 CentOS 主机的网卡、资源限制、磁盘、SELinux 和访问仍需部署后确认。

GitHub Actions 实际构建 Linux Docker 镜像，测试 FFmpeg 解码、ONNX 推理、API、重复结果复用和人工评分独立保存。发布流程另在 0.5 CPU / 1 GiB、单任务线程的容器中执行相同检查，通过后才上传预构建镜像包与 SHA256。合成音只验证执行链路，不代表音质预测准确率。[构建与发布记录](https://github.com/renzhonghua8/audio-review/actions)
