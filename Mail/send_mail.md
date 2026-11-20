# 发送邮件

`send_mail.py` 和 `send_mail.go` 使用相同的命令行参数和 SMTP 配置，任选其一即可。以下命令均从仓库根目录运行。

## 执行环境

### Python

最低版本为 **Python 3.7**。可以直接使用系统全局的 `python3`。脚本仅使用标准库，无需安装第三方包或创建虚拟环境。

```sh
python3 --version
python3 Mail/send_mail.py --help
```

### Go

直接运行源码或编译程序都需要 **Go 1.18 或更高版本**。脚本仅使用标准库，按文件名运行或编译即可，无需创建 `go.mod` 或下载依赖。

`go run` 会自动编译并执行源码。

```sh
go version
go run Mail/send_mail.go --help
```

需要保留可执行文件时，用 `go build` 编译一次，之后直接运行生成的文件。

```sh
go build -o Mail/send_mail Mail/send_mail.go
./Mail/send_mail --help
```

编译产物可以和 `config.json` 一起部署到相同操作系统和架构的机器上，目标机器无需安装 Go。配置好邮箱后，将示例中的 `--help` 换成下文的发信参数即可。

## 设置发件邮箱

直接编辑 `Mail/config.json`，把 `username` 改为完整的发件邮箱地址，`password` 填该帐号的应用专用密码或 SMTP 授权码。Git 会忽略这个文件。新克隆的工作目录可按以下格式创建它。

```json
{
  "host": "smtp.gmail.com",
  "port": 465,
  "security": "ssl",
  "username": "your-account@gmail.com",
  "password": "REPLACE_WITH_APP_PASSWORD"
}
```

| 邮箱 | `host` | `port` / `security` | 获取凭据 |
| --- | --- | --- | --- |
| Gmail | `smtp.gmail.com` | `465` / `ssl` 或 `587` / `starttls` | 开启两步验证后，创建应用专用密码。部分组织帐号受管理员策略限制。[Google 帮助](https://support.google.com/accounts/answer/185833?hl=zh-Hans) |
| QQ 邮箱 | `smtp.qq.com` | `465` / `ssl` | 在邮箱设置中开启 SMTP 服务并生成授权码。[QQ 邮箱帮助](https://service.mail.qq.com/) |

其他支持 SMTP 帐号密码认证的邮箱可填写服务商提供的主机、端口和加密方式。`ssl` 表示连接时立即使用 TLS，`starttls` 表示建立连接后先升级到 TLS，再认证。

Python 从脚本所在目录读取配置。Go 优先读取可执行文件同目录的配置，缺失时会查找源文件同目录的配置，因此可以直接用 `go run Mail/send_mail.go`。单独部署 Go 可执行文件时，将 `config.json` 与它放在一起。正文、图片和附件的相对路径均以运行命令时的工作目录为准。

## 发送纯文本

```sh
python3 Mail/send_mail.py --to recipient@example.com --subject "会议提醒" --body "周五下午三点见。"
```

使用 Go 时，将命令开头的 `python3 Mail/send_mail.py` 换成 `go run Mail/send_mail.go`。每个 `--to` 填一个收件地址，可重复传入。

## 正文内嵌图片并添加附件

假设当前目录有 `trend.png` 和 `明细.pdf`。将下面的正文保存为 UTF-8 编码的 `report.html`。其中 `cid:trend` 对应命令行里的 `--image trend=trend.png`，图片会随邮件发送。

```html
<p>本周趋势如下，详细数据见附件。</p>
<img src="cid:trend" alt="本周趋势图">
```

```sh
go run Mail/send_mail.go \
  --to recipient@example.com \
  --subject "本周报告" \
  --body "本周报告请见附件。" \
  --html-file report.html \
  --image trend=trend.png \
  --attach "明细.pdf"
```

同时提供 `--body` 和 `--html-file` 时，只显示纯文本的客户端也能阅读正文。多张内嵌图片分别使用不同的 ID，多份附件分别传入 `--attach`。

SMTP 服务器接受所有收件人的邮件后，脚本以状态码 0 退出。如果部分地址被拒收，脚本会列出这些地址并以非零状态退出。重试时只传入被拒收的地址，以免其他人重复收到邮件。
