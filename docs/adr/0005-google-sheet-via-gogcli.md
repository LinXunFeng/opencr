---
status: accepted
---

# Google Sheet 支持经 gogcli 以用户身份写入，服务直接调用 gogcli 而非自管 OAuth 令牌

Google Sheet 输出目标最初只支持服务账号（见 0.7.0）。实际部署中，构建机上往往已经用 [gogcli](https://github.com/openclaw/gogcli) 以个人或企业账号登录好了，而服务账号的邮箱未必能被加进表格的共享范围。因此在同一个 `google_sheet` 类型下增加 `auth: gogcli` 鉴权方式：**服务不持有任何用户令牌，所有 Sheets 请求都通过 `gog api call` 透传发出，令牌的存储与刷新完全交给 gogcli。**

## Considered Options

- **原生支持 OAuth 用户授权**（google-auth 的 `authorized_user` 凭据，gogcli 只作为获取 refresh token 的途径）：不引入外部二进制，但服务要自己持有并轮换一份用户令牌，与构建机上 gogcli 管理的登录状态是两份真相——在 gogcli 里重新授权或撤销后，服务手里那份令牌不会跟着变。需求恰恰是复用已有登录，否决。
- **新增 `google_sheet_gogcli` 类型**：代码彻底分开，但 Destination 类型的含义会从"写到哪种平台"变成"平台 × 鉴权方式"，以后飞书的应用凭证与用户授权也要各占一个类型。否决。
- **用 gogcli 的高层 Sheets 命令**：缺少追加列与修改表格属性，默认按 `USER_ENTERED` 写入，只能按操作逐个分支并在每处记得加 `--input RAW`。否决。
- **同一类型下的鉴权方式 + 全部走 `gog api call`**（选中）：插件内抽出传输层，推送、检查、表头定位、补回规则只有一份，两种鉴权方式只在"请求怎么发出去"上不同。

## Consequences

- 服务多了一个运行期外部依赖。launchd 部署由使用者自行安装，`install.sh` 把它所在目录加进服务的 PATH；Docker 镜像默认打入固定版本的 gogcli，可用 build-arg 跳过。
- 账号授权必须有人在浏览器里点同意，部署流程只能自动化它周围的步骤：launchd 由 `install.sh` 交互完成；Docker 由宿主机上的 `scripts/setup-gogcli.sh` 在首次部署时做一次，容器启动时只导入 OAuth 客户端信息并打印授权状态。不支持挂载令牌文件、每次启动自动导入——明文 refresh token 会长期留在宿主机上，且每次重启都会用旧令牌覆盖在容器里重新授权过的令牌。
- 令牌存储随部署形态而异：launchd 沿用登录用户的钥匙串；容器内没有钥匙串，只能用 gogcli 的文件存储，`GOG_KEYRING_PASSWORD` 等变量属于运行环境而非某个 Destination 实例，不写进 `config.yaml`。
- 请求体一律写进仅本用户可读的临时文件、以 `--body @文件` 传给 gogcli，用完即删：命令行参数既有长度上限，又会出现在进程列表里，而正文是 Guest 都看不到的内容。`gog api call` 的 `--body` 不支持从 stdin 读取，所以不是管道。
- `account` 必填，不依赖 gogcli 的默认账号——别人在构建机上执行一次 `gog auth add` 就能悄悄换掉默认账号，推送随之以另一个人的身份写表。
- 错误只能从退出码判断（stderr 是纯文本），退出码与可读报错的映射是这一层自己维护的约定，gogcli 升级时需要复核。
