# JackettSearch — MoviePilot v3 插件

把 **Jackett** 的公开/半公开/私有索引器接入 [MoviePilot](https://github.com/jxxghp/MoviePilot) v3 的搜索，
作为「插件资源源」使用：**不写 MoviePilot 的站点表，不依赖 MoviePilot 的站点认证机制**。

> 参考实现：[ProwlarrExtend](https://github.com/milikii/MoviePilot-ProwlarrExtend)（MIT）的 v3 插件资源源架构；
> 本插件把 Prowlarr 客户端替换为 Jackett API。

## 功能

- 从 Jackett 的 `/api/v2.0/indexers/all/results` 聚合搜索公开/半公开/私有索引器
- 结果映射为 MoviePilot `TorrentInfo`：标题、大小、做种/下载、发布时间、详情页、磁力/下载链接、电影/剧集分类
- 插件资源源架构：`get_module()` 注册 `search_torrents` / `async_search_torrents`
- 自动去重（同 tracker + infohash/下载链接）
- 支持通过 MoviePilot 系统代理访问 Jackett（可选）

## 前置条件

1. MoviePilot v3.1.9+（在 v3.2.x 上测试）
2. 一个可用的 Jackett，并在 Jackett 里添加了至少一个索引器
   （公开站推荐：YTS、EZTV、ThePirateBay、LimeTorrents 等）
3. Jackett 的 API Key（Jackett 首页顶部可复制）

## 安装

在 MoviePilot：**设置 → 插件 → 插件市场 → 右上角齿轮 → 添加仓库**，填入本仓库地址：

```
https://github.com/pandaria2012/jackett_extend
```

更新插件列表后，找到 **JackettSearch** 安装即可（安装后可能需要重启 MoviePilot）。

## 配置

| 配置 | 说明 |
|---|---|
| 启用插件 | 总开关 |
| 使用 MoviePilot 代理访问 Jackett | 如果 Jackett 和 MoviePilot 在同一内网，保持关闭 |
| 搜索超时（秒） | 5–120，默认 30 |
| Jackett 地址 | 例如 `http://192.168.1.10:9117` |
| Jackett API Key | Jackett 首页顶部 API Key |

也可以首次部署时在容器 `/config/jackett_search.json` 放默认配置（插件在无保存配置时读取）：

```json
{"enabled": true, "host": "http://192.168.1.10:9117", "api_key": "YOUR_JACKETT_API_KEY", "proxy": false, "search_timeout": 30}
```

## 实现说明

- MoviePilot 调用插件资源源时传入 `site={}`，插件向 Jackett 请求：
  `GET {host}/api/v2.0/indexers/all/results?apikey=...&Query=...&Category=2000(电影)/5000(剧集)`
- 结果转换为 MoviePilot `TorrentInfo`，由 MoviePilot 负责后续下载/订阅流程
- 不写 MoviePilot 站点表，不注册虚拟站点；MoviePilot 的站点认证（`auth_level`）只约束内置站点功能，
  插件搜索源是否可用取决于宿主版本，请以实测为准

## 已知限制

- Jackett `/all` 聚合接口没有可靠翻页参数，只取第一页（约 50–100 条/次）
- 中文关键词在 Jackett 里匹配一般，建议使用英文关键词（MoviePilot 会负责转英文标题）
- 需要 Jackett v0.18+（JSON API）
- 部分站点需要 Cloudflare 验证时，需要在 Jackett 侧配置 FlareSolverr

## 许可

MIT

