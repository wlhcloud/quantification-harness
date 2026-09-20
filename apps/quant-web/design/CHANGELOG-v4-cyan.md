# Q·LAB 前端 v4-cyan 液态玻璃风格对齐 — 变更说明

**变更日期**：2026-09-13
**变更范围**：apps/quant-web 前端项目

---

## 一、阶段一：原型补全

### 产出文件
- `design/prototype-v4-cyan-full.html`（183KB / 3018 行）— 补全全部 8 个 Tab 的完整原型

### 完成内容
| Tab | 页面 | 核心内容 |
|-----|------|----------|
| 01 | 总览 | 自选股12只 + K线图(SVG含均线/成交量/十字线/涨停标记) + 关键指标/技术信号/T+1信号 + 底部4面板(市场广度/指数行情/回测摘要/任务状态) |
| 02 | 选股 | 左条件面板(技术/基本面/量价/行业标签) + 中12行结果表(评分进度条/信号标签) + 右行业分布SVG/信号环形图/选股记录 |
| 03 | 回测 | 顶部参数条 + 左累计收益SVG曲线(青色面积+灰色基准+节点标注) + 右12绩效卡片+年度柱状图 + 底部3面板(回撤表/交易明细/月度热力图) |
| 04 | 模拟盘 | 左账户概览(大数字+资产环形图+30天曲线) + 中持仓/委托/成交/资金流水4子Tab + 右快速交易面板(买卖切换/仓位快捷键) |
| 05 | ETF | 左12只ETF列表+分类Tab + 中报价条+净值/折溢价/持仓/跟踪误差4子页(SVG) + 右套利机会/轮动信号/轮动绩效 |
| 06 | 数据 | 左数据集树形目录(可展开) + 中查询工具栏+12行日线预览+分页 + 右质量卡片/覆盖矩阵/更新日志/数据血缘 |
| 07 | 同步 | 左12个任务列表(状态点+进度条+类型标签) + 中任务详情+15行终端日志(INFO/WARN/ERROR分色) + 右规则配置/字段映射/WHERE输入 |
| 08 | 模型 | 左10个模型+分类Tab + 中模型信息+6性能卡片+IC时序柱状SVG+Top10特征重要性横向条形图 + 右训练状态/历史表/推理日志 |

### 交互
- Tab 切换：JS 控制 `.tab-page` 显示/隐藏，topbar 与 cmdline 固定
- 列表选中、按钮组切换、子Tab切换、开关、标签多选、买卖按钮、仓位快捷键、表格行选中、树形展开均已绑定
- 时钟实时走秒

### 验证
- 8 个 Tab 全部截图保存至 `design/verification/prototype-01~08-*.png`
- 视觉风格统一，数据充实，无 JS 报错

---

## 二、阶段二：React 对齐

### 新增文件

#### 全局样式
- `src/styles/global.css`（21KB）— v4-cyan 完整设计系统
  - CSS 变量（--bg-deep, --glass-bg, --amber, --up, --down 等）
  - 液态背景（4 blob 动画 + 网格叠加 + 噪点纹理）
  - 玻璃面板基类（.glass, .glass-strong, 顶部高光, 入场动画）
  - 顶部状态条、工作区布局、面板通用样式
  - 数据表格、列表项、工具按钮、报价条、图表区、底部面板、命令行
  - 新增组件类：进度条、开关、标签芯片、子Tab、终端日志、统计卡片、按钮、输入框

#### 共享组件 `src/components/ui/`
| 文件 | 组件 | 用途 |
|------|------|------|
| `GlassPanel.tsx` | GlassPanel | 玻璃面板容器（title/prefix/meta/children） |
| `DataTable.tsx` | DataTable | 泛型数据表格（粘性表头/行高亮/自定义渲染） |
| `TopNav.tsx` | TopNav | 顶部 8 Tab 导航 + 状态灯 + 实时时钟 |
| `CommandLine.tsx` | CommandLine | 底部命令行（装饰性） |
| `LiquidBackground.tsx` | LiquidBackground | 液态背景层 |
| `StatusDot.tsx` | StatusDot | 状态指示灯 |
| `ProgressBar.tsx` | ProgressBar | 进度条 |
| `Toggle.tsx` | Toggle | 开关 |
| `TagChip.tsx` | TagChip | 标签芯片 |
| `StatCard.tsx` | StatCard | 统计卡片 |
| `index.ts` | — | 统一导出 |

### 修改文件

| 文件 | 变更内容 |
|------|----------|
| `src/ProApp.tsx` | 完全重写：移除 antd ProLayout/ConfigProvider/AntApp，改用 TopNav + .workspace + CommandLine 外壳；保留全部页面 lazy 导入、路由映射、navigate 函数、repairPreset 解析、popstate 监听、各页面 props 传递；8 个主 Tab 映射到对应页面 |
| `src/main.tsx` | 移除 `antd/dist/reset.css` 和 `pro.css`，改为导入 `./styles/global.css` |
| `src/Dashboard.tsx` | 移除全部 antd/pro-components/图标；保留所有业务逻辑（行情加载/刷新/轮询/时钟/ETF信号）；MiniLine/IntradayChart SVG 配色改为深色主题；布局改为玻璃面板三栏 |
| `src/SelectionPage.tsx` | 移除全部 antd/图标；保留 ML候选/模拟盘/技术指标加载与富化逻辑；统计卡片用 StatCard；筛选用 .tf-btn 组；表格用 DataTable；V5信号用 .sig-type |
| `src/BacktestPage.tsx` | 移除全部 antd/pro-components/图标；保留 mode切换/ML版本加载/回测运行/V5加载逻辑；renderNavChart SVG 配色改为深色；参数面板用 GlassPanel + .kv-row；指标用 StatCard 网格 |
| `src/StockSimPage.tsx` | 移除全部 antd/图标；保留 run管理/持仓/委托/调仓/推进全部逻辑；三栏布局（账户概览/持仓表+净值曲线/运行控制）；Popconfirm 改为 window.confirm；SVG 配色深色化 |
| `src/EtfQuantPage.tsx` | 移除全部 antd/pro-components/图标；保留 walkforward加载/训练启停/模拟盘状态逻辑；三栏布局（持仓ETF/净值曲线+子Tab/绩效六宫格）；NavCurve/YearBars SVG 深色化 |
| `src/DataCenter.tsx` | 移除全部 antd/pro-components/图标；保留数据集目录/质量检查/预览/修复(onOpenRepair)逻辑；三栏布局（目录树/数据资产+子Tab/SLA+覆盖矩阵+日志）；树形目录用嵌套 div+▶/▼ |
| `src/SyncCenter.tsx` | 移除全部 antd/pro-components/图标；保留任务列表/WebSocket实时推送/分钟同步/历史回填/repairPreset/全部API调用；保留 `export type SyncRepairPreset`；三栏布局（任务列表/详情+终端日志/规则配置）；Modal/Drawer 改为内联遮罩面板 |
| `src/ModelsPage.tsx` | 移除全部 antd/图标；保留 trainLatest/startJob/useJobPolling 逻辑；三栏布局（版本列表/指标卡+IC柱状图+训练区间/训练状态+历史表+日志）；IC时序图用真实历史 rankIc 数据 |

### 保留未修改（仍使用 antd 的次要页面）
- `src/ToolRoutesPage`（ProApp 内部，设置/工具配置）
- `src/SourcesPage`（ProApp 内部，设置/数据源管理）
- `src/HealthStrip`（ProApp 内部，健康状态条）
- `src/Assistant.tsx`、`src/FactorsPage.tsx`、`src/SimPage.tsx`、`src/StockMlPage.tsx`、`src/DictionaryPage.tsx`、`src/BaseDictionaryPage.tsx`

这些页面不在 8 个主 Tab 中，可通过 URL 直接访问，暂保留 antd 样式。

---

## 三、设计规范

### 色彩系统
| 用途 | 变量 | 值 |
|------|------|-----|
| 背景 | `--bg-deep` | `#050508` |
| 玻璃面板 | `--glass-bg` | `rgba(12,14,22,0.55)` |
| 主色调(青) | `--amber` | `#00d4ff` |
| 涨 | `--up` | `#00e6a0`（绿） |
| 跌 | `--down` | `#ff5577`（红） |
| 文字 | `--text` | `#e0e0e8` |

### 布局规范
- 整体：`.terminal` flex column, 100vh, padding 10px, gap 10px
- 顶部导航：32px
- 工作区：flex 1
- 底部命令行：32px
- 面板圆角：12px，导航/命令行圆角：10px
- 字体：等宽字体 JetBrains Mono / SF Mono / Consolas

---

## 四、验证结果

### 类型检查
```
npm run typecheck → tsc -b → 退出码 0，无错误
```

### 生产构建
```
npm run build → tsc -b && vite build → 成功
- 5157 模块转换
- CSS: 39.68 KB (gzip 8.30 KB)
- 主 JS: 27.27 KB (gzip 8.99 KB)
- React vendor: 190.39 KB (gzip 59.84 KB)
```

### 页面渲染验证
- dev server 启动成功（http://127.0.0.1:3082）
- 8 个 Tab 全部可切换并正常渲染
- 深色液态玻璃背景正常（blob 动画 + 网格 + 噪点）
- 顶部导航 Tab 切换正常，时钟实时更新
- 底部命令行正常显示
- 各页面三栏布局与原型对齐
- 截图保存至 `design/verification/react-01~08-*.png`

### 已知说明
- 部分页面显示"数据不可用"或空状态，是因为后端 API 服务未运行，不属于前端问题
- 模型页（08）成功加载到了真实训练数据（LGBMRanker, RankIC 0.0472, 7次训练历史）
- antd 仍保留在 package.json 中，供次要设置页面使用；8 个主页面已完全不依赖 antd UI 组件

---

## 五、备份

原始代码备份位于：
```
apps/quant-web/backups/20260913_235121/
├── src/          （原始 src 目录完整备份）
└── package.json  （原始 package.json）
```
