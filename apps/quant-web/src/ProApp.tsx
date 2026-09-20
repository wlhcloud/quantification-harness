import { lazy, Suspense, useEffect, useMemo, useState } from "react";
import {
  App as AntApp,
  Avatar,
  Badge,
  Button,
  ConfigProvider,
  Form,
  Input,
  InputNumber,
  Modal,
  Popconfirm,
  Select,
  Space,
  Switch,
  Tag,
  Typography,
} from "antd";
import {
  AccountBookOutlined,
  AreaChartOutlined,
  DatabaseOutlined,
  ExperimentOutlined,
  FundOutlined,
  HomeOutlined,
  LineChartOutlined,
  MenuFoldOutlined,
  MenuUnfoldOutlined,
  PlusOutlined,
  RobotOutlined,
  SettingOutlined,
  SyncOutlined,
  ThunderboltOutlined,
  ToolOutlined,
} from "@ant-design/icons";
import {
  PageContainer,
  ProLayout,
  ProTable,
  type ProColumns,
} from "@ant-design/pro-components";
import { settingsApi, type DataSourceDto, type ToolRouteDto } from "./api";
import type { SyncRepairPreset } from "./SyncCenter";
import { healthBadgeStatus, healthText, useServiceHealth } from "./useServiceHealth";

const Assistant = lazy(() => import("./Assistant"));
const BacktestPage = lazy(() => import("./BacktestPage"));
const DataCenter = lazy(() => import("./DataCenter"));
const Dashboard = lazy(() => import("./Dashboard"));
const DictionaryPage = lazy(() => import("./DictionaryPage"));
const EtfQuantPage = lazy(() => import("./EtfQuantPage"));
const FactorsPage = lazy(() => import("./FactorsPage"));
const ModelsPage = lazy(() => import("./ModelsPage"));
const SelectionPage = lazy(() => import("./SelectionPage"));
const SimPage = lazy(() => import("./SimPage"));
const StockMlPage = lazy(() => import("./StockMlPage"));
const StockSimPage = lazy(() => import("./StockSimPage"));
const SyncCenter = lazy(() => import("./SyncCenter"));

const menuItems = [
  { path: "/dashboard", name: "总览", icon: <HomeOutlined /> },
  {
    path: "/stock-strategy",
    name: "股票策略",
    icon: <FundOutlined />,
    routes: [
      { path: "/selection", name: "T+1 选股", icon: <FundOutlined /> },
      { path: "/stock-ml", name: "股票ML模型", icon: <ThunderboltOutlined /> },
      { path: "/backtest", name: "策略回测", icon: <LineChartOutlined /> },
      { path: "/stock-sim", name: "股票模拟盘", icon: <AccountBookOutlined /> },
    ],
  },
  {
    path: "/etf-strategy",
    name: "ETF 策略",
    icon: <AreaChartOutlined />,
    routes: [
      { path: "/etf", name: "ETF 量化", icon: <AreaChartOutlined /> },
      { path: "/sim", name: "ETF 模拟盘", icon: <AccountBookOutlined /> },
      { path: "/factors", name: "ETF 因子研究", icon: <ExperimentOutlined /> },
    ],
  },
  { path: "/data", name: "数据中心", icon: <DatabaseOutlined /> },
  { path: "/sync", name: "同步任务", icon: <SyncOutlined /> },
  { path: "/models", name: "模型训练", icon: <ThunderboltOutlined /> },
  { path: "/assistant", name: "量化助手", icon: <RobotOutlined /> },
  {
    path: "/settings",
    name: "系统配置",
    icon: <SettingOutlined />,
    children: [
      {
        path: "/settings/sources",
        name: "数据源管理",
        icon: <DatabaseOutlined />,
      },
      { path: "/settings/tools", name: "工具配置", icon: <ToolOutlined /> },
      { path: "/settings/dictionary", name: "数据字典", icon: <DatabaseOutlined /> },
    ],
  },
];

function HealthStrip() {
  const health = useServiceHealth();
  const entries = [
    { key: "sync", port: "9101", label: "同步服务", icon: <SyncOutlined />, state: health.sync.state },
    { key: "engine", port: "9102", label: "计算引擎", icon: <ThunderboltOutlined />, state: health.engine.state },
    { key: "query", port: "9103", label: "查询服务", icon: <DatabaseOutlined />, state: health.query.state },
  ];
  return (
    <div className="health-strip">
      <div>
        <b>系统健康</b>
        <Badge status={health.allOk ? "success" : "warning"} text={health.allOk ? "运行正常" : "存在异常"} />
      </div>
      {entries.map((entry) => (
        <div key={entry.key}>
          {entry.icon}
          <b>{entry.port}</b>
          <Badge status={healthBadgeStatus(entry.state)} text={`${entry.label}·${healthText(entry.state)}`} />
        </div>
      ))}
      <Button type="link" size="small" onClick={health.refresh}>重新检测</Button>
    </div>
  );
}

function ToolRoutesPage() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<ToolRouteDto[]>([]),
    [sources, setSources] = useState<DataSourceDto[]>([]),
    [saving, setSaving] = useState<string | null>(null);
  useEffect(() => {
    Promise.all([settingsApi.toolRoutes(), settingsApi.sources()])
      .then(([a, b]) => {
        setRows(a.items);
        setSources(b.items);
      })
      .catch((e) => message.error(String(e)));
  }, []);
  const options = useMemo(
    () =>
      sources
        .filter((x) => x.enabled)
        .map((x) => ({ label: `${x.label} · ${x.protocol === "official" ? "官方 POST" : "X-API-Key"}`, value: x.id })),
    [sources],
  );
  const update = (id: string, patch: Partial<ToolRouteDto>) =>
    setRows((x) => x.map((r) => (r.toolId === id ? { ...r, ...patch } : r)));
  const columns: ProColumns<ToolRouteDto>[] = [
    {
      title: "工具",
      dataIndex: "label",
      render: (_, r) => (
        <div className="tool-name">
          <b>{r.label}</b>
          <span>{r.toolId}</span>
        </div>
      ),
    },
    {
      title: "主数据源",
      dataIndex: "primarySourceId",
      render: (_, r) => (
        <Select
          value={r.primarySourceId}
          options={options}
          onChange={(v) => update(r.toolId, { primarySourceId: v })}
        />
      ),
    },
    {
      title: "降级数据源",
      dataIndex: "fallbackSourceId",
      render: (_, r) => (
        <Select
          allowClear
          placeholder="不降级"
          value={r.fallbackSourceId}
          options={options.filter((x) => x.value !== r.primarySourceId)}
          onChange={(v) => update(r.toolId, { fallbackSourceId: v ?? null })}
        />
      ),
    },
    {
      title: "执行规则",
      render: (_, r) => (
        <Typography.Text code>
          {r.primarySourceId} → {r.fallbackSourceId || "失败"}
        </Typography.Text>
      ),
    },
    {
      title: "状态",
      width: 90,
      render: () => <Badge status="success" text="启用" />,
    },
    {
      title: "操作",
      width: 90,
      render: (_, r) => <Button type="link" loading={saving === r.toolId} onClick={() => void save(r)}>保存</Button>,
    },
  ];
  const save = async (route: ToolRouteDto) => {
    setSaving(route.toolId);
    try {
      const result = await settingsApi.saveToolRoute(route);
      setRows((items) => items.map((x) => x.toolId === route.toolId ? result.item : x));
      message.success(`${route.label}路由已保存`);
    } catch (e) {
      message.error(e instanceof Error ? e.message : String(e));
    } finally {
      setSaving(null);
    }
  };
  return (
    <PageContainer
      title="工具配置"
      subTitle="为每个工具确定性指定主数据源与唯一降级数据源"
    >
      <HealthStrip />
      <ProTable
        rowKey="toolId"
        search={false}
        options={false}
        pagination={false}
        columns={columns}
        dataSource={rows}
        headerTitle="数据源路由"
      />
    </PageContainer>
  );
}

function SourcesPage() {
  const { message } = AntApp.useApp();
  const [rows, setRows] = useState<DataSourceDto[]>([]),
    [editing, setEditing] = useState<DataSourceDto | null>(null),
    [form] = Form.useForm();
  const load = () =>
    settingsApi
      .sources()
      .then((x) => setRows(x.items))
      .catch((e) => message.error(String(e)));
  useEffect(() => {
    void load();
  }, []);
  const columns: ProColumns<DataSourceDto>[] = [
    {
      title: "数据源",
      dataIndex: "label",
      render: (_, r) => (
        <div className="tool-name">
          <b>{r.label}</b>
          <span>{r.id}</span>
        </div>
      ),
    },
    {
      title: "协议",
      dataIndex: "protocol",
      render: (v) => <Tag color="blue">{String(v)}</Tag>,
    },
    { title: "接口地址", dataIndex: "baseUrl", ellipsis: true },
    { title: "优先级", dataIndex: "priority", width: 90 },
    { title: "超时", dataIndex: "timeoutMs", render: (v) => `${v} ms` },
    {
      title: "状态",
      render: (_, r) => (
        <Badge
          status={r.enabled ? "success" : "default"}
          text={r.enabled ? "启用" : "停用"}
        />
      ),
    },
    {
      title: "操作",
      valueType: "option",
      render: (_, r) => (
        <Space>
          <Button
            type="link"
            onClick={() => {
              setEditing(r);
              form.setFieldsValue({ ...r, token: "" });
            }}
          >
            编辑
          </Button>
          <Button
            type="link"
            onClick={() =>
              settingsApi
                .test(r.id)
                .then((x) => message[x.ok ? "success" : "error"](x.message))
            }
          >
            测试
          </Button>
          <Popconfirm title="删除数据源" description="被工具引用时需先调整对应工具路由。" onConfirm={() => settingsApi.deleteSource(r.id).then(() => { message.success("已删除"); void load() }).catch((e) => message.error(e.message))}>
            <Button type="link" danger>删除</Button>
          </Popconfirm>
        </Space>
      ),
    },
  ];
  const save = async () => {
    const v = await form.validateFields();
    const { updatedAt, ...original } = editing!;
    await settingsApi.saveSource({ ...original, ...v });
    message.success("数据源已保存");
    setEditing(null);
    load();
  };
  return (
    <PageContainer
      title="数据源管理"
      subTitle="维护连接、凭据、优先级与可用状态"
    >
      <ProTable
        rowKey="id"
        search={false}
        options={{ density: true, reload: load }}
        pagination={false}
        columns={columns}
        dataSource={rows}
        headerTitle="数据源连接"
        toolBarRender={() => [<Button key="add" type="primary" icon={<PlusOutlined />} onClick={() => { const fresh:DataSourceDto={id:"",label:"",protocol:"x-api-key",baseUrl:"https://",token:"",enabled:true,priority:100,timeoutMs:15000,notes:null,updatedAt:""}; setEditing(fresh); form.setFieldsValue(fresh) }}>新增数据源</Button>]}
      />
      <Modal
        open={!!editing}
        title={editing?.updatedAt ? `编辑 ${editing.label}` : "新增数据源"}
        onCancel={() => setEditing(null)}
        onOk={() => void save()}
        destroyOnHidden
      >
        <Form form={form} layout="vertical">
          <Form.Item name="id" label="唯一标识" rules={[{ required:true, pattern:/^[a-z][a-z0-9_-]{1,31}$/, message:"使用2-32位小写字母、数字、_或-" }]}><Input disabled={!!editing?.updatedAt}/></Form.Item>
          <Form.Item name="label" label="名称" rules={[{ required: true }]}>
            <Input />
          </Form.Item>
          <Form.Item
            name="baseUrl"
            label="接口地址"
            rules={[{ required: true, type: "url" }]}
          >
            <Input />
          </Form.Item>
          <Form.Item name="token" label="Token">
            <Input.Password placeholder="留空保持不变" />
          </Form.Item>
          <Form.Item name="protocol" label="协议" rules={[{ required:true }]}><Select options={[{label:"X-API-Key 直连",value:"x-api-key"},{label:"Tushare 官方 POST",value:"official"}]}/></Form.Item>
          <Form.Item name="notes" label="说明"><Input /></Form.Item>
          <Space size="large">
            <Form.Item name="priority" label="优先级">
              <InputNumber min={0} max={10000} />
            </Form.Item>
            <Form.Item name="timeoutMs" label="超时（毫秒）">
              <InputNumber min={1000} />
            </Form.Item>
            <Form.Item name="enabled" label="启用" valuePropName="checked">
              <Switch />
            </Form.Item>
          </Space>
        </Form>
      </Modal>
    </PageContainer>
  );
}

/** 未匹配路径的兜底页面。
 *  原先是一个"该模块将在下一阶段接入"的占位组件，但每个菜单叶子都已挂真实页面，
 *  它永远不可达；真正需要兜底的是用户直接访问未知路径/根路径的情况——那应当回首页。 */
const DEFAULT_PATH = "/dashboard";

export default function ProApp() {
  const knownPaths = useMemo(() => new Set(menuItems.flatMap((item) => [item.path, ...(item.routes ?? item.children ?? []).map((child) => child.path)])), []);
  const parseRepairPreset = (): SyncRepairPreset | null => {
    const search = new URLSearchParams(window.location.search);
    if (search.get("repair") !== "minute") return null;
    const freq = search.get("freq"), startDate = search.get("start"), endDate = search.get("end"), reason = search.get("reason");
    return (freq === "1m" || freq === "5m") && startDate && endDate && reason ? { id: search.get("id") ?? String(Date.now()), freq, startDate, endDate, reason } : null;
  };
  const [path, setPath] = useState(() => knownPaths.has(window.location.pathname) ? window.location.pathname : DEFAULT_PATH),
    [collapsed, setCollapsed] = useState(false),
    [repairPreset, setRepairPreset] = useState<SyncRepairPreset | null>(() => parseRepairPreset());
  useEffect(() => {
    const onPopState = () => { setPath(knownPaths.has(window.location.pathname) ? window.location.pathname : DEFAULT_PATH); setRepairPreset(parseRepairPreset()); };
    window.addEventListener("popstate", onPopState);
    return () => window.removeEventListener("popstate", onPopState);
  }, [knownPaths]);
  const navigate = (nextPath: string) => {
    const target = new URL(nextPath, window.location.origin);
    if (target.pathname === path && target.search === window.location.search) return;
    window.history.pushState({}, "", nextPath);
    setPath(target.pathname);
    setRepairPreset(parseRepairPreset());
  };
  const content =
    path === "/dashboard" ? (
      <Dashboard onNavigate={navigate} />
    ) : path === "/data" ? (
      <DataCenter onOpenRepair={(repair) => navigate("/sync?" + new URLSearchParams({ repair: "minute", freq: repair.freq, start: repair.startDate, end: repair.endDate, reason: repair.reason, id: String(Date.now()) }))} />
    ) : path === "/sync" ? (
      <SyncCenter repairPreset={repairPreset} />
    ) : path === "/settings/dictionary" ? (
      <DictionaryPage />
    ) : path === "/settings/tools" ? (
      <ToolRoutesPage />
    ) : path === "/settings/sources" ? (
      <SourcesPage />
    ) : path === "/assistant" ? (
      <Assistant />
    ) : path === "/backtest" ? (
      <BacktestPage />
    ) : path === "/factors" ? (
      <FactorsPage />
    ) : path === "/models" ? (
      <ModelsPage />
    ) : path === "/selection" ? (
      <SelectionPage onNavigate={navigate} />
    ) : path === "/etf" ? (
      <EtfQuantPage />
    ) : path === "/sim" ? (
      <SimPage />
    ) : path === "/stock-sim" ? (
      <StockSimPage />
    ) : path === "/stock-ml" ? (
      <StockMlPage />
    ) : (
      <Dashboard onNavigate={navigate} />
    );
  return (
    <ConfigProvider
      theme={{
        cssVar: { key: "quant-app" },
        token: {
          colorPrimary: "#1677ff",
          borderRadius: 6,
          fontFamily: "Inter, 'PingFang SC', 'Microsoft YaHei', sans-serif",
        },
      }}
    >
      <AntApp>
        <ProLayout
          title="量化研究平台"
          logo={
            <div className="brand-mark">
              <AreaChartOutlined />
            </div>
          }
          layout="side"
          navTheme="light"
          location={{ pathname: path }}
          route={{ routes: menuItems }}
          collapsed={collapsed}
          onCollapse={setCollapsed}
          menuItemRender={(item, dom) => (
            <a onClick={() => ((item.routes ?? item.children)?.length ? undefined : item.path && navigate(item.path))}>{dom}</a>
          )}
          menuHeaderRender={(_, dom) => dom}
          headerContentRender={() => (
            <Button
              type="text"
              icon={collapsed ? <MenuUnfoldOutlined /> : <MenuFoldOutlined />}
              onClick={() => setCollapsed((x) => !x)}
            />
          )}
          avatarProps={{ src: <Avatar>A</Avatar>, title: "admin" }}
          contentStyle={{ padding: 0 }}
        >
          <Suspense fallback={<div className="module-placeholder">正在加载模块…</div>}>{content}</Suspense>
        </ProLayout>
      </AntApp>
    </ConfigProvider>
  );
}
