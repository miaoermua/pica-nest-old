# Pica nest

Pica 软件源（pica-app-repo），一切暂定，非最终，一定会变化。

没有测试完成，没有对接 pica 完整，没有固定协议。

## 客户端配置

软件源地址填**根目录**，不是某个索引文件——客户端自己去找 `index.json` 和它指出的后端索引。

| 方式 | 地址 | 说明 |
|---|---|---|
| GitHub raw | `https://raw.githubusercontent.com/miaoermua/pica-nest/main/r` | 跟随 `main` 分支，无需部署 |
| Cloudflare Worker | `https://<你的 worker 域名>` | 需自行 `wrangler deploy`，代码在 `worker/`；`/r` 是同一入口的别名 |

向导会问地址并自动取后端交集：

```bash
pica setup
```

或手写 `/etc/pica/pica.json`（与向导写出的结果一致）：

```json
{
  "language": "zh-CN",
  "repos": [
    {
      "name": "pica-nest",
      "url": "https://raw.githubusercontent.com/miaoermua/pica-nest/main/r",
      "backends": ["opkg", "pica"]
    }
  ]
}
```

`backends` 按设备实际使用的后端写：opkg 设备填 `["opkg", "pica"]`，apk 设备填 `["apk", "pica"]`。写了源没有声明的后端时，`-S` 会直接报错并列出它实际声明了哪些，不会拿别的后端顶替。

然后同步：

```bash
pica -S
```

## 声明

部分应用打包来自 @immortalwrt 软件源提供
