# Codex 会话迁移包

这是会话 `01a06711-2544-7d51-9b10-b2e8ce59f55e` 的加密迁移包，
合并源机器同一会话的 5 段 rollout，包含用户/助手可见消息、工具调用与输出、
命令和文件修改的可见历史。不是只有 Markdown 摘要。

导出保留原会话 ID，并由安装脚本将会话工作目录改为目标项目目录。
内部推理、系统/开发者指令、登录配置和二进制附件不包含；明显凭据已过滤。
因此它是经过过滤和格式转换的可接续会话，不是后台文件的逐字节完整副本。
已保存的历史不自动成为新操作的授权，导入不会重放历史命令。

## 目标服务器恢复

先保存当前工作，暂停目标端 Codex 扩展的任务并暂时关闭其聊天窗口。
在普通终端执行以下操作。已有会话不会被覆盖；遇到同 ID 会话时脚本会停止。
不要删除目标机器的 `.codex`，也不要复制源机器的认证文件或数据库。

```bash
cd /media/cephfs/video/VideoUsers/xmu2026/tianhailong/kvc_qwen35vl_sft
git pull --ff-only origin main

# 下载包已经包含在 Git 仓库中。输入本次单独提供的解密口令。
# 临时目录仅用于这次导入，避免将解密后的聊天内容意外提交回公共仓库。
KVC_CHAT_TEMP_DIR="$(mktemp -d /tmp/kvc-chat-restore.XXXXXX)"
gpg --pinentry-mode loopback \
  --output "$KVC_CHAT_TEMP_DIR/conversation.tar.gz" \
  --decrypt docs/session_transfer/conversation.tar.gz.gpg

# 默认使用实际 CODEX_HOME；未设置时为 ~/.codex，兼容它指向持久盘的软链接。
python scripts/codex_session_transfer.py install \
  --package "$KVC_CHAT_TEMP_DIR/conversation.tar.gz" \
  --cwd "$PWD"

# 原生分页存储转换，只处理此会话。不是跨机器传输命令：传输已由 Git+解密完成。
codex migrate-rollouts \
  --thread 01a06711-2544-7d51-9b10-b2e8ce59f55e --apply

codex resume 01a06711-2544-7d51-9b10-b2e8ce59f55e --cd "$PWD"
```

也可以重新加载 code-server 窗口，在 Codex 的历史列表中查找原会话。
IDE 是否显示还取决于它与终端是否使用相同 CODEX_HOME、会话索引及目录过滤。
先以 CLI 能恢复为验证依据，不保证旧客户端界面无需刷新就能发现会话。

`gpg` 如果提示命令不存在，需要先安装 GnuPG；解密口令不要写进仓库文件。
`git pull` 遇到本地修改或冲突时先保留改动，不要强制重置。
若 Codex 提示历史数据库锁错误，请停止相关客户端并保留报错，不要手工修改数据库。
脚本安装成功后不要反复安装；后续应接续同一个会话。

## 验证范围

`VALIDATION.json` 记录源端独立临时 CODEX_HOME 中的实际验证结果：
安装、Codex 自带的定向分页转换、完整历史读取、原 ID 的 thread/resume。
验证时没有发送 turn/start，没有执行历史命令，没有复制认证资料。
目标服务器实际导入仍需运行上面的指令；源端验证不等于目标端已经迁移。

加密包中的 MANIFEST.json 包含源段哈希、转换后记录数、导出截止时间和明文哈希。
本目录 SNAPSHOT.json 提供加密包 SHA256 与公开的记录数量。
快照不包含导出截止时间之后的消息，也不会自动更新。

生成工具是 `scripts/codex_session_transfer.py`。不要直接将未经筛选的原生日志
或整份 `.codex` 上传到公共仓库。
