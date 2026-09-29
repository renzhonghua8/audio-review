# CentOS 上用 Docker 部署声检

公开仓库：[renzhonghua8/audio-review](https://github.com/renzhonghua8/audio-review)。端口为 **8001**。预构建镜像在 GitHub 发布页提供，服务器下载镜像后直接运行，不在宿主安装 Python、FFmpeg、模型组件，也不构建镜像或调用付费 API。

## 已有其他服务的服务器

部署默认新增一个名为 `audio-review` 的独立容器，使用 `/opt/audio-review/data`。不启动或重启 Docker，不修改 Docker 配置、系统软件源、防火墙、Nginx 或其他容器。不执行清理镜像、卷、容器等全局操作。8001 被任何其他服务占用时，停止部署；容器名冲突时也不会自动替换。

默认限制为 **0.5 核 CPU、256 个进程**，内存按当前可用量自动选择 **1024 或 768 MiB**，禁用声检容器的交换空间，降低竞争时的 CPU 权重。默认同时评测 2 条不同音频，其余自动排队；每条显示独立进度。下载镜像限速 2 MB/s，校验 SHA256 后才导入。[Docker 资源限制说明](https://docs.docker.com/engine/containers/resource_constraints/)

内存保护固定要求容器上限之外有 **512 MiB 宿主余量**。自动选择规则如下；镜像导入后会再次检查，内存下降到不满足所选预算时停止部署，此时还没有停止旧声检实例或修改数据目录。

| 当前可用内存 | 声检内存上限 | 处理方式 |
| --- | --- | --- |
| 至少 1536 MiB | 1024 MiB | 默认双任务、单模型线程 |
| 1280–1535 MiB | 768 MiB | 最多双任务、单模型线程 |
| 小于 1280 MiB，或读取失败 | — | 停止部署 |

例如总内存 1998 MiB、可用 1399 MiB 时，选择 768 MiB，按检查时的可用量扣除容器上限后约有 631 MiB 余量。可设置 `AUDIO_REVIEW_MEMORY_MB=768` 或 `1024`，仍须满足上限加 512 MiB 的门槛；默认值为 `auto`。不提供取消保护的选项。

内存读取优先使用 `/proc/meminfo` 的 `MemAvailable`。旧内核缺少该字段时，仅使用较保守的 `MemFree`；不会将全部缓存视为可释放内存。无效字段会导致退出。[Linux 内存字段说明](https://www.kernel.org/doc/html/latest/filesystems/proc.html)

脚本另要求声检目录及 Docker 镜像目录所在磁盘分别至少 5 GiB 空间，并验证宿主支持 CPU、内存和交换空间限制；不满足时退出。声检数据目录与其他容器挂载重叠时也会退出。当前预构建镜像支持 Linux x86_64。不会为了部署修改内核配置或升级 Docker。

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

部署脚本默认使用发布版 `v2.0.4` 的 `audio-review:2.0.4` 镜像；评测算法版本仍为 `2.0`。镜像下载或校验失败时没有停止原服务。脚本只有通过健康检查后才输出“部署完成”以及实际访问 URL。浏览器访问 `http://实际内网IP:8001/`，健康检查应显示 `ready: true`、`evaluator_version: "2.0"`、`workers: 2`。

容器以 UID/GID `10001:10001` 运行。`:Z` 为声检专用数据目录配置 SELinux 标签，无需关闭 SELinux。不要将其他服务的数据目录作为声检目录。[Docker 挂载说明](https://docs.docker.com/engine/storage/bind-mounts/)

Docker 发布端口会维护其自身的网络转发规则。本脚本不修改已有防火墙配置，但新增容器会增加该端口对应的 Docker 规则；来源检查不是身份认证。[Docker 端口发布说明](https://docs.docker.com/engine/network/port-publishing/)

## 从部署检查失败继续

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

如果旧版提示“当前可用内存不足 1.5 GB”，且 `free -m` 显示可用内存为 1399 MiB，可以更新到 v2.0.4，指定 768 MiB 的容器上限继续。固定的 512 MiB 宿主余量仍保留；若可用内存降到 1280 MiB 以下，脚本仍会停止。

```bash
set +e
bash <<'DEPLOY'
set -e
cd /opt/audio-review/src
git pull --ff-only
AUDIO_REVIEW_MEMORY_MB=768 AUDIO_REVIEW_WORKERS=2 \
  AUDIO_REVIEW_MODEL_THREADS=1 bash scripts/deploy-docker.sh
docker logs --tail 30 audio-review
DEPLOY
```

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

保护已有服务的部署默认使用 2 个任务线程、1 个模型线程。不同音频同时评测，超过 2 条的任务自动排队；不会通过增加 Uvicorn 进程数加速。768 MiB 模式最多允许 2 个任务、模型线程固定为 1，CPU 与内存上限、宿主余量检查仍保留。选择 1024 MiB 模式、资源宽裕并已确认原服务负载时，可调整 `AUDIO_REVIEW_WORKERS` 与 `AUDIO_REVIEW_MODEL_THREADS`（1–8），容器 CPU/内存上限仍保持不变。提高线程数不会突破资源上限，也不保证更快。

“全量快速”约 9 秒一个模型窗口，连续覆盖整段音频，尾部也评分；“快速抽样”覆盖开头与中段共 2 分钟；“全量精细”按 1 秒步长密集评分。基础声学检测始终覆盖全量。模式不同，均分、P10 和最低分可能不同。

网页一次可多选或拖入多条，也可在导入期间继续追加；逐条上传并显示真实传输百分比、大小和服务器确认阶段。每条确认后立即加入列表并勾选，单条失败不影响后续文件，支持查看失败原因并重试。导入尚未结束时，也可先检测已确认的文件。

勾选或全选后批量检测；一次评测请求最多 200 条，网页会自动分批提交更大的已有列表。接口单次上传上限仍是 20 条，网页使用单文件请求保持资源有界。相同内容、模式和评测版本共享自动结果，人工评分独立。结果页“重新检测”会强制计算。

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

GitHub Actions 实际构建 Linux Docker 镜像，测试 FFmpeg 解码、ONNX 推理、API、重复结果复用和人工评分独立保存。发布流程先在 0.5 CPU / 1 GiB、双任务线程的容器中执行检查，再通过部署脚本切换到 768 MiB，确认双任务配置、已有音频、自动结果和人工评分保留，并强制运行实际模型；另一项模拟服务的容器 ID、运行状态和 HTTP 200 保持正常。全部通过后才上传预构建镜像包与 SHA256。[构建与发布记录](https://github.com/renzhonghua8/audio-review/actions)

另对固定的 v2.0.2 发布镜像进行了较低内存限额测量：768 MiB、0.5 CPU、无 swap、单任务单模型线程下，10 分钟 48 kHz 双声道合成音实际重算三次均通过（每次 67 个评分窗口，约 61 秒）；30 秒精细模式也通过（22 个窗口）。容器内存峰值约 660 MiB，包含文件缓存，OOM 与 OOM kill 均为 0。[低内存测量记录](https://github.com/renzhonghua8/audio-review/actions/runs/36525142272)

512 MiB 探索测试虽然完成，但已触及内存上限，因此部署脚本仅开放 768/1024 MiB。上述合成音验证执行流程与所测样本的资源占用，不代表音质预测准确率，也不保证所有输入或任意历史记录规模都足够。测量在 Ubuntu Linux 上执行；CentOS 的实际内核、限制能力和运行状态仍需部署时检查。

发布版 v2.0.4 已补充双任务资源验证：两条不同的 10 分钟合成音同时实际评分，第三条排队，再强制重算两条长音频。在 768 MiB / 0.5 CPU / 无 swap 下，两个任务均推进并完成，OOM 为 0，旁边模拟服务保持正常。内核峰值达到 768 MiB（含文件缓存），匿名内存峰值约 401 MiB；相同 CPU 配额下，并发不会保证吞吐成倍增加。[双任务验证记录](https://github.com/renzhonghua8/audio-review/actions/runs/36529523378)
