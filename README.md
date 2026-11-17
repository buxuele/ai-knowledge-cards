# AI 知识卡

把笔记变成可自测的问答卡，带 SM-2 间隔复习、分类、回收站和多主题界面。

![应用图标](web/icon-512.png)

## 功能

1. 知识卡按分类和 AI 子类浏览，支持只看收藏、到期复习和列表分页。
2. SM-2 间隔重复，没记住与下一个分别写回失败和掌握进度。
3. 卡片可编辑、删除、撤销，删除的卡进入回收站，可逐张恢复或清空。
4. 支持新增卡片、右键删除分类、代码块渲染与复制、侧边栏折叠。
5. React 前端，三套深色主题，状态保存在浏览器本地。

## 技术栈

1. 前端：React 19 + Vite，样式手写，不引入 UI 组件库。
2. 后端：Python 标准库 HTTP 服务，没有第三方运行时依赖。
3. 数据：卡片内容为 JSON，复习排程为 SQLite。
4. 测试：Python unittest 接口测试，外加无头 Chrome 浏览器验收。

## 目录

```text
server.py              HTTP 服务、SM-2、归档与回收站
frontend/              React 源码，构建到 web/dist
web/                   图标与构建产物目录
tests/                 接口测试与浏览器验收
scripts/               导入、拆书、图标与 EPUB 工具
data/cards.json        卡片数据，仓库不提供，需要自己准备
data/progress.db       复习排程，首次运行自动生成
```

## 运行

需要 Python 3.10+ 与 Node.js 18+。

1. 准备 `data/cards.json`，最小格式如下。

```json
{
  "categories": [
    { "id": "ai", "name": "AI 技术", "summary": "" }
  ],
  "cards": [
    {
      "id": "ai-001",
      "cat": "ai",
      "q": "什么是幂等性？",
      "a": "同一个操作执行多次，结果和执行一次相同。",
      "detail": "例如支付回调重复到达时，订单只扣一次款。",
      "level": "basic"
    }
  ]
}
```

2. 执行启动脚本。

```bash
./just_run.sh
```

脚本会检查前端依赖、构建 React、探测端口并在服务就绪后打开浏览器。服务默认访问 `http://127.0.0.1:8899`。

## 数据说明

`data/`、电子书、拆书源文件、个人笔记和截图不包含在仓库里。仓库只保留代码、测试、图标和文档模板。没有 `data/cards.json` 时应用无法启动，这是内容与代码分离的有意设计。

卡片字段：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| id | 是 | 全局唯一 |
| cat | 是 | 分类 id |
| q | 是 | 问题，最多 300 字 |
| a | 是 | 答案，最多 3000 字 |
| detail | 否 | 解释或例子 |
| level | 是 | basic、core、practice、edge |
| source | 否 | 来源标识，不在卡片上显示 |
| theme | 否 | 批次标记 |

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | /api/cards | 卡片、分类与复习汇总 |
| GET | /api/sidebar | 侧边栏分类树 |
| POST | /api/answer | 提交复习评分 |
| POST | /api/add | 新增卡片 |
| POST | /api/edit | 编辑问题与答案 |
| POST | /api/archive | 删除并归档 |
| POST | /api/unarchive | 恢复已删卡片 |
| GET | /api/recycle | 回收站列表 |
| DELETE | /api/recycle | 清空回收站 |
| POST | /api/delete-category | 删除整个分类 |
| GET | /api/open/cards | 对外只读题库接口 |

## 测试

```bash
.venv/bin/python -m unittest discover -s tests
.venv/bin/python tests/browser_check.py
```

浏览器验收需要本机安装 Chrome，要求 `data/cards.json` 存在。测试会自己启动服务和浏览器，跑完清理进程。

## 说明

这是本地单用户应用，默认只监听 `127.0.0.1`。没有登录、没有多租户隔离，不要直接暴露到公网。密钥只从 `.env` 读取，`.env` 已在 `.gitignore` 中。
