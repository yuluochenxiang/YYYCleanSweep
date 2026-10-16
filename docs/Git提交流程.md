# YYYCleanSweep 开发提交流程

## 一、目录分工（记住这张表，不再混乱）

| 用途 | 路径 | 说明 |
| --- | --- | --- |
| 源码编辑区（唯一真源） | `C:\Users\XIANG\WorkBuddy\2026-10-09-23-29-14\YYYCleanSweep` | 日常改代码都在这里 |
| Git 仓库 / venv / 构建部署 | `E:\work\WB_\YYYCleanSweep` | 提交、打包、运行都在这里 |
| 一键脚本 | `E:\work\WB_\git_push.bat` + `git_push.py` | 双击即可完成全部流程 |
| 远程 | `https://github.com/yuluochenxiang/YYYCleanSweep` | 分支 `main` |

源码是**双副本**，但方向固定：`工作区 → 仓库`，由脚本自动同步，不要手动拷。

## 二、日常流程（只需一步）

**双击 `E:\work\WB_\git_push.bat`**，它会自动依次完成：

1. **同步源码**：工作区 → 仓库，按文件内容比对，只增改不删除（避开目录套娃问题）
2. **统一分支名**为 `main`，并清理陈旧的远程跟踪引用
3. **提交**：有改动才提交，无改动自动跳过
4. **推送**到 `origin/main`

想写提交说明时，把说明作为参数传给 bat 即可（见下）。

## 三、可选用法

```
git_push.bat                      同步 + 提交 + 推送（说明自动生成）
git_push.bat "修复某 Bug"          用自定义说明提交
python git_push.py --dry-run      演练，只看会做什么，不实际改动
python git_push.py --no-sync      跳过同步，只提交推送
python git_push.py --save-token 令牌   把令牌交 Windows 凭据管理器加密保存
```

`--save-token` 之后，可把 `git_push.py` 里的 `TOKEN` 改回空字符串，推送依然全程静默，且磁盘上不再有明文令牌。

## 四、排错手册

| 报错 | 原因 | 处理 |
| --- | --- | --- |
| `SSL certificate problem` | 本机证书链校验不过 | 脚本已自动用「仅本次不校验」重试；长期出现可执行 `git config --global http.sslverify false`（不安全，仅排障） |
| `non-fast-forward` / `rejected (fetch first)` | 远程有本地没有的提交 | 确认以本地为准：`git push -u origin main --force` |
| `Authentication failed` / `403` / `Repository not found` | 令牌失效 | GitHub 重建仓库后**旧令牌会失效**，需重新生成。推荐 classic 令牌勾 `repo`（账号级，重建仓库也有效） |
| 窗口一闪就没 | bat 编码或换行符被破坏 | 不要手动改 bat；它必须**纯 ASCII + CRLF**。中文输出全部交给 Python 处理 |

## 五、已固化的重要约定

- **`.gitignore`** 已排除 `venv/`、`app/`、`_shot/`、`_bak*/`、`*.log` 等，只提交源码。
- **令牌**写在 `git_push.py` 配置区的 `TOKEN`（该文件在仓库目录之外，不会被提交）。令牌曾在对话中暴露过，建议定期到 GitHub 轮换。
- **不要手动 `Copy-Item src dst -Recurse` 复制目录**，会把目录再套一层（生成 `core/core` 这类嵌套），同步一律交给脚本。
- 分支统一为 `main`，远程默认分支也是 `main`。

## 六、历史遗留问题（已清理，留档备查）

1. 曾在网页上删除并重建仓库 → 旧的细粒度令牌（`github_pat_` 开头）对新建仓库失效，导致 bat 与 TortoiseGit 同时鉴权失败。现已改用 classic 令牌（`ghp_` 开头）。
2. 本地残留过指向已删除提交的 `origin/master` 引用，已清除。
3. bat 曾因 **LF 换行 + `chcp 65001`** 导致一闪就退出，现改为纯 ASCII 启动器 + Python 实现。
