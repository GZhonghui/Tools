# 下载 Gmail 邮件

`fetch_mails.py` 默认从 Gmail 的 `INBOX` 下载最近 24 小时收到的邮件，并保留原有的已读或未读状态。可直接使用系统的 Python 3.7 或更高版本，脚本仅依赖标准库。

1. 在 `Mail/fetch_mails.py` 顶部将 `GMAIL_ADDRESS` 设为完整的 Gmail 地址。需要调整下载范围时，修改同一处的 `MAILBOX` 和 `LOOKBACK`。
2. 创建 `Mail/keys/gm.txt`，在第一行填写该帐号的 16 位应用专用密码。脚本读取时会去掉分组空格。
3. 从仓库根目录运行收信脚本。

   ```sh
   python3 Mail/fetch_mails.py
   ```

每封邮件连同附件原样保存为 `Mail/mails/` 下的一个 `.eml` 文件。再次运行时，脚本会保留已有的同名文件。应用密码和下载的邮件仅供本机使用，提交改动时请排除 `Mail/keys/gm.txt` 和 `Mail/mails/`。
