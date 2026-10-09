# CI、重建镜像和新版浏览器验收

本轮接续后端稳定性修复，不新增业务工作流。开始 HEAD 是 `027bf24bfdb6e503c34741a628df754adac354e0`，main 上已有上轮未提交实现。未提交、暂存、推送或覆盖它们。

## 工作区与 CI 修复

原始 status、staged/unstaged patch、文件 SHA256、镜像 inspect 均保存在 `/tmp/openharness-ci-next-9p1npvvz/logs`。692 个源文件/文档和现有锁文件复制到独立 source 目录。Web 安装、构建、截图均在副本内；原始 Web dist/test-results/tsbuildinfo 另有备份和 9 项内容哈希。不会重用原工作区的 Playwright 输出目录或替换已有 Docker image tags。

实际发现：`uv.lock` 被忽略，原远端 CI 每次重新解析依赖，无法复现本地锁定环境。修改 `.gitignore` 允许纳入现有锁文件，其内容未变；工作流使用 frozen sync/run，并以锁导出的 constraints 安装 wheel 运行时依赖。`uv build --wheel` 保留有效命令；uv build 没有 `--frozen` 选项，隔离构建后端自身不声称受运行时锁控制。

增加 `workflow_dispatch` 和 `codex/**` push trigger，保持 main/PR 触发；最小 `contents: read` 权限。runner 明确指定 Ubuntu 24.04，避免 latest 标签后续迁移造成验证环境漂移。CI 构建 sandbox 加上 `--pull --no-cache`，确保验收的是此次重建结果。

`scripts/test_docker_sandbox_e2e.py`、`tests/test_research/test_report_sandbox.py` 支持 `OPENHARNESS_TEST_DOCKER_IMAGE` 测试覆盖参数；默认值不变。新镜像使用独立 tag，原 `:latest` tags 不需替换，所有隔离/取消/恢复断言保持不变。

首轮 uncached 构建在 PyPI 下载 lxml 时发生 `ReadTimeoutError`。Dockerfile 的 pip 明确设置 `--timeout 60 --retries 3`，版本约束不变。重试复用**本轮首轮**已成功重建的 apt 层，重新执行 pip；没有复用原 opaque sandbox image。CI 仍按 --pull --no-cache 构建。重建成功的 image ID：`sha256:14d76fa94f744e72de5be2737bc4028cf155eefaca311ec45bb8716f0c6fdc44`。

## 环境与已执行检查

宿主 Ubuntu 20.04，Linux 5.15，Node 24.21.0 / npm 11.19.0，Python 3.11.17 / uv 0.12.22，Docker 26.1.3。源代码及依赖锁仍是当前工作区版本。GitHub [runner-images 清单](https://github.com/actions/runner-images) 在本轮查询时把 `ubuntu-latest` 映射到 Ubuntu 24.04；本机另构建 Ubuntu 24.04 用户态浏览器环境。容器共享宿主内核，不能冒充 GitHub VM 或未知的生产环境。

宿主最初仅约 1.3 GiB 空闲；完整 Playwright Docker image pull 因容量预检被主动停止。仅构建所需依赖的小型 Ubuntu 24.04 容器，将本轮临时 node_modules/browser cache 放到专用 tmpfs；原 node_modules、原浏览器缓存和原 .venv 未删除或替换。

| 实际命令 / 检查 | 当前结果 |
| --- | --- |
| `git status --short --branch` / `git rev-parse HEAD` / `git log -5 --oneline --decorate` / staged diff / remote refs | 已核对，HEAD 未变，staged 为空 |
| `uv lock --check --offline` | 通过，116 packages，锁文件内容未变 |
| `npm ci`（副本 frontend/web） | 通过，0 vulnerabilities |
| `npm ci --prefix tools/sandbox`（副本） | 通过，使用 pinned SRT 0.0.79 |
| `npm run build`（副本 frontend/web） | 通过，6.51s |
| `ruff check --no-cache src tests scripts evals hatch_build.py`（副本） | 通过 |
| `uv run --frozen ruff format --check src evals scripts/check_types.py hatch_build.py` | 通过，240 files |
| Actionlint v1.7.12 `.github/workflows/ci.yml` | 通过 |
| `uv export --frozen --no-dev --extra web --no-emit-project --no-hashes --format requirements-txt -o <临时 constraints>` | 通过 |
| `uv build --wheel --out-dir <临时 wheels>`（副本） | 通过 |
| 安装新 wheel + `python tests/test_install/wheel_smoke.py`（独立 wheel venv） | 通过，162 installed modules，Web/CLI/Shell/persistence |
| `docker build --pull --no-cache --iidfile <日志>/rebuilt-sandbox.id -t openharness-sandbox:ci-next-9p1npvvz <副本>/src/openharness/sandbox` | 首轮网络超时失败；保留 sandbox-build.log |
| `docker build --pull --iidfile <日志>/rebuilt-sandbox-retry.id -t openharness-sandbox:ci-next-9p1npvvz <副本>/src/openharness/sandbox` | 超时参数修复后重建成功；保留 sandbox-build-retry.log |
| 本轮重建镜像的完整 real sandbox acceptance | 修正副本环境后 39 passed，0 failed/skip，123.74s |
| Playwright 1.63 对应完整 Chromium 153.0.8010.12 / revision 1243 | 安装成功；修正环境后完整 E2E 18 passed，0 failed/skip，1.8m |
| 默认 Chrome Headless Shell 153.0.8010.12 / revision 1243 | 安装成功，完整默认 E2E 18 passed，0 failed/skip，1.7m；此前下载 timeout 重试日志保留 |

容器预检发现两项环境限制：managed Python 的通用版本别名需同时只读挂载实际 interpreter 目录；Docker 默认遮蔽的 `/proc` 会阻止 nested bubblewrap。后者使用外层测试容器的 `systempaths=unconfined`、`seccomp=unconfined`、`apparmor=unconfined` 解决，**无需 SYS_ADMIN capability 或 privileged**。应用的 strict sandbox/fail-closed 权限策略未改。此前失败的预检日志保留。

隔离副本初始错误复用了旧 venv 的 editable .pth，造成 SRT 下的 source import 指向原仓库而不可见，首轮 acceptance 为 38 passed/1 failed；改为在副本执行 `uv sync --frozen --extra dev --extra web --extra eval --python 3.11` 创建自身 venv。浏览器容器也按副本的相同绝对路径挂载代码，避免 venv alias 的 canonical command 指向未准入路径。

临时 tmpfs 的 SRT vendor 放在 /dev/shm 会被内层 sandbox 的 /dev 重挂载遮蔽，严格 Shell probe 显示 apply-seccomp not found；SRT 改回副本的普通目录。新版完整 Chrome 首轮因此出现导出失败，加上一项 5s progress deadline 失败，共 16 passed/2 failed。不修改源码 sandbox allowlist，不弱化测试/延长断言 timeout。纠正环境后 strict Shell probe 输出 42、returncode 0。

最终 real acceptance 命令（cwd 为副本根，PATH 指向原有 pinned SRT 的只读使用）：

```bash
OPENHARNESS_TEST_DOCKER_IMAGE=openharness-sandbox:ci-next-9p1npvvz OPENHARNESS_REQUIRE_SANDBOX=1 PATH="/home/jason/pythonproject/OpenHarness/tools/sandbox/node_modules/.bin:$PATH" PYTHON_KEYRING_BACKEND=keyring.backends.fail.Keyring .venv/bin/python -m pytest -q tests/test_research/test_report_sandbox.py tests/test_research/test_dispatch_recovery.py scripts/test_docker_sandbox_e2e.py
```

完整 Chrome E2E 使用原项目支持的 `OPENHARNESS_TEST_BROWSER` 指定 **新版** `/ms-playwright/chromium-1243/chrome-linux64/chrome`，没有使用旧 Chromium 1140，也没有修改 Playwright 配置。容器启动命令见 [run-browser.sh](ci-next-logs/run-browser.sh)，最终命令为 `node /opt/npm/bin/npm-cli.js run test:e2e`（Node 24/npm 11）。原 test-results/dist 的 9 个文件哈希仍全部相同。

随后用 [run-browser-default.sh](ci-next-logs/run-browser-default.sh) **不指定 executable override**，验证 CI 默认 Headless Shell 路径，也完整通过 18 项。两次均使用 Ubuntu 24.04 用户态、同版本 153 engine，原测试断言及等待上限均未改变。

本轮实际修改现有文件仅 5 项：`.github/workflows/ci.yml`、`.gitignore`、`src/openharness/sandbox/Dockerfile`、`scripts/test_docker_sandbox_e2e.py`、`tests/test_research/test_report_sandbox.py`。此前其他 687 个文件/锁的内容哈希不变。新增此文及选取的 [日志/命令证据](ci-next-logs/)；操作日志快照保留在临时目录而不作为用户代码补丁发布。

## 远端运行边界

公开 GitHub Actions API 查询显示最新运行仍是 [37896785111](https://github.com/wkjfeuheru/ResearchWeave/actions/runs/37896785111)，旧 HEAD，completed/failure。没有把它当成新实现的验收。

`git push --dry-run origin HEAD:refs/heads/codex/backend-stabilization-ci` 成功，仅检查 SSH 写权限，未创建分支。当前没有可用的 GitHub REST credential；新的 codex branch push trigger 可以在发布后直接启动 CI，无需先创建 PR。

实际修复仍在未提交工作区。要验证它们，必须将包含此前必需 Tavily/退休工具修复、当前后端改造和 CI/锁文件的版本发布到独立分支。先前明确要求“不要擅自提交用户改动”，因此只有取得对这一具体发布动作的授权后，才在隔离 checkout 提交并推送；不会提交当前主工作区或改写 main。旧 HEAD 的 rerun 不能验证未提交修复。

发布候选已在 `/tmp/openharness-ci-next-9p1npvvz/publish-repo` 的 `codex/backend-stabilization-ci` 分支暂存，尚未 commit/push；当前主工作区 index 仍为空。源代码、配置、开发文档和锁文件的 diff-check 通过。全候选 diff-check 的 whitespace 警告仅来自保留原样的历史 ANSI/Playwright raw logs，不修改原始证据、不配置忽略规则，也不把该全候选检查宣称为通过。

上一轮 Python 3.10/3.11 全量各 1287 passed 仅引用为历史基线，不报告成本轮重跑。远端新修复版 CI 未运行，等待上述具体发布动作授权；不能声明最新远端验收通过。

## 额外依赖审计

本轮副本的 `npm audit --json`（sandbox）退出 1：两个 high 计数来自同一 `node-forge` 漏洞及受影响的父级 SRT 包，不是两个独立 CVE。锁定版本 1.4.0；npm registry 当前最新版也是 1.4.0。[GHSA-86w9-cpqp-85rv](https://github.com/advisories/GHSA-86w9-cpqp-85rv) 标明 <=1.4.0 受影响、无 patched release。

审查 SRT 0.0.79 的 dist，forge 出现在 CA/leaf 证书生成和解析代码，未发现对其 RSA verifier 的显式调用，签名使用 native crypto；这不足以证明所有间接路径不可利用。保留原 pinned SRT，不执行 audit fix --force 建议的旧 SDK 降级，也不删除 advisory 或声称审计通过。部署评估和上游修复跟踪属于遗留风险。
