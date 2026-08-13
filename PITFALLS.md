# 已知边界

- 钉钉应用凭证属于敏感信息，只能保存在服务器 `.env`，不得提交到代码库或聊天记录。
- 发布账号尚未配置时，图片会自动入素材库但不会被分配或推送。
- 已提交到蚁小二云端的历史草稿通常不能回写标题或正文；需要在蚁小二后台处理或创建新草稿。
- 当前钉钉预览提供“回复 `重生成 任务号`”重生成。若要改为钉钉卡片按钮，需在钉钉开放平台为机器人增加互动卡片回调配置后再启用。

## 2026-08-13 - GitHub 自动部署缺少服务器地址

- 症状：养号素材库推送代码后 GitHub Actions 的部署任务失败，线上 `/api/health` 仍显示旧的 `bootstrap` 版本。
- 原因：工作流 `deploy` 作业的 `DEPLOY_HOST` Secret 为空，SSH 无法解析目标主机。
- 解决：在仓库 Actions secrets 中补齐生产服务器地址 `DEPLOY_HOST`；现有 `DEPLOY_PRIVATE_KEY`、`DEPLOY_KNOWN_HOSTS` 继续用于密钥认证与主机校验。
- 预防：每次依赖 GitHub Actions 部署前，先查看最新 workflow 的 deploy job 是否已注入非空 `DEPLOY_HOST`。
- 适用范围：本项目线上发布。
