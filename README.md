# CSDN 文章导出工具

> 通过接口逆向实现
>
> author: codex&claude code(glm5)&本人

本项目用于**通过 CSDN 已登录会话 + 网关签名**批量导出文章，支持：

- 已发布（细分为：公开 / 私密 / 粉丝可见 / VIP可见）
- 草稿
- 审核
- 正文 Markdown 本地化（含图片下载）
- 封面图本地化并附加到文末
- 失败图片占位 + 原始 URL 失败清单

---

## 1. 核心能力

- 逆向调用 CSDN 接口：
  - 列表接口：`/blog/phoenix/console/v1/article/list`
  - 详情接口：`/blog-console-api/v3/editor/getArticle`
- 自动签名：`X-Ca-Key / X-Ca-Nonce / X-Ca-Signature / X-Ca-Signature-Headers`
- 自动分页抓取全量文章
- 文件名采用文章标题（自动处理非法字符、重名）

---

## 2. 分类规则（以接口 JSON 字段为准）

仅对“已发布”文章再细分：

- `isNeedVip=1` 或 `read_type=read_need_vip` => `已发布/VIP可见`
- `isNeedFans=1` 或 `read_type=read_need_fans` => `已发布/粉丝可见`
- `read_type=private` => `已发布/私密`
- 其他已发布 => `已发布/公开`

其余状态：

- `draft` => `草稿`
- `audit` => `审核`

---

## 3. 正文与内容回退

CSDN 存在两种编辑模式，部分文章会出现：

- `markdowncontent` 为空
- 但 `content`（HTML）有内容

处理策略：

1. 优先使用 `markdowncontent`
2. 若为空，回退使用 `content` 做文本化转换
3. 若二者都为空，生成占位文档并标注说明

---

## 4. 图片处理策略

### 4.1 正文图片

- 识别 Markdown 图片和 HTML `<img>`
- 下载到同名资源目录：`文章名.assets/`
- 把链接改写为相对本地路径

### 4.2 封面图

- 读取 `coverImage` 字段
- 下载后统一追加到文末 `## Cover 图`

### 4.3 下载失败兜底

>基本都是图片违规，为什么违规，不知道，我也违规了两张。。。

- 自动写入本地占位图：`_image_failed.svg`
- 文末增加 `## 图片下载失败清单`
- 记录失败原因与原始 URL

---

## 5. 运行方式

### 5.1 环境要求

- Python 3.10+

- 依赖：`requests`（CLI）/ `flask` + `requests`（Web 界面）

  > 按需求下载即可，建议使用虚拟环境

安装依赖：

```bash
# CLI 模式
pip install requests

# Web 界面模式
pip install -r web/requirements.txt
```

### 5.2 Web 界面（推荐）

启动 Web 服务器：

```bash
cd web
python server.py
```

然后在浏览器中打开 http://localhost:5000

Web 界面特性：
- 可视化配置面板（中文界面）
- 实时终端日志输出
- 导出统计展示
- 文章分类表格预览
- 增量更新（自动跳过已导出文章）
- JSON/CSV 一键下载

### 5.3 CLI 模式：使用 .env 注入 Cookie

在项目根目录创建 `.env`：

```env
CSDN_COOKIE=你的完整Cookie
```

然后直接运行：

```bash
python scripts/csdn_export_all.py \
  --output exports/csdn_export
```

可选：如果 `.env` 不在根目录，可通过 `--env-file` 指定：

```bash
python scripts/csdn_export_all.py \
  --env-file path/to/.env \
  --output exports/csdn_export
```

> 安全建议：不要提交 `.env` 到仓库（本项目已在 `.gitignore` 中忽略）。

### 5.4 CLI 模式：直接传 Cookie

```bash
python scripts/csdn_export_all.py \
  --cookie "你的完整Cookie" \
  --output exports/csdn_export
```

### 5.5 CLI 模式：使用 Cookie 文件

```bash
python scripts/csdn_export_all.py \
  --cookie-file cookie.txt \
  --output exports/csdn_export
```

可选参数：

- `--statuses`：默认 `all_v2,draft,audit`
- `--page-size`：默认 `20`
- `--sleep`：默认 `0.2`
- `--timeout`：默认 `20`

---

## 6. 增量更新

重复导出时自动检测已导出文章，仅下载新增内容：

- 读取已有 `articles_full.json` 获取已导出文章 ID
- 对比远程文章列表，筛选新增文章
- 无新文章时提示 "✨ 所有内容已经都拉下来啦！"
- 新导出文章标题标记"新"标签
- 已有文章不会被删除或覆盖

---

## 7. 输出结构

```text
exports/csdn_export_xxx/
├─ articles_full.json
├─ articles_summary.csv
├─ articles_classification.csv
├─ image_failures.json
├─ image_failures.csv
└─ markdown/
   ├─ 已发布/
   │  ├─ 公开/
   │  ├─ 私密/
   │  ├─ 粉丝可见/
   │  └─ VIP可见/
   ├─ 草稿/
   └─ 审核/
```

其中：

- `articles_classification.csv`：每篇文章最终归类审计
- `image_failures.*`：失败图片逐条明细（文章、URL、原因、文件路径）

---

## 8. 常见失败原因

- `http_404`：源图链接已失效（大概率图片违规）
- `exception:ConnectionError`：当前网络不可达（常见于外站图床）
- `empty_body`：响应成功但内容为空

这类失败会被自动占位并记录，不会中断整批导出。

---

## 9. 免责声明

该工具仅用于导出**你自己账号**下可访问的内容，请遵守 CSDN 平台规则与相关法律法规。
