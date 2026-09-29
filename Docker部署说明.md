# CentOS 上用 Docker 部署声检

公开仓库：[renzhonghua8/audio-review](https://github.com/renzhonghua8/audio-review)。端口为 **8001**。预构建镜像在 GitHub 发布页提供，服务器下载镜像后直接运行，不在宿主安装 Python、FFmpeg、模型组件，也不构建镜像或调用付费 API。

## 已有其他服务的服务器

部署默认新增一个名为 `audio-review` 的独立容器，使用 `/opt/audio-review/data`。不启动或重启 Docker，不修改 Docker 配置、系统软件源、防火墙、Nginx 或其他容器。不执行清理镜像、卷、容器等全局操作。8001 被任何其他服务占用时，停止部署；容器名冲突时也不会自动替换。

默认限制为 **0.5 核 CPU、256 个进程**，内存按当前可用量自动选择 **1024 或 768 MiB**，禁用声检容器的交换空间，降低竞争时的 CPU 权重。默认同时评测 2 条不同音频，其余自动排队；每条显示独立进度。下载镜像限速 2 MB/s，校验 SHA256 后才导入。[Docker 资源限制说明](https://docs.docker.com/engine/containers/resource_constraints/)

内存保护固定要求容器上限之外有 **512 MiB 宿主余量**。自动选择规则如下；镜像导入后会再次检查。默认升级在内存不足时退出，保留运行中的旧实例；显式使用 `--upgrade --stop-first` 可先关闭旧声检释放内存，详情见下文。

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

部署脚本默认使用发布版 `v2.0.9` 的 `audio-review:2.0.9` 镜像；评测算法版本仍为 `2.0`。默认模式下载或校验失败时保留原实例；`--stop-first` 模式失败时尝试恢复旧声检。脚本只有通过健康检查后才输出“部署完成”以及实际访问 URL。浏览器访问 `http://实际内网IP:8001/`，健康检查应显示 `ready: true`、`evaluator_version: "2.0"`、`workers: 2`、`task_pause: true`。

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

如果旧版提示“当前可用内存不足 1.5 GB”，且 `free -m` 显示可用内存为 1399 MiB，可以更新到 v2.0.9，指定 768 MiB 的容器上限继续。固定的 512 MiB 宿主余量仍保留；若可用内存降到 1280 MiB 以下，默认模式仍会停止。已经运行声检时，可采用下文的 `--stop-first` 升级。

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

升级前暂停提交新评测，等待队列完成。已支持任务暂停的版本也可点击 **暂停全部**，等待每条任务显示 **已暂停** 后升级；正在暂停时仍会拒绝升级。以下操作只会短暂停止已有声检容器，不会停止其他服务。

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

镜像校验成功、确认无运行、排队或正在暂停的评测后，停止声检，完整备份到 `/opt/audio-review/backups/data-时间戳.tar.gz`，再替换自己的容器。已有音频、自动结果、人工评分及暂停检查点保留。备份失败会尝试恢复原声检；新实例启动或健康检查失败也会尝试恢复旧容器。恢复仍以 `docker ps` 和日志为准。

### 可用内存不足时先关闭旧声检

当当前可用内存为 1099 MiB，而旧声检仍在运行时，使用 `--upgrade --stop-first`。脚本先核对容器管理标记、独立挂载、端口、磁盘和评测队列；仍有运行、排队或正在暂停的评测时退出，需先等待完成或完全暂停。随后只正常关闭旧声检，重新读取实际可用内存。释放后仍须满足 **768 + 512 = 1280 MiB**，否则恢复原实例并退出，不关闭其他服务或取消资源保护。

你的服务器公网来源为 `http://43.165.169.160:8001` 时，完整升级命令如下；网卡地址仍由脚本自动识别：

```bash
set +e
bash <<'DEPLOY'
set -e
cd /opt/audio-review/src
git pull --ff-only
AUDIO_REVIEW_MEMORY_MB=768 \
AUDIO_REVIEW_WORKERS=2 \
AUDIO_REVIEW_MODEL_THREADS=1 \
AUDIO_REVIEW_EXTRA_ORIGINS='http://43.165.169.160:8001' \
bash scripts/deploy-docker.sh --upgrade --stop-first
docker ps --filter name=audio-review
docker logs --tail 30 audio-review
curl --fail --silent --show-error http://10.7.0.7:8001/api/health
DEPLOY
```

仅声检会在此次升级中暂停，暂停时间包含下载、导入、备份和启动。初始内存足够时先下载校验再关闭；不足时先关闭再下载。下载、校验、镜像导入、备份、替换、内存复查或健康检查失败，均尝试恢复原容器的 ID、名称和先前运行状态。正常关闭不设强杀超时；评测与回听后台任务由应用退出流程结束。原音频和评分保留，其他容器不参与恢复操作。

### 中断当前声检任务后升级

如果不能等待任务完成，也无法在旧版网页暂停，可显式增加 `--interrupt-tasks`。该选项必须与 `--upgrade` 联用，只适用于已确认管理标记与独立数据挂载的现有声检。它跳过任务队列检查，给原声检最多 30 秒正常退出；超时后 Docker 结束该容器。内存余量、CPU/内存限制、端口、容器归属、数据备份与失败恢复检查仍保留。

已经保存的音频、自动结果和人工评分保留。运行和排队中的任务会被中断，升级后可能回到“等待重新检测”，需要重新提交检测；仅明确暂停且检查点有效的任务保留断点。失败后尝试恢复旧容器也不会自动接着旧版未完成的评测。

```bash
set +e
bash <<'DEPLOY'
set -e
cd /opt/audio-review/src
git pull --ff-only
AUDIO_REVIEW_MEMORY_MB=768 \
AUDIO_REVIEW_WORKERS=2 \
AUDIO_REVIEW_MODEL_THREADS=1 \
AUDIO_REVIEW_EXTRA_ORIGINS='http://43.165.169.160:8001' \
bash scripts/deploy-docker.sh --upgrade --stop-first --interrupt-tasks
docker inspect --format '{{.Config.Image}}' audio-review
docker logs --tail 30 audio-review
DEPLOY
```

该开关属于宿主部署脚本，复用已经发布的 `audio-review:2.0.9` 镜像。更新仓库脚本后即可使用，无需重新构建镜像。关闭旧声检后可用内存仍不足 1280 MiB 时会停止升级并尝试恢复旧实例，不能用该选项绕过资源保护。

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

每条运行或排队任务可单独 **暂停 / 继续**，结果页也提供对应按钮。勾选后工具栏操作所选任务；未勾选时操作全部任务，大列表自动按最多 200 条分批请求。排队任务立即暂停；运行任务先显示 **正在暂停**，在当前模型窗口或声学块的安全点停止。已完成的窗口与进度保留，释放执行名额，其他未暂停任务继续。已暂停任务不会被普通“批量检测”自动恢复，需要显式点击继续。

暂停仅保留任务状态与小型 JSON 检查点，不占用评测线程，也不保留大体积解码 WAV。继续时会重新解码原音频并接着尚未完成的评分窗口计算，因此可能有短暂准备阶段。服务器重启后已暂停任务仍为暂停；若检查点损坏、丢失或与评测版本不兼容，继续时重新计算。相同内容的两条记录可独立暂停；另一条完成后，暂停记录在点击继续时复用已完成结果，人工评分始终独立保存。

暂停后选择上方新的评测模式，再点击继续，所选任务使用新模式重新计算，旧模式停止，不拼接旧模式窗口；模式未改变时接着原断点。界面会预告将切换的条数。相同内容的其他记录维持各自任务，原音频、人工评分与已保存的上一份自动结果保留。

音频列表和结果页支持单条删除；勾选后可使用 **删除所选**，未勾选不会删除全部。确认后所选记录的原音频、自动结果、人工评分及对应任务删除。相同内容的其他记录与任务保留，最后一条引用删除时清理服务端和当前浏览器的回听副本。仍有线程使用原文件时，待退出后清理，并返回待清理提示。删除不能撤销；升级前备份文件不会自动删除，其他浏览器已保存的离线副本需在相应浏览器清理网站数据。

压缩音频会临时展开为 WAV。例如 1 小时 48 kHz 双声道 float WAV 约 1.38 GB，另有模型 WAV；暂停、完成或失败后清理解码文件。音频和评分数据库长期保留，备份不会自动删除。

## 格式识别与兼容回听

导入根据实际内容探测音轨，不再按扩展名拦截；支持镜像内 FFmpeg 能解码的本地音频和视频音轨。已用 24 种合成输入验证 WAV、MP3、AAC/ALAC M4A、AAC、FLAC、OGG、OPUS、AIFF、CAF、WMA、WavPack、TTA、AU、AC3、WebM、MKA、MOV、3GP、AMR、带视频的 MP4，以及无扩展名、自定义扩展名和大写扩展名。此列表不表示支持每一种编码变体；损坏、加密或缺少音轨的文件明确报错。不会读取播放列表引用的其他文件或远程资源。

点击音频时，后台按需生成 **256 kbps MP3** 兼容回听副本，网页显示真实处理进度。评测队列忙时也会准备回听，不必等待整个队列结束。仅有一个转换任务，FFmpeg 单线程、较低调度优先级，与评分任务共用容器的 CPU、内存限制。相同内容只生成一次，副本保存在服务器，重启后复用；不会自动淘汰已生成副本。单个副本上限 512 MiB，总量默认 1024 MiB，满额时保留旧副本并提示新副本无法生成。可在部署命令中设置 `AUDIO_REVIEW_PLAYBACK_CACHE_MB=4096` 增至 4 GiB，允许 512–16384 MiB，仍需可用磁盘。此设置不增加内存或 CPU 配额。

网页首次通过普通 GET 完整加载 MP3，避开浏览器对远程音频的 Range 兼容问题，显示真实字节数和进度。完成后在浏览器 IndexedDB 保存完整副本；切换、刷新或重新打开同一地址时直接读取，已开始的后台加载不因切换停止。相同内容的多条记录复用同一副本。浏览器禁止存储、空间不足、清理网站数据或更换访问地址时，可能需要重新加载；界面会提示保存是否成功。缓存不会重复执行服务端转换。服务器接口仍支持 Range、HEAD、ETag 与稳定内容地址，下载原音频不受影响。

MP3 回听有压缩损失，细微音质问题请下载原音频复核。评测仍使用原文件，不对原文件降噪、归一化或重编码，也不会修改已有评分。

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
