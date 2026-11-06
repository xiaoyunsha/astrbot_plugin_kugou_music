# astrbot_plugin_kugou_music

纯酷狗音乐点歌插件，不依赖第三方 API 中转服务。支持搜索、点歌、VIP 直取、扫码 / 微信登录、原生 QQ 音乐卡片。

## 特性

- **搜索 + 点歌**：`/酷狗搜歌 <关键词>` 列出结果，再 `/酷狗点歌 <序号>` 发送
- **三种登录方式**
  - `/酷狗扫码登录`：通用，PC 端 / 安卓端二维码轮询，纯插件内置
  - `/酷狗微信登录`：仅概念版（安卓渠道），授权链接 1 分 50 秒后自动撤回防盗用
  - WebUI 手动填写 Cookie（含 token / userid / vip_token）
- **VIP 歌曲**：配置 Cookie 后走 `trackercdn.kugou.com/v5/url` 取 VIP 直链
- **音乐卡片**：按以下优先级发送
  1. OIAPI 签名卡片（免费，无需 token；概念版自动走 `kugoulite` 格式）
  2. 咸鱼 API 签名卡片（需在 WebUI 填 `music_card_key`）
  3. NapCat 原生 `music.type=custom` 段兜底（秒发，不超时）
- **双协议切换**：WebUI 开关一键在标准版（appid=1005）与酷狗概念版 Lite（appid=3116）之间切换
- **本地图片服务**：插件内置 127.0.0.1 静态服务器，给二维码 / 封面提供 http URL

## 安装

在 AstrBot WebUI → 插件市场搜索「酷狗音乐点歌」安装，或从本仓库发布页下载 zip 后手动上传。

依赖会自动通过 `requirements.txt` 安装：

- `aiohttp>=3.8.0`
- `pycryptodome`（用于微信登录的 RSA / AES 加解密）

## 指令

| 指令 | 说明 |
| --- | --- |
| `/酷狗搜歌 <关键词>` | 搜索歌曲，结果缓存 5 分钟 |
| `/酷狗点歌 <序号>` | 点歌，发送音乐卡片 / 封面 + 播放链接 |
| `/酷狗扫码登录` | 生成扫码登录链接（标准版走 PC 端，概念版走安卓端） |
| `/酷狗微信登录` | 微信授权登录（**仅概念版**） |
| `/酷狗退出登录` | 清除已保存的 Cookie |
| `/酷狗登录状态` | 查看当前登录的 userid / token |

## 配置项（WebUI）

| 配置项 | 说明 | 默认值 |
| --- | --- | --- |
| `kugou_cookie` | 酷狗 App 登录 Cookie（含 token、userid、vip_token）；免费歌曲无需填写，VIP 歌曲必填 | 空 |
| `kugou_concept` | 开启后使用酷狗概念版（Lite）协议，独立 token 体系 | `false` |
| `kugou_ua` | 自定义请求 UA，留空使用默认 Chrome UA | Chrome 126 |
| `search_count` | 每次搜索返回条数（1-10） | `5` |
| `debug` | 调试模式，在消息末尾追加 `_debug` JSON 数组 | `false` |
| `card_jump` | 卡片跳转目标：`detail`=歌曲详情页（推荐）/ `audio`=音频直链 | `detail` |
| `music_card_key` | 咸鱼 API 签名 Token（在 https://apii.xianyuw.cn 注册获取），留空走 OIAPI | 空 |
| `card_format` | 卡片平台标识：`kugou` / `netease` | `kugou` |
| `timeout` | HTTP 请求超时（秒） | `15` |

## 支持平台

- `aiocqhttp`（NapCat / OneBot v11）——完整支持，音乐卡片走原生段
- `qq_official`（QQ 官方 API）——支持搜索 / 点歌文本，音乐卡片能力受平台限制

## 版本要求

- AstrBot `>= 4.17.0`
- Python 3.9+

## 反馈 / 交流

使用中遇到 bug、有新功能建议，或想和其他用户交流：

- **提交 Issue**：[GitHub Issues](https://github.com/xiaoyunsha/astrbot_plugin_kugou_music/issues)（附上 AstrBot 版本、报错截图、开 debug 后的 `_debug` 输出，复现更快）
- **QQ 群**：[点击加入交流群](https://qm.qq.com/q/VvXhcfuKs2)

## 致谢

- 接口签名与登录流程参考 [KuGouMusicApi](https://github.com/MakcRe/KuGouMusicApi)（Node.js 版），本仓库用纯 Python 重写
- 音乐卡片签名免费端点：[OIAPI](https://oiapi.net/)

## License

MIT
