---
name: switch-model
description: 在 DeepSeek 配置与 ChatGPT（内置 OpenAI 默认）配置之间切换 Codex 的模型后端。当用户说「切换到 deepseek」「切到 ChatGPT」「切换模型」「换模型」「现在用的是哪套配置」时使用。
---

# 切换 Codex 模型配置

只做一件事：改 `$CODEX_HOME/config.toml` 里的一组托管键，让 Codex 在
**DeepSeek 后端**与**内置 OpenAI 默认**之间切换。不做热切换，改完必须让用户重启 Codex。

## 命令

脚本在本 skill 目录下的 `scripts/model_switch.py`（先把相对路径解析成绝对路径再执行）：

| 用户意图 | 命令 |
|---|---|
| 切到 DeepSeek | `python scripts/model_switch.py use deepseek` |
| 切回 ChatGPT / OpenAI | `python scripts/model_switch.py use chatgpt` |
| 问现在是哪套 | `python scripts/model_switch.py status` |
| 换过 DeepSeek key，重抓快照 | `python scripts/model_switch.py capture` |

脚本幂等（重复切同一目标等于没动），会先备份 `config.toml`、写后校验，失败自动回滚。
**不要**手动编辑 `config.toml` 绕过它。

## 切换成功后必须告诉用户

1. 改的是磁盘配置，**要重启 Codex 才生效**。
2. 切回 ChatGPT 配置时用的是 `auth.json` 里的 OpenAI 凭据。如果那里是 `sk-proj-` 开头的
   平台 API key，就是按量计费，**不等于 ChatGPT 订阅额度**；想用订阅额度要先自己
   `codex login`，本插件不代劳。

## 不要做的事

- 不读、不写、不打印 `auth.json`。
- 不把 DeepSeek 的 key 写进任何会提交到 git 的文件（它只存在于
  `$CODEX_HOME/model-switch/deepseek.toml`）。
